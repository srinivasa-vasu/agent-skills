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

## Rule 3: Shard Child Tables by the Parent's Primary Key

By using the parent's primary key as the child
table's sharding key, you ensure that **all child rows belonging to the same parent are
grouped in the same shard**. This means queries like "fetch all items for an order" hit
a **single tablet** instead of scattering across many.

Use the parent's PK column as the **first (sharding) column** in the child table's composite
primary key.

```sql
-- Parent table: sharded by order_id
CREATE TABLE orders (
    order_id UUID DEFAULT gen_random_uuid(),
    customer_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (order_id HASH)
);

-- ✅ Child table: sharded by the SAME column (order_id) as the parent
--    All items for a given order land in the same tablet of the order_items table
CREATE TABLE order_items (
    item_id BIGINT GENERATED ALWAYS AS IDENTITY,
    order_id UUID NOT NULL,
    product_id BIGINT NOT NULL,
    quantity INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (order_id HASH, item_id ASC),
    CONSTRAINT fk_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
```

**Why this works:**

- All `order_items` rows for a given `order_id` hash to the **same tablet** — reads and writes for one order's items are single-shard operations
- `SELECT * FROM order_items WHERE order_id = ?` is served by **one tablet**, not scattered across many
- Batch inserts for items of the same order go to a **single shard**, reducing cross-node coordination

**Anti-pattern — sharding child by its own key:**

```sql
-- ❌ Child sharded on item_id: items for the SAME order scatter across DIFFERENT tablets
CREATE TABLE order_items (
    item_id BIGINT GENERATED ALWAYS AS IDENTITY,
    order_id UUID NOT NULL,
    PRIMARY KEY (item_id HASH),  -- shards by item_id, not order_id
    CONSTRAINT fk_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
-- "Get all items for order X" fans out to ALL tablets
-- Inserts for a single order's items spread across multiple shards
```

---

## Summary

- Every FK column (or composite FK column set) has an index in the child table
- FK column types match exactly between child and parent
- Composite FK indexes include all FK columns as leading columns
- Shard child tables by the parent's PK so all child rows for a given parent are in a single shard
- Nullable FK columns consider partial indexes (`WHERE fk_col IS NOT NULL`) if NULLs are common
