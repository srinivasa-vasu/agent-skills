#!/usr/bin/env python3
"""
Offline YugabyteDB schema review.

Reads a review bundle and writes findings.json, report.html (or report.md), changes.sql and rollback.sql
(the last three via render.py, which re-renders them after triage).
Never connects to a database and never executes DDL. Python 3.8+, stdlib only.

    python3 analyze.py BUNDLE_DIR [--out OUT_DIR] [--format html|md]
    python3 analyze.py schema.sql [--out OUT_DIR]                    # schema only, no stats
                                                                       # (keys without HASH/ASC: HASH first column)
    python3 analyze.py schema.sql --range-default                    # same, but such keys are range (ASC)

Bundle layout (stats.json or schema.sql is required):

    BUNDLE_DIR/
      stats.json          export-stats.sql      catalog + pg_stats (one node)
      usage/*.json        export-usage.sql      per-node index/table usage + pg_stat_statements
      statements/*.json   tserver :13000/statements per node, a pg_stat_statements JSON
                          array, or one consolidated file (see references/review-mode.md)
      schema.sql          ysql_dump --schema-only --include-yb-metadata, or DDL. Parsed into the
                          catalog (schema_sql.py) only when stats.json is absent.

Each finding carries an evidence level:
    schema    derived from DDL alone (candidate)
    stats     backed by pg_stats / row counts (likely)
    workload  backed by statements or usage counters (confirmed)
"""

import argparse
import glob
import hashlib
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
SCAN_PER_CALL = 100         # and at least this many DocDB rows scanned per call
SLOW_MEAN_MS = 20           # statement mean time worth an index review
HEAVY_SHARE = 0.05          # statement's share of all statement time worth an index review
SMALL_TABLE = 1_000         # known row count below this: a full scan is cheap
UNIQUE_RATIO = 0.5          # distinct >= this fraction of rows: the key is unique in practice
TOP_STATEMENTS = 15

TIME_TYPES = {
    "timestamp without time zone", "timestamp with time zone", "date",
    "time without time zone", "time with time zone",
}
INT_TYPES = {"smallint", "integer", "bigint"}
# leading HASH key types flagged as possibly low-cardinality when no statistics exist
SUSPECT_LOWCARD_TYPES = {"smallint", '"char"'}
# column names that suggest a low-cardinality code (used only where no statistics exist)
LOWCARD_NAME_RE = re.compile(r"(^(is|has)_)|((^|_)(flag|flags|status|state|type|mode|kind|active|enabled|"
                             r"deleted|level|priority)$)", re.I)
TIME_DEFAULT_RE = re.compile(
    r"\b(now|clock_timestamp|statement_timestamp|transaction_timestamp)\s*\(|"
    r"\bCURRENT_(TIMESTAMP|DATE)\b|\bLOCALTIMESTAMP\b", re.I)
# date/time columns whose name suggests "set to now() on insert or update" (snake_case or camelCase)
TIME_NAME_STEM_RE = re.compile(
    r"^(created|inserted|ingested|logged|recorded|received|modified|updated|changed|"
    r"lastmodified|lastupdated|lastchanged|eventtime|ts|timestamp)"
    r"(at|on|date|time|ts|timestamp|datetime|dt)?$")
TIME_NAME_SUFFIX_RE = re.compile(r"(_(at|on|time|ts|date|dt)$)|([a-z0-9](At|On|Time|Ts|Date|Dt)$)")


def time_like_name(name):
    """created_at, createdAt, modified_date, modifiedDate, updatedAt, last_modified, event_ts, ..."""
    return bool(TIME_NAME_STEM_RE.match(re.sub(r"[^a-z]", "", name.lower()))
                or TIME_NAME_SUFFIX_RE.search(name))

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

def load_sibling(name):
    """Import a module from this directory (works under python -I, which drops the script dir)."""
    import importlib.util
    sys.dont_write_bytecode = True          # keep __pycache__ out of the skill directory
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location("yb_review_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Bundle:
    def __init__(self, path, range_default=False, database=None):
        self.schema_file = None
        self.database = database
        if os.path.isfile(path):                # analyze.py schema.sql
            self.schema_file = path
            path = os.path.dirname(os.path.abspath(path))
        self.range_default = range_default
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
        schema = self.schema_file or os.path.join(self.path, "schema.sql")
        if self.schema_file or not os.path.exists(sp):
            if not os.path.exists(schema):
                sys.exit(f"Neither {sp} nor {schema} found. Collect a bundle (collect.sh / export-stats.sql) "
                         "or provide schema.sql (see references/review-mode.md).")
            # schema-only review: build the catalog from the DDL
            self.stats, parse_warnings = load_sibling("schema_sql").parse(
                schema, hash_default=not self.range_default)
            self.warnings += [f"schema.sql: {w}" for w in parse_warnings]
            self.warnings.append(f"Catalog parsed from {os.path.basename(schema)} (no database): no statistics, "
                                 "row counts or tablet counts beyond what the DDL states.")
            self.from_schema = True
        else:
            self.stats = self._read_json(sp)
            self.from_schema = False
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
                rows = d
                if isinstance(d, dict):
                    # export file: {"statements": [...]}; tserver :13000/statements: {"ysql": {"queries": [...]}}
                    rows = d.get("statements")
                    if rows is None and isinstance(d.get("ysql"), dict):
                        rows = d["ysql"].get("queries")
                if not isinstance(rows, list):
                    self.warnings.append(f"Skipped {os.path.relpath(f, self.path)}: no statements list.")
                    continue
                raw.append((os.path.basename(f), rows))
            sources.append(f"{len(raw)} statements file(s)")
        self.statement_source = ", ".join(sources) or None
        dbname = self.database or (None if self.from_schema else self.stats["meta"].get("database"))
        self.statements = merge_statements(raw, dbid, self.warnings, dbname)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def merge_statements(raw, dbid, warnings, dbname=None):
    """Merge per-node statement lists on (dbid, userid, queryid); sum counters. Rows from other
    databases are dropped by dbid, or by datname (tserver :13000/statements) when dbid is unknown."""
    merged = {}
    other_db = 0
    datnames = {}
    for node, rows in raw:
        for s in rows:
            if not isinstance(s, dict) or "query" not in s:
                continue
            sdb = str(s.get("dbid", "")) if s.get("dbid") is not None else ""
            sname = s.get("datname")
            if dbid and sdb and sdb != dbid or not dbid and dbname and sname and sname != dbname:
                other_db += 1
                continue
            if sname:
                datnames[sname] = datnames.get(sname, 0) + 1
            qid = s.get("queryid", s.get("query_id"))
            key = (sdb, str(s.get("userid", "")), str(qid) if qid is not None else s["query"])
            m = merged.get(key)
            if m is None:
                m = merged[key] = {
                    "queryid": qid, "query": s["query"], "calls": 0, "total_time_ms": 0.0,
                    "rows": 0, "docdb_rows_scanned": None, "docdb_read_rpcs": None,
                    "docdb_seeks": None, "max_time_ms": None, "nodes": [],
                }
            m["calls"] += int(_num(s.get("calls")))
            # PG15-based: total_exec_time (+ total_plan_time); PG11-based: total_time
            m["total_time_ms"] += _num(s.get("total_exec_time", s.get("total_time")))
            m["rows"] += int(_num(s.get("rows")))
            mx = s.get("max_exec_time", s.get("max_time"))
            if mx is not None:
                m["max_time_ms"] = max(m["max_time_ms"] or 0.0, _num(mx))
            for k in ("docdb_rows_scanned", "docdb_read_rpcs", "docdb_seeks"):
                if k in s:
                    m[k] = (m[k] or 0) + int(_num(s[k]))
            if node and node not in m["nodes"]:
                m["nodes"].append(node)
    if other_db:
        warnings.append(f"Ignored {other_db} statement rows from other databases.")
    if len(datnames) > 1:
        warnings.append("Statements span several databases ("
                        + ", ".join(f"{k}: {v}" for k, v in sorted(datnames.items()))
                        + "); pass --database NAME to keep only the one this schema belongs to.")
    return sorted((m for m in merged.values()
                   if DML_RE.match(m["query"]) and not CATALOG_RE.search(m["query"])),
                  key=lambda m: -m["total_time_ms"])


# --------------------------------------------------------------------------- model

def partman_templates(st):
    """pg_partman template tables -> {table oid: reason}. They hold no data and normally have no
    PK; the parent and its partitions carry the real definition. Listed by part_config (live
    exports) or recognised by name: template_<parent schema>_<parent table> of a partitioned table."""
    listed = set()
    for name in st["meta"].get("partman_templates") or []:
        parts = [p[1:-1].replace('""', '"') if p.startswith('"') else p
                 for p in re.findall(r'"(?:[^"]|"")+"|[^."]+', name)]
        if len(parts) == 2:
            listed.add((parts[0], parts[1]))
    parents = [(t["schema"], t["name"]) for t in st["tables"] if t["relkind"] == "p"]
    out = {}
    for t in st["tables"]:
        if (t["schema"], t["name"]) in listed:
            out[str(t["oid"])] = "pg_partman template table (part_config)"
        elif t["relkind"] == "r" and t["name"].startswith("template_") and not t.get("is_partition"):
            for ps, pn in parents:
                if t["name"] == f"template_{ps}_{pn}"[:63]:
                    out[str(t["oid"])] = f"pg_partman template table for {ps}.{pn}"
    return out


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
            t["lc_cols"] = {c["name"].lower(): c for c in t.get("columns") or []}
            t["indexes"] = []
            t["pk"] = None
            self.tables[str(t["oid"])] = t
        # pg_partman template tables are not part of the review (their indexes and FKs drop out too)
        self.excluded = {}
        for oid, reason in partman_templates(st).items():
            t = self.tables.pop(oid)
            self.excluded[t["fq"]] = reason
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
        self.by_name = defaultdict(list)
        for t in self.tables.values():
            self.by_name[t["name"].lower()].append(t)
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
        # A1 bucket count for generated DDL: one per node, between 3 and 12 (see hotspots.md)
        self.buckets = min(12, max(3, self.node_count or 3))
        self.cbo_on = (self.meta.get("settings") or {}).get("yb_enable_cbo") == "on" or \
            (self.meta.get("settings") or {}).get("yb_enable_base_scans_cost_model") == "on"

    # ---- helpers

    def partitions(self, t):
        """Direct partitions of a partitioned table."""
        if "_parts" not in t:
            fq = tname(t)
            t["_parts"] = [c for c in self.tables.values() if c.get("parent") in (fq, t["fq"])]
        return t["_parts"]

    def colstat(self, t, colname):
        """pg_stats row for a column. A partitioned parent that was never analyzed has no stats of
        its own (only ANALYZE on the parent collects inherited stats): derive them from its
        partitions: distinct values = the largest per-partition count (a lower bound for the whole
        table), null_frac weighted by rows, most common values from the largest partition."""
        own = self.colstats.get((t["schema"], t["name"], colname))
        if own or t["relkind"] != "p":
            return own
        key = (t["schema"], t["name"], colname, "parts")
        if key not in self.colstats:
            parts = [(c, self.colstat(c, colname)) for c in self.partitions(t)]
            parts = [(c, st, self.rows(c) or 0) for c, st in parts if st]
            if not parts:
                self.colstats[key] = None
            else:
                def absolute(st, rows):
                    nd = st.get("n_distinct")
                    return None if nd is None else (nd if nd > 0 else -nd * rows if rows else None)
                nds = [x for x in (absolute(st, r) for _, st, r in parts) if x is not None]
                total = sum(r for _, _, r in parts)
                big = max(parts, key=lambda p: p[2])[1]
                self.colstats[key] = dict(
                    big, n_distinct=max(nds) if nds else None, inherited=True, from_partitions=len(parts),
                    null_frac=(sum((st.get("null_frac") or 0) * r for _, st, r in parts) / total
                               if total else big.get("null_frac")))
        return self.colstats[key]

    def rows(self, t):
        r = t.get("reltuples")
        if (r is None or r <= 0) and t["relkind"] == "p":
            r = sum(self.rows(c) or 0 for c in self.partitions(t))
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
            if time_like_name(col["name"]):
                return "name suggests it is set to the current time", "medium"
        return None, None

    def split_values(self, t, keycol):
        """Known values for SPLIT AT VALUES (all but the smallest), or None."""
        col = self.table_col(t, keycol)
        if col is None:
            return None
        if col["base_type"] == "boolean":
            return ["true"]                    # false < true: one split, one tablet per value
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

    def table_named(self, name):
        """A table referenced in a statement as name or schema.name (lower-cased), if unambiguous."""
        parts = name.split(".")
        hits = [t for t in self.by_name.get(parts[-1], [])
                if len(parts) < 2 or t["schema"].lower() == parts[-2]]
        return hits[0] if len(hits) == 1 else None

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
            reference, actions=None, confidence="high", confirm=None, data=None):
        self.items.append({
            "rule": rule, "severity": severity, "evidence": evidence, "confidence": confidence,
            "object": obj, "title": title, "detail": detail, "recommendation": recommendation,
            "reference": reference, "actions": actions or [], "needs_confirmation": confirm or [],
            "data": dict(data or {}),
            # set by the reviewer, then re-render: proposed | accepted | rejected
            "status": "proposed", "review_note": "",
        })

        d = self.items[-1]["data"]
        if "queryids" not in d:
            refs = [r for k in ("statements", "range_statements") for r in d.get(k) or [] if isinstance(r, dict)]
            if refs:
                d["queryids"] = [r["queryid"] for r in refs]

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
             "query": s["query"][:1000]} for s in stmts[:n]]


# ---- actions: the DDL behind each finding, rendered into changes.sql / rollback.sql by render.py
#
# phase    prepare (ANALYZE) | create (new indexes, online) | workload (indexes proposed from
#          statement text, online) | drop (drops, swap replacements in) | rewrite (table rewrites:
#          PK / column type) | manual (written commented out: needs a choice or a placeholder filled)
# kind     create | drop | replace | alter | analyze | note
# target   object the action creates/drops; `replaces` ties both halves of an index replacement
#          to the original index so render.py can skip them when that index is dropped anyway

def act(phase, sql, kind, target=None, rollback=None, replaces=None, note=None):
    return {"phase": phase, "kind": kind, "sql": sql.rstrip(), "rollback": rollback,
            "target": target, "replaces": replaces, "note": note}


def tname(t):
    return f"{qi(t['schema'])}.{qi(t['name'])}"


def iname(i):
    return f"{qi(i['table']['schema'])}.{qi(i['name'])}"


def recreate_sql(i):
    """Original index DDL, made idempotent. pg_get_indexdef omits SPLIT clauses, and prints
    partitioned parent indexes as ON ONLY, which would not cascade to the partitions."""
    sql = re.sub(r"^CREATE (UNIQUE )?INDEX ", lambda mm: f"CREATE {mm.group(1) or ''}INDEX IF NOT EXISTS ",
                 i["definition"])
    return re.sub(r" ON ONLY ", " ON ", sql, count=1) + ";"


def index_tail(i, split=""):
    """INCLUDE, SPLIT AT and WHERE clauses of a rebuilt index, in the order YSQL requires."""
    inc = f" INCLUDE ({', '.join(c['expr'] for c in i['include'])})" if i["include"] else ""
    pred = f" WHERE {i['predicate']}" if i["predicate"] else ""
    return inc + split + pred


def split_clause(values):
    return f" SPLIT AT VALUES ({', '.join('(' + v + ')' for v in values)})" if values else ""


def bucket_split(n):
    return split_clause([str(b) for b in range(1, n)])


