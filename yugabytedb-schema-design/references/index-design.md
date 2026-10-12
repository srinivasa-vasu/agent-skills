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

**Rule**: Order key columns **Equality → Sort → Range** (ESR), driven by the queries:
1. Columns filtered with `=` / `IN` first
2. Then columns used in `ORDER BY` (so rows come back pre-sorted, enabling `LIMIT` push-down)
3. Then columns filtered with `<`, `>`, `BETWEEN`

The first column is also the sharding key. If it is HASH, it must be **high cardinality** and
always present as an equality in the queries that use the index (a HASH column without an
equality condition makes the index unusable for seeking). A HASH column like `tenant_id` with
modest cardinality is fine; a boolean or status is not (see [low-cardinality.md](low-cardinality.md)).

```sql
-- ❌ Low-cardinality first → poor distribution
CREATE INDEX idx ON orders(status, customer_id);

-- ✅ High-cardinality equality column first → even distribution
CREATE INDEX idx ON orders(customer_id, status);
-- customer_id may have millions of values → many tablets used

-- ✅ ESR for: WHERE customer_id = ? AND total > 100 ORDER BY created_at DESC LIMIT 20
CREATE INDEX idx ON orders(customer_id HASH, created_at DESC, total ASC);
```

With the default `yb_use_hash_splitting_by_default = on`, an unqualified `CREATE INDEX ON t(a, b)`
means `(a HASH, b ASC)`. Spell out `HASH`/`ASC`/`DESC` explicitly so the DDL means the same thing
in every cluster (see [sharding.md](sharding.md#enhanced-postgresql-compatibility-mode)).

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

Redundant indexes waste storage and add write overhead (every INSERT/UPDATE writes every index,
often on another tablet) for zero read benefit. In YugabyteDB the prefix test must respect
**sharding**, not just column order.

Index A is redundant with Index B (or the PK) when **all** of the following hold:

1. **Key prefix**: A's key columns are a leading prefix of B's key columns, with the same
   expressions, opclass and collation.
2. **Compatible sharding per column**: each A column has the same HASH/ASC/DESC (and NULLS order)
   in B, or A has HASH where B has ASC/DESC (a range column also serves equality, but data
   distribution changes, see i3/i4 below).
3. **B's HASH columns all lie inside A's key**: `((a, b) HASH)` needs equality on both, so it
   never covers `(a HASH)`.
4. **Same predicate**: B has no `WHERE`, or the identical one.
5. **Covering**: A's `INCLUDE` columns ⊆ B's remaining key + `INCLUDE` columns. The **primary key
   covers every column**, because the table is stored in it.
6. **Uniqueness preserved**: A is not UNIQUE, or B is UNIQUE on exactly the same key
   (`UNIQUE (a)` is stronger than `UNIQUE (a, b)`) with the same `NULLS NOT DISTINCT`. A must
   not back a constraint (PK, UNIQUE constraint, exclusion, or the index an FK references).

A fully reversed index (`a DESC` vs `a ASC`) can serve the same scans backward. Treat it as a
manual judgment call; the review tooling doesn't report it.

The rule is implemented in `scripts/checks/redundant_indexes.sql` (live, ad hoc) and in
`scripts/analyze.py` (bundle review). See [review-mode.md](review-mode.md).

```sql
-- ❌ idx_orders_order_id is redundant — same hash column, covered by the composite index
CREATE INDEX idx_orders_order_id ON orders(order_id HASH);
CREATE INDEX idx_orders_order_id_product ON orders(order_id HASH, product_id ASC);

-- ✅ Drop the weaker one
DROP INDEX idx_orders_order_id;
-- The composite index handles all queries that used the single-column index
```

**Not redundant** — these look like prefixes but serve different queries:

```sql
-- Different hash key: ((a, b) HASH) needs equality on BOTH a and b; it can't serve WHERE a = ?
CREATE INDEX i1 ON t(a HASH);
CREATE INDEX i2 ON t((a, b) HASH);

-- Different sharding: HASH serves a = ?; ASC also serves a > ?, ORDER BY a
CREATE INDEX i3 ON t(a HASH);
CREATE INDEX i4 ON t(a ASC);
-- i4 answers everything i3 does, so i3 is reported as redundant (note: "HASH replaced by
-- range"). Keep just one, chosen by workload. If a is monotonic, keep i3 and drop or fix i4
-- instead: i4 is a write hotspot.

-- Narrower UNIQUE: i8 doesn't enforce what i7 does
CREATE UNIQUE INDEX i7 ON t(a);
CREATE UNIQUE INDEX i8 ON t(a, b);

-- Different partial predicates
CREATE INDEX i5 ON t(a) WHERE status = 'active';
CREATE INDEX i6 ON t(a, b);
```

**Redundant with the primary key** — commonly missed:

```sql
-- PK (order_id HASH, item_id ASC) already serves WHERE order_id = ? (and the FK check)
CREATE INDEX idx_items_order ON order_items(order_id);   -- ❌ redundant with the PK
CREATE UNIQUE INDEX idx_items_pk ON order_items(order_id, item_id);   -- ❌ duplicates the PK
```

**Check rule**: If every query that uses Index A would be equally or better served by Index B,
drop Index A. Before dropping, check for `pg_hint_plan` hints or application code that name
the index.

**Unused ≠ redundant.** An index with `idx_scan = 0` in `pg_stat_user_indexes` is a separate
finding. In YugabyteDB those counters are **per node**: sum them across all nodes
(`yb_servers()`), and check how long stats have been accumulating before recommending a drop.

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
