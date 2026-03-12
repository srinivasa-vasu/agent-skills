# Index Design Best Practices for YugabyteDB

## Three Column Roles in a YugabyteDB Index

Every secondary index has three types of columns:

| Role | Purpose | How to specify |
|---|---|---|
| **Sharding key** | Determines which tablet a row goes to | First column(s), `HASH` or `ASC/DESC` |
| **Clustering key** | Sort order within each tablet | `ASC` or `DESC` suffix |
| **Covering columns** | Included for index-only scans, no lookup needed | `INCLUDE (col1, col2)` |

```sql
-- Full example: shard by customer_id, sort by order_date, cover status
CREATE INDEX idx_orders_customer ON orders(
    customer_id HASH,      -- sharding key
    order_date DESC        -- clustering key
) INCLUDE (status, total); -- covering columns
```

---

## Column Ordering in Multi-Column Indexes

**Rule**: The first column is the sharding key. It should be:
1. High cardinality (many distinct values)
2. Frequently used in equality filters (`WHERE col = ?`)

```sql
-- ❌ Low-cardinality first → poor distribution
CREATE INDEX idx ON orders(status, customer_id);

-- ✅ High-cardinality first → even distribution
CREATE INDEX idx ON orders(customer_id, status);
-- customer_id may have millions of values → many tablets used
```

## Range vs Hash for Specific Columns

```sql
-- For columns used in range queries (dates, prices, scores):
CREATE INDEX idx_orders_date ON orders(order_date ASC);   -- RANGE sharded
CREATE INDEX idx_products_price ON products(price DESC);  -- RANGE sharded

-- For columns used only in equality lookups (IDs, enum codes):
CREATE INDEX idx_orders_customer ON orders(customer_id);  -- HASH sharded (default)
```

Note: For range queries, the `ASC`/`DESC` suffix forces RANGE sharding, which is optimized for range scans but can lead to hotspots if the column is monotonically increasing. Apply the hotspot prevention patterns if using RANGE sharding on such columns.

---

## Covering Indexes (Index-Only Scans)

Add frequently-SELECTed columns to the index with `INCLUDE` to avoid going back to the main table.

```sql
-- ❌ Without covering: index lookup + table fetch for every row
CREATE INDEX idx_orders_customer ON orders(customer_id);
SELECT customer_id, status, total FROM orders WHERE customer_id = 42;
-- → hits index, then fetches status + total from main table

-- ✅ With covering: index-only scan, no main table access
CREATE INDEX idx_orders_customer ON orders(customer_id) INCLUDE (status, total);
SELECT customer_id, status, total FROM orders WHERE customer_id = 42;
-- → satisfied entirely from the index
```

Use `INCLUDE` for columns that are:
- Frequently in SELECT but not in WHERE/ORDER BY
- Not useful as sharding/clustering keys themselves

---

## Redundant Indexes

An index is **redundant** if its key columns are a leading prefix of another index's key columns.
Redundant indexes waste storage and add write overhead for zero read benefit.

```sql
-- ❌ idx_orders_order_id is redundant — covered by the composite index
CREATE INDEX idx_orders_order_id ON orders(order_id);
CREATE INDEX idx_orders_order_id_product ON orders(order_id, product_id);

-- ✅ Drop the weaker one
DROP INDEX idx_orders_order_id;
-- The composite index handles all queries that used the single-column index
```

**Check rule**: If every query that uses Index A would be equally or better served by Index B,
drop Index A.

---

## Supported Index Methods

| Method | Supported | Use Case |
|---|---|---|
| `btree` (lsm, default) | ✅ Yes | Equality, range, ordering. This is replaced with LSM |
| `gin` (ybgin) | ✅ Yes (single column only) | JSONB containment, array overlap, full-text |
| `hash` | ✅ Yes | Equality only |
| `gist` | ❌ No | Geometry, ranges (use trigger workaround) |
| `brin` | ❌ No | Large sequential tables |
| `spgist` | ❌ No | Space-partitioned data |

**GIN limitation**: Multi-column GIN indexes are not supported. Use one GIN index per column.

```sql
-- ❌ Fails: multi-column GIN
CREATE INDEX gin_multi ON docs USING gin(tags, metadata);

-- ✅ Fix: separate GIN indexes
CREATE INDEX gin_tags ON docs USING gin(tags);
CREATE INDEX gin_metadata ON docs USING gin(metadata);
```

---
