/*
 * HASH-sharded key columns of a range-queried type (date/time).
 *
 * A HASH key column can only serve equality. If queries filter this column with
 * >, <, BETWEEN or ORDER BY it, the index (or table) can't seek and falls back to
 * a full scan. Candidate fix: make the column ASC/DESC (with a bucket prefix if
 * the value is monotonic, see references/hotspots.md).
 *
 * Covers primary keys (yb_table_properties() returns NULLs for a PK index, so the
 * HASH bit is read from pg_index.indoption instead), domains over date/time types,
 * and expression keys such as (date_trunc('day', ts)) HASH.
 *
 * Read-only. Confirm with the workload: a hit only matters if the column is
 * range-filtered or sorted (pg_stat_statements / statements.json).
 */
SELECT
    n.nspname                                     AS schema_name,
    t.relname                                     AS table_name,
    i.relname                                     AS index_name,
    ix.indisprimary                               AS is_pk,
    k.ord                                         AS key_position,
    pg_get_indexdef(ix.indexrelid, k.ord::int, true) AS key_column,
    format_type(ia.atttypid, ia.atttypmod)        AS key_type,
    pg_get_indexdef(ix.indexrelid)                AS index_def
FROM pg_index ix
JOIN pg_class i     ON i.oid = ix.indexrelid
JOIN pg_class t     ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_am am       ON am.oid = i.relam AND am.amname = 'lsm'
-- indoption holds one entry per KEY column; bit 4 = HASH
CROSS JOIN LATERAL unnest(ix.indoption::int2[]) WITH ORDINALITY AS k(opt, ord)
-- the index's own pg_attribute row types both plain and expression keys
JOIN pg_attribute ia ON ia.attrelid = ix.indexrelid AND ia.attnum = k.ord
JOIN pg_type ty      ON ty.oid = ia.atttypid
WHERE (k.opt & 4) = 4
  AND (CASE WHEN ty.typtype = 'd' THEN ty.typbasetype ELSE ty.oid END)
      IN ('timestamp'::regtype, 'timestamptz'::regtype, 'date'::regtype,
          'time'::regtype, 'timetz'::regtype)
  AND NOT i.relispartition          -- report the partitioned parent index once
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname !~ '^pg_toast'
ORDER BY 1, 2, 3, k.ord;
