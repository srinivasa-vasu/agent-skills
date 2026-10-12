/*
 * Redundant (overshadowed) secondary indexes.
 *
 * Index R is redundant with respect to index E (same table) when E can serve every
 * lookup R serves and enforces at least what R enforces:
 *
 *  1. Both are valid LSM indexes (GIN / vector indexes are excluded).
 *  2. R's key columns are a prefix of E's key columns, with the same opclass and
 *     collation.
 *  3. Each R key column has the same HASH/ASC/DESC + NULLS option as in E, or R has
 *     HASH where E has ASC/DESC (a range column also serves equality).
 *  4. E's HASH columns all lie inside R's key: ((a, b) HASH) can't serve
 *     WHERE a = ?, so it does not cover (a HASH).
 *  5. E has no predicate, or the same predicate as R.
 *  6. R's INCLUDE columns are in E's remaining columns. A primary key covers every
 *     column: the YugabyteDB table is stored in the PK.
 *  7. R is not unique, or E is unique on exactly the same key (UNIQUE (a) is
 *     stronger than UNIQUE (a, b)) and NULLS NOT DISTINCT is preserved.
 *  8. R backs no constraint (PK / UNIQUE / EXCLUDE, or the index an FK references).
 *
 * Exact duplicates are reported once (the alphabetically larger name).
 * Partition child indexes are skipped; the partitioned parent index is reported.
 *
 * Conservative: a fully reversed index (a DESC vs a ASC) can serve the same scans
 * backwards but is not reported. Before dropping, check hints that name the index
 * and per-node usage (see references/index-design.md).
 *
 * Read-only. Works on PG11-based and PG15-based YSQL.
 */
WITH idx AS (
  SELECT
    ix.indexrelid,
    ix.indrelid,
    n.nspname                                   AS schema_name,
    t.relname                                   AS table_name,
    i.relname                                   AS index_name,
    i.relispartition                            AS is_partition,
    ix.indisunique                              AS is_unique,
    ix.indisprimary                             AS is_pk,
    ix.indisvalid AND ix.indisready AND ix.indislive AS is_valid,
    -- indnullsnotdistinct only exists on PG15-based releases
    COALESCE((to_jsonb(ix) ->> 'indnullsnotdistinct')::bool, false) AS nulls_not_distinct,
    ix.indnkeyatts                              AS nkey,
    pg_get_expr(ix.indpred, ix.indrelid)        AS pred,
    EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = ix.indexrelid) AS backs_constraint,
    -- all columns (key + INCLUDE), as text, 1-based
    ARRAY(SELECT pg_get_indexdef(ix.indexrelid, g, true)
          FROM generate_series(1, ix.indnatts) g ORDER BY g)   AS cols,
    -- key column options, 1-based
    ARRAY(SELECT CASE WHEN (o & 4) = 4 THEN 'HASH'
                      ELSE CASE WHEN (o & 1) = 1 THEN 'DESC' ELSE 'ASC' END
                        || CASE WHEN (o & 2) = 2 THEN ' NULLS FIRST' ELSE ' NULLS LAST' END
                 END
          FROM unnest(ix.indoption::int2[]) WITH ORDINALITY u(o, p) ORDER BY p) AS opts,
    ARRAY(SELECT c FROM unnest(ix.indclass::oid[])     WITH ORDINALITY u(c, p) ORDER BY p) AS opclasses,
    ARRAY(SELECT c FROM unnest(ix.indcollation::oid[]) WITH ORDINALITY u(c, p) ORDER BY p) AS collations
  FROM pg_index ix
  JOIN pg_class i     ON i.oid = ix.indexrelid
  JOIN pg_class t     ON t.oid = ix.indrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
  JOIN pg_am am       ON am.oid = i.relam AND am.amname = 'lsm'
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_toast'
)
SELECT DISTINCT ON (r.schema_name, r.table_name, r.index_name)
  r.schema_name,
  r.table_name,
  r.index_name                       AS redundant_index,
  e.index_name                       AS covered_by,
  concat_ws('; ',
    CASE WHEN e.is_pk THEN 'covered by the primary key' END,
    CASE WHEN r.cols = e.cols AND r.opts = e.opts AND r.pred IS NOT DISTINCT FROM e.pred
              AND r.is_unique = e.is_unique AND r.nulls_not_distinct = e.nulls_not_distinct
         THEN 'exact duplicate' END,
    CASE WHEN r.pred IS NOT NULL AND e.pred IS NULL THEN 'partial index covered by a full index' END,
    CASE WHEN r.opts <> e.opts[1:r.nkey] THEN 'HASH replaced by range: data distribution changes' END
  )                                  AS notes,
  pg_get_indexdef(r.indexrelid)      AS redundant_def,
  pg_get_indexdef(e.indexrelid)      AS covered_by_def,
  format('DROP INDEX %I.%I;', r.schema_name, r.index_name) AS suggested_ddl
FROM idx r
JOIN idx e
  ON  e.indrelid = r.indrelid
  AND e.indexrelid <> r.indexrelid
WHERE r.is_valid AND e.is_valid
  AND NOT r.backs_constraint
  AND NOT r.is_partition
  -- (2) key prefix, opclass, collation
  AND r.nkey <= e.nkey
  AND e.cols[1:r.nkey]       = r.cols[1:r.nkey]
  AND e.opclasses[1:r.nkey]  = r.opclasses
  AND e.collations[1:r.nkey] = r.collations
  -- (3) per-column sharding/sort options
  AND (SELECT bool_and(r.opts[g] = e.opts[g] OR (r.opts[g] = 'HASH' AND e.opts[g] <> 'HASH'))
       FROM generate_series(1, r.nkey) g)
  -- (4) E's hash columns are inside R's key
  AND e.opts[r.nkey + 1] IS DISTINCT FROM 'HASH'
  -- (5) predicate
  AND (e.pred IS NULL OR e.pred = r.pred)
  -- (6) INCLUDE columns
  AND (e.is_pk OR r.cols[r.nkey + 1:] <@ e.cols[r.nkey + 1:])
  -- (7) uniqueness
  AND (NOT r.is_unique
       OR (e.is_unique AND e.nkey = r.nkey
           AND (NOT r.nulls_not_distinct OR e.nulls_not_distinct OR e.is_pk)))
  -- exact duplicates: report only one of the pair
  AND NOT (r.cols = e.cols AND r.opts = e.opts
           AND r.pred IS NOT DISTINCT FROM e.pred
           AND r.is_unique = e.is_unique
           AND r.nulls_not_distinct = e.nulls_not_distinct
           AND NOT e.backs_constraint
           AND r.index_name < e.index_name)
ORDER BY r.schema_name, r.table_name, r.index_name,
         e.is_pk DESC, e.backs_constraint DESC, e.index_name;
