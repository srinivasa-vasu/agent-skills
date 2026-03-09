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

In **Enhanced PostgreSQL Compatibility mode**, the default sharding strategy for secondary
indexes changes from HASH to RANGE. Check whether your cluster has this enabled before
deciding whether to explicitly specify `ASC`/`DESC`.

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
