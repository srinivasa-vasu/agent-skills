# Low-Cardinality Column Index Design

## The Problem

YugabyteDB's default HASH sharding distributes rows across tablets by hashing the sharding
key. If the sharding key has very few distinct values, the hash function can only produce a
small number of distinct hash outputs — limiting data to a small number of tablets.

**Example**: A boolean column with 2 values. An ENUM with 5 values.

This creates both a distribution problem (uneven data) and a scalability ceiling.

**Low-cardinality columns** include:
- `BOOLEAN` (true/false)
- `ENUM` types with few values (status, category, day-of-week)
- Columns with only a handful of distinct values (region codes, priority levels)
- `SMALLINT` / `"char"` code columns (priority, type or state codes): the type allows many values,
  but these usually hold a handful. Confirm with `pg_stats.n_distinct` (after `ANALYZE`); the
  review analyzer flags a HASH-led `smallint` as a candidate when no statistics exist

---

## Single-Column Index on a Low-Cardinality Column

### Option 1: Combine with a high-cardinality column

Turn the single-column index into a multi-column index where the high-cardinality column
comes first (as the sharding key):

```sql
-- ❌ Single low-cardinality index: only 5 tablets max
CREATE INDEX idx_order_status ON orders(status);

-- ✅ Multi-column: customer_id shards evenly, status is just a clustering key
CREATE INDEX idx_order_status ON orders(customer_id HASH, status ASC);
-- Queries: WHERE customer_id = ? AND status = 'active' → efficient
```

Only valid if the queries actually filter on `customer_id`. This changes the access path: a
query with only `WHERE status = 'active'` cannot use this index (no skip scan on a HASH
column). If `status` alone is the filter, use Option 2 or a range-sharded index instead.

### Option 2: Use a partial index for each value

If each value has clearly distinct query patterns, create a partial index per value:

```sql
-- ✅ Partial indexes — each index is small and focused
CREATE INDEX idx_orders_active    ON orders(customer_id) WHERE status = 'active';
CREATE INDEX idx_orders_shipped   ON orders(customer_id) WHERE status = 'shipped';
CREATE INDEX idx_orders_cancelled ON orders(customer_id) WHERE status = 'cancelled';
```

---

## Multi-Column Index with Low-Cardinality as the First Column

When a low-cardinality column is the first (sharding) column of a multi-column index,
distribution is still poor because the hash space is constrained by the first column.

```sql
-- ❌ status is the sharding key → max 5 tablets regardless of order_id diversity
CREATE INDEX idx_order_status_id ON orders(status, order_id);

-- ✅ Option 1: Reorder — high-cardinality column first
CREATE INDEX idx_order_id_status ON orders(order_id HASH, status ASC);

-- ✅ Option 2: Force RANGE sharding on the combined value
CREATE INDEX idx_order_status_id ON orders(status ASC, order_id ASC)
SPLIT AT VALUES (('cancelled'), ('pending'), ('shipped'));   -- 4 values → 3 split points → 4 tablets
-- Range sharding splits on the full key, which includes the base-table row key suffix, so even a
-- low-cardinality status distributes across tablets. Works well if the table's PK is HASH sharded
-- (see "the hidden PK suffix" below).
```

### Why range sharding works here: the hidden PK suffix

A non-unique secondary index entry is physically keyed on **(indexed columns…, base-table row
key)**. YugabyteDB appends the table's row key (`ybctid`) to the declared index columns. Range
splits are computed on this **entire** key, suffix included.

When the table's PK is **HASH-sharded**, its row key starts with the 16-bit hash code. Within a
single `status` value, index entries are therefore ordered by hash, i.e. effectively at random:

```sql
-- orders PK: (order_id HASH)
CREATE INDEX idx_order_status ON orders(status ASC)
SPLIT AT VALUES (('cancelled'), ('pending'), ('shipped'));
-- physical key: (status, hash(order_id), order_id)
-- new 'pending' rows land at random positions inside the 'pending' range → after splits,
-- writes for even a single dominant value spread across many tablets
```

This is why "works well if the PK is hash sharded" keeps appearing in this skill. Adding the
PK column explicitly (`(status ASC, order_id ASC)`) is not needed for distribution. Add it only
if queries filter or sort on it.

**What breaks the suffix trick** — anything that makes the key *before* the suffix monotonic,
or the suffix itself monotonic:

