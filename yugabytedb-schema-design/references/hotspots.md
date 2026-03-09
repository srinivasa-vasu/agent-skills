# Preventing Write/Read Hotspots in YugabyteDB

## What is a Hotspot?

A **hotspot** occurs when a disproportionate amount of reads or writes hit a single tablet.
This defeats the purpose of distributed SQL — one node gets overwhelmed while others sit idle.

**Most common cause**: A range-sharded index (or PK) on a **monotonically increasing column**
(timestamps, serial IDs). As new rows are inserted, they always go to the last tablet, which
becomes the "hot" tablet until it auto-splits. The next tablet then becomes the new hotspot.

---

## Pattern 1: Timestamp / Date Columns

### The Problem

```sql
-- ❌ WRONG: range-sharded on timestamp — all inserts hit one tablet
CREATE TABLE orders (
    order_id BIGINT,
    created_at TIMESTAMP,
    PRIMARY KEY (order_id HASH)
);
CREATE INDEX idx_orders_created ON orders(created_at DESC);
-- All new orders go to the "latest" tablet → hotspot
```

### Fix A1: Add a Synthetic Hash Shard Key (Secondary Indexes)

Prefix the index with a modulo-hash column to spread writes logically across N tablets, while keeping
the timestamp as the clustering key for efficient range reads.

```sql
-- ✅ Distribute the data logically into 'N' physical shards, still sorted by timestamp within each shard
CREATE INDEX idx_orders_created ON orders(
    (yb_hash_code(created_at) % 16) HASH,
    created_at DESC
);

-- Query must include the shard key filter (all values = full scan across all shards):
SELECT * FROM orders
WHERE yb_hash_code(created_at) % 16 IN (0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15)
  AND created_at >= NOW() - INTERVAL '1 month';
```

This approach utilizes a virtual hash-based logical shard key to distribute writes across "N" physical shards. While queries must include a shard key filter, this requirement effectively prevents hotspots. The mod value should be chosen based on expected write throughput, with 8–16 being typical for most workloads.
This approach uses a virual hash based shard key that distributes the writes across "N" physical shards. Queries must include the shard key filter, but this is a small price to pay for preventing hotspots.
As the data is logically distributed across N shards, this may have less even distribution in some physical shards.

### Fix A2: Add a Synthetic Range shard Key (Secondary Indexes)

Prefix the index with a modulo-hash column to spread writes physically across N tablets, while keeping
the timestamp as the clustering key for efficient range reads.

```sql
-- ✅ Distribute across 3 shards, still sorted by timestamp within each shard
CREATE INDEX idx_orders_created ON orders(
    (yb_hash_code(created_at) % 3) ASC,
    created_at DESC
) split at values((1), (2));

-- Query must include the shard key filter (all values = full scan across all shards):
SELECT * FROM orders
WHERE created_at >= NOW() - INTERVAL '1 month';
```

Choose N (number of physical shards) based on expected write throughput. 3–9 is typical for most workloads. Keep the minimum to the number of replication factor which will be typically 3. This approach is simpler for queries since the shard key is hidden. While physical shards cannot be changed manually after the initial setup, they will split automatically during runtime as data grows, providing flexibility for evolving workloads. Because data is physically distributed across N shards, this method ensures a more even distribution. Additionally, if the primary key of the table is already HASH sharded,prefer A2 over A1 for secondary indexes where possible.

### Fix B: Add a `shard_id` Column (Primary Keys / Unique Constraints)

When the timestamp is part of the PK or a unique key, add a physical `shard_id` column:

```sql
-- ❌ WRONG: monotonic PK → all inserts to one tablet
CREATE TABLE event_log (
    event_type TEXT,
    event_logged_at TIMESTAMP,
    PRIMARY KEY (event_logged_at DESC)
);

-- ✅ Add shard_id column that randomly assigns a bucket
CREATE TABLE event_log (
    event_type TEXT,
    shard_id INT DEFAULT (floor(random() * 100)::int % 16),
    event_logged_at TIMESTAMP,
    PRIMARY KEY (shard_id HASH, event_logged_at DESC)
);

-- Query:
SELECT * FROM event_log
WHERE shard_id IN (0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15)
  AND event_logged_at >= NOW() - INTERVAL '1 month';
```

---

## Pattern 2: Serial / Auto-Increment Primary Keys

### The Problem

```sql
-- ❌ WRONG: SERIAL PK with range sharding → sequential inserts = hotspot
CREATE TABLE users (
    user_id SERIAL,
    email TEXT,
    PRIMARY KEY (user_id ASC)   -- range-sharded, monotonically increasing
);
```

### Fix: Use HASH sharding for the PK (default), or use UUIDs

```sql
-- ✅ Option 1: HASH sharding on SERIAL (default behavior, no ASC/DESC needed)
CREATE TABLE users (
    user_id SERIAL,
    email TEXT,
    PRIMARY KEY (user_id)   -- HASH by default → distributed evenly
);

-- ✅ Option 2: UUID primary key (even better distribution)
CREATE TABLE users (
    user_id UUID DEFAULT gen_random_uuid(),
    email TEXT,
    PRIMARY KEY (user_id)
);
```

**Note**: UUID PKs with HASH sharding distribute extremely evenly and are the recommended
pattern for high-write tables where you don't need range scans on the PK.

---

## Pattern 3: Colocated Tables — Hotspots Don't Apply

If your table is **colocated**, all data lives on a single tablet by design. Hotspot
prevention is irrelevant — but so is horizontal scalability for that table. Use colocated
tables for small reference/lookup tables only.

---

## Choosing the Right N for Shard Buckets

| Expected peak writes/sec | Recommended N |
|---|---|
| < 1,000 | 4–8 |
| 1,000–10,000 | 8–16 |
| 10,000–100,000 | 16–64 |
| > 100,000 | 64+ or rethink schema |

A larger N gives better distribution but requires your queries to include all N values in the
`IN (...)` filter, slightly increasing query complexity.
