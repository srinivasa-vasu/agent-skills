#!/usr/bin/env python3
"""
Build a stats.json-compatible catalog from a schema.sql file, for offline review without a
database. Handles `ysql_dump --schema-only --include-yb-metadata` output and hand-written DDL.

    python3 schema_sql.py schema.sql [-o catalog.json] [--range-default]

analyze.py uses this automatically when a bundle has schema.sql but no stats.json.

What it reads: CREATE TABLE (columns, types, defaults, identity/serial, inline and table
constraints, PARTITION BY, SPLIT INTO/AT, colocation), CREATE [UNIQUE] INDEX, CREATE TYPE ... ENUM
(+ ALTER TYPE ... ADD VALUE), CREATE DOMAIN, CREATE SEQUENCE / OWNED BY, ALTER TABLE ... ADD
CONSTRAINT (PRIMARY KEY, UNIQUE [USING INDEX], FOREIGN KEY), ALTER COLUMN SET DEFAULT / ADD
GENERATED AS IDENTITY, ATTACH PARTITION, ALTER INDEX ... ATTACH PARTITION, SET search_path.
Everything else (views, functions, grants, comments, ...) is skipped.

Keys without HASH/ASC/DESC get YugabyteDB's default: the first key column HASH, the rest ASC
(--range-default: all ASC, as with yb_use_hash_splitting_by_default = off). Colocated tables are
always range. No statistics or row counts exist in this mode.
Stdlib only.
"""

import argparse
import json
import re
import sys
from collections import OrderedDict

STOP_WORDS = {"default", "not", "null", "constraint", "generated", "collate", "check", "references",
              "unique", "primary"}
TYPE_ALIASES = {
    "int": "integer", "int4": "integer", "int8": "bigint", "int2": "smallint", "bool": "boolean",
    "float8": "double precision", "float4": "real", "float": "double precision",
    "timestamptz": "timestamp with time zone", "timestamp": "timestamp without time zone",
    "timetz": "time with time zone", "time": "time without time zone",
    "varchar": "character varying", "char": "character", "decimal": "numeric",
    "serial": "integer", "serial4": "integer", "bigserial": "bigint", "serial8": "bigint",
    "smallserial": "smallint", "serial2": "smallint",
}
SERIAL_TYPES = {"serial", "serial4", "bigserial", "serial8", "smallserial", "serial2"}


# --------------------------------------------------------------------------- lexing

def split_statements(text):
    """Split SQL into statements, honouring quotes, dollar quotes and comments.
    psql meta-commands (lines starting with a backslash) are dropped."""
    out, buf, i, n = [], [], 0, len(text)
    at_line_start = True
    while i < n:
        c = text[i]
        if at_line_start and c == "\\" and not "".join(buf).strip():
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        at_line_start = False
        if c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            buf.append(" ")
            continue
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            buf.append(text[i:j + 1])
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"\$[A-Za-z_]*\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                j = n if j < 0 else j + len(tag)
                buf.append(text[i:j])
                i = j
                continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            at_line_start = False
            continue
        if c == "\n":
            at_line_start = True
        buf.append(c)
        i += 1
    stmt = "".join(buf).strip()
    if stmt:
        out.append(stmt)
    return [re.sub(r"\s+", " ", s) for s in out]


def split_top(s, sep=","):
    """Split on sep at paren depth 0, outside quotes."""
    parts, depth, buf, i, q = [], 0, [], 0, None
    while i < len(s):
        c = s[i]
        if q:
            buf.append(c)
            if c == q:
                q = None
        elif c in ("'", '"'):
            q = c
            buf.append(c)
        elif c == "(":
            depth += 1
            buf.append(c)
        elif c == ")":
            depth -= 1
            buf.append(c)
        elif c == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def paren_block(s, start):
    """Given s[start] == '(', return (inner, index after the matching ')')."""
    depth, i, q = 0, start, None
    while i < len(s):
        c = s[i]
        if q:
            if c == q:
                q = None
        elif c in ("'", '"'):
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[start + 1:i], i + 1
        i += 1
    return s[start + 1:], len(s)


IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
QNAME = rf"{IDENT}(?:\s*\.\s*{IDENT})?"


def unquote(ident):
    ident = ident.strip()
    if ident.startswith('"') and ident.endswith('"'):
        return ident[1:-1].replace('""', '"')
    return ident.lower()


