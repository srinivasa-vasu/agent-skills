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

### Option A1: Add a Synthetic Range shard Key (Secondary Indexes)

Prefix the index with a range-sharded modulo-hash column and **pre-split** it into N physical
tablets using `SPLIT AT VALUES`. This gives physically even distribution from the moment the
index is created, with no `IN (...)` filter required in queries.

> ⚠️ **`SPLIT AT VALUES` is mandatory for A1.** Without it, all writes land on a single tablet
> until YugabyteDB auto-splits. Always declare N−1 split points.
> Formula: for `(yb_hash_code(col) % N) ASC`, split points are `(1), (2), ..., (N-1)`.

```sql
-- ✅ Distribute across 3 physical shards, still sorted by timestamp within each shard
CREATE INDEX idx_orders_created ON orders(
    (yb_hash_code(created_at) % 3) ASC,   -- range-sharded synthetic prefix
    created_at DESC
) SPLIT AT VALUES ((1), (2));              -- N=3 → N-1=2 split points → 3 tablets

-- Query does NOT need a shard key filter — the hidden shard prefix fans out automatically:
SELECT * FROM orders
WHERE created_at >= NOW() - INTERVAL '1 month';
```

Choose N based on expected write throughput (see table below). Keep N at a minimum equal to
the replication factor (typically 3). Physical shard boundaries cannot be changed manually
after creation, but tablets will auto-split as data grows. Because data is physically
distributed across N shards, distribution is more even. Prefer this option if the table's primary key is already HASH sharded.


### Option A2: Add a Synthetic Hash Shard Key (Secondary Indexes)

Prefix the index with a modulo-hash column to spread writes logically across N tablets, while keeping the timestamp as the clustering key for efficient range reads.

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

This approach uses a virtual hash-based logical shard key to distribute writes across N physical shards. Queries must include the shard key filter (all N values in an `IN (...)` clause) to fan out across all shards. As data is logically distributed across N shards, physical distribution may be slightly uneven. Prefer this option if the table's primary key is already RANGE sharded.

---

### A1 vs A2 Decision Rule

**If the table's primary key is already HASH sharded → always prefer A1 over A2.**

| Condition | Use |
|---|---|
| Table PK is HASH sharded | **A2** ✅ |
| Table PK is RANGE sharded | A1 |
| Want queries without `IN (...)` shard filter | **A2** ✅ |
| Need N > 9 logical shards | A1 |

### Option B: Add a `shard_id` Column (Primary Keys / Unique Constraints)

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

### Use HASH sharding for the PK (default), or use UUIDs

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
tables for small reference/lookup tables only. Colocation should be enabled at database or cluster level in order to colocate tables. Table definition needs to be suffixed with `WITH (COLOCATION = true|false)`. `WITH (COLOCATION = true)` is the default in a colocated database. To exclude a table from colocation, use `WITH (COLOCATION = false)` explicitly.

---

## Choosing the Right N for Shard Buckets

### A1 (RANGE sharded synthetic key + SPLIT AT VALUES) — N is fixed at creation; no IN filter needed

| Expected peak writes/sec | Recommended N |
|---|---|
| < 10,000 | 3 (minimum = replication factor) |
| > 10,000 | 6-12 (keep it in multiple of 3) |

N **cannot be reduced** after index creation (tablets only auto-split, never merge).
Choose conservatively — tablets will grow via auto-split as data increases.
Always declare exactly N−1 split points: `SPLIT AT VALUES ((1), (2), ..., (N-1))`.

### A2 (HASH sharded synthetic key) — N can be large; queries must enumerate all values

| Expected peak writes/sec | Recommended N |
|---|---|
| < 5,000 | 4–8 |
| 5,000–50,000 | 8–16 |
| > 50,000 | 16–64 |

A larger N gives better distribution but every query must include all N values in an
`IN (0, 1, ..., N-1)` filter. N can be changed by dropping and recreating the index.