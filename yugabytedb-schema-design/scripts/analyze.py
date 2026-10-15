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
# leading HASH key types flagged as possibly low-cardinality when no statistics exist
SUSPECT_LOWCARD_TYPES = {"smallint", '"char"'}
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
    def __init__(self, path, range_default=False):
        self.schema_file = None
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
            "data": data or {},
            # set by the reviewer, then re-render: proposed | accepted | rejected
            "status": "proposed", "review_note": "",
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


# ---- actions: the DDL behind each finding, rendered into changes.sql / rollback.sql by render.py
#
# phase    prepare (ANALYZE) | create (new indexes, online) | drop (drops, swap replacements in)
#          | rewrite (table rewrites: PK / column type) | manual (written commented out)
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


def index_tail(i):
    inc = f" INCLUDE ({', '.join(c['expr'] for c in i['include'])})" if i["include"] else ""
    pred = f" WHERE {i['predicate']}" if i["predicate"] else ""
    return inc + pred


def split_clause(values):
    return f" SPLIT AT VALUES ({', '.join('(' + v + ')' for v in values)})" if values else ""


def bucket_split(n):
    return split_clause([str(b) for b in range(1, n)])


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
    create = f"CREATE INDEX IF NOT EXISTS {qi(tmp)} ON {tname(t)} ({keys}){index_tail(i)}{split};"
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
                                  f"({keys}){index_tail(i)}{split};",
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
                              f"({bucket_keys(m, i, k0)}){index_tail(i)}{bucket_split(m.buckets)};",
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
        ev = "stats" if "stats" in srcs else "schema"
        rows = m.rows(t)
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
        vals = m.split_values(t, i["keys"][0])
        hint = (f" Known values allow SPLIT AT VALUES ({', '.join('(' + ql(v) + ')' for v in vals)})."
                if vals else "")
        actions = [manual(f"-- yb-admin -master_addresses <masters> list_tablets ysql.{m.meta.get('database')} "
                          f"{i['name'] if not i['is_pk'] else t['name']}   # confirm the tablet count first")]
        if vals and not i["is_pk"] and not i["is_unique"]:
            actions.append(manual(replace_index(i, ", ".join(key_sql(c) for c in i["keys"]),
                                                split_clause([ql(v) for v in vals]))[0]["sql"],
                                  note="rebuild pre-split, then swap it in"))
        F.add("range-single-tablet", sev, "stats", i["fq"], "Range-sharded relation on a single tablet",
              f"{i['name']} on {t['fq']} ({int(rows):,} rows) is range-sharded and has 1 tablet: all reads and "
              f"writes go to one tablet leader until auto-splitting catches up.",
              "Pre-split new range indexes/tables with SPLIT AT VALUES; for existing ones, confirm auto-splitting "
              "is enabled and the tablet count in yb-admin (num_tablets can be stale)." + hint,
              REF["lowcard"] + "#why-range-sharding-works-here-the-hidden-pk-suffix", actions=actions,
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
            new = f"{t['name']}_{'_'.join(c['name'] for c in cols)}_idx"[:63]
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
    ap.add_argument("--range-default", action="store_true",
                    help="schema.sql only. By default, keys without HASH/ASC/DESC follow YugabyteDB's default: the first key column is HASH, the rest ASC. Pass this to treat them as range (ASC) instead, for clusters running with yb_use_hash_splitting_by_default = off (e.g. Enhanced PostgreSQL Compatibility).")
    args = ap.parse_args()
    b = Bundle(args.bundle, range_default=args.range_default)
    m = Model(b)
    F = Findings()
    for rule in RULES:
        rule(m, F)
    findings = F.sorted()
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
