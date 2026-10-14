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

This works because YugabyteDB can **skip scan** a range-sharded prefix: it seeks to each bucket
value and applies the `created_at` condition inside each one. (Skip scan is not available on a
partially specified HASH key, which is why A2 below needs the explicit `IN (...)` list.)

> ⚠️ **Global ordering**: rows are sorted by `created_at` only *within* each bucket. For
> `ORDER BY created_at DESC LIMIT n`, v2025.2.1+ can merge the buckets into a globally ordered
> result without query changes; on older versions expect a Sort over all matching rows. Verify
> with `EXPLAIN` for top-N / pagination queries.

### A1 inside a low-cardinality value

When a low-cardinality or skewed column leads the index and a monotonic column follows it, every
value gets its own write hot spot (`(status ASC, created_at ASC)`: new rows append at the end of
each status range; with `DESC`, at the start). Splitting doesn't help: the monotonic column sorts
before the primary-key suffix, so every new entry is newer than every split point. Put a bucket
**after** the leading column, so equality on it still seeks and each value's writes spread across
N buckets:

```sql
-- ❌ one write tail per status value
CREATE INDEX idx_orders_status_created ON orders(status ASC, created_at ASC);

-- ✅ bucket inside each status value (bucket on the primary key)
CREATE INDEX idx_orders_status_created ON orders(
    status ASC,
    (yb_hash_code(order_id) % 3) ASC,
    created_at ASC
);

-- WHERE status = 'pending' AND created_at > ... seeks to the status, then skip-scans the 3 buckets
```

No pre-split is needed here: once auto-split divides the index at `(status, bucket)` boundaries,
each bucket's writes go to their own tablet. (Splitting only on the status values would not help
on day one either: all buckets of a status would still share one tablet.)

**Bucket on the primary key, not on the timestamp.** `now()` is fixed for a whole transaction, so
rows inserted in one transaction share `created_at`: hashing it would put the whole batch in one
bucket. The primary key differs per row.

The same applies to booleans, small code columns, and a skewed tenant column with a modified
timestamp:

```sql
-- ❌ one tenant holds most rows: all its new/updated rows land at the start of its range
CREATE INDEX idx_docs_tenant_mod ON docs(tenant_id ASC, modified_date DESC);
-- ✅
CREATE INDEX idx_docs_tenant_mod ON docs(tenant_id ASC, (yb_hash_code(id) % 3) ASC, modified_date DESC);
```

Updates count as writes here: changing `modified_date` moves the index entry to the hot end.
`WHERE tenant_id = ? ORDER BY modified_date DESC LIMIT n` then merges N sorted streams (no Sort
from v2025.2.1). With many tenants and even writes, no bucket is needed: the hot spots spread.
See [low-cardinality.md](low-cardinality.md#why-range-sharding-works-here-the-hidden-pk-suffix).

### Choosing N

Choose N based on expected write throughput (see table below). Keep N at a minimum equal to
the replication factor (typically 3). The review analyzer (`scripts/analyze.py`) generates N =
number of nodes, clamped to 3–12; adjust it in `findings.json` before rendering `changes.sql`.
Physical shard boundaries cannot be changed manually after creation, but tablets will auto-split
as data grows. Because the split points sit exactly on the bucket boundaries, each bucket gets
its own tablet: the spread is even by construction, whatever the table's primary key is.


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

This approach uses a virtual hash-based logical shard key to distribute writes across N shards.
Queries must include the shard key filter (all N values in an `IN (...)` clause) to fan out
across all shards.

The spread is **not guaranteed to be even**. Each bucket value is hashed again into the 0–65535
hash space, and N hashed points don't land evenly across the tablets' equal hash ranges: some
tablets get two or three buckets, others none. Every row of a bucket shares one hash code, so a
heavy bucket can never split. Uneven placement is most pronounced at small N.

---

### A1 vs A2 Decision Rule

**Default to A1, whatever the table's primary key is.**

| | A1 (range bucket + `SPLIT AT VALUES`) | A2 (HASH bucket) |
|---|---|---|
| Write spread | Even: one pre-split tablet per bucket | Depends on where the N bucket hashes fall; can be uneven |
| Bucket growth | Each bucket keeps auto-splitting | A bucket is one hash code: never splits |
| Range queries | No `IN (...)` filter (skip scan over buckets) | Must list all N buckets in `IN (...)` |
| Large N (> ~12) | Many split points to declare | ✅ the reason to pick A2 |

The table's primary key type doesn't change this. In both patterns the indexed timestamp comes
**before** the primary-key suffix in the index key, so `(bucket, created_at)` decides where a new
entry lands, not the suffix. The suffix only matters for indexes with nothing monotonic ahead of
it, like `(status ASC)` (see [low-cardinality.md](low-cardinality.md#why-range-sharding-works-here-the-hidden-pk-suffix)).

> ⚠️ If the **table's own** primary key is a monotonic range key (sequence, identity, timestamp
> `ASC`), the table itself is the write hotspot. Neither A1 nor A2 fixes that: they only fix the
> index. Change the primary key (HASH it, or Option B below).

### Option B: Add a `shard_id` Column (Primary Keys / Unique Constraints)

When the timestamp is part of the PK or a unique key, add a physical `shard_id` column. A1/A2
don't work there: a PRIMARY KEY can't contain an expression like `yb_hash_code(col) % N`.

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
    shard_id INT DEFAULT floor(random() * 16)::int,   -- uniform over 0..15
    event_logged_at TIMESTAMP,
    PRIMARY KEY (shard_id HASH, event_logged_at DESC)
);

-- Query:
SELECT * FROM event_log
WHERE shard_id IN (0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15)
  AND event_logged_at >= NOW() - INTERVAL '1 month';
```

> ⚠️ Generate the bucket directly as `floor(random() * N)`. A pattern like
> `floor(random() * 100)::int % 16` is **not uniform** (buckets 0–3 get 7% of rows, 4–15 get 6%).
>
> `shard_id` has only N distinct hash values, so at most N tablets ever receive data for this
> table. This is an intentional low-cardinality hash key — choose N ≥ the tablet count you expect
> the table to grow to.

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

**Sequence notes**:
- Prefer `BIGINT GENERATED ... AS IDENTITY` / `BIGSERIAL` over `SERIAL` (`INT`, overflows at ~2.1B).
- YSQL caches sequence values per connection (`ysql_sequence_cache_minval`, default 100; a lower
  `CACHE` clause is overridden). Values are therefore **not ordered across connections and have
  gaps** — never use them for ordering or gap-free numbering.
- Every cache refill goes to the single tablet holding the sequence. With very high connection
  churn or multi-region apps, raise `CACHE` or use UUIDs.

---

## Pattern 3: Colocated Tables — Hotspots Don't Apply

If your table is **colocated**, all data lives on a single tablet by design. Hotspot
prevention is irrelevant — but so is horizontal scalability for that table. Use colocated
tables for small reference/lookup tables only. Colocation should be enabled at database or cluster level in order to colocate tables. Table definition needs to be suffixed with `WITH (COLOCATION = true|false)`. `WITH (COLOCATION = true)` is the default in a colocated database. To exclude a table from colocation, use `WITH (COLOCATION = false)` explicitly. It's recommended to explicitly specify `WITH (COLOCATION = true|false)` for tables.

Colocated tables and their indexes are **range-sharded by default** (`num_hash_key_columns = 0`
in `yb_table_properties()`), so a plain `PRIMARY KEY (id)` is `ASC`. If a table is later moved
out of colocation, re-check its keys for monotonic range hotspots.

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