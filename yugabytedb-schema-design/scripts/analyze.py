#!/usr/bin/env python3
"""
Offline YugabyteDB schema review.

Reads a review bundle and writes findings.json, report.md and proposed.sql.
Never connects to a database and never executes DDL. Python 3.8+, stdlib only.

    python3 analyze.py BUNDLE_DIR [--out OUT_DIR]

Bundle layout (only stats.json is required):

    BUNDLE_DIR/
      stats.json          export-stats.sql      catalog + pg_stats (one node)
      usage/*.json        export-usage.sql      per-node index/table usage + pg_stat_statements
      statements/*.json   tserver :13000/statements per node, a pg_stat_statements JSON
                          array, or one consolidated file (see references/review-mode.md)
      schema.sql          ysql_dump --schema-only --include-yb-metadata (read by the model, not parsed here)

Each finding carries an evidence level:
    schema    derived from DDL alone (candidate)
    stats     backed by pg_stats / row counts (likely)
    workload  backed by statements or usage counters (confirmed)
"""

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

# --------------------------------------------------------------------------- thresholds

LOWCARD_MAX = 64            # distinct values at or below this = low cardinality
LOWCARD_SEVERE = 16
SKEW_FREQ = 0.30            # one value holding >= 30% of rows
NULL_HEAVY = 0.30           # null_frac at or above this
LARGE_TABLE = 1_000_000     # reltuples considered large
PRESPLIT_ROWS = 100_000     # range index on one tablet above this many rows -> pre-split
SCAN_RATIO = 50             # docdb_rows_scanned / rows returned
TOP_STATEMENTS = 15

TIME_TYPES = {
    "timestamp without time zone", "timestamp with time zone", "date",
    "time without time zone", "time with time zone",
}
INT_TYPES = {"smallint", "integer", "bigint"}
TIME_DEFAULT_RE = re.compile(
    r"\b(now|clock_timestamp|statement_timestamp|transaction_timestamp)\s*\(|"
    r"\bCURRENT_(TIMESTAMP|DATE)\b|\bLOCALTIMESTAMP\b", re.I)
TIME_NAME_RE = re.compile(r"(^|_)(created|inserted|ingested|logged|recorded|event_time|ts|timestamp)(_|$)|_(at|on|time|ts)$", re.I)

# statements worth reviewing; DDL, utility and session commands are dropped
DML_RE = re.compile(r"^\s*(/\*.*?\*/\s*)*\(?\s*(SELECT|INSERT|UPDATE|DELETE|WITH|MERGE|VALUES|TABLE|COPY)\b", re.I | re.S)
CATALOG_RE = re.compile(r"\b(pg_catalog\.|pg_(class|index|namespace|attribute|stats|constraint|type|settings|"
                        r"database|am|extension|stat_\w+)\b|yb_table_properties|yb_servers|to_regclass|"
                        r"\bversion\(\))", re.I)

SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
EVIDENCE_ORDER = {"workload": 0, "stats": 1, "schema": 2}

REF = {
    "sharding": "references/sharding.md",
    "hotspots": "references/hotspots.md",
    "index": "references/index-design.md",
    "partial": "references/partial-indexes.md",
    "lowcard": "references/low-cardinality.md",
    "pk": "references/primary-keys.md",
    "fk": "references/foreign-keys.md",
    "review": "references/review-mode.md",
}


