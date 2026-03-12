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
CREATE INDEX idx_order_status_id ON orders(status ASC, order_id ASC);
-- Range sharding distributes based on combined sorted value during tablet splits, so even if status has low cardinality, the order_id component allows for good distribution across tablets. Works well if the primary key of the table is hash sharded.
```

---

## Practical Examples by Column Type

### ENUM / Status Column

```sql
CREATE TYPE order_status AS ENUM ('pending', 'active', 'shipped', 'delivered', 'cancelled');

-- ❌ Poor distribution
CREATE INDEX idx_status ON orders(status);

-- ✅ Reorder: hash on high-cardinality customer_id
CREATE INDEX idx_status ON orders(customer_id HASH, status);

-- ✅ Or range-shard the composite
CREATE INDEX idx_status ON orders(status ASC, order_id ASC);
```

### Boolean Column

```sql
-- ❌ Only 2 possible hash values
CREATE INDEX idx_active ON users(is_active);

-- ✅ Use a partial index instead (usually the right answer for booleans)
CREATE INDEX idx_active_users ON users(created_at DESC) WHERE is_active = true;
-- Only active users indexed; sorted by recency for common "recent active users" queries. Also follow the hotspot prevention patterns if necessary.

-- ✅ Or combine with a high-cardinality column
CREATE INDEX idx_active ON users(user_id HASH, is_active);
```

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
