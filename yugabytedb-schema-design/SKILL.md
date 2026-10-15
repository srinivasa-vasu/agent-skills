---
name: yugabytedb-schema-design
description: >
  Design and review high-performance YugabyteDB YSQL schemas. Covers hash vs range sharding,
  write hotspots on sequential columns, partial indexes for nullable or skewed columns,
  low-cardinality indexes, covering indexes, primary keys, foreign key indexes, and redundant
  index cleanup. Includes read-only scripts and an offline analyzer that review an existing
  database from its catalog, pg_stats, pg_stat_statements or statements.json. Use this skill
  when the user asks how to design a YugabyteDB schema, choose a sharding key, avoid hotspots,
  optimize indexes, or model tables for distributed SQL; or wants an existing YSQL database
  reviewed or audited (redundant/unused indexes, HASH timestamp columns, low-cardinality hash
  keys, missing FK indexes), live or from a schema dump, stats export or statements.json.
  Trigger on "YugabyteDB indexes", "YSQL schema", "tablet hotspot", "sharding key", or slow or
  skewed YugabyteDB writes.
---

# YugabyteDB Schema Design Best Practices

This skill covers **how to design schemas well** for YugabyteDB: the design patterns that make a schema performant in a distributed SQL environment.

## Two Modes

- **Design mode**: the user is designing or changing tables and indexes. Use the reference files
  below and the golden rules.
- **Review mode**: the user wants an *existing* database reviewed: live (you may connect) or offline
  (they have, or can produce, a schema dump / stats export / statements file). Read
  `references/review-mode.md` first. It covers the bundle, safety rules, the scripts, how to triage
  the analyzer's findings, and the rollout runbook. Never run DDL or `ANALYZE` against the user's
  database. Changes go into `changes.sql` (phased, with `rollback.sql`), rendered from the
  triaged `findings.json` so the SQL always matches the report.

## Core Concepts (Read First)

YugabyteDB is an index-organized, distributed SQL database. Every design decision flows from
three foundational facts:

1. **The primary key IS the table.** Rows are stored sorted by PK. No PK = system assigns `ybrowid` (hidden, hash-sharded).
2. **Default sharding is HASH.** Data is distributed by hashing the sharding key across tablets. Great for point lookups, bad for range queries.
3. **Tablets split automatically, but hotspots happen.** Sequential values (timestamps, auto-increment IDs) written to a range-sharded index concentrate all writes on the last tablet. Splitting doesn't fix it: after a split, the new last tablet becomes the hotspot.

## Reference Files

Read only the section(s) relevant to the user's question:

- `references/sharding.md` — Hash vs range, when to use each, choosing a sharding key
- `references/hotspots.md` — Preventing write/read hotspots on timestamps and monotonic PKs
- `references/index-design.md` — Index column ordering, covering indexes, redundant indexes
- `references/partial-indexes.md` — Partial indexes for NULL-heavy and skewed-value columns
- `references/low-cardinality.md` — Avoiding poor distribution from boolean/ENUM/low-distinct sharding keys
- `references/primary-keys.md` — PK selection, explicit PKs, composite PKs for partitioned tables
- `references/foreign-keys.md` — FK type alignment, mandatory FK indexes
- `references/review-mode.md` — Reviewing an existing database (live or offline): bundle, scripts, triage, rollout

## Scripts (review mode)

- `scripts/collect.sh` — read-only collector: schema dump, `stats.json`, per-node `usage/*.json` (TLS: `--sslrootcert`)
- `scripts/export-stats.sql` / `scripts/export-usage.sql` — the exports, runnable by hand with `ysqlsh`
- `scripts/analyze.py` — offline analyzer (stdlib Python): bundle or `schema.sql` → `findings.json`, `report.html` (`--format md` for `report.md`), `changes.sql`, `rollback.sql`
- `scripts/schema_sql.py` — parses `schema.sql` (ysql_dump or hand-written DDL) into the catalog, for schema-only review (`analyze.py schema.sql`)
- `scripts/render.py` — re-renders the report / `changes.sql` / `rollback.sql` from a triaged `findings.json`
- `scripts/checks/*.sql` — standalone live checks: `hash_on_range_type.sql`, `redundant_indexes.sql`

## Quick Decision Guide

| User's question | Go to |
|---|---|
| "Should I use hash or range sharding?" | `sharding.md` |
| "My timestamp writes are all going to one tablet" | `hotspots.md` |
| "How do I index this column?" | `index-design.md` |
| "Half my rows have NULL in this column" | `partial-indexes.md` |
| "Indexing a status/boolean column" | `low-cardinality.md` |
| "What should my primary key be?" | `primary-keys.md` |
| "FK performance is slow" | `foreign-keys.md` |
| "Can you design schema for this?" | `Refer all the files for the optimal design` |
| "Can you optimize this schema?" | `Refer all the files for the optimal design` |
| "Review / audit my database", "here is my schema dump / stats / statements.json" | `review-mode.md`, then the files each finding cites |
| "Which indexes are redundant / unused?" | `review-mode.md` + `index-design.md` |

## Golden Rules at a Glance

```
✅ Always define an explicit PRIMARY KEY (inline in CREATE TABLE; ADD PRIMARY KEY rewrites the table)
✅ Write HASH / ASC / DESC explicitly — the default depends on yb_use_hash_splitting_by_default
✅ Use ASC/DESC on index columns that are range-queried (pre-split new range indexes)
✅ Order index columns Equality → Sort → Range; a HASH leading column must be high-cardinality
✅ Index every foreign key column in child tables (a PK whose leading column is the FK counts)
✅ Use partial indexes for nullable or skewed columns, keyed on columns other than the predicate
✅ Match FK column types exactly (INT vs BIGINT matters)
✅ Drop redundant indexes (key prefix, wider index's HASH columns inside it, same predicate, uniqueness kept)
✅ Use TEXT wherever possible instead of VARCHAR(n) (no performance benefit; use a CHECK constraint if a limit is needed)

❌ Never use a low-cardinality column (boolean, enum, smallint code) as the only HASH column
❌ Never range-shard on a monotonically increasing column without a synthetic shard key
❌ Never leave tables without a PK if UNIQUE NOT NULL columns exist
❌ Never create multi-column GIN indexes
❌ Never rely on partial indexes for queries that bind the predicate value as a parameter
```