def qi(name):
    """Quote an identifier the way format('%I') would (good enough for DDL suggestions)."""
    if re.fullmatch(r"[a-z_][a-z0-9_$]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def ql(value):
    return "'" + str(value).replace("'", "''") + "'"


# --------------------------------------------------------------------------- loading

class Bundle:
    def __init__(self, path):
        self.path = path
        self.warnings = []
        self.stats = None
        self.usage = []          # list of usage docs
        self.statements = []     # merged statements
        self.statement_source = None
        self._load()

    def _read_json(self, f):
        try:
            with open(f, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as e:
            self.warnings.append(f"Could not read {os.path.relpath(f, self.path)}: {e}")
            return None

    def _load(self):
        sp = os.path.join(self.path, "stats.json")
        if not os.path.exists(sp):
            sys.exit(f"{sp} not found. Run export-stats.sql (live) or replay schema.sql into a "
                     f"scratch cluster first (see references/review-mode.md).")
        self.stats = self._read_json(sp)
        if not self.stats or self.stats.get("format") != "yb-schema-review/stats/v1":
            sys.exit("stats.json is not a yb-schema-review/stats/v1 export.")

        for f in sorted(glob.glob(os.path.join(self.path, "usage", "*.json"))):
            d = self._read_json(f)
            if d and d.get("format") == "yb-schema-review/usage/v1":
                d["_file"] = os.path.relpath(f, self.path)
                self.usage.append(d)
            elif d is not None:
                self.warnings.append(f"Skipped {os.path.relpath(f, self.path)}: not a usage/v1 export.")

        dbid = str(self.stats["meta"].get("dbid") or "")
        raw, sources = [], []
        usage_stmts = [u for u in self.usage if u.get("statements")]
        if usage_stmts:
            for u in usage_stmts:
                raw.append((u["meta"].get("node"), u["statements"]))
            sources.append(f"pg_stat_statements from {len(usage_stmts)} usage file(s)")
        files = sorted(glob.glob(os.path.join(self.path, "statements", "*.json")))
        single = os.path.join(self.path, "statements.json")
        if os.path.exists(single):
            files.append(single)
        if files and usage_stmts:
            self.warnings.append("Both usage/*.json statements and statements files present; "
                                 "using usage/*.json only to avoid double counting.")
        elif files:
            for f in files:
                d = self._read_json(f)
                if d is None:
                    continue
                rows = d.get("statements") if isinstance(d, dict) else d
                if not isinstance(rows, list):
                    self.warnings.append(f"Skipped {os.path.relpath(f, self.path)}: no statements list.")
                    continue
                raw.append((os.path.basename(f), rows))
            sources.append(f"{len(raw)} statements file(s)")
        self.statement_source = ", ".join(sources) or None
        self.statements = merge_statements(raw, dbid, self.warnings)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def merge_statements(raw, dbid, warnings):
    """Merge per-node statement lists on (dbid, userid, queryid); sum counters."""
    merged = {}
    other_db = 0
    for node, rows in raw:
        for s in rows:
            if not isinstance(s, dict) or "query" not in s:
                continue
            sdb = str(s.get("dbid", "")) if s.get("dbid") is not None else ""
            if dbid and sdb and sdb != dbid:
                other_db += 1
                continue
            qid = s.get("queryid", s.get("query_id"))
            key = (sdb, str(s.get("userid", "")), str(qid) if qid is not None else s["query"])
            m = merged.get(key)
            if m is None:
                m = merged[key] = {
                    "queryid": qid, "query": s["query"], "calls": 0, "total_time_ms": 0.0,
                    "rows": 0, "docdb_rows_scanned": None, "docdb_read_rpcs": None,
                    "docdb_seeks": None, "nodes": [],
                }
            m["calls"] += int(_num(s.get("calls")))
            # PG15-based: total_exec_time (+ total_plan_time); PG11-based: total_time
            m["total_time_ms"] += _num(s.get("total_exec_time", s.get("total_time")))
            m["rows"] += int(_num(s.get("rows")))
            for k in ("docdb_rows_scanned", "docdb_read_rpcs", "docdb_seeks"):
                if k in s:
                    m[k] = (m[k] or 0) + int(_num(s[k]))
            if node and node not in m["nodes"]:
                m["nodes"].append(node)
    if other_db:
        warnings.append(f"Ignored {other_db} statement rows from other databases.")
    return sorted((m for m in merged.values()
                   if DML_RE.match(m["query"]) and not CATALOG_RE.search(m["query"])),
                  key=lambda m: -m["total_time_ms"])


# --------------------------------------------------------------------------- model

class Model:
    def __init__(self, bundle):
        st = bundle.stats
        self.meta = st["meta"]
        self.enums = {e["type"]: e["labels"] for e in st.get("enums", [])}
        self.tables = {}
        for t in st["tables"]:
            t["fq"] = f"{t['schema']}.{t['name']}"
            t["cols_by_num"] = {c["attnum"]: c for c in t.get("columns") or []}
            t["cols_by_name"] = {c["name"]: c for c in t.get("columns") or []}
            t["indexes"] = []
            t["pk"] = None
            self.tables[str(t["oid"])] = t
        self.indexes = []
        for i in st["indexes"]:
            t = self.tables.get(str(i["table_oid"]))
            if not t:
                continue
            i["table"] = t
            i["fq"] = f"{t['schema']}.{i['name']}"
            i["keys"] = [c for c in i["columns"] if c["key"]]
            i["include"] = [c for c in i["columns"] if not c["key"]]
            i["hash_count"] = sum(1 for c in i["keys"] if c["opt"] == "HASH")
            t["indexes"].append(i)
            if i["is_pk"]:
                t["pk"] = i
            self.indexes.append(i)
        self.colstats = {}
        for s in st.get("column_stats", []):
            key = (s["schema"], s["table"], s["column"])
            # partitioned parents only have inherited stats; otherwise prefer non-inherited
            if key not in self.colstats or not s.get("inherited"):
                self.colstats[key] = s
        self.fks = st.get("foreign_keys", [])
        self.sequences = st.get("sequences", [])
        self.statements = bundle.statements
        self.usage = bundle.usage
        self.node_count = self.meta.get("node_count") or 0
        self.cbo_on = (self.meta.get("settings") or {}).get("yb_enable_cbo") == "on" or \
            (self.meta.get("settings") or {}).get("yb_enable_base_scans_cost_model") == "on"

    # ---- helpers

    def colstat(self, t, colname):
        return self.colstats.get((t["schema"], t["name"], colname))

    def rows(self, t):
        r = t.get("reltuples")
        return r if r is not None and r > 0 else None

    def table_col(self, t, keycol):
        return t["cols_by_num"].get(keycol["attnum"]) if keycol["attnum"] else None

    def distinct(self, t, keycol):
        """Estimate distinct values of an index key column -> (count or None, source)."""
        col = self.table_col(t, keycol)
        if col is None:
            return None, None
        s = self.colstat(t, col["name"])
        if s and s.get("n_distinct") is not None:
            nd = s["n_distinct"]
            if nd > 0:
                return nd, "stats"
            if nd < 0 and self.rows(t):
                return -nd * self.rows(t), "stats"
        if col["base_type"] == "boolean":
            return 2, "schema"
        if col.get("is_enum"):
            labels = self.enums.get(col["type"]) or self.enums.get(col["base_type"])
            if labels:
                return len(labels), "schema"
        return None, None

    def monotonic(self, t, keycol):
        """Is this key column monotonically increasing on insert? -> (reason, confidence) or (None, None)."""
        col = self.table_col(t, keycol)
        if col is None:
            return None, None
        if col.get("identity"):
            return "identity column", "high"
        d = col.get("default") or ""
        if "nextval(" in d:
            return "sequence default", "high"
        if col["base_type"] in TIME_TYPES:
            if TIME_DEFAULT_RE.search(d):
                return f"default {d}", "high"
            if TIME_NAME_RE.search(col["name"]):
                return "timestamp-like column name", "medium"
        return None, None

    def split_values(self, t, keycol):
        """Known values for SPLIT AT VALUES (all but the smallest), or None."""
        col = self.table_col(t, keycol)
        if col is None:
            return None
        labels = self.enums.get(col["type"]) or self.enums.get(col["base_type"])
        if labels:
            vals = labels                      # enum sort order = declared order
        else:
            s = self.colstat(t, col["name"])
            mcv = s and s.get("most_common_vals")
            if not mcv:
                return None
            vals = parse_pg_array(mcv)
            if vals is None or col["base_type"] in TIME_TYPES:
                return None
            if col["base_type"] in INT_TYPES or col["base_type"] == "numeric":
                try:
                    vals = sorted(vals, key=float)
                except ValueError:
                    return None
            else:
                vals = sorted(vals)            # verify against the column collation
        return vals[1:] if len(vals) > 1 else None

    def statements_for(self, t):
        pat = re.compile(r'(?<![\w."])(?:"?%s"?\s*\.\s*)?"?%s"?(?![\w"])' % (
            re.escape(t["schema"]), re.escape(t["name"])), re.I)
        return [s for s in self.statements if pat.search(s["query"])]


def parse_pg_array(text):
    """Parse a one-dimensional PostgreSQL array literal of scalars."""
    if not text or text[0] != "{" or text[-1] != "}":
        return None
    out, buf, i, quoted, s = [], "", 0, False, text[1:-1]
    if not s:
        return []
    while i < len(s):
        ch = s[i]
        if quoted:
            if ch == "\\" and i + 1 < len(s):
                buf += s[i + 1]
                i += 1
            elif ch == '"':
                quoted = False
            else:
                buf += ch
        elif ch == '"':
            quoted = True
        elif ch == ",":
            out.append(buf)
            buf = ""
        else:
            buf += ch
        i += 1
    out.append(buf)
    return out


# --------------------------------------------------------------------------- findings

class Findings:
    def __init__(self):
        self.items = []

    def add(self, rule, severity, evidence, obj, title, detail, recommendation,
            reference, ddl=None, confidence="high", confirm=None, data=None):
        self.items.append({
            "rule": rule, "severity": severity, "evidence": evidence, "confidence": confidence,
            "object": obj, "title": title, "detail": detail, "recommendation": recommendation,
            "reference": reference, "suggested_ddl": ddl, "needs_confirmation": confirm or [],
            "data": data or {},
        })

    def sorted(self):
        out = sorted(self.items, key=lambda f: (SEV_ORDER[f["severity"]],
                                                EVIDENCE_ORDER[f["evidence"]], f["rule"], f["object"]))
        for n, f in enumerate(out, 1):
            f["id"] = f"F{n:03d}"
        return out


def key_text(i):
    return ", ".join(f"{c['expr']} {c['opt']}" for c in i["keys"])


def range_usage(model, t, colname):
    """Statements on table t that range-filter or sort on colname (heuristic text match)."""
    c = r'(?:[\w"]+\.)?"?%s"?' % re.escape(colname)
    pat = re.compile(r"%s\s*(<=|>=|<|>|\bBETWEEN\b)|(<=|>=|<|>)\s*%s\b|\bORDER\s+BY\b[^;]*?%s" % (c, c, c), re.I)
    return [s for s in model.statements_for(t) if pat.search(s["query"])]


def eq_param_usage(model, t, colname):
    c = r'(?:[\w"]+\.)?"?%s"?' % re.escape(colname)
    pat = re.compile(r"%s\s*=\s*\$\d+|\$\d+\s*=\s*%s(?![\w\"])|%s\s+IN\s*\(\s*\$\d+" % (c, c, c), re.I)
    return [s for s in model.statements_for(t) if pat.search(s["query"])]


def stmt_ref(stmts, n=3):
    return [{"queryid": s["queryid"], "calls": s["calls"], "total_time_ms": round(s["total_time_ms"], 1),
             "query": s["query"][:300]} for s in stmts[:n]]


# ---- rules -----------------------------------------------------------------

def rule_no_pk(m, F):
    for t in m.tables.values():
        if t["relkind"] != "r" or t["pk"] or t.get("parent"):
            continue
        uniq = [i for i in t["indexes"] if i["is_unique"] and not i["predicate"] and i["is_valid"]
                and all(c["attnum"] and t["cols_by_num"][c["attnum"]]["not_null"] for c in i["keys"])]
        rec = "Define an explicit PRIMARY KEY. Without one, rows are keyed on a hidden ybrowid " \
              "(HASH) and every secondary-index lookup needs an extra hop."
        if uniq:
            rec += f" Candidate: UNIQUE NOT NULL index {uniq[0]['name']} ({key_text(uniq[0])})."
        rec += " ALTER TABLE ... ADD PRIMARY KEY rewrites the table: plan a maintenance window or migrate to a new table."
        F.add("no-primary-key", "high", "schema", t["fq"], "Table has no primary key",
              f"{t['fq']} is stored on the hidden ybrowid.", rec, REF["pk"])


def rule_hash_on_range_type(m, F):
    for i in m.indexes:
        if i["am"] != "lsm" or i["is_partition"] or not i["is_valid"]:
            continue
        t = i["table"]
        for pos, k in enumerate(i["keys"], 1):
            if k["opt"] != "HASH" or k["base_type"] not in TIME_TYPES:
                continue
            col = m.table_col(t, k)
            used = range_usage(m, t, col["name"]) if col else []
            sev, ev = ("high", "workload") if used else ("medium", "schema")
            what = "primary key" if i["is_pk"] else "index"
            mono, _ = m.monotonic(t, k)
            rec = (f"A HASH column only serves equality: range filters / ORDER BY on {k['expr']} cannot seek. "
                   f"Make it a range column ({k['expr']} ASC/DESC)")
            if pos > 1 or i["hash_count"] > 1:
                rec += ", keeping the high-cardinality equality columns as HASH and moving the time column to the range part"
            if mono or pos == 1:
                rec += ". A leading range time column is a write hotspot: add a bucket prefix (pattern A1)"
            rec += ". Keep HASH only if every query is an exact-match lookup."
            F.add("hash-on-range-type", sev, ev, i["fq"],
                  f"HASH-sharded {k['base_type']} column in {what}",
                  f"{what.capitalize()} {i['name']} on {t['fq']}: key position {pos} `{k['expr']}` is HASH.",
                  rec, REF["hotspots"], confidence="high" if used else "medium",
                  confirm=[] if used else [f"Are there range filters or ORDER BY on {t['fq']}.{k['expr']}?"],
                  data={"definition": i["definition"], "range_statements": stmt_ref(used)})


def _opts(i):
    return [c["opt"] if c["opt"] == "HASH" else f"{c['opt']} NULLS {'FIRST' if c['nulls_first'] else 'LAST'}"
            for c in i["keys"]]


def covers(r, e):
    """Does index e make index r redundant? Mirrors scripts/checks/redundant_indexes.sql."""
    n = r["nkey"]
    if n > e["nkey"]:
        return False
    rk, ek = r["keys"], e["keys"]
    if [c["expr"] for c in ek[:n]] != [c["expr"] for c in rk]:
        return False
    if [c["opclass"] for c in ek[:n]] != [c["opclass"] for c in rk] or \
       [c["collation"] for c in ek[:n]] != [c["collation"] for c in rk]:
        return False
    ro, eo = _opts(r), _opts(e)
    if not all(a == b or (a == "HASH" and b != "HASH") for a, b in zip(ro, eo[:n])):
        return False
    if len(eo) > n and eo[n] == "HASH":                 # e's hash columns must be inside r's key
        return False
    if e["predicate"] is not None and e["predicate"] != r["predicate"]:
        return False
    if not e["is_pk"]:                                   # the PK holds every column
        rest = {c["expr"] for c in e["columns"][n:]}
        if not all(c["expr"] in rest for c in r["include"]):
            return False
    if r["is_unique"]:
        if not (e["is_unique"] and e["nkey"] == n):
            return False
        if r["nulls_not_distinct"] and not (e["nulls_not_distinct"] or e["is_pk"]):
            return False
    return True


def identical(r, e):
    return ([c["expr"] for c in r["columns"]] == [c["expr"] for c in e["columns"]]
            and _opts(r) == _opts(e) and r["predicate"] == e["predicate"]
            and r["is_unique"] == e["is_unique"] and r["nulls_not_distinct"] == e["nulls_not_distinct"])


def rule_redundant(m, F):
    for t in m.tables.values():
        idx = [i for i in t["indexes"] if i["am"] == "lsm" and i["is_valid"]]
        for r in idx:
            if r["backs_constraint"] or r["referenced_by_fk"] or r["is_partition"] or r["is_pk"]:
                continue
            cands = []
            for e in idx:
                if e is r or not covers(r, e):
                    continue
                if identical(r, e) and not e["backs_constraint"] and not e["is_pk"] and r["name"] < e["name"]:
                    continue                       # exact duplicates: drop the larger name only
                cands.append(e)
            if not cands:
                continue
            e = sorted(cands, key=lambda x: (not x["is_pk"], not x["backs_constraint"], x["name"]))[0]
            notes = []
            if e["is_pk"]:
                notes.append("covered by the primary key")
            if identical(r, e):
                notes.append("exact duplicate")
            if r["predicate"] and not e["predicate"]:
                notes.append("partial index covered by a full index (the partial one may be smaller/cheaper to scan)")
            if _opts(r) != _opts(e)[:r["nkey"]]:
                notes.append("HASH replaced by range: data distribution changes")
            # generic pre-drop checks (hints, per-node usage) are listed once in the report
            confirm = []
            if e["keys"][0]["opt"] != "HASH" and m.monotonic(t, e["keys"][0])[0]:
                confirm.append(f"{e['name']} leads with a monotonic range column: fix that hotspot before "
                               f"routing {r['name']}'s traffic to it.")
            F.add("redundant-index", "medium", "schema", r["fq"],
                  "Redundant index",
                  f"{r['name']} ({key_text(r)}) is covered by {e['name']} ({key_text(e)})."
                  + (f" Notes: {'; '.join(notes)}." if notes else ""),
                  "Drop it after confirming nothing depends on it. Every index adds a write per row change.",
                  REF["index"] + "#redundant-indexes",
                  ddl=f"DROP INDEX {qi(t['schema'])}.{qi(r['name'])};  -- recreate: {r['definition']};",
                  confirm=confirm, data={"redundant": r["definition"], "covered_by": e["definition"]})


def rule_monotonic_range(m, F):
    for i in m.indexes:
        if i["am"] != "lsm" or i["is_partition"] or not i["keys"]:
            continue
        t = i["table"]
        yb = (t.get("yb") if i["is_pk"] else i.get("yb")) or {}
        if yb.get("is_colocated"):
            continue
        k0 = i["keys"][0]
        if k0["opt"] == "HASH":
            continue
        reason, conf = m.monotonic(t, k0)
        if not reason:
            continue
        what = "Primary key" if i["is_pk"] else "Index"
        sev = "high" if conf == "high" else "medium"
        rec = (f"Every insert lands on the last tablet of {i['name']}; tablet splitting moves the hotspot, "
               f"it doesn't remove it. Options: HASH it if no range scans are needed; "
               f"a bucket prefix ((yb_hash_code({k0['expr']}) % N) ASC, {k0['expr']} {k0['opt']}) with "
               f"SPLIT AT VALUES (pattern A1); or lead with a high-cardinality HASH column.")
        if i["is_pk"]:
            rec += " Changing a PK rewrites the table."
        F.add("monotonic-range-key", sev, "schema", i["fq"],
              f"{what} leads with a monotonic range column",
              f"{i['name']} on {t['fq']} is range-sharded on `{k0['expr']} {k0['opt']}` ({reason}).",
              rec, REF["hotspots"], confidence=conf,
              confirm=[] if conf == "high" else [f"Is {t['fq']}.{k0['expr']} set to the current time on insert?"],
              data={"definition": i["definition"]})


def rule_lowcard_hash(m, F):
    for i in m.indexes:
        if i["am"] != "lsm" or i["is_partition"] or not i["is_valid"] or not i["hash_count"]:
            continue
        t = i["table"]
        if i["predicate"] and _fixed_by_predicate(i["predicate"], i["keys"][0]["expr"]):
            continue                                   # reported by partial-keyed-on-predicate
        est, srcs = 1.0, set()
        for k in i["keys"][:i["hash_count"]]:
            nd, src = m.distinct(t, k)
            if nd is None:
                est = None
                break
            est *= nd
            srcs.add(src)
        if est is None or est > LOWCARD_MAX:
            continue
        ev = "stats" if "stats" in srcs else "schema"
        rows = m.rows(t)
        sev = "high" if est <= LOWCARD_SEVERE and (rows is None or rows >= PRESPLIT_ROWS) else "medium"
        hk = ", ".join(c["expr"] for c in i["keys"][:i["hash_count"]])
        if i["is_pk"]:
            rec = "Lead the PK with a high-cardinality HASH column; keep the low-cardinality column in the range part."
        else:
            vals = m.split_values(t, i["keys"][0])
            pk = t["pk"]
            pk_hash = bool(pk and pk["hash_count"])
            rest = [c for c in i["keys"][1:]] if i["hash_count"] == 1 else []
            rest_txt = "".join(f", {c['expr']} ASC" for c in rest)
            split = f"\n  SPLIT AT VALUES ({', '.join('(' + ql(v) + ')' for v in vals)})" if vals and i["hash_count"] == 1 else ""
            rec = (f"All rows with one {hk} value share a hash code, so they sit on one tablet that can't split "
                   f"(at most {int(est)} tablets ever). Options: (1) lead with a high-cardinality HASH column the "
                   f"queries filter on; (2) range-shard it ({hk} ASC) and pre-split")
            rec += (" — the HASH primary key suffix spreads entries within each value." if pk_hash else
                    " — but this table's PK is not HASH, so each value gets a write tail; add a bucket (pattern A1).")
            rec += " (3) A partial index if only some values are queried."
            ddl = None
            if i["hash_count"] == 1 and not i["is_unique"]:
                inc = f" INCLUDE ({', '.join(c['expr'] for c in i['include'])})" if i["include"] else ""
                pred = f" WHERE {i['predicate']}" if i["predicate"] else ""
                ddl = (f"CREATE INDEX {qi(i['name'] + '_range')} ON {qi(t['schema'])}.{qi(t['name'])} "
                       f"({i['keys'][0]['expr']} ASC{rest_txt}){inc}{pred}{split};\n"
                       f"-- verify plans, then: DROP INDEX {qi(t['schema'])}.{qi(i['name'])};")
        F.add("low-cardinality-hash", sev, ev, i["fq"],
              "HASH sharding on a low-cardinality key",
              f"{i['name']} on {t['fq']} hashes on ({hk}): ~{int(est)} distinct values"
              + (f", {int(rows):,} rows" if rows else "") + ".",
              rec, REF["lowcard"], ddl=None if i["is_pk"] else ddl,
              confidence="high" if ev == "stats" else "medium",
              confirm=[f"Which queries use {i['name']}, and do they also filter on a high-cardinality column?"],
              data={"definition": i["definition"], "distinct_estimate": est})


def rule_skew_or_nulls(m, F):
    for i in m.indexes:
        if i["am"] != "lsm" or i["is_partition"] or not i["is_valid"] or not i["keys"] or i["is_pk"]:
            continue
        k0 = i["keys"][0]
        if k0["opt"] != "HASH" or i["hash_count"] != 1:
            continue
        t = i["table"]
        col = m.table_col(t, k0)
        if not col:
            continue
        nd, _ = m.distinct(t, k0)
        if nd is not None and nd <= LOWCARD_MAX:
            continue                                   # reported by low-cardinality-hash
        s = m.colstat(t, col["name"])
        if not s:
            continue
        pred = i["predicate"] or ""
        mentions = re.search(r"\b%s\b" % re.escape(col["name"]), pred)
        nf = s.get("null_frac") or 0
        top = (s.get("most_common_freqs") or [0])[0] or 0
        if nf >= NULL_HEAVY and not mentions:
            F.add("null-heavy-hash", "medium", "stats", i["fq"], "NULL-heavy HASH index column",
                  f"{nf:.0%} of {t['fq']}.{col['name']} is NULL; all NULL entries of {i['name']} hash to one tablet.",
                  f"If NULLs are never looked up: add WHERE {col['name']} IS NOT NULL. If they are, range-shard "
                  f"({col['name']} ASC); a HASH PK suffix spreads the NULL range after splits.",
                  REF["partial"],
                  ddl=f"CREATE INDEX {qi(i['name'] + '_nn')} ON {qi(t['schema'])}.{qi(t['name'])} "
                      f"({key_text(i)}) WHERE {col['name']} IS NOT NULL;",
                  confirm=[f"Do any queries look up {col['name']} IS NULL via this index?"],
                  data={"null_frac": nf})
        elif top >= SKEW_FREQ and not mentions:
            F.add("skewed-hash", "medium", "stats", i["fq"], "Skewed HASH index column",
                  f"One value of {t['fq']}.{col['name']} holds {top:.0%} of rows; its entries all sit on one tablet of {i['name']}.",
                  "Exclude the dominant value with a partial index if it's rarely queried, or range-shard "
                  f"({col['name']} ASC) with a HASH PK suffix.", REF["partial"] + "#case-2-dominant-single-value-skewed-distribution",
                  confirm=["Is the dominant value queried through this index?"], data={"top_freq": top})


def rule_range_tails(m, F):
    """Low-cardinality leading range column followed by a monotonic key: one hot tail per value."""
    for i in m.indexes:
        if i["am"] != "lsm" or i["is_partition"] or not i["keys"] or i["is_pk"]:
            continue
        k0 = i["keys"][0]
        if k0["opt"] == "HASH":
            continue
        t = i["table"]
        nd, src = m.distinct(t, k0)
        if nd is None or nd > LOWCARD_MAX:
            continue
        cause = None
        if len(i["keys"]) > 1:
            r, _ = m.monotonic(t, i["keys"][1])
            if r:
                cause = f"`{i['keys'][1]['expr']}` ({r}) follows it"
        if not cause and not i["is_unique"] and len(i["keys"]) == 1:
            pk = t["pk"]
            if pk and not pk["hash_count"]:
                r, _ = m.monotonic(t, pk["keys"][0])
                if r:
                    cause = f"the range PK suffix `{pk['keys'][0]['expr']}` ({r}) is appended to every entry"
        if not cause:
            continue
        F.add("range-hot-tails", "medium", src or "schema", i["fq"], "One write tail per low-cardinality value",
              f"{i['name']} on {t['fq']} is range-sharded on ({key_text(i)}): `{k0['expr']}` has ~{int(nd)} values "
              f"and {cause}, so new rows for each value append at the end of that value's range.",
              "Fine if inserts spread across values and values ≥ nodes; a hotspot if most new rows share one "
              "value. Drop the monotonic column if nothing filters/sorts on it (with a HASH PK the suffix "
              "spreads entries), or add a bucket prefix (pattern A1).",
              REF["lowcard"] + "#why-range-sharding-works-here-the-hidden-pk-suffix", confidence="medium",
              confirm=[f"What share of new rows get the same {k0['expr']} value?"],
              data={"definition": i["definition"]})


def rule_presplit(m, F):
    for i in m.indexes:
        if i["am"] != "lsm" or i["is_partition"] or not i["keys"]:
            continue
        t = i["table"]
        yb = (t.get("yb") if i["is_pk"] else i.get("yb")) or {}
        if not yb or yb.get("is_colocated") or yb.get("num_hash_key_columns") != 0 or yb.get("num_tablets") != 1:
            continue
        rows = m.rows(t)
        if not rows or rows < PRESPLIT_ROWS:
            continue
        sev = "medium" if rows >= LARGE_TABLE else "low"
        vals = m.split_values(t, i["keys"][0])
        hint = (f" Known values allow SPLIT AT VALUES ({', '.join('(' + ql(v) + ')' for v in vals)})."
                if vals else "")
        F.add("range-single-tablet", sev, "stats", i["fq"], "Range-sharded relation on a single tablet",
              f"{i['name']} on {t['fq']} ({int(rows):,} rows) is range-sharded and has 1 tablet: all reads and "
              f"writes go to one tablet leader until auto-splitting catches up.",
              "Pre-split new range indexes/tables with SPLIT AT VALUES; for existing ones, confirm auto-splitting "
              "is enabled and the tablet count in yb-admin (num_tablets can be stale)." + hint,
              REF["lowcard"] + "#why-range-sharding-works-here-the-hidden-pk-suffix",
              confirm=["Current tablet count (yb-admin list_tablets) — the catalog value may be stale."])


def _fixed_by_predicate(pred, col):
    c = r'"?%s"?' % re.escape(col)
    return bool(re.search(r"\b%s\s*=\s*(?!\$)" % c, pred, re.I) or
                re.search(r"\b%s\s+IS\s+(NULL|TRUE|FALSE)\b" % c, pred, re.I) or
                re.fullmatch(r"\(?\s*(NOT\s+)?%s\s*\)?" % c, pred.strip(), re.I))


def rule_partial(m, F):
    for i in m.indexes:
        if not i["predicate"] or i["is_partition"] or not i["keys"]:
            continue
        t = i["table"]
        k0 = i["keys"][0]
        if k0["attnum"] and _fixed_by_predicate(i["predicate"], k0["expr"]):
            F.add("partial-keyed-on-predicate", "medium", "schema", i["fq"],
                  "Partial index keyed on its own predicate column",
                  f"{i['name']} on {t['fq']} is keyed on `{k0['expr']}`, which WHERE {i['predicate']} fixes to one value: "
                  "every entry has the same leading key (one hash → one tablet if HASH) and the planner drops the "
                  "implied equality from the index condition (yugabyte-db#18888).",
                  "Key the partial index on the columns the queries filter or sort on, e.g. "
                  f"(<high-cardinality col> HASH, <sort col> DESC) WHERE {i['predicate']}.",
                  REF["partial"] + "#case-2-dominant-single-value-skewed-distribution",
                  data={"definition": i["definition"]})
        # parameterized predicate columns (statements are normalized: literals look like $n too)
        pcols = [c["name"] for c in t.get("columns") or [] if _fixed_by_predicate(i["predicate"], c["name"])]
        hits = []
        for c in pcols:
            hits += eq_param_usage(m, t, c)
        if hits:
            F.add("partial-index-parameterized", "low", "workload", i["fq"],
                  "Partial index predicate column is compared to a parameter",
                  f"Queries compare {', '.join(pcols)} to $n. pg_stat_statements normalizes literals to $n too, so "
                  "this may be a literal (fine) or a bind parameter (generic plans can't use the partial index).",
                  "Confirm in application code / ORM how the value is sent, or EXPLAIN EXECUTE after 5+ executions.",
                  REF["partial"] + "#gotcha-partial-indexes-and-prepared-statements", confidence="low",
                  confirm=[f"Is {', '.join(pcols)} sent as a literal or a bind parameter?"],
                  data={"statements": stmt_ref(hits)})


def _index_supports(i, attnums):
    if i["am"] != "lsm" or not i["is_valid"] or i["predicate"] and "IS NOT NULL" not in i["predicate"].upper():
        return False
    keys = [c["attnum"] for c in i["keys"]]
    n = len(attnums)
    return len(keys) >= n and set(keys[:n]) == set(attnums) and i["hash_count"] <= n


def rule_fk(m, F):
    for fk in m.fks:
        t = m.tables.get(str(fk["table_oid"]))
        ref = m.tables.get(str(fk["ref_oid"]))
        if not t:
            continue
        cols = fk["columns"]
        attnums = [t["cols_by_name"][c["name"]]["attnum"] for c in cols]
        if not any(_index_supports(i, attnums) for i in t["indexes"]):
            names = ", ".join(c["name"] for c in cols)
            rows = m.rows(t)
            F.add("fk-without-index", "high" if rows and rows >= PRESPLIT_ROWS else "medium",
                  "stats" if rows else "schema", f"{t['fq']}.{fk['name']}", "Foreign key without a supporting index",
                  f"{t['fq']}({names}) references {ref['fq'] if ref else '?'} but no index leads with these columns: "
                  "each parent DELETE/key UPDATE scans the child table.",
                  "Create an index whose leading key columns are exactly the FK columns (a PK leading with them also counts).",
                  REF["fk"],
                  ddl=f"CREATE INDEX {qi(t['name'] + '_' + '_'.join(c['name'] for c in cols) + '_idx')} "
                      f"ON {qi(t['schema'])}.{qi(t['name'])} ({', '.join(qi(c['name']) for c in cols)});")
        bad = [c for c in cols if c["type"] != c["ref_type"] and not
               ({re.sub(r"\(.*\)", "", c["type"]), re.sub(r"\(.*\)", "", c["ref_type"])} <= {"text", "character varying"})]
        if bad:
            F.add("fk-type-mismatch", "medium", "schema", f"{t['fq']}.{fk['name']}", "Foreign key type mismatch",
                  "; ".join(f"{c['name']} {c['type']} → {c['ref_name']} {c['ref_type']}" for c in bad) + ".",
                  "Use identical types on both sides: cross-type comparisons cost CPU per lookup, can change plans, "
                  "and a narrower child type overflows first (INT child of BIGINT parent).", REF["fk"])


def rule_int_sequences(m, F):
    for s in m.sequences:
        if s["type"] in ("integer", "smallint") and s.get("owned_by"):
            F.add("int-sequence", "low", "schema", s["owned_by"], "Sequence-backed INT column",
                  f"{s['owned_by']} uses a {s['type']} sequence ({s['schema']}.{s['name']}).",
                  "Use BIGINT (GENERATED ... AS IDENTITY). Sequence values are cached per connection "
                  "(ysql_sequence_cache_minval, default 100), so values burn faster than rows are inserted.",
                  REF["hotspots"])


def rule_invalid(m, F):
    for i in m.indexes:
        if not i["is_valid"]:
            F.add("invalid-index", "high", "schema", i["fq"], "Invalid index",
                  f"{i['name']} on {i['table']['fq']} is not valid (failed or interrupted CONCURRENTLY build): "
                  "it costs writes and serves no reads.",
                  "Drop and recreate it.", REF["index"],
                  ddl=f"DROP INDEX {qi(i['table']['schema'])}.{qi(i['name'])};\n{i['definition']};")


def rule_gin(m, F):
    for i in m.indexes:
        if i["am"] == "ybgin" and i["nkey"] > 1:
            F.add("multi-column-gin", "medium", "schema", i["fq"], "Multi-column GIN index",
                  f"{i['name']} has {i['nkey']} key columns.", "Use one GIN index per column.", REF["index"])


def rule_analyze(m, F):
    stale = [t for t in m.tables.values() if t["relkind"] in ("r", "m") and not t.get("last_analyze")
             and not (t.get("reltuples") or 0) > 0]
    real = [t for t in m.tables.values() if t["relkind"] in ("r", "m")]
    if real and len(stale) == len(real) and not m.colstats:
        F.add("schema-only", "info", "schema", m.meta.get("database", "db"), "Schema-only bundle",
              "No table has statistics (a schema replay or a never-analyzed database): cardinality, NULL and skew "
              "checks were skipped; enum/boolean types were used as the only cardinality signal.",
              "For stats-backed findings, ask the customer to run export-stats.sql on the source database.",
              REF["review"])
        return
    if stale:
        names =", ".join(t["fq"] for t in stale[:15]) + (" …" if len(stale) > 15 else "")
        F.add("missing-statistics", "medium" if m.cbo_on else "low", "stats", m.meta.get("database", "db"),
              "Tables without optimizer statistics",
              f"{len(stale)} table(s) have never been analyzed: {names}. Stats-based checks were skipped for them.",
              "Run ANALYZE (with the customer's agreement) — the cost-based optimizer relies on it. "
              "Auto Analyze is the default from 2025.2; check yb_disable_auto_analyze / ysql_enable_auto_analyze.",
              REF["review"], data={"tables": [t["fq"] for t in stale]})


def rule_unused(m, F):
    if not m.usage:
        return
    nodes = {u["meta"].get("node") for u in m.usage}
    full = m.node_count and len(nodes) >= m.node_count
    scans = defaultdict(int)
    for u in m.usage:
        for r in u.get("index_usage") or []:
            scans[(r["schema"], r["index"])] += int(r.get("idx_scan") or 0)
    unused = defaultdict(list)
    for i in m.indexes:
        if i["is_pk"] or i["backs_constraint"] or i["referenced_by_fk"] or i["is_partition"]:
            continue
        key = (i["table"]["schema"], i["name"])
        if key in scans and scans[key] == 0:
            unused[i["table"]["fq"]].append(i)
    resets = sorted({str(u["meta"].get("stats_reset")) for u in m.usage})
    for fq, idx in unused.items():
        F.add("unused-index", "medium" if full else "info", "workload", fq,
              f"{len(idx)} index(es) never scanned",
              f"idx_scan = 0 on {len(nodes)} of {m.node_count or '?'} node(s) (stats reset: {', '.join(resets)}): "
              + ", ".join(i["name"] for i in idx) + ".",
              "Candidates to drop if the observation window covers a full business cycle (month-end, batch "
              "jobs). Unused is not the same as redundant: an unused index may still be the right one for a "
              "rare query." + ("" if full else " Not all nodes were collected: usage counters are per node."),
              REF["index"], confidence="medium" if full else "low",
              confirm=["How long is the observation window, and does it include periodic jobs?"],
              data={"indexes": [i["definition"] for i in idx]})


def rule_inefficient_statements(m, F):
    for s in m.statements[:200]:
        scanned = s.get("docdb_rows_scanned")
        if scanned is None or s["calls"] < 10:
            continue
        ratio = scanned / max(s["rows"], 1)
        if ratio < SCAN_RATIO or scanned < 10_000:
            continue
        F.add("scan-heavy-statement", "medium", "workload", f"queryid {s['queryid']}",
              "Statement scans far more rows than it returns",
              f"{s['calls']:,} calls, {s['total_time_ms']:,.0f} ms total; DocDB scanned {scanned:,} rows to return "
              f"{s['rows']:,} ({ratio:,.0f}×).",
              "Check for a missing index, a HASH column used for range/ORDER BY, or a filter that is not pushed "
              "down. Get the plan with EXPLAIN (ANALYZE, DIST) on staging.",
              REF["index"], confidence="medium", data={"statement": stmt_ref([s], 1)[0]})


def rule_settings(m, F):
    st = m.meta.get("settings") or {}
    if st.get("yb_use_hash_splitting_by_default") == "off":
        F.add("range-default", "info", "schema", m.meta.get("database", "db"), "Range sharding is the default",
              "yb_use_hash_splitting_by_default = off (e.g. Enhanced PostgreSQL Compatibility): a key without "
              "HASH/ASC/DESC is range-sharded.",
              "Write HASH / ASC / DESC explicitly in DDL; monotonic keys created without HASH are flagged above.",
              REF["sharding"] + "#enhanced-postgresql-compatibility-mode")


RULES = [rule_no_pk, rule_invalid, rule_hash_on_range_type, rule_monotonic_range, rule_lowcard_hash,
         rule_skew_or_nulls, rule_range_tails, rule_presplit, rule_partial, rule_redundant, rule_fk,
         rule_int_sequences, rule_gin, rule_analyze, rule_unused, rule_inefficient_statements, rule_settings]


# --------------------------------------------------------------------------- validation & output

def validate(b, m):
    w = list(b.warnings)
    meta = m.meta
    if meta.get("values_mode") == "all":
        w.append("stats.json was exported with values_mode=all: it may contain customer data values.")
    if b.usage:
        dbs = {u["meta"].get("database") for u in b.usage}
        if dbs != {meta.get("database")}:
            w.append(f"usage files are for database(s) {sorted(dbs)}, stats.json for {meta.get('database')}.")
        nodes = {u["meta"].get("node") for u in b.usage}
        if m.node_count and len(nodes) < m.node_count:
            w.append(f"usage collected from {len(nodes)} of {m.node_count} nodes: unused-index findings are informational only.")
    else:
        w.append("No usage/*.json: unused-index checks skipped.")
    if not b.statements:
        w.append("No statements: workload-backed checks skipped; findings stay at schema/stats evidence.")
    return w


def write_outputs(out, b, m, findings, warnings):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "findings.json"), "w", encoding="utf-8") as fh:
        json.dump({"format": "yb-schema-review/findings/v1", "meta": m.meta, "warnings": warnings,
                   "statement_source": b.statement_source, "findings": findings}, fh, indent=2, default=str)

    ddl = [f for f in findings if f["suggested_ddl"]]
    creates = [f for f in ddl if not f["suggested_ddl"].lstrip().upper().startswith("DROP")]
    drops = [f for f in ddl if f["suggested_ddl"].lstrip().upper().startswith("DROP")]
    with open(os.path.join(out, "proposed.sql"), "w", encoding="utf-8") as fh:
        fh.write("-- Proposed changes from the YugabyteDB schema review. REVIEW ONLY: nothing here has been run.\n"
                 "-- Order: create replacements, verify plans on staging, then drop. See references/review-mode.md.\n\n")
        for title, group in (("1. Create / replace", creates), ("2. Drop (after verification)", drops)):
            fh.write(f"-- ===== {title} =====\n\n")
            for f in group:
                fh.write(f"-- {f['id']} [{f['severity']}] {f['rule']}: {f['object']}\n{f['suggested_ddl']}\n\n")

    L = []
    L.append(f"# YugabyteDB schema review: {m.meta.get('database')}\n")
    L.append(f"- Exported: {m.meta.get('exported_at')}  \n- Version: {m.meta.get('version')}  ")
    L.append(f"- Nodes: {m.node_count} {m.meta.get('placements') or ''}  \n- Tables: {len(m.tables)}, indexes: {len(m.indexes)}  ")
    L.append(f"- Statements: {len(b.statements)} ({b.statement_source or 'none'})  \n- Usage files: {len(b.usage)}\n")
    if warnings:
        L.append("## Bundle warnings\n")
        L += [f"- {x}" for x in warnings]
        L.append("")
    counts = defaultdict(int)
    for f in findings:
        counts[f["severity"]] += 1
    L.append("## Summary\n")
    L.append("| Severity | Count |\n|---|---|")
    L += [f"| {s} | {counts[s]} |" for s in SEV_ORDER if counts[s]]
    L.append("\nEvidence: **schema** = from DDL (candidate), **stats** = backed by pg_stats/row counts, "
             "**workload** = backed by statements/usage.\n")
    L.append("## Findings\n")
    for f in findings:
        L.append(f"### {f['id']} [{f['severity']}] {f['title']} — `{f['object']}`\n")
        L.append(f"*Rule `{f['rule']}` · evidence: {f['evidence']} · confidence: {f['confidence']} · see `{f['reference']}`*\n")
        L.append(f"{f['detail']}\n\n**Recommendation:** {f['recommendation']}\n")
        if f["suggested_ddl"]:
            L.append(f"```sql\n{f['suggested_ddl']}\n```\n")
        if f["needs_confirmation"]:
            L.append("Confirm: " + " ".join(f"☐ {q}" for q in f["needs_confirmation"]) + "\n")
    questions = [(f["id"], q) for f in findings for q in f["needs_confirmation"]]
    if questions:
        L.append("## Questions for the customer\n")
        grouped = defaultdict(list)
        for i, q in questions:
            grouped[q].append(i)
        L += [f"- ({', '.join(ids)}) {q}" for q, ids in grouped.items()]
        L.append("")
    if any(f["rule"] in ("redundant-index", "unused-index") for f in findings):
        L.append("## Before dropping any index\n")
        L.append("- No query hint (`pg_hint_plan` `IndexScan(t idx)`) or application code names it.")
        L.append("- Usage counters were collected on **every** node (they are per node) over a full business cycle.")
        L.append("- It doesn't back a constraint the application relies on (checked automatically for PK/UNIQUE/FK).")
        L.append("- Keep the recreate statement (in `proposed.sql`); drop one index at a time and watch latency.\n")
    if b.statements:
        L.append(f"## Top {TOP_STATEMENTS} statements by total time\n")
        L.append("| # | calls | total ms | rows | DocDB rows scanned | nodes | query |\n|---|---|---|---|---|---|---|")
        for n, s in enumerate(b.statements[:TOP_STATEMENTS], 1):
            q = re.sub(r"\s+", " ", s["query"])[:120].replace("|", "\\|")
            L.append(f"| {n} | {s['calls']:,} | {s['total_time_ms']:,.0f} | {s['rows']:,} | "
                     f"{'' if s['docdb_rows_scanned'] is None else format(s['docdb_rows_scanned'], ',')} | "
                     f"{len(s['nodes'])} | `{q}` |")
        L.append("")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle", help="bundle directory containing stats.json")
    ap.add_argument("--out", help="output directory (default: BUNDLE/review)")
    args = ap.parse_args()
    b = Bundle(args.bundle)
    m = Model(b)
    F = Findings()
    for rule in RULES:
        rule(m, F)
    findings = F.sorted()
    warnings = validate(b, m)
    out = args.out or os.path.join(args.bundle, "review")
    write_outputs(out, b, m, findings, warnings)
    counts = defaultdict(int)
    for f in findings:
        counts[f["severity"]] += 1
    print(f"{len(findings)} findings ({', '.join(f'{v} {k}' for k, v in sorted(counts.items(), key=lambda x: SEV_ORDER[x[0]]))}) "
          f"→ {out}/report.md, findings.json, proposed.sql")
    for x in warnings:
        print(f"warning: {x}", file=sys.stderr)


if __name__ == "__main__":
    main()