| Index / table | Where new entries land within one `status` value | Result |
|---|---|---|
| `(status ASC)`, PK `HASH` | random (hash suffix) | ✅ spreads after splits |
| `(status ASC)`, PK `ASC` on a sequence/timestamp | tail (monotonic suffix) | ❌ one hot tail per value |
| `(status ASC, created_at ASC)`, any PK | tail (`created_at` precedes the suffix) | ❌ one hot tail per value |
| `UNIQUE (status, …)` | the row key is not part of a unique index's key | suffix trick doesn't apply |

With a hot tail per value you get at most K write hot spots for K distinct values. That's fine
when new rows spread evenly across values and K ≥ node count; it's a hotspot when most new rows
share one value (e.g. every new order is `'pending'`). Fixes: drop the monotonic column from the
key if nothing sorts or filters on it (and rely on a HASH PK suffix), or bucket the monotonic
column **inside** each value: `(status ASC, (yb_hash_code(order_id) % N) ASC, created_at ASC)`,
bucketing on the primary key (a batch inserted in one transaction shares `now()`)
([A1 inside a low-cardinality value](hotspots.md#a1-inside-a-low-cardinality-value)). Equality on
`status` still seeks, and each value's new rows spread across N buckets.

**Pre-split range indexes.** A new range-sharded index starts as **one tablet**. The suffix only
helps after auto-splitting kicks in. Split points can be a prefix of the key, so splitting on
the known low-cardinality values (every value except the smallest, sorted in the column's order —
for an ENUM, its declared order) gives one tablet per value from day one. Later splits happen on
the full key (suffix included), so a large value range keeps splitting further.

---

## Practical Examples by Column Type

### ENUM / Status Column

```sql
CREATE TYPE order_status AS ENUM ('pending', 'active', 'shipped', 'delivered', 'cancelled');

-- ❌ Poor distribution
CREATE INDEX idx_status ON orders(status);

-- ✅ Reorder: hash on high-cardinality customer_id
CREATE INDEX idx_status ON orders(customer_id HASH, status);

-- ✅ Or range-shard the composite (pre-split on the enum values, in declared enum order)
CREATE INDEX idx_status ON orders(status ASC, order_id ASC)
SPLIT AT VALUES (('active'), ('shipped'), ('delivered'), ('cancelled'));
```

### Boolean Column

```sql
-- ❌ Only 2 possible hash values
CREATE INDEX idx_active ON users(is_active);

-- ✅ Use a partial index instead (usually the right answer for booleans)
CREATE INDEX idx_active_users ON users(created_at DESC) WHERE is_active = true;
-- Only active users indexed; sorted by recency for common "recent active users" queries. Also follow the hotspot prevention patterns if necessary.

-- ✅ If both values are queried: range-shard and pre-split on the boolean
CREATE INDEX idx_active ON users(is_active ASC) SPLIT AT VALUES (('true'));
-- false < true: one tablet per value from day one; a HASH PK suffix spreads each value further.
-- With a monotonic column after it, bucket on the PK: (is_active ASC, (yb_hash_code(user_id) % 3) ASC, created_at ASC)

-- ❌ Don't "fix" it by prefixing the PK column:
CREATE INDEX idx_active ON users(user_id HASH, is_active);
-- WHERE is_active = true can't use it (no skip scan on HASH), and WHERE user_id = ?
-- is already served by the PK → a redundant index that only adds write cost.
```

If the boolean is queried for a large fraction of rows (e.g. 50% active), no index on it helps;
a sequential scan with the filter pushed down to storage is usually faster. Index it only
together with the columns the query actually filters or sorts on.

### Day-of-Week / Hour-of-Day

```sql
-- ❌ Only 7 distinct values → 7 tablets max
CREATE INDEX idx_dow ON schedule(day_of_week);

-- ✅ Range shard combined with another column
CREATE INDEX idx_dow ON schedule(day_of_week ASC, start_time ASC);
-- Range sharding on (day_of_week, start_time) allows efficient time-range queries per day. Also follow the hotspot prevention patterns if necessary.
```

---

## Summary Decision Tree

| Low-Cardinality? | All Distinct Values Queried? | High-Cardinality Pairing Column? | Recommendation |
|---|---|---|---|
| Yes | Yes | Yes | Reorder: high-cardinality column first |
| Yes | Yes | No | Force range sharding with a high cardinality composite column key |
| Yes | No (some values never/rarely queried) | — | Use partial indexes, one per queried value |
| No | — | — | Standard index design (see [index-design.md](index-design.md)) |