def split_points(m, t, cols, desc=()):
    """Pre-split points for a range key over columns `cols` -> (points or None, split column, prefix).
    Leading columns that hold one value (by stats) form a fixed prefix, since splitting on them gives
    nothing; the first column after them supplies the points from its known values (enum, boolean,
    MCVs), at most one per node beyond the first, chosen so the slices hold about equal shares of
    the rows. Points are composite when there is a prefix: ((1, 'a'), (1, 'b')), and follow the
    column's order (descending for a DESC column)."""
    prefix = []
    for c in cols:
        col = t["cols_by_name"].get(c)
        if col is None:
            return None, None, prefix
        s = m.colstat(t, c) or {}
        mcv = parse_pg_array(s["most_common_vals"]) if s.get("most_common_vals") else None
        nd, src = m.distinct(t, {"attnum": col["attnum"]})
        if src == "stats" and nd is not None and nd <= 1 and mcv and len(mcv) == 1:
            prefix.append((c, mcv[0]))
            continue
        vals = m.split_values(t, {"attnum": col["attnum"]})
        if not vals:
            return None, c, prefix
        n = m.buckets
        if len(vals) > n - 1:
            freqs = parse_pg_array(s.get("most_common_freqs") or "") or []
            share = {str(v): float(f) for v, f in zip(mcv or [], freqs)}
            tot = sum(share.values()) or 1
            picked = []
            if any(str(v) in share for v in vals):
                # a point starts a new slice once the rows before it reach the next 1/n
                acc = (tot - sum(share.get(str(v), 0) for v in vals)) / tot    # values below the first point
                for v in vals:
                    if acc >= (len(picked) + 1) / n and len(picked) < n - 1:
                        picked.append(v)
                    acc += share.get(str(v), 0) / tot
            # no frequencies (enum labels): evenly spaced values
            vals = picked or [vals[round(k * len(vals) / n)] for k in range(1, n)]
        if c in desc:
            vals = vals[::-1]
        return [", ".join([ql(v) for _, v in prefix] + [ql(v)]) for v in vals], c, prefix
    return None, None, prefix


def split_query(m, t, col, prefix, desc=False, pred=None):
    """SQL that computes split points for `col` from the data: the boundary of each of N equal
    slices (N = buckets, one tablet per node), all but the last, in the column's index order.
    pred: a partial index's predicate, so only the rows it holds count."""
    n = m.buckets
    where = " AND ".join([f"{qi(c)} = {ql(v)}" for c, v in prefix] + ([f"({pred})"] if pred else []))
    agg, order = ("min", " DESC") if desc else ("max", "")
    return (f"SELECT {agg}({qi(col)}) AS split_at FROM (SELECT {qi(col)}, ntile({n}) OVER (ORDER BY {qi(col)}{order}) AS b "
            f"FROM {tname(t)}{' WHERE ' + where if where else ''}) s GROUP BY b ORDER BY b LIMIT {n - 1};")


def split_template(m, prefix):
    """SPLIT AT VALUES with placeholders for the points split_query returns."""
    pre = "".join(f"{ql(v)}, " for _, v in prefix)
    return split_clause([f"{pre}<split_at {k}>" for k in range(1, m.buckets)])


def index_keys_sql(i):
    """An index's key as DDL text, with a multi-column HASH part grouped: ((a, b) HASH, c ASC)."""
    hk = [k["expr"] if k["attnum"] else f"({k['expr']})" for k in i["keys"][:i["hash_count"]]]
    head = [f"({', '.join(hk)}) HASH" if len(hk) > 1 else f"{hk[0]} HASH"] if hk else []
    return ", ".join(head + [key_sql(k) for k in i["keys"][i["hash_count"]:]])


def key_sql(k, opt=None):
    expr = k["expr"] if k["attnum"] else f"({k['expr']})"
    return f"{expr} {opt or k['opt']}"


def swap_sql(i, tmp):
    """Drop the old index and rename the replacement, only while the replacement still exists,
    so re-running the phase never drops the (already renamed) new index."""
    t = i["table"]
    new = f"{qi(t['schema'])}.{qi(tmp)}"
    return (f"SELECT to_regclass({ql(new)}) IS NOT NULL AS swap_ready \\gset\n"
            f"\\if :swap_ready\n"
            f"DROP INDEX IF EXISTS {iname(i)};\n"
            f"ALTER INDEX {new} RENAME TO {qi(i['name'])};\n"
            f"\\else\n"
            f"\\echo 'skip: {tmp} not found (already swapped, or the create phase has not run)'\n"
            f"\\endif")


def replace_index(i, keys, split="", note=None):
    """Build a replacement next to the old index, then drop the old one and take over its name
    (hints and monitoring keep working)."""
    t = i["table"]
    tmp = (i["name"][:59] + "_new")
    create = f"CREATE INDEX IF NOT EXISTS {qi(tmp)} ON {tname(t)} ({keys}){index_tail(i, split)};"
    swap = swap_sql(i, tmp)
    return [act("create", create, "create", target=f"{t['schema']}.{tmp}", replaces=i["fq"],
                rollback=f"DROP INDEX IF EXISTS {qi(t['schema'])}.{qi(tmp)};", note=note),
            act("drop", swap, "replace", target=i["fq"], replaces=i["fq"],
                rollback=f"DROP INDEX IF EXISTS {iname(i)};\n{recreate_sql(i)}")]


def bucket_keys(m, i, k, desc=False):
    """A1 pattern: (yb_hash_code(col) % N) ASC, col, <rest of the key>."""
    expr = k["expr"] if k["attnum"] else f"({k['expr']})"
    rest = [key_sql(c, "ASC" if c["opt"] == "HASH" else None) for c in i["keys"] if c is not k]
    col = f"{expr} {'DESC' if desc else ('ASC' if k['opt'] == 'HASH' else k['opt'])}"
    return ", ".join([f"(yb_hash_code({expr}) % {m.buckets}) ASC", col] + rest)


def inner_bucket(m, t, i, mono_key):
    """Bucket expression for the 'bucket inside each value' pattern. Prefer the PK column: rows
    inserted in one transaction share now(), so hashing the timestamp would put a whole batch in
    one bucket. Fall back to the monotonic column. -> (sql, column label)"""
    pk = t["pk"]
    used = {c["attnum"] for c in i["keys"]}
    if pk and pk["keys"] and pk["keys"][0]["attnum"] and pk["keys"][0]["attnum"] not in used:
        col = pk["keys"][0]["expr"]
    else:
        col = mono_key["expr"] if mono_key["attnum"] else f"({mono_key['expr']})"
    return f"(yb_hash_code({col}) % {m.buckets}) ASC", col


def pk_alter(t, i, keys):
    con = i["backs_constraint"] or f"{t['name']}_pkey"
    return f"ALTER TABLE {tname(t)} DROP CONSTRAINT {qi(con)}, ADD CONSTRAINT {qi(con)} PRIMARY KEY ({keys});"


def manual(sql, note=None, target=None, kind="note"):
    return act("manual", sql, kind, target=target, note=note)


# ---- statement predicates: which columns each statement binds, per table (heuristic text parse)
#
# Only top-level AND-ed conditions of WHERE / JOIN ... ON count: anything under OR or NOT is a
# filter, not an access path. INSERT statements are skipped (no lookup to serve).

SQL_TOKEN_RE = re.compile(r"""(?P<ws>\s+)|(?P<str>'(?:[^']|'')*')|(?P<qid>"(?:[^"]|"")+")|(?P<param>\$\d+)|
    (?P<num>\d+(?:\.\d+)?)|(?P<op><=|>=|<>|!=|::|[=<>(),.;])|(?P<word>[A-Za-z_][\w$]*)|(?P<other>.)""", re.S | re.X)
SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "join", "inner", "left", "right", "full", "outer", "cross",
    "natural", "lateral", "on", "using", "group", "order", "by", "having", "limit", "offset", "fetch", "for",
    "union", "intersect", "except", "window", "returning", "set", "values", "as", "in", "is", "null", "between",
    "like", "ilike", "similar", "exists", "case", "when", "then", "else", "end", "asc", "desc", "nulls", "first",
    "last", "distinct", "all", "any", "some", "into", "update", "delete", "insert", "with", "true", "false",
    "current_date", "current_time", "current_timestamp", "localtime", "localtimestamp", "only", "do", "conflict",
}
CLAUSE_END = {"group", "order", "limit", "offset", "having", "returning", "union", "intersect", "except",
              "window", "fetch", "for", "join", "inner", "left", "right", "full", "cross", "natural", "where",
              "on", "using", "set", "values", "select"}
CMP_OPS = {"=", "<", ">", "<=", ">=", "<>", "!=", "between", "in", "is", "like", "ilike", "similar"}


def sql_tokens(q):
    """-> [(kind, value)]: kinds id (dotted, lower-cased), fn (identifier followed by '('), kw, param,
    lit, op. Comments and string contents are dropped."""
    q = re.sub(r"/\*.*?\*/|--[^\n]*", " ", q, flags=re.S)
    raw = []
    for mm in SQL_TOKEN_RE.finditer(q):
        kind = mm.lastgroup
        v = mm.group()
        if kind == "ws":
            continue
        if kind == "qid":
            raw.append(("name", v[1:-1].replace('""', '"').lower()))
        elif kind == "word":
            raw.append(("name", v.lower()))
        elif kind in ("str", "num"):
            raw.append(("lit", v))
        else:
            raw.append((kind if kind != "other" else "op", v))
    out, n = [], 0
    while n < len(raw):
        k, v = raw[n]
        if k == "name":
            parts = [v]
            while n + 2 < len(raw) and raw[n + 1] == ("op", ".") and raw[n + 2][0] == "name":
                parts.append(raw[n + 2][1])
                n += 2
            name = ".".join(parts)
            call = n + 1 < len(raw) and raw[n + 1] == ("op", "(")
            if len(parts) == 1 and v in SQL_KEYWORDS and not (call and v in ("left", "right")):
                out.append(("kw", v))
            elif call:
                out.append(("fn", name))
            else:
                out.append(("id", name))
        else:
            out.append((k, v))
        n += 1
    return out


def _depths(toks):
    """Paren depth before each token, and whether that paren belongs to a function call."""
    d, stack, depth, infn = 0, [], [], []
    for n, (k, v) in enumerate(toks):
        if v == ")" and k == "op" and stack:
            stack.pop()
            d -= 1
        depth.append(d)
        infn.append(bool(stack) and stack[-1])
        if v == "(" and k == "op":
            stack.append(n > 0 and toks[n - 1][0] == "fn")
            d += 1
    return depth, infn


def _clause(toks, depth, start):
    """Tokens of the clause starting at toks[start], up to the next clause keyword at the same depth."""
    if start >= len(toks):
        return []
    d, out = depth[start], []
    for n in range(start, len(toks)):
        k, v = toks[n]
        if depth[n] < d or (depth[n] == d and (k == "kw" and v in CLAUSE_END or v == ";")):
            break
        out.append(toks[n])
    return out


def _split(toks, word):
    """Split at top-level `word` (and/or). BETWEEN's own AND does not split."""
    parts, cur, d, between = [], [], 0, False
    for k, v in toks:
        if v == "(":
            d += 1
        elif v == ")":
            d -= 1
        if d == 0 and k == "kw" and v == "between":
            between = True
        elif d == 0 and k == "kw" and v == word:
            if word == "and" and between:
                between = False
            else:
                parts.append(cur)
                cur = []
                continue
        cur.append((k, v))
    parts.append(cur)
    return [p for p in parts if p]


def _conjuncts(toks):
    """Top-level AND-ed conditions; a condition under OR is dropped."""
    if len(_split(toks, "or")) > 1:
        return []
    out = []
    for c in _split(toks, "and"):
        if c[0] == ("op", "(") and c[-1] == ("op", ")") and _balanced(c[1:-1]):
            out += _conjuncts(c[1:-1])
        else:
            out.append(c)
    return out


def _balanced(toks):
    d = 0
    for _, v in toks:
        d += (v == "(") - (v == ")")
        if d < 0:
            return False
    return d == 0


def _side_col(side):
    """A comparison side -> (column id, wrapped) when it is one column, possibly cast or inside a
    function; (None, False) when it has no column (a parameter, literal, now(), ...)."""
    side = [tk for j, tk in enumerate(side) if not (j and side[j - 1] == ("op", "::"))]   # drop cast types
    ids = [v for k, v in side if k == "id"]
    if not ids:
        return None, False
    if len(set(ids)) > 1 or any(k == "kw" and v == "select" for k, v in side):
        return "?", False
    plain = side == [("id", ids[0])]
    return ids[0], not plain


def _blocks(toks):
    """Query block of each token: a parenthesised SELECT / WITH starts a block nested in the
    enclosing one. -> (block id per token, parent per block id); block 0 is the statement."""
    block, parent, stack, cur = [], {0: None}, [], 0
    for n, (k, v) in enumerate(toks):
        if v == "(" and k == "op":
            nxt = toks[n + 1] if n + 1 < len(toks) else None
            block.append(cur)
            if nxt in (("kw", "select"), ("kw", "with")):
                parent[len(parent)] = cur
                stack.append(cur)
                cur = len(parent) - 1
            else:
                stack.append(None)
            continue
        if v == ")" and k == "op" and stack:
            prev = stack.pop()
            if prev is not None:
                cur = prev
        block.append(cur)
    return block, parent


