# Primary Key Design in YugabyteDB

## Why Primary Keys Are Critical

In YugabyteDB, **the primary key IS the table**. Rows are stored as a sorted, distributed
index organized by the PK. This means:

- PK choice determines data distribution across tablets
- PK determines row sort order within tablets (for range PKs)
- Without an explicit PK, YugabyteDB assigns a hidden `ybrowid` — a UUID-like column with
  HASH sharding. This wastes storage and prevents meaningful clustering.

**Always define an explicit primary key.**

---

## Choosing the Right Primary Key

### Rule 1: Prefer High-Cardinality Keys

A PK with many distinct values distributes data evenly across tablets.

```sql
-- ✅ UUID: extremely high cardinality, even hash distribution
CREATE TABLE users (
    user_id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    email TEXT NOT NULL
);

-- ✅ BIGSERIAL with HASH (default): distributes evenly even though sequential
CREATE TABLE orders (
    order_id BIGSERIAL PRIMARY KEY,  -- HASH by default
    customer_id INT
);
```

### Rule 2: Match the PK to the Most Common Access Pattern. Prefer business key over surrogate

```sql
-- If you almost always query by email:
CREATE TABLE users (
    email TEXT PRIMARY KEY,       -- email IS the PK
    user_id UUID DEFAULT gen_random_uuid(),
    name TEXT
);

-- If you query by both user_id (exact) and email (exact), make one the PK
-- and put a unique index on the other:
CREATE TABLE users (
    user_id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT
);
```

### Rule 3: Don't Range-Shard a Monotonically Increasing PK

```sql
-- ❌ Hotspot: all new rows go to the last tablet
CREATE TABLE events (
    event_id BIGSERIAL,
    created_at TIMESTAMP,
    PRIMARY KEY (event_id ASC)  -- range-sharded SERIAL = hotspot
);

-- ✅ Hash-shard the SERIAL (the default — just don't add ASC/DESC)
CREATE TABLE events (
    event_id BIGSERIAL PRIMARY KEY,  -- HASH sharded → distributed evenly
    created_at TIMESTAMP
);

-- ✅ Or use UUID for truly random distribution
CREATE TABLE events (
    event_id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    created_at TIMESTAMP
);
```

Note: If you need to support range queries on a monotonically increasing column, follow the hotspot prevention patterns outlined in `hotspots.md`.

---

## Composite Primary Keys

Use composite PKs when rows are most often accessed by a combination of columns, or when
you want to co-locate related rows on the same tablet.

```sql
-- Tenant + resource pattern: co-locate each tenant's data
CREATE TABLE tenant_documents (
    tenant_id INT,
    doc_id BIGINT,
    content TEXT,
    PRIMARY KEY (tenant_id HASH, doc_id ASC)
    -- All of tenant 42's documents → same tablet set, sorted by doc_id
);

-- Time-series per device: distribute by device, sort by time
CREATE TABLE sensor_readings (
    device_id UUID,
    recorded_at TIMESTAMP,
    temperature NUMERIC,
    PRIMARY KEY (device_id HASH, recorded_at DESC)
);
```

---

## Partitioned Tables: PK Must Be Defined in CREATE TABLE

For partitioned tables, you **cannot** add a PK with `ALTER TABLE` after creation (fixed in
v2024.1+, but best practice is always to define it inline):

```sql
-- ❌ May fail on older versions
CREATE TABLE sales (id INT, region TEXT NOT NULL) PARTITION BY LIST (region);
ALTER TABLE sales ADD CONSTRAINT sales_pkey PRIMARY KEY (id, region);

-- ✅ Always define PK in CREATE TABLE for partitioned tables
CREATE TABLE sales (
    id INT,
    region TEXT NOT NULL,
    PRIMARY KEY (id, region)
) PARTITION BY LIST (region);
```

---

## Promoting UNIQUE NOT NULL Columns to Primary Key

If a table has UNIQUE NOT NULL columns but no explicit PK, YugabyteDB assigns `ybrowid`.
This means you have both:
1. The hidden `ybrowid` index structure (main table)
2. An additional unique index structure for the UNIQUE constraint

This is redundant. Promote one of the UNIQUE NOT NULL columns to be the PK:

```sql
-- ❌ Wastes storage: ybrowid PK + 2 unique indexes
CREATE TABLE users (
    user_id INT NOT NULL,
    email TEXT NOT NULL,
    CONSTRAINT users_email_unique UNIQUE (email),
    CONSTRAINT users_user_id_unique UNIQUE (user_id)
);

-- ✅ user_id becomes the PK (replaces ybrowid), email still has its unique index
CREATE TABLE users (
    user_id INT NOT NULL,
    email TEXT NOT NULL,
    PRIMARY KEY (user_id),
    CONSTRAINT users_email_unique UNIQUE (email)
);
```
