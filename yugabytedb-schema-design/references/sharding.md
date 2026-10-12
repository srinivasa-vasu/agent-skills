# Hash vs Range Sharding in YugabyteDB

## How Sharding Works

YugabyteDB distributes data across **tablets**. The sharding strategy determines how rows are
assigned to tablets based on the primary key (or index key).

| Strategy | How rows are assigned | Good for | Bad for |
|---|---|---|---|
| **HASH** (default) | Hash of key → tablet | Point lookups (`=`), even distribution | Range scans (`>`, `<`, `BETWEEN`, `ORDER BY`) |
| **RANGE** | Sorted order → tablet | Range scans, ordered reads | Sequential inserts (hotspot risk) |

## Declaring Sharding Strategy

```sql
-- HASH sharding (default — no keyword needed, but can be explicit)
CREATE TABLE orders (
    order_id BIGINT,
    customer_id INT,
    PRIMARY KEY (order_id HASH)
);

-- RANGE sharding on PK
CREATE TABLE events (
    event_id BIGINT,
    created_at TIMESTAMP,
    PRIMARY KEY (event_id ASC)   -- ASC or DESC forces range sharding
);

-- Composite: hash shard key + range clustering key
CREATE TABLE metrics (
    sensor_id INT,
    recorded_at TIMESTAMP,
    value NUMERIC,
    PRIMARY KEY (sensor_id HASH, recorded_at ASC)
    -- Distributes by sensor_id, then sorts by time within each shard
);
```

## When to Use HASH

Use HASH sharding when:
- Queries are predominantly point lookups by exact key value
- You want even data distribution across all tablets automatically
- The key is already high-cardinality (UUIDs, random IDs)
- You don't need to scan ranges or sort by the key

```sql
-- Good candidate for HASH: UUID primary key, point lookups
CREATE TABLE sessions (
    session_id UUID DEFAULT gen_random_uuid(),
    user_id INT,
    data JSONB,
    PRIMARY KEY (session_id)   -- HASH by default, UUID distributes well
);
```

## When to Use RANGE

Use RANGE sharding when:
- Queries filter with `>`, `<`, `BETWEEN`, or `ORDER BY` on the key
- You need time-series data accessed in chronological windows
- You're building a leaderboard, sorted feed, or paginated list by key

```sql
-- Good candidate for RANGE: time-series queried by date window
CREATE INDEX idx_orders_created ON orders(created_at ASC);
-- Now: WHERE created_at BETWEEN '2025-01-01' AND '2025-03-01' uses the index efficiently
```

## Enhanced PostgreSQL Compatibility Mode

In **Enhanced PostgreSQL Compatibility mode**, the default sharding strategy changes from HASH to RANGE. Check whether your cluster has this enabled before
deciding whether to explicitly specify `ASC`/`DESC`.

The underlying setting is `yb_use_hash_splitting_by_default` (`on` = HASH default, `off` = RANGE
default). With it `off`, a plain `PRIMARY KEY (id)` on a sequence/identity becomes `id ASC`, which
is a monotonic write hotspot.

```sql
SHOW yb_use_hash_splitting_by_default;

-- How keys were actually created: pg_get_indexdef() always spells out HASH / ASC / DESC
SELECT indexrelid::regclass, pg_get_indexdef(indexrelid)
FROM pg_index WHERE indrelid = 'orders'::regclass;

-- num_hash_key_columns = 0 → range-sharded (pass the TABLE oid for the PK, the index oid for secondary indexes)
SELECT * FROM yb_table_properties('orders'::regclass);
```

**Best practice**: always write `HASH` or `ASC`/`DESC` explicitly in DDL so the schema behaves
the same regardless of cluster defaults.

To audit a whole database, `scripts/checks/hash_on_range_type.sql` lists HASH key columns of
date/time types, including PKs. Don't use `yb_table_properties()` for this: it returns NULL for PK
indexes. Read the HASH bit from `pg_index.indoption` instead. For a full review, see
[review-mode.md](review-mode.md).

## Colocated Tables

Tables and indexes in a colocated database (or tablegroup) are **range-sharded by default**, since
all of them share a single tablet. HASH doesn't apply there. A table moved out of colocation keeps
its range keys, so re-check them for monotonic hotspots (see [hotspots.md](hotspots.md)).

## Composite Sharding Keys

For tables with multiple access patterns, use a composite key:

```sql
-- Distribute by tenant_id (hash), sort by created_at within each tenant (range)
CREATE TABLE tenant_events (
    tenant_id INT,
    created_at TIMESTAMP,
    event_type TEXT,
    payload JSONB,
    PRIMARY KEY (tenant_id HASH, created_at DESC)
);

-- Query for a single tenant's recent events — efficient: 
SELECT * FROM tenant_events
WHERE tenant_id = 42 AND created_at >= NOW() - INTERVAL '7 days';
```

This pattern distributes load across tenants while keeping each tenant's data sorted for fast range reads.