def statement_access(m, s):
    """Columns each query block binds per table -> [(table, {"eq": {col: True}, "range": ..,
    "wrapped": .., "order": [col], "desc": {col}, "limit": bool})]. A subquery is its own block, so
    `WHERE id IN (SELECT id FROM t WHERE ...)` doesn't merge the inner conditions into the outer t."""
    toks = sql_tokens(s["query"])
    if not toks or toks[0] == ("kw", "insert"):
        return []
    depth, infn = _depths(toks)
    block, parent = _blocks(toks)
    refs = defaultdict(dict)                    # block -> alias or name -> table
    for n, (k, v) in enumerate(toks):
        if k != "kw" or v not in ("from", "join", "update") or infn[n]:
            continue
        j = n + 1
        while j < len(toks) and toks[j] == ("kw", "only"):
            j += 1
        while j < len(toks) and toks[j][0] == "id":
            name = toks[j][1]
            t = m.table_named(name)
            j += 1
            alias = None
            if j < len(toks) and toks[j] == ("kw", "as"):
                j += 1
            if j < len(toks) and toks[j][0] == "id":
                alias = toks[j][1]
                j += 1
            if t:
                refs[block[n]][alias or name.split(".")[-1]] = t
                refs[block[n]].setdefault(name.split(".")[-1], t)
            if v == "from" and j < len(toks) and toks[j] == ("op", ","):
                j += 1
                continue
            break
    if not refs:
        return []

    def resolve(ident, b):
        """Column in this block's tables first, then the enclosing blocks' (correlated subquery)."""
        parts = ident.split(".")
        col = parts[-1]
        while b is not None:
            r = refs.get(b, {})
            if len(parts) > 1:
                t = r.get(parts[-2])
                if t:
                    return ((b, t), col) if col in t["lc_cols"] else (None, None)
            else:
                hits = list({t["oid"]: t for t in r.values() if col in t["lc_cols"]}.values())
                if len(hits) == 1:
                    return (b, hits[0]), col
                if hits:
                    return None, None
            b = parent[b]
        return None, None

    acc = {}

    def mark(ident, what, b):
        bt, col = resolve(ident, b)
        if bt:
            t = bt[1]
            a = acc.setdefault((bt[0], t["oid"]), (t, {"eq": {}, "range": {}, "wrapped": {}, "in": {}, "order": [],
                                                       "desc": set(), "limit": False, "cols": {}}))[1]
            name = t["lc_cols"][col]["name"]
            if what in ("order", "desc"):
                a["order"].append(name)
                if what == "desc":
                    a["desc"].add(name)
            else:
                a[what][name] = True            # dicts: keep the order columns appear in

    for n, (k, v) in enumerate(toks):
        if k != "kw" or v not in ("where", "on") or infn[n]:
            continue
        b = block[n]
        for c in _conjuncts(_clause(toks, depth, n + 1)):
            if c[0] in (("kw", "not"), ("kw", "exists")):
                continue
            op = next((j for j, (ck, cv) in enumerate(c) if cv in CMP_OPS and ck in ("op", "kw")
                       and _balanced(c[:j])), None)
            if op is None:
                continue
            o = c[op][1]
            left, right = c[:op], c[op + 1:]
            if left and left[-1] == ("kw", "not") or right and right[0] == ("kw", "not") and o != "is":
                continue
            lc, lw = _side_col(left)
            if o == "between":
                if lc and lc != "?":
                    mark(lc, "wrapped" if lw else "range", b)
                elif lc is None:
                    for side in _split(right, "and"):
                        rc, rw = _side_col(side)
                        if rc and rc != "?":
                            mark(rc, "wrapped" if rw else "range", b)
                continue
            rc, rw = _side_col(right) if o not in ("in", "is") else (None, False)
            if o == "is":
                if right == [("kw", "null")] and lc and lc != "?":
                    mark(lc, "wrapped" if lw else "eq", b)
                continue
            kind = {"=": "eq", "in": "eq"}.get(o, "range" if o in ("<", ">", "<=", ">=") else None)
            if kind is None:
                continue
            if lc and lc != "?" and (rc is None or kind == "eq" and rc != "?"):
                mark(lc, "wrapped" if lw else kind, b)
                if o == "in" and not lw:
                    mark(lc, "in", b)
            if rc and rc != "?" and (lc is None or kind == "eq" and lc != "?"):
                mark(rc, "wrapped" if rw else kind, b)
    limited = {block[n] for n, (k, v) in enumerate(toks) if k == "kw" and v in ("limit", "fetch")}
    for n, (k, v) in enumerate(toks):
        if k == "kw" and v == "order" and n + 1 < len(toks) and toks[n + 1] == ("kw", "by") and not infn[n]:
            for item in _split([("kw", "and") if t == ("op", ",") else t
                                for t in _clause(toks, depth, n + 2)], "and"):
                if item[0][0] == "id":
                    mark(item[0][1], "desc" if ("kw", "desc") in item[1:2] else "order", block[n])
    # every column of the table the block references: an Index Only Scan needs them all in the index
    star = {block[n] for n, tk in enumerate(toks) if tk == ("op", "*") and n and
            (toks[n - 1] in (("kw", "select"), ("op", ","), ("op", ".")))}
    first_tok = {}
    for n, b in enumerate(block):
        first_tok.setdefault(b, toks[n + 1] if b and toks[n] == ("op", "(") and n + 1 < len(toks) else toks[n])
    for n, (k, v) in enumerate(toks):
        if k == "id":
            bt, col = resolve(v, block[n])
            if bt and (bt[0], bt[1]["oid"]) in acc:
                acc[(bt[0], bt[1]["oid"])][1]["cols"][bt[1]["lc_cols"][col]["name"]] = True
    for (b, _), (t, a) in acc.items():
        a["limit"] = b in limited
        a["star"] = b in star
        a["reads"] = first_tok.get(b) in (("kw", "select"), ("kw", "with"))
        a["block"] = b
    return list(acc.values())


def stmt_cost(s, total_ms):
    """Workload weight of a statement -> (tier high|medium|low, share of all statement time)."""
    mean = s["total_time_ms"] / max(s["calls"], 1)
    share = s["total_time_ms"] / total_ms if total_ms else 0.0
    if mean >= SLOW_MEAN_MS * 5 and s["calls"] >= 100 or share >= 0.20 and mean >= SLOW_MEAN_MS / 2:
        return "high", share
    if mean >= SLOW_MEAN_MS or share >= HEAVY_SHARE:
        return "medium", share
    return "low", share


# ---- rules -----------------------------------------------------------------

def rule_no_pk(m, F):
    for t in m.tables.values():
        if t["relkind"] != "r" or t["pk"] or t.get("parent"):
            continue
        uniq = [i for i in t["indexes"] if i["is_unique"] and not i["predicate"] and i["is_valid"]
                and all(c["attnum"] and t["cols_by_num"][c["attnum"]]["not_null"] for c in i["keys"])]
        rec = "Define an explicit PRIMARY KEY. Without one, rows are keyed on a hidden ybrowid " \
              "(HASH) and every secondary-index lookup needs an extra hop."
        pk_name = qi(f"{t['name']}_pkey")
        if uniq:
            u = uniq[0]
            rec += f" Candidate: UNIQUE NOT NULL index {u['name']} ({key_text(u)})."
            keys = ", ".join(key_sql(c) for c in u["keys"])
            actions = [
                act("rewrite", f"ALTER TABLE {tname(t)} ADD CONSTRAINT {pk_name} PRIMARY KEY ({keys});", "alter",
                    target=t["fq"], rollback=f"ALTER TABLE {tname(t)} DROP CONSTRAINT {pk_name};"),
                manual(f"DROP INDEX IF EXISTS {iname(u)};",
                       note=f"{u['name']} duplicates the new primary key once it exists"),
            ]
        else:
            actions = [manual(f"ALTER TABLE {tname(t)} ADD CONSTRAINT {pk_name} PRIMARY KEY (<column> HASH);",
                              note="choose the key column(s); they must be NOT NULL and unique")]
        rec += " ALTER TABLE ... ADD PRIMARY KEY rewrites the table: plan a maintenance window or migrate to a new table."
        F.add("no-primary-key", "high", "schema", t["fq"], "Table has no primary key",
              f"{t['fq']} is stored on the hidden ybrowid.", rec, REF["pk"], actions=actions)


def pk_reshape(t, i, k):
    """Optional PK change for a HASH time column: other PK columns become the HASH part.
    Expressions (bucket prefixes) are not allowed in a PRIMARY KEY."""
    others = [c for c in i["keys"] if c is not k]
    if not others:
        return manual(f"-- {i['name']} is keyed only on {k['expr']}: a range PK on it would be a write "
                      "hotspot and a PK can't hold a bucket expression; keep it and use the secondary index above")
    hk = others[0]["expr"] + " HASH" if len(others) == 1 else f"({', '.join(c['expr'] for c in others)}) HASH"
    return manual(pk_alter(t, i, f"{hk}, {key_sql(k, 'DESC')}"),
                  note="optional: reshape the PK itself (full table rewrite); same columns, so same uniqueness")


def rule_hash_on_range_type(m, F):
    for i in m.indexes:
        if i["am"] != "lsm" or i["is_partition"] or not i["is_valid"]:
            continue
        t = i["table"]
        for pos, k in enumerate(i["keys"], 1):
            if k["opt"] != "HASH" or k["base_type"] not in TIME_TYPES:
                continue
            col = m.table_col(t, k)
            # an existing index already scans k as a range column after the other HASH columns
            # (e.g. a range twin of this PK): the range access exists; pk-range-twin handles the double write
            others_c = {c["attnum"] for c in i["keys"][:i["hash_count"]] if c is not k}
            if col and any(j is not i and j["am"] == "lsm" and j["is_valid"] and not j["predicate"]
                           and not j["is_partition"] and len(j["keys"]) > len(others_c)
                           and {c["attnum"] for c in j["keys"][:len(others_c)]} == others_c
                           and j["keys"][len(others_c)]["attnum"] == k["attnum"]
                           and j["hash_count"] <= len(others_c)
                           for j in t["indexes"]):
                continue
            used = range_usage(m, t, col["name"]) if col else []
            sev, ev = ("high", "workload") if used else ("medium", "schema")
            what = "primary key" if i["is_pk"] else "index"
            mono, _ = m.monotonic(t, k)
            others = [c for c in i["keys"][:i["hash_count"]] if c is not k]
            rec = (f"A HASH column only serves equality: range filters / ORDER BY on {k['expr']} cannot seek. "
                   f"Make it a range column ({k['expr']} ASC/DESC)")
            if others:
                rec += ", keeping the high-cardinality equality columns as HASH and moving the time column to the range part"
            else:
                rec += ". A leading range time column is a write hotspot: add a bucket prefix (pattern A1)"
            rec += ". Keep HASH only if every query is an exact-match lookup."
            if others:
                hk = others[0]["expr"] + " HASH" if len(others) == 1 else \
                    f"({', '.join(c['expr'] for c in others)}) HASH"
                rest = [key_sql(c) for c in i["keys"][i["hash_count"]:]]
                keys = ", ".join([hk, key_sql(k, "DESC")] + rest)
                split = ""
            else:
                keys, split = bucket_keys(m, i, k, desc=True), bucket_split(m.buckets)
            if i["is_pk"]:
                # keep the PK (changing it rewrites the table); add a range-capable index for the scans
                new = f"{t['name']}_{(col['name'] if col else 'expr')}_range"[:63]
                actions = [act("create", f"CREATE INDEX IF NOT EXISTS {qi(new)} ON {tname(t)} "
                               f"({bucket_keys(m, i, k, desc=True) if not others else keys}){'' if others else split};",
                               "create", target=f"{t['schema']}.{new}",
                               rollback=f"DROP INDEX IF EXISTS {qi(t['schema'])}.{qi(new)};",
                               note="secondary index for range scans; the PK itself is unchanged"),
                           pk_reshape(t, i, k)]
            elif i["is_unique"]:
                actions = [manual(f"CREATE UNIQUE INDEX IF NOT EXISTS {qi(i['name'][:59] + '_new')} ON {tname(t)} "
                                  f"({keys}){index_tail(i, split)};",
                                  note="unique index: verify the new key enforces the same rule, then swap")]
            else:
                actions = replace_index(i, keys, split)
            F.add("hash-on-range-type", sev, ev, i["fq"],
                  f"HASH-sharded {k['base_type']} column in {what}",
                  f"{what.capitalize()} {i['name']} on {t['fq']}: key position {pos} `{k['expr']}` is HASH.",
                  rec, REF["hotspots"], actions=actions, confidence="high" if used else "medium",
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
                  actions=[act("drop", f"DROP INDEX IF EXISTS {iname(r)};", "drop", target=r["fq"],
                               rollback=recreate_sql(r), note=f"covered by {e['name']}")],
                  confirm=confirm, data={"redundant": r["definition"], "covered_by": e["definition"],
                                         "covered_by_index": e["fq"]})


def rule_pk_range_twin(m, F):
    """A secondary index on exactly the PK columns, range-sharded where the PK is HASH: every row
    is written twice for one access path the PK lacks (prefix lookups, ranges, sorts). Keep the
    range shape as the PK and drop the twin; or, when every statement binds the whole PK by
    equality, just drop the twin."""
    for t in m.tables.values():
        pk = t["pk"]
        if not pk or not pk["hash_count"] or t.get("parent") or not all(k["attnum"] for k in pk["keys"]):
            continue
        pkc = [k["attnum"] for k in pk["keys"]]
        for i in t["indexes"]:
            if i is pk or i["is_partition"] or i["am"] != "lsm" or not i["is_valid"] or i["predicate"] \
                    or i["hash_count"] or [k["attnum"] for k in i["keys"]] != pkc:
                continue
            cols = [t["cols_by_num"][a]["name"] for a in pkc]
            # statements on the table that touch the key columns: full-key lookups (the HASH PK serves),
            # prefix / range / sort on them (only the range twin serves), or neither (e.g. job_id alone)
            accs = [a for s in m.statements_for(t) for t2, a in statement_access(m, s) if t2 is t
                    and set(cols) & (set(a["eq"]) | set(a["range"]) | set(a["order"]))]
            full = [a for a in accs if set(cols) <= set(a["eq"])]
            needs_range = [a for a in accs if a not in full and cols[0] in a["eq"]]
            neither = [a for a in accs if a not in full and a not in needs_range]
            point_only = bool(accs) and len(full) == len(accs)
            # the twins themselves are not evidence for how this table should lead
            twins = [j for j in m.indexes if j is not pk and j["table"]["pk"] and not j["hash_count"]
                     and [k["attnum"] for k in j["keys"]] == [k["attnum"] for k in j["table"]["pk"]["keys"]]]
            lopt, lwhy = lead_opt(m, t, cols[0], exclude=twins)
            nkeys = [dict(k, opt=(lopt if n == 0 and lopt == "HASH" else ("DESC" if k["opt"] == "DESC" else "ASC")))
                     for n, k in enumerate(i["keys"])]
            new_pk = dict(pk, keys=nkeys, columns=nkeys, nkey=len(nkeys), hash_count=int(lopt == "HASH"))
            new_keys = ", ".join(key_sql(k) for k in nkeys)
            old_keys = index_keys_sql(pk)
            mono = [c for c, k in zip(cols, pk["keys"]) if m.monotonic(t, k)[0]]
            detail = (f"{i['name']} ({key_text(i)}) has the same columns as the primary key {pk['name']} "
                      f"({key_text(pk)}), range-sharded instead of HASH: every insert and key update writes "
                      f"both. The PK can only serve lookups that bind every column; {i['name']} also serves "
                      f"{cols[0]}-only lookups and ranges or sorts on {', '.join(cols[1:])}.")
            confirm, ev = [], "schema"
            if accs:
                ev = "workload"
                detail += (f" Statements on these columns: {len(full)} bind the full key, {len(needs_range)} "
                           f"need the range shape ({cols[0]} bound, the rest not)"
                           + (f", {len(neither)} bind neither shape's leading column ({cols[0]}) and are not "
                              "served by either" if neither else "") + ".")
            if point_only:
                rec = (f"Every statement here is a full-key lookup the HASH primary key serves: drop {i['name']} "
                       "(no table rewrite).")
                actions = [act("drop", f"DROP INDEX IF EXISTS {iname(i)};", "drop", target=i["fq"],
                               rollback=recreate_sql(i), note=f"same columns as the primary key {pk['name']}")]
                confirm.append(f"Is {i['name']} used by queries not in the statements sample "
                               f"({cols[0]}-only lookups, ranges or ORDER BY on {', '.join(cols[1:])})?")
            else:
                if lopt == "HASH":
                    rec = (f"Keep one copy that serves both: make the primary key ({new_keys}), so {cols[0]} stays "
                           f"HASH and {', '.join(cols[1:])} becomes a range column inside each {cols[0]}; then "
                           f"drop {i['name']}.")
                else:
                    rec = (f"Keep the range shape and only one copy: make it the primary key ({new_keys}), then "
                           f"drop {i['name']}.")
                rec += " Same columns, so the same uniqueness; the reshape rewrites the table."
                if lwhy:
                    rec += f" {cols[0]} {'HASH' if lopt == 'HASH' else 'as a range column'}: {lwhy}."
                if mono and lopt != "HASH":
                    rec += (f" {', '.join(mono)} increases on insert, so each {cols[0]}'s new rows go to the end "
                            f"of its range: {i['name']} already has this per-{cols[0]} tail today, the reshape "
                            "moves the table writes there too. For a heavy insert rate per tenant, see the "
                            "bucketed pattern in hotspots.md.")
                alter = pk_alter(t, pk, new_keys)
                undo = pk_alter(t, pk, old_keys)
                # other indexes the reshaped PK makes redundant (not already dropped by another finding)
                gone = {a.get("target") for f in F.items for a in f["actions"] if a["kind"] in ("drop", "replace")}
                extra = [j for j in t["indexes"] if j is not pk and j is not i and j["am"] == "lsm"
                         and not j["is_partition"] and not j["backs_constraint"] and not j["referenced_by_fk"]
                         and j["fq"] not in gone and covers(j, new_pk)]
                if extra:
                    rec += (f" The new primary key also covers {', '.join(j['name'] for j in extra)}: "
                            "dropped after the reshape.")
                # indexes dropped as redundant because this twin covered them: still covered afterwards?
                for rf in [f for f in F.items if f["rule"] == "redundant-index"
                           and f["data"].get("covered_by_index") == i["fq"]]:
                    r = next(j for j in t["indexes"] if j["fq"] == rf["object"])
                    if covers(r, new_pk):
                        rf["recommendation"] += (f" {i['name']} is later dropped by pk-range-twin; the reshaped "
                                                 f"primary key ({new_keys}) covers {r['name']} as well.")
                    else:
                        rf["actions"] = []
                        rf["severity"] = "info"
                        rf["recommendation"] = (f"Keep {r['name']}: {i['name']}, which covers it today, is dropped "
                                                f"by pk-range-twin, and the reshaped primary key ({new_keys}) "
                                                "doesn't cover it.")
                        rec += (f" Keep {r['name']}: the new primary key doesn't cover it, so its redundant-index "
                                "drop is withdrawn.")
                # another finding already changes this primary key: let the reviewer pick one
                other_pk = [f for f in F.items for a in f["actions"]
                            if "PRIMARY KEY" in a["sql"] and f" {tname(t)} " in f" {a['sql']} "]
                if other_pk and not (pk.get("referenced_by_fk") or t["relkind"] == "p"):
                    why = (f"{other_pk[0]['rule']} also changes this primary key: pick one of the two reshapes")
                    actions = [manual(alter + f"\nDROP INDEX IF EXISTS {iname(i)};", note=why)]
                    confirm.append(why[0].upper() + why[1:] + ".")
                elif pk.get("referenced_by_fk") or t["relkind"] == "p":
                    why = ("foreign keys reference the primary key: drop and recreate them around the change"
                           if pk.get("referenced_by_fk") else
                           "partitioned table: change the PK per partition (it must include the partition key)")
                    actions = [manual(alter + f"\nDROP INDEX IF EXISTS {iname(i)};", note=why)]
                    confirm.append(why[0].upper() + why[1:] + ".")
                else:
                    actions = [act("rewrite", alter, "alter", target=t["fq"], rollback=undo,
                                   note=f"primary key becomes ({new_keys}) (full table rewrite)")]
                    actions += [act("rewrite", f"DROP INDEX IF EXISTS {iname(j)};", "drop", target=j["fq"],
                                    rollback=recreate_sql(j), note="covered by the reshaped primary key")
                                for j in [i] + extra]
                if neither:
                    rec += (f" {len(neither)} statement(s) filter on {', '.join(c for c in cols[1:])} without "
                            f"{cols[0]}: neither shape serves them; add {cols[0]} to their WHERE clause "
                            "(see the statement findings for this table).")
                if not accs:
                    confirm.append(f"Do queries look up {t['fq']} by {cols[0]} alone, or range/sort on "
                                   f"{', '.join(cols[1:])}? If every lookup binds all of ({', '.join(cols)}), "
                                   f"drop {i['name']} instead and keep the HASH primary key.")
            F.add("pk-range-twin", "medium", ev, i["fq"], "Index repeats the primary key with range sharding",
                  detail, rec, REF["index"] + "#redundant-indexes", actions=actions, confirm=confirm,
                  confidence="high" if accs else "medium",
                  data={"indexes": [pk["definition"], i["definition"]]})


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
            keys = ", ".join([key_sql(k0, "HASH")] + [key_sql(c) for c in i["keys"][1:]])
            if t["relkind"] == "p":
                actions = [manual(pk_alter(t, i, keys), note="partitioned table: change the PK per partition "
                                  "or migrate; the PK must keep the partition key columns")]
            else:
                actions = [act("rewrite", pk_alter(t, i, keys), "alter", target=t["fq"],
                               rollback=pk_alter(t, i, ", ".join(key_sql(c) for c in i["keys"])),
                               note="HASH the PK; range scans / ORDER BY on the key lose their index order")]
        elif i["is_unique"]:
            actions = [manual(f"CREATE UNIQUE INDEX IF NOT EXISTS {qi(i['name'][:59] + '_new')} ON {tname(t)} "
                              f"({bucket_keys(m, i, k0)}){index_tail(i, bucket_split(m.buckets))};",
                              note="unique index: verify, then swap it in by hand")]
        else:
            actions = replace_index(i, bucket_keys(m, i, k0), bucket_split(m.buckets),
                                    note=f"A1 bucket prefix, N={m.buckets}")
        F.add("monotonic-range-key", sev, "schema", i["fq"],
              f"{what} leads with a monotonic range column",
              f"{i['name']} on {t['fq']} is range-sharded on `{k0['expr']} {k0['opt']}` ({reason}).",
              rec, REF["hotspots"], actions=actions, confidence=conf,
              confirm=([] if conf == "high" else [f"Is {t['fq']}.{k0['expr']} set to the current time on insert or update?"])
              + ([f"Do queries range-scan or ORDER BY {t['fq']}.{k0['expr']}?"] if i["is_pk"] else []),
              data={"definition": i["definition"]})