def qi(name):
    if re.fullmatch(r"[a-z_][a-z0-9_$]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def norm_type(t):
    t = t.strip()
    low = t.lower()
    m = re.match(r"^([a-z_][\w ]*?)\s*(\(.*\))?(\[\])?$", low)
    if not m:
        return t
    base, mod, arr = m.group(1).strip(), re.sub(r"\s+", "", m.group(2) or ""), m.group(3) or ""
    if base in ("timestamp", "time") and "with time zone" not in low and "without time zone" not in low:
        base = TYPE_ALIASES[base]
    elif base in ("timestamp with time zone", "timestamp without time zone",
                  "time with time zone", "time without time zone"):
        pass
    else:
        base = TYPE_ALIASES.get(base, base)
    return f"{base}{mod}{arr}"


# --------------------------------------------------------------------------- model

class Catalog:
    def __init__(self, hash_default=True):
        self.hash_default = hash_default
        self.search_path = "public"
        self.tables = OrderedDict()     # (schema, name) -> table
        self.indexes = []
        self.fks = []
        self.sequences = OrderedDict()  # (schema, name) -> seq
        self.enums = OrderedDict()      # "schema.type" -> labels
        self.domains = {}               # "schema.type" -> base type
        self.oid = 16384
        self.implicit_keys = 0
        self.skipped = {}
        self.version = None
        self.warnings = []
        self.index_attaches = []

    def next_oid(self):
        self.oid += 1
        return str(self.oid)

    def qname(self, text):
        parts = [unquote(p) for p in re.findall(IDENT, text)]
        if len(parts) == 1:
            return (self.search_path, parts[0])
        return (parts[0], parts[1])

    def type_key(self, t):
        """'app.state' style key for enums/domains (schema-qualified unless public). An unqualified
        name resolves through the current search_path, like the database does."""
        parts = [unquote(p) for p in re.findall(IDENT, t)]
        if len(parts) == 2:
            return f"{parts[0]}.{parts[1]}" if parts[0] != "public" else parts[1]
        if not parts:
            return t
        if self.search_path != "public":
            q = f"{self.search_path}.{parts[0]}"
            if q in self.enums or q in self.domains:
                return q
        return parts[0]

    def base_type(self, t):
        key = self.type_key(re.sub(r"\(.*\)|\[\]", "", t))
        if key in self.domains:
            return self.base_type(self.domains[key])
        if key in self.enums or (f"{self.search_path}.{key}" in self.enums):
            return key
        return re.sub(r"\(.*\)", "", norm_type(t))

    def is_enum(self, t):
        key = self.type_key(re.sub(r"\(.*\)|\[\]", "", t))
        while key in self.domains:
            key = self.type_key(self.domains[key])
        return key in self.enums

    def table(self, key):
        return self.tables.get(key)

    def col(self, t, name):
        for c in t["columns"]:
            if c["name"] == name:
                return c
        return None


# --------------------------------------------------------------------------- key specs

OPT_RE = re.compile(r"\s+(HASH|ASC|DESC)(?:\s+NULLS\s+(FIRST|LAST))?$", re.I)


def parse_keys(cat, t, spec, colocated, am="lsm"):
    """Parse an index/PK key list -> list of key dicts (expr, attnum, type, base_type, opt, ...)."""
    keys, items = [], split_top(spec)
    for pos, item in enumerate(items):
        opt, nulls = None, None
        m = OPT_RE.search(item)
        if m:
            opt, nulls = m.group(1).upper(), (m.group(2) or "").upper() or None
            item = item[:m.start()].strip()
        collation = None
        mc = re.search(r"\s+COLLATE\s+(" + IDENT + r"(?:\." + IDENT + r")?)\s*$", item, re.I)
        if mc:
            collation = unquote(mc.group(1).split(".")[-1])
            item = item[:mc.start()].strip()
        opclass = None
        mo = re.search(r"\s+([a-z_][\w]*_ops)\s*$", item, re.I)
        if mo:
            opclass = mo.group(1).lower()
            item = item[:mo.start()].strip()
        group = None
        if item.startswith("("):
            inner, end = paren_block(item, 0)
            if end == len(item):
                parts = split_top(inner)
                if len(parts) > 1 and opt == "HASH":
                    group = parts
                else:
                    item = inner.strip()
                    while item.startswith("("):            # ((expr)) -> expr
                        inner2, end2 = paren_block(item, 0)
                        if end2 != len(item):
                            break
                        item = inner2.strip()
        for member in (group or [item]):
            member = member.strip()
            keys.append(make_key(cat, t, member, opt, nulls, opclass, collation))
    if am != "lsm":                                       # GIN etc.: the catalog reports ASC
        for k in keys:
            k["opt"] = k["opt"] or "ASC"
            k["nulls_first"] = False
    # implicit options: first key HASH (hash default, not colocated), the rest ASC
    if am == "lsm":
        for n, k in enumerate(keys):
            if k["opt"] is None:
                cat.implicit_keys += 1
                k["opt"] = "HASH" if (n == 0 and cat.hash_default and not colocated) else "ASC"
            if k["opt"] in ("ASC", "DESC") and k["nulls_first"] is None:
                k["nulls_first"] = k["opt"] == "DESC"
            if k["opt"] == "HASH":
                k["nulls_first"] = None
        # HASH columns must lead: a HASH after a range column is not valid, keep as written
    return keys


def make_key(cat, t, text, opt, nulls, opclass, collation):
    ident = re.fullmatch(IDENT, text)
    col = cat.col(t, unquote(text)) if ident else None
    if col:
        typ, base = col["type"], col["base_type"]
        expr, attnum = qi(col["name"]), col["attnum"]
    else:
        typ = base = infer_expr_type(cat, t, text)
        expr, attnum = re.sub(r"\((" + IDENT + r")\)::", r"\1::", text), 0
    return {"expr": expr, "attnum": attnum, "type": typ, "base_type": base, "key": True,
            "opt": opt, "nulls_first": (nulls == "FIRST") if nulls else None,
            "opclass": opclass, "collation": collation}


def infer_expr_type(cat, t, text):
    m = re.search(r"::\s*([a-z_][\w ]*(?:\([\d, ]+\))?)\s*\)*$", text, re.I)
    if m:
        return norm_type(m.group(1))
    f = re.match(r"^([a-z_]+)\s*\(", text, re.I)
    if f:
        fn = f.group(1).lower()
        if fn == "date_trunc":
            return ("timestamp with time zone"
                    if "with time zone" in text or any(
                        c["base_type"] == "timestamp with time zone" and re.search(r"\b%s\b" % re.escape(c["name"]), text)
                        for c in t["columns"]) and "without time zone" not in text
                    else "timestamp without time zone")
        if fn in ("lower", "upper", "btrim", "trim", "concat", "substr", "substring", "md5", "left", "right"):
            return "text"
        if fn in ("yb_hash_code", "length", "char_length"):
            return "integer"
    if re.match(r"^yb_hash_code\(.*\)\s*%\s*\d+$", text, re.I):
        return "integer"
    return None


# --------------------------------------------------------------------------- statements

def h_set(cat, s):
    m = re.match(r"^SET\s+search_path\s*(?:=|TO)\s*(.+)$", s, re.I)
    if m:
        first = split_top(m.group(1))[0].strip().strip("'")
        if first and first.lower() not in ("pg_catalog", "''"):
            cat.search_path = unquote(first) if not first.startswith("$") else "public"


def h_enum(cat, s):
    m = re.match(rf"^CREATE TYPE ({QNAME}) AS ENUM \((.*)\)$", s, re.I)
    if m:
        sch, name = cat.qname(m.group(1))
        key = name if sch == "public" else f"{sch}.{name}"
        cat.enums[key] = [v.strip()[1:-1].replace("''", "'") for v in split_top(m.group(2)) if v.strip()]
        return True
    m = re.match(rf"^ALTER TYPE ({QNAME}) ADD VALUE (?:IF NOT EXISTS )?'((?:[^']|'')*)'(.*)$", s, re.I)
    if m:
        sch, name = cat.qname(m.group(1))
        key = name if sch == "public" else f"{sch}.{name}"
        labels = cat.enums.setdefault(key, [])
        val, rest = m.group(2).replace("''", "'"), m.group(3).strip()
        p = re.match(r"^(BEFORE|AFTER) '((?:[^']|'')*)'$", rest, re.I)
        if p and p.group(2) in labels:
            at = labels.index(p.group(2)) + (1 if p.group(1).upper() == "AFTER" else 0)
            labels.insert(at, val)
        else:
            labels.append(val)
        return True
    return False


def h_domain(cat, s):
    m = re.match(rf"^CREATE DOMAIN ({QNAME}) (?:AS )?(.+?)(?: (?:DEFAULT|CONSTRAINT|NOT NULL|NULL|CHECK|COLLATE)\b.*)?$",
                 s, re.I)
    if m:
        sch, name = cat.qname(m.group(1))
        key = name if sch == "public" else f"{sch}.{name}"
        cat.domains[key] = m.group(2).strip()
        return True
    return False


def h_create_table(cat, s):
    m = re.match(rf"^CREATE (?:(?:GLOBAL |LOCAL )?(?:TEMP|TEMPORARY|UNLOGGED) )?TABLE (?:IF NOT EXISTS )?({QNAME})\s*(.*)$",
                 s, re.I)
    if not m:
        return False
    sch, name = cat.qname(m.group(1))
    rest = m.group(2)
    t = {"oid": cat.next_oid(), "schema": sch, "name": name, "relkind": "r", "reltuples": None,
         "is_partition": False, "parent": None, "partition_key": None, "last_analyze": None,
         "yb": None, "columns": [], "_pk": None, "_split": None, "_colocated": None}
    cat.tables[(sch, name)] = t
    po = re.match(rf"^PARTITION OF ({QNAME})(.*)$", rest, re.I)
    if po:
        parent = cat.table(cat.qname(po.group(1)))
        t["is_partition"] = True
        t["parent"] = f"{qi(parent['schema'])}.{qi(parent['name'])}" if parent else po.group(1)
        if parent:
            for c in parent["columns"]:
                t["columns"].append(dict(c))
        rest = po.group(2)
        if rest.strip().startswith("("):
            body, end = paren_block(rest.strip(), 0)
            rest = rest.strip()[end:]
        tail = rest
    elif rest.startswith("("):
        body, end = paren_block(rest, 0)
        tail = rest[end:]
        inline_pk, uniques, fks = None, [], []
        attnum = 0
        for el in split_top(body):
            el_low = el.lower()
            if re.match(r"^(constraint\s+" + IDENT + r"\s+)?(primary key|unique|foreign key|check|exclude)\b", el, re.I) \
                    or el_low.startswith("like "):
                table_constraint(cat, t, el, deferred_fks=fks, uniques=uniques)
                continue
            attnum += 1
            col, extra = column_def(cat, el, attnum)
            t["columns"].append(col)
            if extra.get("pk"):
                inline_pk = qi(col["name"])
            if extra.get("unique"):
                uniques.append((None, qi(col["name"])))
            if extra.get("ref"):
                fks.append((None, qi(col["name"]), extra["ref"]))
            if extra.get("serial"):
                seq = f"{t['name']}_{col['name']}_seq"
                col["default"] = f"nextval('{qi(sch)}.{qi(seq)}'::regclass)"
                cat.sequences[(sch, seq)] = {"schema": sch, "name": seq, "type": col["type"], "cache": 1,
                                             "increment": 1, "owned_by": f"{qi(sch)}.{qi(name)}.{qi(col['name'])}"}
        if inline_pk and not t["_pk"]:
            t["_pk"] = {"name": f"{name}_pkey", "spec": inline_pk}
        t["_uniques"] = uniques
        t["_fks"] = fks
    else:
        tail = rest
    pb = re.search(r"\bPARTITION BY (\w+) \((.*?)\)(?=\s|$)", tail, re.I)
    if pb:
        t["relkind"] = "p"
        t["partition_key"] = f"{pb.group(1).upper()} ({pb.group(2)})"
    if re.search(r"colocation_id\s*=|colocation\s*=\s*'?true", tail, re.I):
        t["_colocated"] = True
    elif re.search(r"colocation\s*=\s*'?false", tail, re.I):
        t["_colocated"] = False
    sp = re.search(r"\bSPLIT INTO (\d+) TABLETS", tail, re.I)
    sa = re.search(r"\bSPLIT AT VALUES \(", tail, re.I)
    if sp:
        t["_split"] = int(sp.group(1))
    elif sa:
        inner, _ = paren_block(tail, sa.end() - 1)
        t["_split"] = len(split_top(inner)) + 1
    return True


def column_def(cat, el, attnum):
    m = re.match(rf"^({IDENT})\s+(.*)$", el)
    name, rest = unquote(m.group(1)), m.group(2)
    # type runs until the first stop word at depth 0
    tokens, i, depth, typ_end = rest, 0, 0, len(rest)
    for mm in re.finditer(r"\(|\)|\b([A-Za-z]+)\b", rest):
        tok = mm.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and tok.lower() in STOP_WORDS:
            typ_end = mm.start()
            break
    raw_type = rest[:typ_end].strip()
    opts = rest[typ_end:]
    serial = raw_type.lower() in SERIAL_TYPES
    tkey = cat.type_key(re.sub(r"\[\]$", "", raw_type))
    if tkey in cat.enums or tkey in cat.domains:
        typ = tkey + ("[]" if raw_type.endswith("[]") else "")      # canonical, like format_type()
    else:
        typ = norm_type(raw_type)
    col = {"attnum": attnum, "name": name, "type": typ, "base_type": cat.base_type(typ),
           "is_enum": cat.is_enum(typ), "not_null": bool(re.search(r"\bNOT NULL\b", opts, re.I)),
           "default": None, "identity": None, "generated": None}
    extra = {}
    d = re.search(r"\bDEFAULT\s+", opts, re.I)
    if d:
        col["default"] = take_expr(opts[d.end():])
    gi = re.search(r"\bGENERATED (ALWAYS|BY DEFAULT) AS IDENTITY\b", opts, re.I)
    if gi:
        col["identity"] = "a" if gi.group(1).upper() == "ALWAYS" else "d"
        col["not_null"] = True
    else:
        g = re.search(r"\bGENERATED ALWAYS AS \(", opts, re.I)
        if g:
            col["generated"] = "s"
            col["default"] = paren_block(opts, g.end() - 1)[0].strip()
    if serial:
        col["not_null"] = True                      # default set by the caller (needs the table name)
        extra["serial"] = True
    if re.search(r"\bPRIMARY KEY\b", opts, re.I):
        extra["pk"] = True
        col["not_null"] = True
    if re.search(r"\bUNIQUE\b", opts, re.I):
        extra["unique"] = True
    r = re.search(rf"\bREFERENCES ({QNAME})\s*(\(([^)]*)\))?", opts, re.I)
    if r:
        extra["ref"] = (r.group(1), r.group(3))
    return col, extra


def take_expr(s):
    """Default expression: up to the next top-level column keyword."""
    depth, q = 0, None
    for mm in re.finditer(r"'|\"|\(|\)|\b(NOT|NULL|CONSTRAINT|GENERATED|COLLATE|CHECK|REFERENCES|UNIQUE|PRIMARY)\b", s, re.I):
        tok = mm.group(0)
        if q:
            if tok == q:
                q = None
            continue
        if tok in ("'", '"'):
            q = tok
        elif tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0:
            return s[:mm.start()].strip()
    return s.strip()


def table_constraint(cat, t, el, deferred_fks=None, uniques=None):
    """PRIMARY KEY / UNIQUE / FOREIGN KEY inside CREATE TABLE or ALTER TABLE ... ADD."""
    cname = None
    m = re.match(rf"^CONSTRAINT ({IDENT})\s+(.*)$", el, re.I)
    if m:
        cname, el = unquote(m.group(1)), m.group(2)
    pk = re.match(r"^PRIMARY KEY\s*\(", el, re.I)
    if pk:
        spec, _ = paren_block(el, pk.end() - 1)
        t["_pk"] = {"name": cname or f"{t['name']}_pkey", "spec": spec}
        return
    uq_idx = re.match(rf"^UNIQUE USING INDEX ({IDENT})", el, re.I)
    if uq_idx:
        iname = unquote(uq_idx.group(1))
        for i in cat.indexes:
            if i["name"] == iname and i["_table"] is t:
                i["backs_constraint"] = cname or iname
        return
    uq = re.match(r"^UNIQUE(?: NULLS (NOT )?DISTINCT)?\s*\(", el, re.I)
    if uq:
        spec, _ = paren_block(el, uq.end() - 1)
        entry = (cname, spec, bool(uq.group(1)))
        if uniques is not None:
            uniques.append(entry)
        else:
            add_unique(cat, t, *entry)
        return
    fk = re.match(rf"^FOREIGN KEY\s*\(([^)]*)\)\s*REFERENCES ({QNAME})\s*(\(([^)]*)\))?", el, re.I)
    if fk:
        entry = (cname, fk.group(1), (fk.group(2), fk.group(4)))
        if deferred_fks is not None:
            deferred_fks.append(entry)
        else:
            cat.fks.append((t, entry))
        return


def add_unique(cat, t, cname, spec, nnd=False):
    cols = [unquote(c.split()[0]) for c in split_top(spec)]
    name = cname or f"{t['name']}_{'_'.join(cols)}_key"
    add_index(cat, t, name, spec, unique=True, backs=name, nnd=nnd)


def add_index(cat, t, name, spec, unique=False, backs=None, nnd=False, am="lsm", include=None,
              predicate=None, split=None, is_pk=False, only=False):
    colocated = bool(t.get("_colocated"))
    keys = parse_keys(cat, t, spec, colocated, am)
    inc = []
    for c in split_top(include or ""):
        col = cat.col(t, unquote(c))
        inc.append({"expr": qi(col["name"]) if col else c, "attnum": col["attnum"] if col else 0,
                    "type": col["type"] if col else None, "base_type": col["base_type"] if col else None,
                    "key": False, "opt": None, "nulls_first": None, "opclass": None, "collation": None})
    keytxt = ", ".join(f"{k['expr']} {k['opt']}" if k["opt"] else k["expr"] for k in keys)
    if keys and keys[0]["opt"] == "HASH":
        nh = sum(1 for k in keys if k["opt"] == "HASH")
        if nh > 1:
            keytxt = f"({', '.join(k['expr'] for k in keys[:nh])}) HASH" + "".join(
                f", {k['expr']} {k['opt']}" for k in keys[nh:])
    definition = (f"CREATE {'UNIQUE ' if unique else ''}INDEX {qi(name)} ON {'ONLY ' if only else ''}"
                  f"{qi(t['schema'])}.{qi(t['name'])} USING {am} ({keytxt})"
                  + (f" INCLUDE ({', '.join(c['expr'] for c in inc)})" if inc else "")
                  + (" NULLS NOT DISTINCT" if nnd else "")
                  + (f" WHERE {predicate}" if predicate else ""))
    idx = {"oid": cat.next_oid(), "table_oid": t["oid"], "name": name, "definition": definition, "am": am,
           "is_pk": is_pk, "is_unique": unique or is_pk, "nulls_not_distinct": nnd, "is_valid": True,
           "is_partition": False, "backs_constraint": backs, "referenced_by_fk": False,
           "nkey": len(keys), "predicate": predicate,
           "yb": None if is_pk else {"num_tablets": split, "is_colocated": colocated,
                                     "num_hash_key_columns": sum(1 for k in keys if k["opt"] == "HASH"),
                                     "colocation_id": None, "tablegroup_oid": None},
           "columns": keys + inc, "_table": t}
    cat.indexes.append(idx)
    return idx


def h_create_index(cat, s):
    m = re.match(rf"^CREATE (UNIQUE )?INDEX (?:NONCONCURRENTLY |CONCURRENTLY )?(?:IF NOT EXISTS )?({IDENT})?\s*ON (ONLY )?({QNAME})\s*(.*)$",
                 s, re.I)
    if not m:
        return False
    t = cat.table(cat.qname(m.group(4)))
    if t is None:
        cat.skipped["index on unknown or non-table relation (e.g. materialized view)"] = \
            cat.skipped.get("index on unknown or non-table relation (e.g. materialized view)", 0) + 1
        return True
    rest = m.group(5)
    am = "lsm"
    u = re.match(r"^USING (\w+)\s*", rest, re.I)
    if u:
        am = u.group(1).lower()
        am = "lsm" if am == "btree" else am
        rest = rest[u.end():]
    spec, end = paren_block(rest, rest.index("("))
    tail = rest[end:]
    include = None
    mi = re.search(r"\bINCLUDE\s*\(", tail, re.I)
    if mi:
        include, _ = paren_block(tail, mi.end() - 1)
    nnd = bool(re.search(r"\bNULLS NOT DISTINCT\b", tail, re.I))
    predicate = None
    mw = re.search(r"\bWHERE\s+(.*)$", tail, re.I)
    if mw:
        predicate = mw.group(1).strip()
    split = None
    sp = re.search(r"\bSPLIT INTO (\d+) TABLETS", tail, re.I)
    sa = re.search(r"\bSPLIT AT VALUES \(", tail, re.I)
    if sp:
        split = int(sp.group(1))
    elif sa:
        inner, _ = paren_block(tail, sa.end() - 1)
        split = len(split_top(inner)) + 1
    name = unquote(m.group(2)) if m.group(2) else f"{t['name']}_idx{cat.oid}"
    add_index(cat, t, name, spec, unique=bool(m.group(1)), nnd=nnd, am=am, include=include,
              predicate=predicate, split=split, only=bool(m.group(3)))
    return True


def h_alter_table(cat, s):
    m = re.match(rf"^ALTER TABLE (?:IF EXISTS )?(?:ONLY )?({QNAME})\s+(.*)$", s, re.I)
    if not m:
        return False
    t = cat.table(cat.qname(m.group(1)))
    rest = m.group(2)
    if t is None:
        return True
    ap = re.match(rf"^ATTACH PARTITION ({QNAME})", rest, re.I)
    if ap:
        child = cat.table(cat.qname(ap.group(1)))
        if child:
            child["is_partition"] = True
            child["parent"] = f"{qi(t['schema'])}.{qi(t['name'])}"
        return True
    for action in split_top(rest):
        if re.match(r"^ADD (CONSTRAINT|PRIMARY KEY|UNIQUE|FOREIGN KEY|CHECK|EXCLUDE)\b", action, re.I):
            table_constraint(cat, t, action[4:].strip())
            continue
        ac = re.match(rf"^ALTER (?:COLUMN )?({IDENT})\s+(.*)$", action, re.I)
        if ac:
            col = cat.col(t, unquote(ac.group(1)))
            if not col:
                continue
            what = ac.group(2)
            sd = re.match(r"^SET DEFAULT (.*)$", what, re.I)
            if sd:
                col["default"] = sd.group(1).strip()
            gi = re.match(r"^ADD GENERATED (ALWAYS|BY DEFAULT) AS IDENTITY(?:\s*\((.*)\))?", what, re.I)
            if gi:
                col["identity"] = "a" if gi.group(1).upper() == "ALWAYS" else "d"
                col["not_null"] = True
                opts = gi.group(2) or ""
                sn = re.search(rf"SEQUENCE NAME ({QNAME})", opts, re.I)
                sch, sname = cat.qname(sn.group(1)) if sn else (t["schema"], f"{t['name']}_{col['name']}_seq")
                cache = re.search(r"CACHE (\d+)", opts, re.I)
                cat.sequences[(sch, sname)] = {"schema": sch, "name": sname, "type": col["type"],
                                               "cache": int(cache.group(1)) if cache else 1, "increment": 1,
                                               "owned_by": f"{qi(t['schema'])}.{qi(t['name'])}.{qi(col['name'])}"}
            if re.match(r"^SET NOT NULL", what, re.I):
                col["not_null"] = True
            ty = re.match(r"^(?:SET DATA )?TYPE (.+?)(?: USING .*)?$", what, re.I)
            if ty:
                col["type"] = norm_type(ty.group(1))
                col["base_type"] = cat.base_type(col["type"])
            continue
        ad = re.match(rf"^ADD (?:COLUMN )?(?:IF NOT EXISTS )?({IDENT}\s+.*)$", action, re.I)
        if ad:
            col, extra = column_def(cat, ad.group(1), len(t["columns"]) + 1)
            t["columns"].append(col)
    return True


def h_alter_index(cat, s):
    m = re.match(rf"^ALTER INDEX ({QNAME}) ATTACH PARTITION ({QNAME})$", s, re.I)
    if not m:
        return False
    cat.index_attaches.append(cat.qname(m.group(2))[1])     # applied in finalize (PKs come later)
    return True


def h_sequence(cat, s):
    m = re.match(rf"^CREATE SEQUENCE (?:IF NOT EXISTS )?({QNAME})\s*(.*)$", s, re.I)
    if m:
        sch, name = cat.qname(m.group(1))
        opts = m.group(2)
        ty = re.search(r"\bAS (smallint|integer|bigint|int2|int4|int8|int)\b", opts, re.I)
        cache = re.search(r"\bCACHE (\d+)", opts, re.I)
        inc = re.search(r"\bINCREMENT (?:BY )?(-?\d+)", opts, re.I)
        own = re.search(rf"\bOWNED BY ({QNAME}\.{IDENT}|{QNAME})", opts, re.I)
        cat.sequences[(sch, name)] = {"schema": sch, "name": name,
                                      "type": norm_type(ty.group(1)) if ty else "bigint",
                                      "cache": int(cache.group(1)) if cache else 1,
                                      "increment": int(inc.group(1)) if inc else 1, "owned_by": None}
        if own:
            set_owner(cat, (sch, name), own.group(1))
        return True
    m = re.match(rf"^ALTER SEQUENCE ({QNAME}) OWNED BY (.+)$", s, re.I)
    if m:
        set_owner(cat, cat.qname(m.group(1)), m.group(2))
        return True
    return False


def set_owner(cat, seqkey, target):
    parts = [unquote(p) for p in re.findall(IDENT, target)]
    if len(parts) < 2 or seqkey not in cat.sequences:
        return
    sch, tbl, col = (parts[0], parts[1], parts[2]) if len(parts) == 3 else (cat.search_path, parts[0], parts[1])
    cat.sequences[seqkey]["owned_by"] = f"{qi(sch)}.{qi(tbl)}.{qi(col)}"


# --------------------------------------------------------------------------- assembly

def finalize(cat):
    for t in cat.tables.values():
        colocated = bool(t.get("_colocated"))
        if t["_pk"]:
            idx = add_index(cat, t, t["_pk"]["name"], t["_pk"]["spec"], unique=True, backs=t["_pk"]["name"],
                            is_pk=True)
            for k in idx["columns"]:
                c = cat.col(t, re.sub(r'^"|"$', "", k["expr"])) if k["attnum"] else None
                if c:
                    c["not_null"] = True
            # move the PK first among the table's indexes (cosmetic)
        for entry in t.pop("_uniques", []) or []:
            add_unique(cat, t, *entry)
        for entry in t.pop("_fks", []) or []:
            cat.fks.append((t, entry))
        pk = next((i for i in cat.indexes if i["_table"] is t and i["is_pk"]), None)
        nh = sum(1 for k in pk["columns"] if k["key"] and k["opt"] == "HASH") if pk else (0 if colocated else 1)
        t["yb"] = {"num_tablets": t["_split"], "num_hash_key_columns": nh, "is_colocated": colocated,
                   "colocation_id": None, "tablegroup_oid": None} if t["relkind"] == "r" else None
        for i in cat.indexes:
            if i["_table"] is t and i["yb"] is not None:
                i["yb"]["is_colocated"] = colocated

    fks = []
    for t, (cname, cols, (ref_name, ref_cols)) in cat.fks:
        ref = cat.table(cat.qname(ref_name))
        child_cols = [unquote(c) for c in split_top(cols)]
        if ref is None:
            cat.warnings.append(f"FK on {t['schema']}.{t['name']} references unknown table {ref_name}")
            continue
        ref_pk = next((i for i in cat.indexes if i["_table"] is ref and i["is_pk"]), None)
        if ref_cols:
            parent_cols = [unquote(c) for c in split_top(ref_cols)]
        elif ref_pk:
            parent_cols = [re.sub(r'^"|"$', "", k["expr"]) for k in ref_pk["columns"] if k["key"]]
        else:
            continue
        # the referenced unique index/PK
        for i in cat.indexes:
            if i["_table"] is ref and i["is_unique"] and \
                    sorted(re.sub(r'^"|"$', "", k["expr"]) for k in i["columns"] if k["key"]) == sorted(parent_cols):
                i["referenced_by_fk"] = True
        name = cname or f"{t['name']}_{'_'.join(child_cols)}_fkey"
        pairs = []
        for c, p in zip(child_cols, parent_cols):
            cc, pc = cat.col(t, c), cat.col(ref, p)
            if cc and pc:
                pairs.append({"name": cc["name"], "type": cc["type"], "ref_name": pc["name"], "ref_type": pc["type"]})
        if len(pairs) == len(child_cols):
            fks.append({"name": name, "table_oid": t["oid"], "ref_oid": ref["oid"], "columns": pairs})

    for i in cat.indexes:
        if i["name"] in cat.index_attaches:
            i["is_partition"] = True
    indexes = []
    for i in cat.indexes:
        i = dict(i)
        i.pop("_table")
        indexes.append(i)
    tables = []
    for t in cat.tables.values():
        t = {k: v for k, v in t.items() if not k.startswith("_")}
        tables.append(t)
    enums = [{"type": k, "labels": v} for k, v in cat.enums.items()]
    return tables, indexes, fks, enums


def parse(path, hash_default=True):
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    cat = Catalog(hash_default)
    v = re.search(r"--\s*Dumped from database version (\S+)", text)
    cat.version = v.group(1) if v else None
    is_dump = bool(re.search(r"--\s*(YSQL|PostgreSQL) database dump", text))
    handlers = [h_set, h_enum, h_domain, h_create_table, h_create_index, h_alter_table, h_alter_index, h_sequence]
    for s in split_statements(text):
        handled = False
        for h in handlers:
            r = h(cat, s)
            if r:
                handled = True
                break
        if not handled:
            kind = " ".join(s.split()[:2]).upper()
            if kind.startswith(("CREATE VIEW", "CREATE MATERIALIZED", "CREATE FUNCTION", "CREATE OR")):
                cat.skipped[kind] = cat.skipped.get(kind, 0) + 1
    tables, indexes, fks, enums = finalize(cat)
    seqs = list(cat.sequences.values())
    # sequence-backed defaults (nextval) without an explicit OWNED BY: link them
    for t in tables:
        for c in t["columns"]:
            d = c.get("default") or ""
            nm = re.search(r"nextval\('([^']+)'", d)
            if nm:
                sch, name = cat.qname(nm.group(1))
                if (sch, name) not in cat.sequences and (t["schema"], name) in cat.sequences:
                    sch = t["schema"]
                s = cat.sequences.get((sch, name))
                if s and not s.get("owned_by"):
                    s["owned_by"] = f"{qi(t['schema'])}.{qi(t['name'])}.{qi(c['name'])}"
    warnings = list(cat.warnings)
    if cat.implicit_keys:
        warnings.append(f"{cat.implicit_keys} key column(s) had no explicit HASH/ASC/DESC: assumed "
                        + ("the hash default (first key column HASH, the rest ASC). If the cluster runs with "
                           "yb_use_hash_splitting_by_default = off (e.g. Enhanced PostgreSQL Compatibility), "
                           "re-run with --range-default." if hash_default else "range (ASC)."))
    if not is_dump:
        warnings.append("schema.sql is not a ysql_dump file: colocation and tablet splits are only known "
                        "where the DDL states them.")
    for k, n in cat.skipped.items():
        warnings.append(f"skipped {n} statement(s): {k}")
    doc = {
        "format": "yb-schema-review/stats/v1",
        "meta": {"exported_at": None, "database": re.sub(r"\.sql$", "", path.split("/")[-1]), "dbid": None,
                 "version": cat.version, "values_mode": "none", "node_count": None, "placements": None,
                 "source": "schema.sql",
                 "settings": {"yb_use_hash_splitting_by_default": "on" if hash_default else "off"}},
        "enums": enums, "tables": tables, "indexes": indexes, "foreign_keys": fks,
        "sequences": seqs, "column_stats": [],
    }
    return doc, warnings


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("schema", help="schema.sql (ysql_dump --schema-only --include-yb-metadata, or DDL)")
    ap.add_argument("-o", "--out", help="write the catalog JSON here (default: stdout)")
    ap.add_argument("--range-default", action="store_true",
                    help="keys without HASH/ASC are range (yb_use_hash_splitting_by_default = off)")
    args = ap.parse_args()
    doc, warnings = parse(args.schema, hash_default=not args.range_default)
    text = json.dumps(doc, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    print(f"{len(doc['tables'])} tables, {len(doc['indexes'])} indexes, {len(doc['foreign_keys'])} FKs, "
          f"{len(doc['sequences'])} sequences", file=sys.stderr)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
