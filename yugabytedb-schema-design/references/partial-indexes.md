# Partial Indexes for Nullable and Skewed Columns

## Why Partial Indexes Matter in YugabyteDB

In a HASH-sharded index, all rows with the same key value are stored in the same tablet.
If a column has a **disproportionate concentration** of one value (NULLs or a dominant value),
all those rows pile up on one tablet — creating a hotspot and wasting index space on rows
that are never queried.

A **partial index** indexes only the rows matching a `WHERE` clause, solving both problems:
- Hotspot avoided (concentrated values aren't indexed)
- Smaller index (fewer rows → faster reads and writes)

---

## Case 1: High-NULL Columns

When a column has many NULLs and queries never need to find NULL rows, exclude NULLs
from the index entirely. Follow this for nullable columns.

```sql
-- Scenario: users.middle_name is NULL for ~60% of rows
-- Queries only look up users with a known middle name

-- ❌ Full index: 60% of tablets store NULLs → hotspot + wasted storage
CREATE INDEX idx_users_middle_name ON users(middle_name);

-- ✅ Partial index: only non-NULL rows indexed
CREATE INDEX idx_users_middle_name ON users(middle_name)
WHERE middle_name IS NOT NULL;

-- ✅ Partial index: only non-NULL rows indexed (with composite keys)
CREATE INDEX idx_users_middle_name ON users(middle_name, user_id)
WHERE middle_name IS NOT NULL;

-- ✅ If NULLs are sometimes queried, range shard with a second column to distribute NULLs across tablets
CREATE INDEX idx_users_middle_name ON users(middle_name ASC, user_id);
```

**When to use partial vs range shard**:
- If NULLs are **never queried** → partial index (smaller, faster)
- If NULLs are **sometimes queried** but cause hotspots → range shard with a second column so that NULLs are distributed across tablets during tablet splits (as range sharding splits based on the combined value of the columns). This works well if the primary key of the table is hash sharded.

---

## Case 2: Dominant Single Value (Skewed Distribution)

When one value makes up a large percentage of rows (e.g., 80% of `event_type = 'login'`),
HASH sharding concentrates 80% of index data on one tablet.

```sql
-- Scenario: user_activity.event_type is 'login' for 80% of rows
-- Queries filtering on 'login' are rare; other event types are queried frequently

-- ❌ Full index: 80% of index on one tablet
CREATE INDEX idx_activity_event ON user_activity(event_type);

-- ✅ Partial index: exclude the dominant value
CREATE INDEX idx_activity_event ON user_activity(event_type)
WHERE event_type <> 'login';
-- Now this index only covers the 20% of rows with non-login events

-- For 'login' queries specifically: use a separate partial index for the dominant value
CREATE INDEX idx_activity_login ON user_activity(event_type)
WHERE event_type = 'login';
```

**Rule of thumb**: If any single value accounts for more than ~30% of rows in a column,
consider a partial index excluding that value (or a range-sharded index with a secondary column).

---

## Case 3: Soft-Delete / Status Columns

A very common pattern: `deleted_at IS NULL` or `is_active = true` covers 95%+ of queries.

```sql
-- Scenario: orders table with soft-delete, almost all queries are on active orders

-- ❌ Full index on status includes huge amounts of 'completed'/'cancelled' orders
CREATE INDEX idx_orders_status ON orders(status);

-- ✅ Partial index: only active orders
CREATE INDEX idx_orders_active ON orders(customer_id, created_at DESC)
WHERE status = 'active';
-- Small, fast, never includes historical noise
```

---

## Case 4: Nullable Foreign Keys

When a FK column is optional (nullable) and queries only join on non-NULL values:

```sql
-- Scenario: items.assigned_to_user_id is NULL when unassigned (50% of rows)

-- ❌ Full index: half the index is NULLs
CREATE INDEX idx_items_assigned ON items(assigned_to_user_id);

-- ✅ Partial index: only assigned items
CREATE INDEX idx_items_assigned ON items(assigned_to_user_id)
WHERE assigned_to_user_id IS NOT NULL;
```

---

## Summary: Choosing Between Partial Index and Range Sharding

| Situation | Recommended Approach |
|---|---|
| Concentrated value is **never queried** | Partial index (exclude it) |
| Concentrated value is **rarely queried** | Partial index on common values + rare ones |
| NULLs are **sometimes queried** | Range shard with additional high-cardinality column |
| Value is skewed but **all values queried** | Range shard (`col ASC, id`) to distribute evenly |
| Multiple skewed values | Consider partial indexes per value, or range shard |
