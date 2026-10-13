# YugabyteDB Schema Design Skill

An AI agent skill that encodes **best practices for designing high-performance YugabyteDB schemas** — from scratch or when optimizing existing ones.

## Overview

YugabyteDB is an index-organized, distributed SQL database. Schema decisions that seem minor in a single-node RDBMS — primary key order, sharding strategy, index column placement — have outsized impact on performance at scale. This skill gives AI agents the knowledge to make those decisions correctly.

## What It Covers

| Topic | Reference | Description |
|---|---|---|
| **Sharding** | [`sharding.md`](references/sharding.md) | Hash vs range sharding, choosing a sharding key |
| **Hotspot Prevention** | [`hotspots.md`](references/hotspots.md) | Avoiding write/read hotspots on timestamps and monotonic PKs |
| **Index Design** | [`index-design.md`](references/index-design.md) | Column ordering, covering indexes, redundant index cleanup |
| **Partial Indexes** | [`partial-indexes.md`](references/partial-indexes.md) | Efficient indexing for NULL-heavy and skewed-value columns |
| **Low-Cardinality Columns** | [`low-cardinality.md`](references/low-cardinality.md) | Handling boolean, ENUM, and low-distinct-value sharding keys |
| **Primary Keys** | [`primary-keys.md`](references/primary-keys.md) | PK selection, explicit PKs, composite PKs for partitioned tables |
| **Foreign Keys** | [`foreign-keys.md`](references/foreign-keys.md) | FK type alignment and mandatory FK indexes |
| **Review Mode** | [`review-mode.md`](references/review-mode.md) | Reviewing an existing database live or offline: bundle, scripts, triage, rollout |

## When to Use

This skill activates when a user:

- Asks how to **design a YugabyteDB schema** or structure tables for distributed SQL
- Needs to **choose a sharding strategy** (hash vs range)
- Wants to **avoid write hotspots** on sequential columns
- Asks how to **optimize indexes** or handle NULL-heavy columns
- Mentions keywords like *"YSQL schema"*, *"tablet hotspot"*, *"sharding key"*
- Reports that their **YugabyteDB writes are slow or skewed**
- Wants an existing database **reviewed or audited**, live or from a schema dump, pg_stats export or `statements.json`

## Review Mode

Review an existing database with read-only scripts and an offline analyzer:

```bash
# live: collect a bundle (schema dump, catalog + pg_stats, per-node usage and pg_stat_statements)
scripts/collect.sh -h <host> -d <db> -U <readonly_user> -o bundle [--sslrootcert ca.crt]

# offline: analyze a bundle (no DB connection; stdlib Python 3.8+)
python3 scripts/analyze.py bundle      # -> bundle/review/findings.json, report.md, changes.sql, rollback.sql

# after triage (status / review_note / edited SQL in findings.json): re-render report + SQL
python3 scripts/render.py bundle/review/findings.json

# apply one phase at a time
ysqlsh -d <db> -v phase=create -f bundle/review/changes.sql   # prepare | create | drop | rewrite
```

Checks include HASH-sharded date/time key columns, redundant and unused indexes, low-cardinality,
NULL-heavy or skewed HASH keys, monotonic range keys, per-value write tails, single-tablet range
indexes, partial-index pitfalls, missing or mistyped FK indexes, tables without a PK, and
scan-heavy statements. Each finding carries an evidence level (schema / stats / workload). The
analyzer never runs DDL. Every change in the report is written to `changes.sql` (phased:
prepare → create → drop → rewrite, idempotent) with a matching `rollback.sql`. See
[`review-mode.md`](references/review-mode.md) for the customer-side commands, schema-only replay,
the accepted statements formats, and the rollout runbook.

## Golden Rules

### Do

- ✅ Always define an explicit `PRIMARY KEY` (inline in `CREATE TABLE`; `ADD PRIMARY KEY` rewrites the table)
- ✅ Write `HASH` / `ASC` / `DESC` explicitly — the default depends on `yb_use_hash_splitting_by_default`
- ✅ Use `ASC`/`DESC` on index columns that are range-queried (pre-split new range indexes)
- ✅ Order index columns Equality → Sort → Range; a `HASH` leading column must be high-cardinality
- ✅ Index every foreign key column in child tables (a PK whose leading column is the FK counts)
- ✅ Use partial indexes for nullable or skewed columns, keyed on columns other than the predicate
- ✅ Match FK column types exactly (`INT` vs `BIGINT` matters)
- ✅ Drop redundant indexes (key prefix, wider index's HASH columns inside it, same predicate, uniqueness kept)
- ✅ Use `TEXT` wherever possible instead of `VARCHAR(n)` (no performance benefit; use a `CHECK` constraint if a limit is needed)

### Don't

- ❌ Never use a low-cardinality column (boolean, enum, smallint code) as the only `HASH` column
- ❌ Never range-shard on a monotonically increasing column without a synthetic shard key
- ❌ Never leave tables without a PK if `UNIQUE NOT NULL` columns exist
- ❌ Never create multi-column GIN indexes
- ❌ Never rely on partial indexes for queries that bind the predicate value as a parameter

## Project Structure

```
yugabytedb-schema-design/
├── SKILL.md              # Skill definition and quick-decision guide
├── README.md
├── references/
│   ├── sharding.md        # Hash vs range sharding strategies
│   ├── hotspots.md        # Write/read hotspot prevention
│   ├── index-design.md    # Index column ordering, covering and redundant indexes
│   ├── partial-indexes.md # Partial indexes for sparse data
│   ├── low-cardinality.md # Low-cardinality column indexing
│   ├── primary-keys.md    # Primary key selection patterns
│   ├── foreign-keys.md    # Foreign key design and performance
│   └── review-mode.md     # Reviewing an existing database (live or offline)
└── scripts/
    ├── collect.sh         # Read-only bundle collector
    ├── export-stats.sql   # Catalog + pg_stats -> stats.json
    ├── export-usage.sql   # Per-node usage + pg_stat_statements -> usage/<node>.json
    ├── analyze.py         # Offline analyzer -> findings.json (+ render)
    ├── render.py          # findings.json -> report.md, changes.sql, rollback.sql
    └── checks/
        ├── hash_on_range_type.sql  # HASH key columns of date/time types
        └── redundant_indexes.sql   # Redundant (overshadowed) indexes
```
