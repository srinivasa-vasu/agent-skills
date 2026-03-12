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

## When to Use

This skill activates when a user:

- Asks how to **design a YugabyteDB schema** or structure tables for distributed SQL
- Needs to **choose a sharding strategy** (hash vs range)
- Wants to **avoid write hotspots** on sequential columns
- Asks how to **optimize indexes** or handle NULL-heavy columns
- Mentions keywords like *"YSQL schema"*, *"tablet hotspot"*, *"sharding key"*
- Reports that their **YugabyteDB writes are slow or skewed**

## Golden Rules

### Do

- ✅ Always define an explicit `PRIMARY KEY`
- ✅ Use `ASC`/`DESC` on index columns that are range-queried
- ✅ Put high-cardinality columns first in multi-column indexes
- ✅ Index every foreign key column in child tables
- ✅ Use partial indexes for nullable or skewed columns
- ✅ Match FK column types exactly (`INT` vs `BIGINT` matters)
- ✅ Drop redundant indexes (prefix-covered by another index)
- ✅ Use `TEXT` datatype wherever possible instead of `VARCHAR(n)`

### Don't

- ❌ Never use a low-cardinality column as the sole sharding key
- ❌ Never range-shard on a monotonically increasing column without a synthetic shard key
- ❌ Never leave tables without a PK if `UNIQUE NOT NULL` columns exist
- ❌ Never create multi-column GIN indexes

## Project Structure

```
yugabytedb-schema-design/
├── SKILL.md              # Skill definition and quick-decision guide
├── README.md             # 
└── references/
    ├── sharding.md       # Hash vs range sharding strategies
    ├── hotspots.md       # Write/read hotspot prevention
    ├── index-design.md   # Index column ordering and covering indexes
    ├── partial-indexes.md# Partial indexes for sparse data
    ├── low-cardinality.md# Low-cardinality column indexing
    ├── primary-keys.md   # Primary key selection patterns
    └── foreign-keys.md   # Foreign key design and performance
```
