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

-- ❌ Full index: all NULLs hash to ONE tablet → that tablet holds 60% of the index (hotspot + wasted storage)
CREATE INDEX idx_users_middle_name ON users(middle_name);

-- ✅ Partial index: only non-NULL rows indexed
CREATE INDEX idx_users_middle_name ON users(middle_name)
WHERE middle_name IS NOT NULL;

-- ✅ Partial index: only non-NULL rows indexed (with composite keys)
CREATE INDEX idx_users_middle_name ON users(middle_name, user_id)
WHERE middle_name IS NOT NULL;

-- ✅ If NULLs are sometimes queried, range shard so NULLs are distributed across tablets
CREATE INDEX idx_users_middle_name ON users(middle_name ASC);
-- physical key: (middle_name, base-table row key); with users PK (user_id HASH) the NULL
-- entries are ordered by hash(user_id) → spread across tablets as the index splits
```

**When to use partial vs range shard**:
- If NULLs are **never queried** → partial index (smaller, faster)
- If NULLs are **sometimes queried** but cause hotspots → range shard. Range splits use the full
  index key, which includes the base-table row key appended to every non-unique index entry, so
  NULLs spread across tablets as the index splits. This works when the table's PK is HASH sharded
  (the suffix starts with a hash code). With a monotonic range PK the NULL range gets a hot tail.
  See [low-cardinality.md](low-cardinality.md#why-range-sharding-works-here-the-hidden-pk-suffix).
  Add an explicit second column only if queries filter or sort on it.

> ⚠️ A new range-sharded index starts as **one tablet** and only spreads once auto-splitting
> kicks in. For large tables or heavy write rates, pre-split it with `SPLIT AT VALUES` (see
> [low-cardinality.md](low-cardinality.md)).

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

-- For 'login' queries specifically: a separate partial index for the dominant value,
-- keyed on a DIFFERENT, high-cardinality column that the login queries filter/sort on
CREATE INDEX idx_activity_login ON user_activity(user_id HASH, created_at DESC)
WHERE event_type = 'login';

-- ❌ Do NOT key the partial index on the predicate column itself:
CREATE INDEX idx_activity_login ON user_activity(event_type) WHERE event_type = 'login';
-- Every entry has the same key → one hash value → one tablet (the original hotspot again),
-- and the key carries no information the predicate doesn't already guarantee.
```

**Rule of thumb**: If any single value accounts for more than ~30% of rows in a column,
consider a partial index excluding that value (or a range-sharded index with a secondary column).

**Rule**: The key columns of a partial index should be columns *other than* those fixed by its
`WHERE` clause. The planner drops equality conditions already implied by the index predicate,
so a leading predicate column cannot be used to seek or route
([yugabyte-db#18888](https://github.com/yugabyte/yugabyte-db/issues/18888)).

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

## Gotcha: Partial Indexes and Prepared Statements

The planner uses a partial index only when it can prove the query's `WHERE` clause implies the
index predicate. With a **parameterized** filter on the predicate column, it usually can't:

```sql
CREATE INDEX idx_orders_active ON orders(customer_id, created_at DESC) WHERE status = 'active';

-- ✅ Uses the partial index: literal matches the predicate
SELECT * FROM orders WHERE status = 'active' AND customer_id = $1;

-- ❌ Generic plan cannot use it: $2 might not be 'active'
SELECT * FROM orders WHERE status = $2 AND customer_id = $1;
```

Custom plans (the first 5 executions of a prepared statement) can still use it; once the planner
switches to a generic plan, it can't. ORMs and JDBC/driver-side prepared statements commonly bind
every value. When recommending a partial index, confirm that the application's queries contain
the predicate value **as a literal** (or check the plan with `EXPLAIN EXECUTE` after 5+ executions).

---

## Summary: Choosing Between Partial Index and Range Sharding

| Situation | Recommended Approach |
|---|---|
| Concentrated value is **never queried** | Partial index (exclude it) |
| Concentrated value is **rarely queried** | Partial index on common values + rare ones |
| NULLs are **sometimes queried** | Range shard (`col ASC`); a HASH PK suffix spreads the NULLs |
| Value is skewed but **all values queried** | Range shard (`col ASC`) + `SPLIT AT VALUES`; needs a HASH PK, and no monotonic column ahead of the suffix |
| Multiple skewed values | Consider partial indexes per value, or range shard |