def rule_lowcard_hash(m, F):
    for i in m.indexes:
        if i["am"] != "lsm" or i["is_partition"] or not i["is_valid"] or not i["hash_count"]:
            continue
        t = i["table"]
        if i["predicate"] and _fixed_by_predicate(i["predicate"], i["keys"][0]["expr"]):
            continue                                   # reported by partial-keyed-on-predicate
        # a HASH key that is the whole key of a unique index (or the PK) has one hash value per row
        if (i["is_unique"] or i["is_pk"]) and i["hash_count"] == len(i["keys"]):
            continue
        est, srcs = 1.0, set()
        for k in i["keys"][:i["hash_count"]]:
            nd, src = m.distinct(t, k)
            if nd is None:
                est = None
                break
            est *= nd
            srcs.add(src)
        k0 = i["keys"][0]
        if est is None and i["hash_count"] == 1 and k0["base_type"] in SUSPECT_LOWCARD_TYPES:
            # no stats for the column: a HASH-led smallint is usually a status/priority/type code
            pk = t["pk"]
            keys = ", ".join(key_sql(c, "ASC" if c["opt"] == "HASH" else None) for c in i["keys"])
            F.add("low-cardinality-hash", "low", "schema", i["fq"],
                  f"HASH-led {k0['base_type']} key: possible low cardinality",
                  f"{i['name']} on {t['fq']} hashes on `{k0['expr']}` ({k0['base_type']}), with no statistics "
                  "to tell how many distinct values it holds. Code/status/priority columns typically have a "
                  "handful: each value's rows then sit on one tablet that can't split.",
                  "Check the distinct count (ANALYZE, then pg_stats.n_distinct). If it is small, range-shard "
                  f"it ({k0['expr']} ASC) with SPLIT AT VALUES on the known values"
                  + (" — the HASH primary key suffix spreads entries within each value." if pk and pk["hash_count"]
                     else " plus an A1 bucket, since the PK is not HASH."),
                  REF["lowcard"], confidence="low",
                  actions=[] if i["is_pk"] or i["is_unique"] else
                  [manual(replace_index(i, keys)[0]["sql"],
                          note=f"if {k0['expr']} turns out low-cardinality: range-shard it and add "
                               "SPLIT AT VALUES on its values, then swap it in")],
                  confirm=[f"How many distinct values does {t['fq']}.{k0['expr']} hold?"],
                  data={"definition": i["definition"]})
            continue
        if est is None or est > LOWCARD_MAX:
            continue
        rows = m.rows(t)
        # a key with about as many values as rows is unique in practice: few values then only
        # means a small table, not skew
        if rows is not None and (min(est, rows) >= UNIQUE_RATIO * rows or rows < SMALL_TABLE):
            continue
        ev = "stats" if "stats" in srcs else "schema"
        sev = "high" if est <= LOWCARD_SEVERE and (rows is None or rows >= PRESPLIT_ROWS) else "medium"
        hk = ", ".join(c["expr"] for c in i["keys"][:i["hash_count"]])
        actions = []
        if i["is_pk"]:
            rec = "Lead the PK with a high-cardinality HASH column; keep the low-cardinality column in the range part."
            actions = [manual(pk_alter(t, i, "<high-cardinality column> HASH, " +
                                       ", ".join(key_sql(c, "ASC" if c["opt"] == "HASH" else None) for c in i["keys"])),
                              note="pick the leading HASH column (full table rewrite)")]
        else:
            vals = m.split_values(t, i["keys"][0])
            pk = t["pk"]
            pk_hash = bool(pk and pk["hash_count"])
            range_cols = [key_sql(c, "ASC" if c["opt"] == "HASH" else None) for c in i["keys"]]
            k1 = i["keys"][1] if len(i["keys"]) > 1 else None
            mono_next = k1 is not None and m.monotonic(t, k1)[0]
            pk0 = pk["keys"][0] if pk and pk["keys"] else None
            split = split_clause([ql(v) for v in vals]) if vals and i["hash_count"] == 1 else ""
            rec = (f"All rows with one {hk} value share a hash code, so they sit on one tablet that can't split "
                   f"(at most {int(est)} tablets ever). Range-shard it ({hk} ASC)")
            if mono_next:
                # (lowcard ASC, ts ASC, pk): ts precedes the PK suffix, so every new row of a value
                # sorts after all existing ones -> one write tail per value, splits don't divide it
                bexpr, bcol = inner_bucket(m, t, i, k1)
                bucketed = list(range_cols)
                bucketed.insert(1, bexpr)
                range_keys = ", ".join(bucketed)
                rec += (f" with a bucket inside each value. `{k1['expr']}` follows it and only increases, and it comes before the primary-key "
                        "suffix in the index key: every new row for a value sorts after all existing ones, so "
                        "writes go to that value's last tablet even after splits (the PK suffix only orders "
                        f"rows with the same {k1['expr']}). The generated index adds an A1 bucket on "
                        f"{bcol} (N={m.buckets}) inside each value; once auto-split divides the index at bucket "
                        "boundaries, each bucket's writes go to their own tablet (no pre-split needed). Cost: "
                        f"`WHERE {hk} = … ORDER BY {k1['expr']} LIMIT n` merges N sorted streams (no sort from "
                        "v2025.2.1; older versions sort). Alternatives under manual decisions: a partial index "
                        f"keyed on {k1['expr']} for the value the queries poll (typical for queue tables), or "
                        "no bucket when the insert rate is low.")
                note = f"A1 bucket on {bcol} (N={m.buckets}): {k1['expr']} is monotonic and precedes the PK suffix"
            elif pk_hash:
                range_keys = ", ".join(range_cols)
                rec += (" and pre-split on its values: the HASH primary-key suffix spreads entries within each "
                        "value as the index splits.")
                note = "range-sharded; HASH PK suffix spreads each value"
            else:
                # range PK: the suffix is ordered too; bucket on the PK (high cardinality), not on the
                # low-cardinality column, whose hash has only as many values as the column itself
                pke = pk0["expr"] if pk0 and pk0["attnum"] else None
                range_keys = ", ".join(range_cols[:1] + ([f"(yb_hash_code({pke}) % {m.buckets}) ASC"] if pke else [])
                                       + range_cols[1:])
                rec += (": this table's PK is range-sharded, so the PK suffix doesn't spread entries; the "
                        f"generated index adds an A1 bucket on {pke} inside each value.")
                note = f"A1 bucket on the PK column {pke} (N={m.buckets})"
            rec += " If only some values are queried, a partial index is smaller still."
            if i["is_unique"]:
                actions = [manual(f"-- unique index {i['name']}: reshape by hand, keeping the same uniqueness rule")]
            else:
                # bucketed keys: no SPLIT AT VALUES; auto-split divides at (value, bucket) boundaries,
                # after which each bucket's writes go to their own tablet
                bucketed_key = mono_next or not pk_hash
                actions = replace_index(i, range_keys, "" if bucketed_key else split, note=note)
                if mono_next:
                    rest = ", ".join(key_sql(c) for c in i["keys"][1:])
                    inc = f" INCLUDE ({', '.join(c['expr'] for c in i['include'])})" if i["include"] else ""
                    pred = (f"({i['predicate']}) AND " if i["predicate"] else "") + f"{hk} = <polled value>"
                    actions += [
                        manual(f"CREATE INDEX IF NOT EXISTS {qi(i['name'][:59] + '_new')} ON {tname(t)} "
                               f"({rest}){inc} WHERE {pred};",
                               note=f"alternative for queue polling: partial index on the value the queries "
                                    f"poll; small, and rows leave it when {hk} changes"),
                        manual(replace_index(i, ", ".join(range_cols), split)[0]["sql"],
                               note="alternative for low insert rates: no bucket (one write tail per value)"),
                    ]
        F.add("low-cardinality-hash", sev, ev, i["fq"],
              "HASH sharding on a low-cardinality key",
              f"{i['name']} on {t['fq']} hashes on ({hk}): ~{int(est)} distinct values"
              + (f", {int(rows):,} rows" if rows else "") + ".",
              rec, REF["lowcard"], actions=actions,
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
        pk = t["pk"]
        pk_hash = bool(pk and pk["hash_count"])
        if nf >= NULL_HEAVY and not mentions:
            t_pred = f"({pred}) AND " if pred else ""
            keys = ", ".join(key_sql(c) for c in i["keys"])
            tmp = i["name"][:59] + "_new"
            create = (f"CREATE INDEX IF NOT EXISTS {qi(tmp)} ON {tname(t)} ({keys})"
                      + (f" INCLUDE ({', '.join(c['expr'] for c in i['include'])})" if i["include"] else "")
                      + f" WHERE {t_pred}{qi(col['name'])} IS NOT NULL;")
            actions = [act("create", create, "create", target=f"{t['schema']}.{tmp}", replaces=i["fq"],
                           rollback=f"DROP INDEX IF EXISTS {qi(t['schema'])}.{qi(tmp)};",
                           note="IS NULL lookups will no longer use this index"),
                       act("drop", swap_sql(i, tmp), "replace", target=i["fq"], replaces=i["fq"],
                           rollback=f"DROP INDEX IF EXISTS {iname(i)};\n{recreate_sql(i)}")]
            F.add("null-heavy-hash", "medium", "stats", i["fq"], "NULL-heavy HASH index column",
                  f"{nf:.0%} of {t['fq']}.{col['name']} is NULL; all NULL entries of {i['name']} hash to one tablet.",
                  f"If NULLs are never looked up: add WHERE {col['name']} IS NOT NULL. If they are, range-shard "
                  f"({col['name']} ASC); a HASH PK suffix spreads the NULL range after splits.",
                  REF["partial"], actions=actions,
                  confirm=[f"Do any queries look up {col['name']} IS NULL via this index?"],
                  data={"null_frac": nf})
        elif top >= SKEW_FREQ and not mentions:
            keys = ", ".join(key_sql(c, "ASC" if c["opt"] == "HASH" else None) for c in i["keys"])
            actions = (replace_index(i, keys, note="range-sharded; HASH PK suffix spreads the dominant value")
                       if pk_hash else
                       [manual(f"-- {i['name']}: exclude the dominant value with WHERE {col['name']} <> <value>, "
                               "or add an A1 bucket prefix (the PK is not HASH)")])
            F.add("skewed-hash", "medium", "stats", i["fq"], "Skewed HASH index column",
                  f"One value of {t['fq']}.{col['name']} holds {top:.0%} of rows; its entries all sit on one tablet of {i['name']}.",
                  "Exclude the dominant value with a partial index if it's rarely queried, or range-shard "
                  f"({col['name']} ASC) with a HASH PK suffix.", REF["partial"] + "#case-2-dominant-single-value-skewed-distribution",
                  actions=actions, confirm=["Is the dominant value queried through this index?"], data={"top_freq": top})


def rule_range_tails(m, F):
    """Range index led by a low-cardinality or skewed column, followed by a monotonic column:
    every new row of a value lands at the newest end of that value's range (one hot spot per value).
    Splits don't help: the monotonic column sorts before the PK suffix."""
    for i in m.indexes:
        if i["am"] != "lsm" or i["is_partition"] or not i["keys"] or i["is_pk"]:
            continue
        k0 = i["keys"][0]
        if k0["opt"] == "HASH":
            continue
        t = i["table"]
        nd, src = m.distinct(t, k0)
        col0 = m.table_col(t, k0)
        st0 = m.colstat(t, col0["name"]) if col0 else None
        top = ((st0 or {}).get("most_common_freqs") or [0])[0] or 0
        lowcard = nd is not None and nd <= LOWCARD_MAX
        skewed = not lowcard and top >= SKEW_FREQ
        if not (lowcard or skewed):
            continue
        cause, mono_key, where = None, None, "end"
        if len(i["keys"]) > 1:
            r, _ = m.monotonic(t, i["keys"][1])
            if r:
                mono_key = i["keys"][1]
                where = "start" if mono_key["opt"] == "DESC" else "end"
                cause = f"`{mono_key['expr']} {mono_key['opt']}` ({r}) follows it"
        if not cause and lowcard and not i["is_unique"] and len(i["keys"]) == 1:
            pk = t["pk"]
            if pk and not pk["hash_count"]:
                r, _ = m.monotonic(t, pk["keys"][0])
                if r:
                    cause = f"the range PK suffix `{pk['keys'][0]['expr']}` ({r}) is appended to every entry"
        if not cause:
            continue
        what = (f"`{k0['expr']}` has ~{int(nd)} values" if lowcard else
                f"one `{k0['expr']}` value holds {top:.0%} of rows")
        actions = []
        if mono_key is not None and not i["is_unique"]:
            bexpr, bcol = inner_bucket(m, t, i, mono_key)
            rest = [key_sql(c) for c in i["keys"] if c is not k0]
            bucketed = ", ".join([key_sql(k0), bexpr] + rest)
            actions = [manual(replace_index(i, bucketed)[0]["sql"],
                              note=f"option A: bucket on {bcol} inside each value (N={m.buckets}); "
                                   f"ORDER BY {mono_key['expr']} within a value merges N streams"),
                       manual(replace_index(i, key_sql(k0))[0]["sql"],
                              note="option B: drop the monotonic column (only if nothing filters/sorts on it)")]
            if skewed:
                actions.append(manual(replace_index(i, f"{k0['expr']} HASH, {key_sql(mono_key)}")[0]["sql"],
                                      note=f"option C: only if every query filters on {k0['expr']}: HASH spreads "
                                           "the other values, but the dominant value stays on one tablet"))
        F.add("range-hot-tails", "medium", src or ("stats" if skewed else "schema"), i["fq"],
              "One write hot spot per value" if lowcard else "Write hot spot on a dominant value",
              f"{i['name']} on {t['fq']} is range-sharded on ({key_text(i)}): {what} and {cause}, so new and "
              f"updated rows for each value land at the {where} of that value's range. Splits don't divide those "
              "writes: the monotonic column sorts before the primary-key suffix.",
              ("Fine if writes spread across values and values ≥ nodes; a hot spot if most writes share one value. "
               if lowcard else
               f"The dominant {k0['expr']} value's writes all go to one tablet. ")
              + "Bucket inside each value (A1 on the primary key, so a batch inserted in one transaction "
              "doesn't share one bucket), or drop the monotonic column if nothing filters/sorts on it.",
              REF["hotspots"] + "#a1-inside-a-low-cardinality-value", actions=actions,
              confidence="medium",
              confirm=[f"What share of new/updated rows get the same {k0['expr']} value?"]
              + ([f"Is {mono_key['expr']} set to the current time on insert or update?"]
                 if mono_key is not None and m.monotonic(t, mono_key)[1] != "high" else []),
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
        # split points are positional: stop at the first expression key (e.g. a bucket)
        cols = []
        for c in i["keys"]:
            if not c["attnum"]:
                break
            cols.append(c["expr"])
        desc = {c["expr"] for c in i["keys"] if c["opt"] == "DESC"}
        points, scol, prefix = split_points(m, t, cols, desc)
        fixed = (f" {', '.join(f'{c} holds one value ({v})' for c, v in prefix)}, so the split points fall "
                 f"inside it, on {scol}." if prefix and scol else "")
        if scol and m.monotonic(t, {"attnum": t["cols_by_name"][scol]["attnum"]})[0]:
            fixed += (f" {scol} grows with inserts: splitting spreads the existing rows and their reads, but new "
                      "rows still all land on the newest tablet (see range-hot-tails for the write side).")
        name = i["name"] if not i["is_pk"] else t["name"]
        rebuild = not i["is_pk"] and not i["is_unique"]
        keys = ", ".join(key_sql(c) for c in i["keys"])
        actions = []
        if points:
            if rebuild:
                actions.append(manual(replace_index(i, keys, split_clause(points))[0]["sql"],
                                      note="rebuild pre-split on the known values, then swap it in"))
        elif scol:
            actions.append(manual(split_query(m, t, scol, prefix, scol in desc, i["predicate"]),
                                  note=f"compute {m.buckets - 1} split points on {scol} (reads the whole "
                                       "table: run it off-peak)"))
            if rebuild:
                actions.append(manual(replace_index(i, keys, split_template(m, prefix))[0]["sql"],
                                      note="rebuild with the points from the query above, then swap it in"))
        split_live = "split the live tablet with `yb-admin split_tablet <tablet_id>`"
        if rebuild and (points or scol):
            how = f"rebuild it pre-split (below) and swap it in, or {split_live}"
        elif rebuild:
            how = f"{split_live} (no split points can be derived for its leading key)"
        else:
            how = f"{split_live} (rebuilding a primary key or unique index means a table rewrite)"
        F.add("range-single-tablet", sev, "stats", i["fq"], "Range-sharded relation on a single tablet",
              f"{i['name']} on {t['fq']} ({int(rows):,} rows) is range-sharded and has 1 tablet: all reads and "
              "writes go to one tablet leader until auto-splitting catches up, and a small index can stay below "
              f"the split size threshold for a long time.{fixed}",
              f"Confirm the tablet count first (the catalog value can be stale). If it is still 1, {how}. Create "
              "new range indexes on large tables with SPLIT AT VALUES.",
              REF["lowcard"] + "#why-range-sharding-works-here-the-hidden-pk-suffix", actions=actions,
              confirm=[f"Current tablet count: yb-admin -master_addresses <masters> list_tablets "
                       f"ysql.{m.meta.get('database')} {name}"])


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
            pk_cols = ", ".join(key_sql(c) for c in t["pk"]["keys"]) if t["pk"] else "<high-cardinality col> HASH"
            F.add("partial-keyed-on-predicate", "medium", "schema", i["fq"],
                  "Partial index keyed on its own predicate column",
                  f"{i['name']} on {t['fq']} is keyed on `{k0['expr']}`, which WHERE {i['predicate']} fixes to one value: "
                  "every entry has the same leading key (one hash → one tablet if HASH) and the planner drops the "
                  "implied equality from the index condition (yugabyte-db#18888).",
                  "Key the partial index on the columns the queries filter or sort on, e.g. "
                  f"(<high-cardinality col> HASH, <sort col> DESC) WHERE {i['predicate']}.",
                  REF["partial"] + "#case-2-dominant-single-value-skewed-distribution",
                  actions=[manual(replace_index(i, f"<filter col> HASH, <sort col> DESC")[0]["sql"],
                                  note=f"replace the key with what the queries filter/sort on (PK is {pk_cols})")],
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


INT_WIDTH = {"smallint": 2, "integer": 4, "bigint": 8}


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
            new = new_index_name(t, [c["name"] for c in cols])
            F.add("fk-without-index", "high" if rows and rows >= PRESPLIT_ROWS else "medium",
                  "stats" if rows else "schema", f"{t['fq']}.{fk['name']}", "Foreign key without a supporting index",
                  f"{t['fq']}({names}) references {ref['fq'] if ref else '?'} but no index leads with these columns: "
                  "each parent DELETE/key UPDATE scans the child table.",
                  "Create an index whose leading key columns are exactly the FK columns (a PK leading with them also counts).",
                  REF["fk"],
                  actions=[act("create", f"CREATE INDEX IF NOT EXISTS {qi(new)} ON {tname(t)} "
                               f"({', '.join(qi(c['name']) + (' HASH' if n == 0 else ' ASC') for n, c in enumerate(cols))});",
                               "create", target=f"{t['schema']}.{new}",
                               rollback=f"DROP INDEX IF EXISTS {qi(t['schema'])}.{qi(new)};")])
        bad = [c for c in cols if c["type"] != c["ref_type"] and not
               ({re.sub(r"\(.*\)", "", c["type"]), re.sub(r"\(.*\)", "", c["ref_type"])} <= {"text", "character varying"})]
        if bad:
            actions = []
            for c in bad:
                if INT_WIDTH.get(c["type"], 99) < INT_WIDTH.get(c["ref_type"], 0):
                    actions.append(act("rewrite", f"ALTER TABLE {tname(t)} ALTER COLUMN {qi(c['name'])} TYPE {c['ref_type']};",
                                       "alter", target=f"{t['fq']}.{c['name']}",
                                       rollback=f"ALTER TABLE {tname(t)} ALTER COLUMN {qi(c['name'])} TYPE {c['type']};",
                                       note="widen the child column to the parent's type (table rewrite)"))
                else:
                    actions.append(manual(f"-- align {t['fq']}.{c['name']} ({c['type']}) with "
                                          f"{ref['fq'] if ref else '?'}.{c['ref_name']} ({c['ref_type']}): pick one type "
                                          "for both sides (changing the parent rewrites it and its other children)"))
            F.add("fk-type-mismatch", "medium", "schema", f"{t['fq']}.{fk['name']}", "Foreign key type mismatch",
                  "; ".join(f"{c['name']} {c['type']} → {c['ref_name']} {c['ref_type']}" for c in bad) + ".",
                  "Use identical types on both sides: cross-type comparisons cost CPU per lookup, can change plans, "
                  "and a narrower child type overflows first (INT child of BIGINT parent).", REF["fk"],
                  actions=actions)


def rule_int_sequences(m, F):
    for s in m.sequences:
        if s["type"] in ("integer", "smallint") and s.get("owned_by"):
            tbl, col = s["owned_by"].rsplit(".", 1)
            seq = f"{qi(s['schema'])}.{qi(s['name'])}"
            F.add("int-sequence", "low", "schema", s["owned_by"], "Sequence-backed INT column",
                  f"{s['owned_by']} uses a {s['type']} sequence ({s['schema']}.{s['name']}).",
                  "Use BIGINT (GENERATED ... AS IDENTITY). Sequence values are cached per connection "
                  "(ysql_sequence_cache_minval, default 100), so values burn faster than rows are inserted.",
                  REF["hotspots"],
                  actions=[act("rewrite", f"ALTER TABLE {tbl} ALTER COLUMN {col} TYPE bigint;\n"
                                          f"ALTER SEQUENCE {seq} AS bigint;",
                               "alter", target=s["owned_by"],
                               rollback=f"ALTER SEQUENCE {seq} AS {s['type']};\n"
                                        f"ALTER TABLE {tbl} ALTER COLUMN {col} TYPE {s['type']};",
                               note="table rewrite; widen FK columns that reference it too")])


def rule_invalid(m, F):
    for i in m.indexes:
        if not i["is_valid"]:
            F.add("invalid-index", "high", "schema", i["fq"], "Invalid index",
                  f"{i['name']} on {i['table']['fq']} is not valid (failed or interrupted CONCURRENTLY build): "
                  "it costs writes and serves no reads.",
                  "Drop and recreate it.", REF["index"],
                  actions=[act("create", f"DROP INDEX IF EXISTS {iname(i)};\n{recreate_sql(i)}", "replace",
                               target=i["fq"], note="rebuild")])


def rule_gin(m, F):
    for i in m.indexes:
        if i["am"] == "ybgin" and i["nkey"] > 1:
            t = i["table"]
            per_col = "\n".join(f"CREATE INDEX IF NOT EXISTS {qi((i['name'] + '_' + str(n))[:63])} ON {tname(t)} "
                                f"USING ybgin ({c['expr']} {c['opclass'] or ''});".replace(" ;", ";")
                                for n, c in enumerate(i["keys"], 1))
            F.add("multi-column-gin", "medium", "schema", i["fq"], "Multi-column GIN index",
                  f"{i['name']} has {i['nkey']} key columns.", "Use one GIN index per column.", REF["index"],
                  actions=[manual(per_col + f"\nDROP INDEX IF EXISTS {iname(i)};")])


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
    # a partitioned parent gets inherited stats only from ANALYZE on the parent itself
    parents = [t for t in m.tables.values() if t["relkind"] == "p" and not t.get("parent")
               and not any(k[:2] == (t["schema"], t["name"]) and len(k) == 3 for k in m.colstats)]
    if parents:
        names = ", ".join(t["fq"] for t in parents[:15]) + (" …" if len(parents) > 15 else "")
        F.add("missing-statistics", "medium" if m.cbo_on else "low", "stats", f"{len(parents)} partitioned table(s)",
              "Partitioned tables without inherited statistics",
              f"{len(parents)} partitioned table(s) have no statistics of their own: {names}. ANALYZE on a "
              "partition doesn't fill them, and auto-analyze may skip partitioned parents (PostgreSQL's does), so "
              "the planner estimates queries on the parent without them. This review derived the parents' column statistics from "
              "their partitions.",
              "ANALYZE each partitioned parent (it samples across its partitions); repeat after large loads.",
              REF["review"],
              actions=[act("prepare", f"ANALYZE {tname(t)};", "analyze", target=t["fq"]) for t in parents],
              data={"tables": [t["fq"] for t in parents]})
    if stale:
        names = ", ".join(t["fq"] for t in stale[:15]) + (" …" if len(stale) > 15 else "")
        F.add("missing-statistics", "medium" if m.cbo_on else "low", "stats", m.meta.get("database", "db"),
              "Tables without optimizer statistics",
              f"{len(stale)} table(s) have never been analyzed: {names}. Stats-based checks were skipped for them.",
              "Run ANALYZE (with the customer's agreement) — the cost-based optimizer relies on it. "
              "Auto Analyze is the default from 2025.2; check yb_disable_auto_analyze / ysql_enable_auto_analyze.",
              REF["review"],
              actions=[act("prepare", f"ANALYZE {tname(t)};", "analyze", target=t["fq"]) for t in stale],
              data={"tables": [t["fq"] for t in stale]})


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
              actions=[manual(f"DROP INDEX IF EXISTS {iname(i)};", note="unused: confirm the observation window",
                              target=i["fq"], kind="drop") for i in idx],
              confirm=["How long is the observation window, and does it include periodic jobs?"],
              data={"indexes": [i["definition"] for i in idx]})


AGG_RE = re.compile(r"\b(?:count|sum|avg|min|max)\s*\(|\bgroup\s+by\b|\bdistinct\b", re.I)
# conditions the statement parser does not map to index columns: applied after the index read
RESIDUAL_RE = [(re.compile(r"\bnot\s+in\s*\(|!=|<>", re.I), "NOT IN / != (an exclusion can't seek: list the wanted "
                                                            "values with IN (...) or index a partial on the excluded ones)"),
               (re.compile(r"->>?|#>>?", re.I), "a JSON field (promote it to a column, or index the expression "
                                                "when the key is a constant)"),
               (re.compile(r"\bor\b", re.I), "OR (split into UNION ALL branches that each use an index)"),
               (re.compile(r"\b(?:i?like)\b", re.I), "LIKE (only a constant prefix with text_pattern_ops can seek)")]


def scan_diagnosis(m, s):
    """Why a scan-heavy statement reads many rows when no statement finding covers it ->
    (all tables small, text). Per table: small-table scan, or the best index and what it leaves to
    filtering (wrapped columns, exclusions, JSON, OR), and aggregates (few rows back by design)."""
    q = s["query"]
    per_call = s["docdb_rows_scanned"] / max(s["calls"], 1)
    parts, small_all = [], True
    for t, a in statement_access(m, s):
        rows = m.rows(t)
        small = rows is not None and rows < SMALL_TABLE
        small_all &= small
        usable = [i for i in t["indexes"] if i["am"] == "lsm" and i["is_valid"] and not i["is_partition"]
                  and _pred_cols(t, i) <= set(a["eq"]) | set(a["range"])]
        best, served = None, []
        for i in usable:
            out, rcol, _ = index_reach(t, i, a)
            cols = out + ([rcol] if rcol else [])
            if len(cols) > len(served):
                best, served = i, cols
        unserved = [c for c in list(a["eq"]) + list(a["range"]) if c not in served]
        if small:
            parts.append(f"{t['fq']} has ~{rows:,.0f} rows, so each call reads most of the table (~{per_call:,.0f} "
                         "rows). That is cheap per call; the total comes from the call count."
                         + (f" An index on ({', '.join(served + unserved)}) would make it a direct lookup: worth it "
                            "only if this latency matters." if unserved else ""))
            continue
        if best:
            txt = f"On {t['fq']}, {best['name']} serves {', '.join(served)}"
            if unserved:
                txt += f"; {', '.join(unserved)} are filtered after the index read"
            if unserved:
                txt += (f" (an index on ({', '.join(served + unserved)}) would read only the matching rows; "
                        "check that the extra columns narrow it enough to pay for the extra write)")
        else:
            txt = f"On {t['fq']}, no index serves these conditions"
        # an IN list ahead of the sort column: each IN value is in order, but the rows of all values
        # are read and sorted before LIMIT applies
        ins = [c for c in a["in"] if c in served]
        if a["limit"] and a["order"] and ins and a["order"][0] not in a["eq"]:
            txt += (f". The IN lists on {', '.join(ins)} come before the ORDER BY column {a['order'][0]}: every "
                    "matching row is read and sorted before LIMIT applies. Paginate by key (`AND "
                    f"{a['order'][0]} > $last`), shorten the IN lists, or run one query per IN value and merge")
        # a JSON field (col->>'k') is listed under JSON below, not as a cast
        residual = [w for w in a["wrapped"] if not re.search(r'\b%s"?\s*(?:->|#>)' % re.escape(w), q)]
        why = [f"`{', '.join(residual)}` is cast or wrapped in a function (compare the bare column instead, "
               "e.g. `col < $1::date + 1` for `col::date <= $1`)"] if residual else []
        where = re.split(r"\bwhere\b", q, maxsplit=1, flags=re.I)
        why += [label for rx, label in RESIDUAL_RE if len(where) > 1 and rx.search(where[1])]
        if why:
            txt += ". Rows that match are then filtered by " + "; ".join(why)
        parts.append(txt + ".")
    if AGG_RE.search(q):
        parts.append("It aggregates (COUNT / GROUP BY / DISTINCT), so it returns few rows by design: the cost is "
                     f"the ~{per_call:,.0f} matching rows each call reads. An Index Only Scan (every referenced "
                     "column in the index) or a maintained counter cuts it.")
    if not small_all:
        parts.append("If an index above was created during the statement window, the counters still include "
                     "scans from before it: reset pg_stat_statements and re-collect to confirm.")
    return small_all, " ".join(parts)


def rule_inefficient_statements(m, F):
    """Statements that read far more DocDB rows than they return. Runs after the statement access
    rules: a statement one of their findings covers links to it (resolved to finding ids by
    resolve_links), otherwise scan_diagnosis says why it reads that much."""
    covering = defaultdict(list)
    for f in F.items:
        for qid in f["data"].get("queryids", []):
            covering[str(qid)].append(f)
    total_ms = sum(x["total_time_ms"] for x in m.statements)
    for s in m.statements[:200]:
        scanned = s.get("docdb_rows_scanned")
        if scanned is None or s["calls"] < 10:
            continue
        ratio = scanned / max(s["rows"], 1)
        if ratio < SCAN_RATIO or scanned < 10_000 or scanned / s["calls"] < SCAN_PER_CALL:
            continue
        mean = s["total_time_ms"] / max(s["calls"], 1)
        detail = (f"{s['calls']:,} calls, {s['total_time_ms']:,.0f} ms total (mean {mean:,.1f} ms); DocDB scanned "
                  f"{scanned:,} rows to return {s['rows']:,} ({ratio:,.0f}×), ~{scanned / s['calls']:,.0f} rows "
                  "per call.")
        links = [f for f in covering[str(s["queryid"])] if f["rule"] != "scan-heavy-statement"]
        sev = "medium"
        if links:
            rec = "{links}. Then confirm with EXPLAIN (ANALYZE, DIST) and real parameters."
        else:
            small, why = scan_diagnosis(m, s)
            detail += " " + why
            sev = "low" if small else "medium"
            rec = ("No index change is proposed: a full read of a small table is cheap per call. " if small else
                   "No index is proposed: the detail names what the best index leaves to filtering and how to "
                   "cut it (rewrite the condition, paginate by key, or extend the index). ") + \
                  "Confirm where the rows are read with EXPLAIN (ANALYZE, DIST) and real parameters."
        F.add("scan-heavy-statement", sev, "workload", f"queryid {s['queryid']}",
              "Statement scans far more rows than it returns", detail, rec, REF["index"], confidence="medium",
              data={"statements": [dict(stmt_ref([s], 1)[0], mean_ms=round(mean, 2),
                                        share=round(s["total_time_ms"] / total_ms, 4) if total_ms else None)],
                    "queryids": [s["queryid"]], "_links": links})


def resolve_links(findings):
    """Turn a scan-heavy finding's links to the statement findings that cover it into their ids."""
    for f in findings:
        links = f["data"].pop("_links", None)
        if links:
            fix = [x for x in links
                   if any(re.match(r"\s*(CREATE|ALTER|DROP)\b", a["sql"], re.I) for a in x["actions"])]
            see = [x for x in links if x not in fix]
            ref = lambda xs: ", ".join(f"{x['id']} ({x['rule']})" for x in sorted(xs, key=lambda x: x["id"]))
            txt = (f"The fix (index or key change) is proposed in {ref(fix)}" if fix else
                   f"Covered by {ref(see)}, which explains the access path but proposes no change here")
            if fix and see:
                txt += f"; see also {ref(see)}"
            f["recommendation"] = f["recommendation"].replace("{links}", txt)


# ---- statement access rules

def _index_cols(t, i):
    return [t["cols_by_num"][k["attnum"]]["name"] if k["attnum"] else None for k in i["keys"]]


def _pred_cols(t, i):
    if not i["predicate"]:
        return set()
    return {t["lc_cols"][w]["name"] for w in re.findall(r"[a-z_]\w*", i["predicate"].lower()) if w in t["lc_cols"]}


def index_reach(t, i, a):
    """How a statement's conditions reach into index i -> (equality-bound key columns, range column
    or None, the HASH columns bound when only some of them are). HASH columns are only usable when
    every one of them is bound by equality; range columns are usable as a leading prefix."""
    keys = _index_cols(t, i)
    hc = i["hash_count"]
    if hc:
        bound = [c for c in keys[:hc] if c in a["eq"]]
        if len(bound) < hc:
            return [], None, bound
        out, rest = list(keys[:hc]), keys[hc:]
    else:
        out, rest = [], keys
    rcol = None
    for c in rest:
        if c is not None and c in a["eq"]:
            out.append(c)
            continue
        if c is not None and (c in a["range"] or a["limit"] and a["order"][:1] == [c]):
            rcol = c
        break
    return out, rcol, []


def _lowcard_col(m, t, name):
    """Low-cardinality column: by pg_stats when present, else by type (boolean, enum, smallint) or
    name (flag, status, type, mode, is_/has_ ...)."""
    col = t["cols_by_name"][name]
    nd, src = m.distinct(t, {"attnum": col["attnum"]})
    if src == "stats":
        return nd <= LOWCARD_MAX
    return (nd is not None and nd <= LOWCARD_MAX) or col["base_type"] in SUSPECT_LOWCARD_TYPES or \
        col["base_type"] == "boolean" or bool(LOWCARD_NAME_RE.search(name))


def _leads(indexes):
    """Leading column name -> [HASH or range] over secondary LSM indexes."""
    out = defaultdict(list)
    for i in indexes:
        if i["am"] == "lsm" and not i["is_pk"] and not i["is_partition"] and i["keys"] and i["keys"][0]["attnum"]:
            k = i["keys"][0]
            out[i["table"]["cols_by_num"][k["attnum"]]["name"]].append("HASH" if k["opt"] == "HASH" else "range")
    return out


def lead_opt(m, t, col, exclude=()):
    """Sharding of a proposed index's leading column -> ("HASH" | "ASC", reason). Statistics decide
    first; without them, follow how existing indexes lead with this column (on this table, then
    anywhere in the schema), then the schema's convention for secondary indexes."""
    c = t["cols_by_name"][col]
    mono, _ = m.monotonic(t, {"attnum": c["attnum"]})
    if mono:
        return "HASH", f"{col} increases on insert ({mono}), so a range-led index would be a write hot spot"
    nd, src = m.distinct(t, {"attnum": c["attnum"]})
    if src == "stats":
        st = m.colstat(t, col)
        where = (f"pg_stats of {st['from_partitions']} partition(s) (the parent has no stats of its own) show"
                 if st and st.get("from_partitions") else "pg_stats shows")
        if nd <= LOWCARD_MAX:
            return "ASC", (f"{where} ~{nd:,.0f} distinct {col} values: HASH would spread them over at most "
                           f"{nd:,.0f} hash values, while a range index splits inside a value as it grows")
        return "HASH", f"{where} ~{nd:,.0f} distinct {col} values, enough to spread a HASH key"
    skip = {id(i) for i in exclude}
    for scope, idx in (("on this table", t["indexes"]), ("in the schema", m.indexes)):
        opts = _leads([i for i in idx if id(i) not in skip]).get(col)
        if opts:
            opt = max(set(opts), key=opts.count)
            return ("HASH" if opt == "HASH" else "ASC",
                    f"no statistics; existing indexes {scope} lead with {col} "
                    f"{'HASH' if opt == 'HASH' else 'as a range column'} ({opts.count(opt)} of {len(opts)})")
    allopts = [o for v in _leads([i for i in m.indexes if id(i) not in skip]).values() for o in v]
    if len(allopts) >= 10 and allopts.count("range") >= 0.8 * len(allopts):
        return "ASC", (f"no statistics; {allopts.count('range')} of {len(allopts)} secondary indexes in the schema "
                       "lead with a range column")
    if _lowcard_col(m, t, col):
        return "ASC", f"no statistics; {col} looks low-cardinality ({c['base_type']})"
    return "HASH", None


def proposed_keys(m, t, eq, rng, lead=(), desc=(), inlist=()):
    """Index key for these conditions -> (key sql list, columns). A multi-column `lead` is kept as
    a HASH group (it mirrors a HASH primary key); otherwise columns existing indexes on the table lead
    with come first, then selective columns before low-cardinality ones, then one range column. The
    first column is HASH or range as lead_opt decides; the rest are ASC."""
    lead = list(lead)
    hash_lead = lead if len(lead) > 1 else []
    rest = [c for c in dict.fromkeys(lead[:1] + list(eq)) if c not in hash_lead]
    # columns more statements filter on go first, so proposals for one table share a leading key
    freq = t.get("eq_freq") or {}
    leads = _leads(t["indexes"])
    # order: the given lead, columns existing indexes lead with, selective before low-cardinality,
    # columns more of the table's statements filter on (so proposals share a prefix), single-value
    # = before IN lists, then query order
    rest = sorted(rest, key=lambda c: (c not in lead[:1] or bool(hash_lead), c not in leads,
                                       _lowcard_col(m, t, c) and c not in leads, -freq.get(c, 0),
                                       c in inlist, rest.index(c)))
    if not hash_lead:
        # equality columns can go in any order: start with the longest existing secondary index key
        # they all contain, so the new index covers (and supersedes) that index
        pref = max((p for p in (_index_cols(t, i) for i in t["indexes"]
                                if i["am"] == "lsm" and not i["is_pk"] and not i["is_partition"]
                                and not i["predicate"] and i["is_valid"])
                    if p and None not in p and set(p) <= set(rest) and (not lead or p[0] == lead[0])),
                   key=len, default=[])
        rest = list(pref) + [c for c in rest if c not in pref]
    if hash_lead:
        keys = [f"({', '.join(qi(c) for c in hash_lead)}) HASH"] + [f"{qi(c)} ASC" for c in rest]
    else:
        keys = [f"{qi(c)} {lead_opt(m, t, c)[0] if n == 0 else 'ASC'}" for n, c in enumerate(rest)]
    cols = hash_lead + rest
    r = [c for c in rng if c not in cols][:1]
    return keys + [f"{qi(c)} {'DESC' if c in desc else 'ASC'}" for c in r], cols + r


def new_index_name(t, cols):
    """<table>_<cols>_idx, unique among the table's indexes and the names proposed so far in this run.
    A name over 63 bytes is cut and gets a short hash of the full column list, so two proposals whose
    columns differ only past the cut still get different names."""
    taken = t.setdefault("proposed_names", set()) | {i["name"] for i in t["indexes"]}
    base = f"{t['name']}_{'_'.join(cols)}"
    if len(base) + 4 > 63:
        base = f"{base[:52]}_{hashlib.md5(base.encode()).hexdigest()[:6]}"
    for n in range(1, 100):
        cand = f"{base}_idx" if n == 1 else f"{base[:56]}_{n}_idx"
        if cand not in taken:
            t["proposed_names"].add(cand)
            return cand
    return base[:63]


LOWCARD_SELECTIVE = 0.10    # low-cardinality filters still worth an index when one call reads at most this share


def filter_share(m, t, cols):
    """Share of the table one call reads when it binds `cols` by equality (1/distinct per column,
    independent columns assumed) -> fraction or None without estimates."""
    share = 1.0
    for c in cols:
        nd, _ = m.distinct(t, {"attnum": t["cols_by_name"][c]["attnum"]})
        if not nd:
            return None
        share /= max(nd, 1)
    return share


INCLUDE_MAX = 4             # more non-key columns than this: no INCLUDE proposed
WIDE_TYPES = {"json", "jsonb", "bytea", "xml", "tsvector"}   # never copied into an index via INCLUDE


def include_cols(t, accs, keycols):
    """Non-key columns the SELECT blocks behind a proposal read, for INCLUDE -> list or None. None
    when a block is not a SELECT (UPDATE/DELETE need the row anyway), selects *, or needs too many."""
    if not accs or any(a.get("star") or not a.get("reads") for a in accs):
        return None
    need = [c for a in accs for c in a["cols"] if c not in keycols]
    need = list(dict.fromkeys(need))
    if any(t["cols_by_name"][c]["base_type"] in WIDE_TYPES or t["cols_by_name"][c]["base_type"].endswith("[]")
           for c in need):
        return None
    return need if 0 < len(need) <= INCLUDE_MAX else None


def workload_index(m, t, name, keys, cols, eq_cols):
    """A CREATE INDEX proposed from statement text: runnable in its own changes.sql phase.
    eq_cols: key columns the statements bind by equality (their order in the key is free). A
    range-led index on a large table is pre-split when stats give the points; otherwise the note
    carries the query that computes them (it would start on one tablet)."""
    note, split = "derived from the statement text; confirm with EXPLAIN (ANALYZE, DIST)", ""
    rows = m.rows(t)
    colocated = (t.get("yb") or {}).get("is_colocated")
    if "HASH" not in keys[0] and rows and rows >= PRESPLIT_ROWS and not colocated and t["relkind"] != "p":
        desc = {k.rsplit(" ", 1)[0] for k in keys if k.endswith(" DESC")}
        points, scol, prefix = split_points(m, t, cols, desc)
        if points:
            split = split_clause(points)
        elif scol:
            note += (f". {int(rows):,} rows: it starts on one tablet. To pre-split, get points with "
                     f"{split_query(m, t, scol, prefix, scol in desc)} and append{split_template(m, prefix)}")
    a = act("workload", f"CREATE INDEX IF NOT EXISTS {qi(name)} ON {tname(t)} ({', '.join(keys)}){split};",
            "create", target=f"{t['schema']}.{name}", rollback=f"DROP INDEX IF EXISTS {qi(t['schema'])}.{qi(name)};",
            note=note)
    a["eq_cols"] = list(eq_cols)
    return a


def _stmt_line(s, share):
    mean = s["total_time_ms"] / max(s["calls"], 1)
    mx = f", max {s['max_time_ms']:,.0f} ms" if s.get("max_time_ms") else ""
    return (f"{s['calls']:,} calls, {s['total_time_ms']:,.0f} ms total ({share:.1%} of statement time), "
            f"mean {mean:,.1f} ms{mx}")


def rule_statement_access(m, F):
    """Compare each statement's WHERE / JOIN conditions with the table's PK and indexes."""
    total_ms = sum(s["total_time_ms"] for s in m.statements)
    first = len(F.items)
    groups = {}       # (rule, table oid, signature) -> {"stmts": [(s, tier, share)], ...}
    parsed = [(s, statement_access(m, s)) for s in m.statements]
    for t in m.tables.values():
        t["eq_freq"] = defaultdict(int)
    for _, accs in parsed:
        for t, a in accs:
            for c in a["eq"]:
                t["eq_freq"][c] += 1
    for s, accs in parsed:
        tier, share = stmt_cost(s, total_ms)
        scans, weak = False, []
        for t, a in accs:
            filters = list(a["eq"]) + list(a["range"])
            if not filters and not a["wrapped"] and not (a["limit"] and a["order"]):
                continue
            usable = [i for i in t["indexes"] if i["am"] == "lsm" and i["is_valid"] and not i["is_partition"]
                      and _pred_cols(t, i) <= set(filters)]
            reach = {id(i): index_reach(t, i, a) for i in usable}
            best, best_n = None, 0
            for i in usable:
                out, rcol, _ = reach[id(i)]
                n = len(out) + (1 if rcol else 0)
                if n > best_n:
                    best, best_n = i, n
            reached = set()
            if best:
                out, rcol, _ = reach[id(best)]
                reached = set(out) | {rcol} | _pred_cols(t, best)
            # range filter / ORDER BY ... LIMIT on a HASH key column (date/time ones: hash-on-range-type)
            hash_ranged = []
            ranged = list(a["range"]) + (a["order"][:1] if a["limit"] else [])
            for c in dict.fromkeys(ranged):
                if c in reached or t["cols_by_name"][c]["base_type"] in TIME_TYPES:
                    continue
                if any(reach[id(i)][1] == c for i in usable):
                    continue
                hi = next((i for i in usable if c in _index_cols(t, i)[:i["hash_count"]]), None)
                if hi:
                    hash_ranged.append(c)
                    key = ("range-on-hash-column", t["oid"], (hi["name"], c, tuple(sorted(a["eq"]))))
                    g = groups.setdefault(key, {"t": t, "a": a, "index": hi, "col": c, "stmts": []})
                    g["stmts"].append((s, tier, share))
                    g.setdefault("accs", []).append(a)
            unbound_eq = [c for c in a["eq"] if c not in reached]
            if best_n == 0 and (a["eq"] or [c for c in a["range"] if c not in hash_ranged] or a["wrapped"]):
                partly = [(i, reach[id(i)][2]) for i in usable]
                partly = [(i, b) for i, b in partly if b and i["hash_count"] > 1]
                if partly:
                    i, bound = max(partly, key=lambda x: len(x[1]))
                    key = ("hash-key-partly-bound", t["oid"], (i["name"], tuple(bound), tuple(sorted(a["eq"]))))
                    g = groups.setdefault(key, {"t": t, "a": a, "index": i, "bound": bound, "stmts": [],
                                                "elsewhere": set()})
                    # the missing HASH columns bound by another block of the same statement (subquery)
                    hk = _index_cols(t, i)[:i["hash_count"]]
                    g["elsewhere"] |= {c for t2, a2 in accs if t2 is t and a2 is not a
                                       for c in hk if c not in bound and c in a2["eq"]}
                else:
                    key = ("statement-no-index", t["oid"], (tuple(sorted(a["eq"])), tuple(sorted(a["range"])),
                                                           tuple(sorted(a["wrapped"]))))
                    g = groups.setdefault(key, {"t": t, "a": a, "stmts": []})
                g["stmts"].append((s, tier, share))
                g.setdefault("accs", []).append(a)
                scans = True
            elif best and unbound_eq and tier != "low" and not (
                    best["is_unique"] and set(_index_cols(t, best)) <= reached):   # point lookup
                weak.append((("statement-weak-index", t["oid"], (best["name"], tuple(unbound_eq), tuple(sorted(a["eq"])))),
                             {"t": t, "a": a, "index": best, "reached": reached, "unbound": unbound_eq,
                              "stmts": []}, a))
        # a block scanning a whole table explains the statement's time; don't also blame a block that
        # only filters part of an index scan
        for key, new, a in ([] if scans else weak):
            g = groups.setdefault(key, new)
            g["stmts"].append((s, tier, share))
            g.setdefault("accs", []).append(a)

    for (rule, _, _), g in groups.items():
        t, a = g["t"], g["a"]
        stmts = sorted(g["stmts"], key=lambda x: -x[0]["total_time_ms"])
        tiers = [x[1] for x in stmts]
        sev = min(tiers, key=SEV_ORDER.get)
        rows = m.rows(t)
        if rows is not None and rows < SMALL_TABLE:
            sev = "low"
        elif rows and rows >= LARGE_TABLE and sev == "low":
            sev = "medium"
        s0, _, share0 = stmts[0]
        calls = sum(x[0]["calls"] for x in stmts)
        tot = sum(x[0]["total_time_ms"] for x in stmts)
        cost = (f"Workload: {_stmt_line(s0, share0)}." if len(stmts) == 1 else
                f"Workload: {len(stmts)} statements, {calls:,} calls, {tot:,.0f} ms total "
                f"({sum(x[2] for x in stmts):.1%} of statement time); heaviest: {_stmt_line(s0, share0)}.")
        size = f" Table rows: {rows:,.0f}." if rows else ""
        conf = "high" if sev != "low" and rows is not None else ("medium" if sev != "low" else "low")
        confirm = [f"Check the plan: EXPLAIN (ANALYZE, DIST) of queryid {s0['queryid']} with real parameters."]
        data = {"indexes": [i["definition"] for i in t["indexes"]],
                "access": {k: list(a[k]) for k in ("eq", "range", "wrapped")},
                "queryids": [x[0]["queryid"] for x in stmts],
                "statements": [dict(r, mean_ms=round(r["total_time_ms"] / max(r["calls"], 1), 2),
                                    share=round(x[2], 4)) for r, x in zip(stmt_ref([x[0] for x in stmts]), stmts)]}
        wrapped = list(a["wrapped"])
        wnote = (f" `{', '.join(wrapped)}` is cast or wrapped in a function in the filter, so an index on the bare "
                 "column can't serve it: compare the bare column (e.g. `col >= $1::date AND col < $1::date + 1` "
                 "for `col::date = $1`) or index the expression.") if wrapped else ""
        actions, cols, keys = [], None, None
        if rule == "statement-no-index":
            eq, rng = list(a["eq"]), list(a["range"])
            cond = "; ".join(x for x in [eq and "equality on " + ", ".join(eq),
                                         rng and "range on " + ", ".join(rng),
                                         wrapped and "a cast/function of " + ", ".join(wrapped)] if x)
            detail = (f"Statements filter {t['fq']} with {cond}, but neither the primary key nor any index "
                      f"leads with these columns: each call scans the whole table. {cost}{size}{wnote}")
            # an index holding the filter columns behind a leading column the statements leave unbound
            near = [(i, _index_cols(t, i)) for i in t["indexes"] if i["am"] == "lsm" and i["is_valid"]]
            near = [(i, cs) for i, cs in near if cs and cs[0] not in a["eq"] and set(eq) & set(cs[1:])]
            hint = ""
            if near:
                i, cs = near[0]
                detail += (f" {i['name']} ({', '.join(cs)}) has {', '.join(c for c in eq if c in cs[1:])} but "
                           f"leads with {cs[0]}, which the statements don't bind: if the application knows "
                           f"{cs[0]}, adding it to the WHERE clause uses that index without a new one.")
                hint = f"First check whether the statements should also filter on {cs[0]} (see above). "
            fshare = filter_share(m, t, eq) if eq else None
            # low-cardinality filters on a large table still pay when each call reads a small share
            selective = rows and rows >= PRESPLIT_ROWS and fshare is not None and fshare <= LOWCARD_SELECTIVE
            if eq and all(_lowcard_col(m, t, c) for c in eq) and not rng and not selective:
                rec = (f"The only filters ({', '.join(eq)}) look low-cardinality: an index on them alone puts all "
                       "rows in a few keys. If the value queried is rare, add a partial index "
                       f"(WHERE {eq[0]} = <value>) keyed on a selective column; otherwise "
                       + ("a scan of a small table is fine" if rows is not None and rows < SMALL_TABLE else
                          "each call reads a large share of the table anyway")
                       + ", and caching the result in the application is cheaper.")
            elif eq or rng:
                keys, cols = proposed_keys(m, t, eq, rng, desc=a["desc"], inlist=a["in"])
                new = new_index_name(t, cols)
                rec = (f"{hint}Add an index that leads with the equality columns ({', '.join(keys)}); low-cardinality "
                       "columns go after the selective ones.")
                if selective and all(_lowcard_col(m, t, c) for c in eq):
                    rec += (f" The filters are low-cardinality, but on {rows:,.0f} rows each call reads about "
                            f"{fshare:.1%} of the table through the index instead of all of it.")
                mono, _ = m.monotonic(t, {"attnum": t["cols_by_name"][rng[0]]["attnum"]}) if rng and not eq else (None, None)
                if mono:
                    rec += f" {rng[0]} increases on insert ({mono}): a range index leading with it is a write hot spot; see the bucketed pattern."
                actions = [workload_index(m, t, new, keys, cols, [c for c in cols if c in a["eq"]])]
            else:
                rec = "Rewrite the filter on the bare column, or create an expression index."
            title = "Statements filter on columns no index leads with"
        elif rule == "hash-key-partly-bound":
            i, bound = g["index"], g["bound"]
            hk = _index_cols(t, i)[:i["hash_count"]]
            missing = [c for c in hk if c not in bound]
            what = "Primary key" if i["is_pk"] else "Index"
            detail = (f"{what} {i['name']} on {t['fq']} is HASH on ({', '.join(hk)}), and the statements bind "
                      f"{', '.join(bound)} but not {', '.join(missing)}. A HASH key is usable only when all its "
                      f"HASH columns are bound by equality, so each call scans the table. {cost}{size}{wnote}")
            other = [c for c in a["eq"] if c not in bound]
            keys, cols = proposed_keys(m, t, other, list(a["range"]), lead=tuple(bound), desc=a["desc"], inlist=a["in"])
            new = new_index_name(t, cols)
            rec = (f"Add an index on ({', '.join(keys)}). If lookups by {', '.join(bound)} alone are common, a key "
                   f"of ({proposed_keys(m, t, [], [], lead=tuple(bound))[0][0]}, "
                   f"{', '.join(c + ' ASC' for c in missing)}) serves both, but "
                   "changing the primary key rewrites the table.")
            actions = [workload_index(m, t, new, keys, cols, [c for c in cols if c in a["eq"]])]
            title = "Statements bind only part of a HASH key"
            if g["elsewhere"]:
                el = sorted(g["elsewhere"])
                rec = (f"The statement already binds {', '.join(el)} in a subquery on the same table: bind it in "
                       f"the outer statement too (add `{' AND '.join(c + ' = <same value>' for c in el)}`, or match "
                       f"on `({', '.join(hk)}) IN (SELECT {', '.join(hk)} ...)`), so each row is a primary-key "
                       "lookup and no new index is needed. Otherwise: " + rec)
                actions = []
        elif rule == "range-on-hash-column":
            i, c = g["index"], g["col"]
            what = "primary key" if i["is_pk"] else "index"
            detail = (f"Statements range-filter or sort (ORDER BY ... LIMIT) on {t['fq']}.{c}, which is a HASH column "
                      f"of {what} {i['name']}. HASH columns only serve equality, and no index has {c} as a range "
                      f"column after the equality-bound columns: each call reads every candidate row. {cost}{size}")
            eq = [x for x in a["eq"]]
            keys, cols = proposed_keys(m, t, eq, [c], desc=a["desc"], inlist=a["in"])
            new = new_index_name(t, cols)
            rec = (f"Add an index with {c} as a range column after the equality columns ({', '.join(keys)}), "
                   f"or change {c} to ASC/DESC in {i['name']} if no query needs it as an exact-match hash.")
            actions = [workload_index(m, t, new, keys, cols, [c for c in cols if c in a["eq"]])]
            title = "Range filter or sort on a HASH column"
        else:
            i, reached, unbound = g["index"], g["reached"], g["unbound"]
            detail = (f"The closest index, {i['name']}, serves {', '.join(c for c in reached if c)} on {t['fq']}; "
                      f"the conditions on {', '.join(unbound)} are applied to every row it returns. The statements "
                      f"are expensive enough for this to matter. {cost}{size}{wnote}")
            keys, cols = proposed_keys(m, t, [c for c in a["eq"]], list(a["range"]),
                                       lead=tuple(_index_cols(t, i)[:i["hash_count"]]), desc=a["desc"], inlist=a["in"])
            new = new_index_name(t, cols)
            rec = (f"Extend the key with the selective unbound columns ({', '.join(keys)}), as a new index or by "
                   f"replacing {i['name']} if nothing else depends on its current order.")
            actions = [workload_index(m, t, new, keys, cols, [c for c in cols if c in a["eq"]])]
            title = "Index serves only part of the statement's filter"
        if actions and cols and keys and not keys[0].startswith("("):
            opt, why = lead_opt(m, t, cols[0])
            if why and keys[0].endswith(" " + opt):
                rec += f" {cols[0]} leads {'HASH' if opt == 'HASH' else 'as a range column (ASC)'}: {why}."
                if opt == "ASC":
                    rec += (" It is created pre-split (SPLIT AT VALUES) so it doesn't start on one tablet."
                            if "SPLIT AT VALUES" in actions[0]["sql"] else
                            " A range-led index starts on one tablet and splits as it grows (see the pre-split "
                            "note in changes.sql for a large table).")
        inc = include_cols(t, g.get("accs") or [], cols) if actions and cols else None
        if inc:
            pn, pt, pk_, _, prest = parse_create_index(actions[0]["sql"])
            actions[0]["sql"] = (f"CREATE INDEX IF NOT EXISTS {pn} ON {pt} ({pk_}) "
                                 f"INCLUDE ({', '.join(qi(c) for c in inc)})" + (f" {prest}" if prest else "") + ";")
            rec += f" INCLUDE ({', '.join(inc)}) lets the SELECT run as an Index Only Scan."
            pkc = [c for c in inc if t["pk"] and c in _index_cols(t, t["pk"])]
            if pkc:
                rec += (f" {', '.join(pkc)} {'is' if len(pkc) == 1 else 'are'} in the primary key, but a YSQL index "
                        "entry holds the primary key only in encoded form, so the query can't read it back without "
                        "INCLUDE.")
        # a range filter left out of the key because the ORDER BY ... LIMIT column holds that place (E-S-R)
        if actions and cols:
            sort_col = cols[-1] if a["limit"] and a["order"][:1] == [cols[-1]] else None
            behind = [c for c in a["range"] if c not in cols]
            if sort_col and behind:
                where = "in INCLUDE" if inc and set(behind) <= set(inc) else "on the rows read"
                rec += (f" {', '.join(behind)} {'is a range filter' if len(behind) == 1 else 'are range filters'} "
                        f"applied {where}, not a key column: the key ends with the sort column {sort_col}, so "
                        f"the index returns rows in ORDER BY order and stops after LIMIT. Putting "
                        f"{behind[0]} before {sort_col} would seek on it but read and sort every match before "
                        "LIMIT; after it, it can't be used to seek. This works best when the range filter "
                        f"and {sort_col} move together (newest {sort_col} = newest {behind[0]}).")
        F.add(rule, sev, "workload", t["fq"], title, detail, rec, REF["index"], actions=actions,
              confidence=conf, confirm=confirm, data=data)
    dedupe_proposals(m, F, first)


def parse_create_index(sql):
    """A proposed "CREATE INDEX IF NOT EXISTS name ON tbl (keys) [INCLUDE (cols)] ...;" ->
    (name, table, keys, include columns, rest) or None."""
    mm = re.match(r"^CREATE INDEX IF NOT EXISTS (\S+) ON (\S+) \(", sql)
    if not mm:
        return None
    d, n = 1, mm.end()
    while n < len(sql) and d:
        d += (sql[n] == "(") - (sql[n] == ")")
        n += 1
    keys, tail = sql[mm.end():n - 1], sql[n:].rstrip(";").strip()
    inc = re.match(r"INCLUDE \(([^)]*)\)\s*", tail)
    cols = [c.strip() for c in inc.group(1).split(",")] if inc else []
    return mm.group(1), mm.group(2), keys, cols, tail[inc.end():] if inc else tail


def _key_shape(keys):
    """Key text "(a, b) HASH, c ASC" -> (HASH columns, range columns); direction is ignored (a range
    index scans both ways)."""
    hash_cols, rng, buf, d = (), [], "", 0
    parts = []
    for ch in keys + ",":
        d += (ch == "(") - (ch == ")")
        if ch == "," and d == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    for n, p in enumerate(parts):
        mm = re.fullmatch(r"\(?(.*?)\)?\s+(HASH|ASC|DESC)(\s+NULLS\s+(FIRST|LAST))?", p, re.I | re.S)
        expr, opt = (mm.group(1), mm.group(2).upper()) if mm else (p, "ASC")
        if opt == "HASH" and n == 0:
            hash_cols = tuple(c.strip() for c in expr.split(","))
        else:
            rng.append(expr.strip())
    return hash_cols, tuple(rng)


def _shape_covers(a, b, b_eq=None):
    """Index shape a serves every lookup index shape b serves: same HASH columns, and b's range
    columns a prefix of a's. When b's equality columns are known (b_eq), their order is free: a's
    leading range columns must be the same set, followed by b's other columns in order."""
    if a[0] != b[0]:
        return False
    if b_eq is None:
        return a[1][:len(b[1])] == b[1]
    eqp = [c for c in b[1] if c in b_eq]
    rest = [c for c in b[1] if c not in b_eq]
    n = len(eqp)
    return set(a[1][:n]) == set(eqp) and list(a[1][n:n + len(rest)]) == rest


def _key_parts(keys):
    """Top-level items of a key text: "(a, b) HASH, c ASC" -> ["(a, b) HASH", "c ASC"]."""
    parts, buf, d = [], "", 0
    for ch in keys + ",":
        d += (ch == "(") - (ch == ")")
        if ch == "," and d == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    return parts


def pk_twin_reshape(m, F, f, a, tbl, keys, existing, reshaped):
    """A proposal whose leading key columns are exactly the HASH primary key's columns would be a
    range twin of the PK (every row written twice). Reshape the PK to that shape instead: the PK
    holds every column, so it serves the statements with no extra index. -> True when converted."""
    t = next((x for x in m.tables.values() if tname(x) == tbl), None)
    pk = t and t["pk"]
    if not pk or not pk["hash_count"] or not all(k["attnum"] for k in pk["keys"]):
        return False
    pkcols = {qi(t["cols_by_num"][k["attnum"]]["name"]) for k in pk["keys"]}
    parts, cols, lead = _key_parts(keys), [], []
    for p in parts:
        sh = _key_shape(p)
        cols += list(sh[0]) + list(sh[1])
        lead.append(p)
        if len(cols) >= len(pkcols):
            break
    if set(cols) != pkcols:
        return False
    new_keys = ", ".join(lead)
    blocked = (pk.get("referenced_by_fk") and "foreign keys reference the primary key") or \
              (t["relkind"] == "p" and "the table is partitioned (the PK must include the partition key)") or \
              (tbl in reshaped and f"{reshaped[tbl][1]} already reshapes this primary key") or \
              next((f"{g['rule']} also changes this primary key" for g in F.items for x in g["actions"]
                    if "PRIMARY KEY" in x["sql"] and f" {tbl} " in f" {x['sql']} "), None)
    note = (f"This index would repeat the primary key's columns ({index_keys_sql(pk)}) with range sharding: "
            "every row written twice.")
    if blocked:
        f["recommendation"] += (f" {note} Reshaping the primary key to ({new_keys}) instead would avoid that, "
                                f"but {blocked}, so the index is proposed.")
        return False
    f["actions"].remove(a)
    f["actions"].append(act("rewrite", pk_alter(t, pk, new_keys), "alter", target=t["fq"],
                            rollback=pk_alter(t, pk, index_keys_sql(pk)),
                            note=f"primary key becomes ({new_keys}) instead of a twin index (full table rewrite)"))
    f["recommendation"] += (f" {note} So instead of a new index, reshape the primary key to ({new_keys}): same "
                            "columns, so the same uniqueness, and it serves these statements directly. It rewrites "
                            "the table; if that can't be scheduled, create the index instead "
                            f"(CREATE INDEX ... ({keys})) and accept the double write.")
    reshaped[tbl] = (new_keys, f["rule"])
    existing[tbl] = [e for e in existing.get(tbl, []) if not e[1].startswith(f"existing index {pk['name']}")]
    existing[tbl].append((_key_shape(new_keys), f"the primary key as reshaped for {f['rule']} ({new_keys})", None))
    return True


def supersede(m, f, a, tbl, shape, inc, gone, existing):
    """A proposed index that fully covers an existing plain secondary index replaces it: drop the
    old one after the new one is built, so the change doesn't leave two indexes for one access path."""
    for i in m.indexes:
        if tname(i["table"]) != tbl or i["is_pk"] or i["is_unique"] or i["backs_constraint"] \
                or i["referenced_by_fk"] or i["is_partition"] or i["predicate"] or i["am"] != "lsm" \
                or i["fq"] in gone or not all(k["attnum"] for k in i["keys"]):
            continue
        old = _key_shape(index_keys_sql(i))
        cols = set(shape[0]) | set(shape[1]) | {qi(c) for c in inc}
        if _shape_covers(shape, old) and {qi(c["expr"]) for c in i["include"]} <= cols:
            f["actions"].append(act("drop", f"DROP INDEX IF EXISTS {iname(i)};", "drop", target=i["fq"],
                                    rollback=recreate_sql(i), note=f"superseded by the new index above"))
            f["recommendation"] += (f" The new index covers {i['name']} ({key_text(i)}): drop it once the new "
                                    "one is built and the plans use it.")
            gone.add(i["fq"])
            existing[tbl] = [e for e in existing.get(tbl, []) if e[1] != f"existing index {i['name']}"]


def dedupe_proposals(m, F, first):
    """Drop a statement finding's proposed index when an existing index (not being dropped or
    replaced), another finding's change, or a longer proposal already has its key."""
    gone = {a.get("replaces") or a.get("target") for f in F.items for a in f["actions"]
            if a["kind"] in ("drop", "replace")}
    existing = defaultdict(list)          # table sql name -> [(shape, label, action or None)]
    # primary keys another finding reshapes: the new shape counts, the old one doesn't
    reshaped = {}
    for f in F.items[:first]:
        for a in f["actions"]:
            mm = re.match(r"ALTER TABLE (\S+) DROP CONSTRAINT \S+, ADD CONSTRAINT \S+ PRIMARY KEY \((.*)\);$",
                          a["sql"], re.S)
            if mm and a["phase"] == "rewrite":
                reshaped[mm.group(1)] = (mm.group(2), f["rule"])
    for tbl, (keys, rule) in reshaped.items():
        existing[tbl].append((_key_shape(keys), f"the primary key as reshaped by {rule} ({keys})", None))
    for i in m.indexes:
        if i["is_pk"] and tname(i["table"]) in reshaped:
            continue
        if i["is_valid"] and i["am"] == "lsm" and not i["predicate"] and not i["is_partition"] \
                and i["fq"] not in gone and all(k["attnum"] for k in i["keys"]):
            existing[tname(i["table"])].append((_key_shape(index_keys_sql(i)), f"existing index {i['name']}", None))
    for f in F.items[:first]:
        for a in f["actions"]:
            p = parse_create_index(a["sql"])
            if p and "WHERE" not in p[4].upper():
                existing[p[1]].append((_key_shape(p[2]), f"{p[0]} (proposed by {f['rule']})", None))
    props = []
    for f in F.items[first:]:
        for a in f["actions"]:
            p = parse_create_index(a["sql"])
            if p:
                props.append((f, a, p, _key_shape(p[2])))
    # longest keys first, so a shorter proposal folds into a longer one with the same leading key
    props.sort(key=lambda x: -len(x[3][0]) - len(x[3][1]))
    kept = defaultdict(list)
    for f, a, (name, tbl, keys, inc, rest), shape in props:
        b_eq = {qi(c) for c in a.get("eq_cols") or []} or None
        by = next(((lbl, ka) for sh, lbl, ka in existing.get(tbl, []) + kept[tbl]
                   if _shape_covers(sh, shape, b_eq)), None)
        if not by:
            if pk_twin_reshape(m, F, f, a, tbl, keys, existing, reshaped):
                continue
            kept[tbl].append((shape, f"{name}, proposed by another {f['rule']} finding on {f['object']}", a))
            supersede(m, f, a, tbl, shape, inc, gone, existing)
            continue
        lbl, ka = by
        f["actions"].remove(a)
        f["recommendation"] = (f"Use {lbl}: it starts with the key these statements need ({keys}), "
                               "so no separate index is proposed here.")
        if not inc:
            continue
        if ka is None:
            f["recommendation"] += f" For an Index Only Scan it would also need INCLUDE ({', '.join(inc)})."
            continue
        # carry the folded proposal's INCLUDE columns over to the index that replaces it
        kn, kt, kkeys, kinc, krest = parse_create_index(ka["sql"])
        keycols = set(_key_shape(kkeys)[0]) | set(_key_shape(kkeys)[1])
        merged = list(dict.fromkeys(kinc + [c for c in inc if c not in keycols]))
        if merged != kinc:
            ka["sql"] = (f"CREATE INDEX IF NOT EXISTS {kn} ON {kt} ({kkeys}) INCLUDE ({', '.join(merged)})"
                         + (f" {krest}" if krest else "") + ";")
            f["recommendation"] += f" Its INCLUDE list carries {', '.join(inc)} for this statement's Index Only Scan."


def rule_settings(m, F):
    st = m.meta.get("settings") or {}
    if st.get("yb_use_hash_splitting_by_default") == "off":
        F.add("range-default", "info", "schema", m.meta.get("database", "db"), "Range sharding is the default",
              "yb_use_hash_splitting_by_default = off (e.g. Enhanced PostgreSQL Compatibility): a key without "
              "HASH/ASC/DESC is range-sharded.",
              "Write HASH / ASC / DESC explicitly in DDL; monotonic keys created without HASH are flagged above.",
              REF["sharding"] + "#enhanced-postgresql-compatibility-mode")


RULES = [rule_no_pk, rule_invalid, rule_hash_on_range_type, rule_monotonic_range, rule_lowcard_hash,
         rule_skew_or_nulls, rule_range_tails, rule_presplit, rule_partial, rule_redundant, rule_pk_range_twin,
         rule_fk, rule_int_sequences, rule_gin, rule_analyze, rule_unused, rule_statement_access,
         rule_inefficient_statements, rule_settings]


# --------------------------------------------------------------------------- validation & output

def validate(b, m):
    w = list(b.warnings)
    if m.excluded:
        w.append(f"Excluded {len(m.excluded)} pg_partman template table(s) from the review (no data; "
                 "the parent and partitions hold the real definition): " + ", ".join(sorted(m.excluded)) + ".")
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


def load_render():
    return load_sibling("render")


def write_outputs(out, b, m, findings, warnings, fmt="html"):
    os.makedirs(out, exist_ok=True)
    doc = {
        "format": "yb-schema-review/findings/v1", "meta": m.meta, "warnings": warnings,
        "statement_source": b.statement_source, "statement_count": len(b.statements),
        "usage_file_count": len(b.usage), "table_count": len(m.tables), "index_count": len(m.indexes),
        "top_statements": b.statements[:TOP_STATEMENTS], "findings": findings,
    }
    with open(os.path.join(out, "findings.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, default=str)
    return load_render().render_all(json.loads(json.dumps(doc, default=str)), out, fmt=fmt)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle", help="bundle directory (stats.json or schema.sql), or a schema.sql file")
    ap.add_argument("--out", help="output directory (default: BUNDLE/review)")
    ap.add_argument("--format", choices=["html", "md"], default="html",
                    help="report format: html (default, self-contained report.html) or md (report.md)")
    ap.add_argument("--database", help="keep only statements whose datname matches (statements files "
                    "covering several databases; default: the database stats.json was exported from)")
    ap.add_argument("--range-default", action="store_true",
                    help="schema.sql only. By default, keys without HASH/ASC/DESC follow YugabyteDB's default: the first key column is HASH, the rest ASC. Pass this to treat them as range (ASC) instead, for clusters running with yb_use_hash_splitting_by_default = off (e.g. Enhanced PostgreSQL Compatibility).")
    args = ap.parse_args()
    b = Bundle(args.bundle, range_default=args.range_default, database=args.database)
    m = Model(b)
    F = Findings()
    for rule in RULES:
        rule(m, F)
    findings = F.sorted()
    resolve_links(findings)
    # every finding about an existing index carries its current definition (shown in the report)
    defs = {i["fq"]: i["definition"] for i in m.indexes}
    for f in findings:
        if f["object"] in defs and "redundant" not in f["data"]:
            f["data"].setdefault("definition", defs[f["object"]])
    warnings = validate(b, m)
    out = args.out or os.path.join(b.path, "review")
    phases, n_manual = write_outputs(out, b, m, findings, warnings, args.format)
    counts = defaultdict(int)
    for f in findings:
        counts[f["severity"]] += 1
    print(f"{len(findings)} findings ({', '.join(f'{v} {k}' for k, v in sorted(counts.items(), key=lambda x: SEV_ORDER[x[0]]))})")
    print(f"changes.sql: {', '.join(f'{p} {n}' for p, n in phases.items()) or 'no changes'}, {n_manual} manual")
    print(f"→ {out}/report.{args.format}, findings.json, changes.sql, rollback.sql")
    for x in warnings:
        print(f"warning: {x}", file=sys.stderr)


if __name__ == "__main__":
    main()
