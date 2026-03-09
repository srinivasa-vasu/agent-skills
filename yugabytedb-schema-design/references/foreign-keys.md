# Foreign Key Best Practices in YugabyteDB

## Rule 1: Always Index Foreign Key Columns in the Child Table

Without an index on the FK column in the child table, any DELETE or UPDATE on the parent
table requires a **full sequential scan** (and potential lock) of the entire child table to
check for referencing rows. In YugabyteDB this is especially costly — it may scan across
multiple tablets.

**The index must include all FK columns as leading columns** (exact order, any permutation,
or as a prefix of a composite index).

```sql
-- ❌ No FK index: DELETE FROM parent WHERE id = ? → full scan of child
CREATE TABLE parent (id INT PRIMARY KEY);
CREATE TABLE child (
    id INT,
    parent_id INT,
    CONSTRAINT fk_parent FOREIGN KEY (parent_id) REFERENCES parent(id)
);

-- ✅ Add index on the FK column
CREATE TABLE child (
    id INT,
    parent_id INT,
    CONSTRAINT fk_parent FOREIGN KEY (parent_id) REFERENCES parent(id)
);
CREATE INDEX idx_child_parent_id ON child(parent_id);
```

### Composite Foreign Keys

```sql
-- FK on (tenant_id, order_id) → index must have both as leading columns
CREATE TABLE order_items (
    item_id BIGINT,
    tenant_id INT,
    order_id BIGINT,
    quantity INT,
    CONSTRAINT fk_order FOREIGN KEY (tenant_id, order_id)
        REFERENCES orders(tenant_id, order_id)
);

-- ✅ Index covers both FK columns
CREATE INDEX idx_order_items_fk ON order_items(tenant_id, order_id);
```

---

## Rule 2: Match FK Column Data Types Exactly

When the referencing column (child) and referenced column (parent) have **different but
compatible** types (e.g., `INT` vs `BIGINT`), every FK check requires an implicit type cast.
At scale, this degrades INSERT and UPDATE performance significantly.

```sql
-- ❌ Type mismatch: child.parent_id is INT, parent.id is BIGINT
CREATE TABLE parent (id BIGINT PRIMARY KEY);
CREATE TABLE child (
    id INT,
    parent_id INT,   -- ← INT vs BIGINT → implicit cast on every FK check
    CONSTRAINT fk_parent FOREIGN KEY (parent_id) REFERENCES parent(id)
);

-- ✅ Align types exactly
CREATE TABLE child (
    id INT,
    parent_id BIGINT,  -- ← matches parent.id exactly
    CONSTRAINT fk_parent FOREIGN KEY (parent_id) REFERENCES parent(id)
);
```

**Common mismatches to watch for**:
- `INT` ↔ `BIGINT` (very common in migrated schemas)
- `VARCHAR(n)` ↔ `TEXT`
- `NUMERIC` ↔ `FLOAT`
- `SERIAL` (resolves to `INT`) ↔ `BIGSERIAL` (resolves to `BIGINT`)

---

## Rule 3: Consider FK Index Sharding for High-Write Child Tables

For child tables with very high insert rates, ensure the FK index itself is well-sharded:

```sql
-- High-write child table: order_items referencing orders
-- If most inserts reference a small set of hot orders, the FK index can hotspot

-- ✅ If parent_id is high-cardinality and evenly distributed: default HASH is fine
CREATE INDEX idx_items_order ON order_items(order_id);  -- HASH by default

-- ✅ If you also need to query items in order (e.g., list items for an order sorted by time):
CREATE INDEX idx_items_order ON order_items(order_id HASH, created_at DESC);
```

---

## FK Best Practices

- Every FK column (or composite FK column set) has an index in the child table
- FK column types match exactly between child and parent
- Composite FK indexes include all FK columns as leading columns
- High-write FK indexes use appropriate sharding (HASH for even distribution, or HASH+RANGE for both distribution and ordered reads)
- Nullable FK columns consider partial indexes (`WHERE fk_col IS NOT NULL`) if NULLs are common
