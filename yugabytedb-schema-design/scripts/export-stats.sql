/*
 * Export schema catalog + optimizer statistics as one JSON document (stats.json).
 * Run once per database, against any one node:
 *
 *   ysqlsh -h <host> -d <db> -X -q -f export-stats.sql -o stats.json
 *   ysqlsh ... -v values_mode=none -f export-stats.sql -o stats.json
 *
 * values_mode controls whether pg_stats sample values (most_common_vals) are exported:
 *   lowcard (default) only for columns whose MCV list covers every value (≤ 64 values,
 *                     e.g. status/enum columns; needed to suggest SPLIT AT VALUES)
 *   none              never
 *   all               every column (may contain customer data)
 * histogram_bounds are never exported. Table rows are never read.
 *
 * Read-only: catalog views, pg_stats and yb_table_properties() only.
 * index/table usage counters are per node: collect them with export-usage.sql on every node.
 */
\set ON_ERROR_STOP on
\if :{?values_mode}
\else
  \set values_mode lowcard
\endif
\pset format unaligned
\pset tuples_only on
\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '300s';

-- pg_partman template tables (part_config.template_table): structure-only tables with no data
-- and usually no PK. Exported so the analyzer can leave them out of the review.
SELECT COALESCE((SELECT format('%I.part_config', n.nspname) FROM pg_extension e
                 JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'pg_partman'), '')
       AS partman_cfg \gset
SELECT CASE WHEN :'partman_cfg' <> '' THEN has_table_privilege(:'partman_cfg', 'SELECT') ELSE false END
       AS has_partman \gset
\if :has_partman
SELECT COALESCE(jsonb_agg(template_table), '[]') AS partman_templates
FROM :partman_cfg WHERE template_table IS NOT NULL \gset
\else
\set partman_templates '[]'
\endif

WITH
rels AS (
  SELECT c.oid, n.nspname AS schema, c.relname AS name, c.relkind, c.reltuples,
         c.relispartition
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'p', 'm')
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp_)'
    -- objects that belong to an extension (e.g. pg_partman's part_config) are not the schema under review
    AND NOT EXISTS (SELECT 1 FROM pg_depend d
                    WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
),
idxs AS (
  SELECT ix.*, i.relname AS index_name, i.relkind AS index_relkind, i.relispartition AS index_is_partition,
         am.amname
  FROM pg_index ix
  JOIN rels r     ON r.oid = ix.indrelid
  JOIN pg_class i ON i.oid = ix.indexrelid
  JOIN pg_am am   ON am.oid = i.relam
)
SELECT jsonb_build_object(
  'format', 'yb-schema-review/stats/v1',
  'meta', jsonb_build_object(
    'exported_at',   now(),
    'database',      current_database(),
    'dbid',          (SELECT oid FROM pg_database WHERE datname = current_database()),
    'version',       version(),
    'values_mode',   :'values_mode',
    'node_count',    (SELECT count(*) FROM yb_servers()),
    'placements',    (SELECT jsonb_agg(DISTINCT cloud || '.' || region || '.' || zone) FROM yb_servers()),
    'partman_templates', :'partman_templates'::jsonb,
    'settings',      (SELECT jsonb_object_agg(name, setting) FROM pg_settings
                      WHERE name IN ('server_version', 'yb_use_hash_splitting_by_default',
                                     'yb_enable_cbo', 'yb_enable_base_scans_cost_model',
                                     'yb_enable_optimizer_statistics', 'yb_disable_auto_analyze',
                                     'yb_enable_bitmapscan', 'yb_bnl_batch_size',
                                     'default_statistics_target', 'plan_cache_mode'))
  ),

  'enums', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
              'type', format_type(t.oid, NULL),
              'labels', (SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder)
                         FROM pg_enum e WHERE e.enumtypid = t.oid))), '[]')
            FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE t.typtype = 'e' AND n.nspname NOT IN ('pg_catalog', 'information_schema')),

  'tables', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'oid',          r.oid,
      'schema',       r.schema,
      'name',         r.name,
      'relkind',      r.relkind,
      'reltuples',    r.reltuples,
      'is_partition', r.relispartition,
      'parent',       (SELECT format('%I.%I', pn.nspname, pc.relname) FROM pg_inherits h
                       JOIN pg_class pc ON pc.oid = h.inhparent
                       JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                       WHERE h.inhrelid = r.oid LIMIT 1),
      'partition_key', CASE WHEN r.relkind = 'p' THEN pg_get_partkeydef(r.oid) END,
      'last_analyze', (SELECT greatest(s.last_analyze, s.last_autoanalyze)
                       FROM pg_stat_user_tables s WHERE s.relid = r.oid),
      'yb',           CASE WHEN r.relkind IN ('r', 'm')
                           THEN (SELECT to_jsonb(p) FROM yb_table_properties(r.oid) p) END,
      'columns', (SELECT jsonb_agg(jsonb_build_object(
          'attnum',    a.attnum,
          'name',      a.attname,
          'type',      format_type(a.atttypid, a.atttypmod),
          'base_type', format_type(CASE WHEN ty.typtype = 'd' THEN ty.typbasetype ELSE ty.oid END, NULL),
          'is_enum',   (CASE WHEN ty.typtype = 'd' THEN (SELECT bt.typtype FROM pg_type bt WHERE bt.oid = ty.typbasetype)
                             ELSE ty.typtype END) = 'e',
          'not_null',  a.attnotnull,
          'default',   pg_get_expr(d.adbin, d.adrelid),
          'identity',  NULLIF(a.attidentity, ''),
          'generated', NULLIF(to_jsonb(a) ->> 'attgenerated', '')  -- PG12+ only
        ) ORDER BY a.attnum)
        FROM pg_attribute a
        JOIN pg_type ty ON ty.oid = a.atttypid
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = r.oid AND a.attnum > 0 AND NOT a.attisdropped)
    ) ORDER BY r.schema, r.name), '[]') FROM rels r),

  'indexes', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'oid',                x.indexrelid,
      'table_oid',          x.indrelid,
      'name',               x.index_name,
      'definition',         pg_get_indexdef(x.indexrelid),
      'am',                 x.amname,
      'is_pk',              x.indisprimary,
      'is_unique',          x.indisunique,
      'nulls_not_distinct', COALESCE((to_jsonb(x) ->> 'indnullsnotdistinct')::bool, false),
      'is_valid',           x.indisvalid AND x.indisready AND x.indislive,
      'is_partition',       x.index_is_partition,
      'backs_constraint',   (SELECT c.conname FROM pg_constraint c
                             WHERE c.conindid = x.indexrelid AND c.contype IN ('p', 'u', 'x') LIMIT 1),
      'referenced_by_fk',   EXISTS (SELECT 1 FROM pg_constraint c
                                    WHERE c.conindid = x.indexrelid AND c.contype = 'f'),
      'nkey',               x.indnkeyatts,
      'predicate',          pg_get_expr(x.indpred, x.indrelid),
      -- PK: yb_table_properties(<pk index>) is all NULL; the table row holds its tablets
      'yb',                 CASE WHEN NOT x.indisprimary AND x.index_relkind = 'i'
                                 THEN (SELECT to_jsonb(p) FROM yb_table_properties(x.indexrelid) p) END,
      'columns', (SELECT jsonb_agg(jsonb_build_object(
          'expr',        pg_get_indexdef(x.indexrelid, k.ord::int, true),
          'attnum',      k.attnum,
          'type',        format_type(ia.atttypid, ia.atttypmod),
          'base_type',   format_type(CASE WHEN ty.typtype = 'd' THEN ty.typbasetype ELSE ty.oid END, NULL),
          'key',         k.ord <= x.indnkeyatts,
          'opt',         CASE WHEN k.ord > x.indnkeyatts THEN NULL
                              WHEN (x.indoption[k.ord - 1] & 4) = 4 THEN 'HASH'
                              WHEN (x.indoption[k.ord - 1] & 1) = 1 THEN 'DESC' ELSE 'ASC' END,
          'nulls_first', CASE WHEN k.ord <= x.indnkeyatts AND (x.indoption[k.ord - 1] & 4) = 0
                              THEN (x.indoption[k.ord - 1] & 2) = 2 END,
          'opclass',     CASE WHEN k.ord <= x.indnkeyatts
                              THEN (SELECT opcname FROM pg_opclass WHERE oid = x.indclass[k.ord - 1]) END,
          'collation',   CASE WHEN k.ord <= x.indnkeyatts
                              THEN (SELECT collname FROM pg_collation WHERE oid = x.indcollation[k.ord - 1]) END
        ) ORDER BY k.ord)
        FROM unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute ia ON ia.attrelid = x.indexrelid AND ia.attnum = k.ord
        JOIN pg_type ty ON ty.oid = ia.atttypid)
    ) ORDER BY x.indrelid, x.index_name), '[]') FROM idxs x),

  'foreign_keys', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'name',       c.conname,
      'table_oid',  c.conrelid,
      'ref_oid',    c.confrelid,
      'columns',    (SELECT jsonb_agg(jsonb_build_object(
                        'name',     a.attname,
                        'type',     format_type(a.atttypid, a.atttypmod),
                        'ref_name', fa.attname,
                        'ref_type', format_type(fa.atttypid, fa.atttypmod)) ORDER BY k.ord)
                     FROM unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(att, fatt, ord)
                     JOIN pg_attribute a  ON a.attrelid = c.conrelid AND a.attnum = k.att
                     JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = k.fatt)
    )), '[]')
    FROM pg_constraint c JOIN rels r ON r.oid = c.conrelid
    WHERE c.contype = 'f' AND NOT r.relispartition),

  'sequences', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'schema',    n.nspname,
      'name',      c.relname,
      'type',      format_type(s.seqtypid, NULL),
      'cache',     s.seqcache,
      'increment', s.seqincrement,
      'owned_by',  (SELECT format('%s.%I', d.refobjid::regclass, a.attname)
                    FROM pg_depend d
                    JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
                    WHERE d.objid = c.oid AND d.classid = 'pg_class'::regclass
                      AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i') LIMIT 1)
    )), '[]')
    FROM pg_sequence s JOIN pg_class c ON c.oid = s.seqrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace),

  'column_stats', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'schema',            s.schemaname,
      'table',             s.tablename,
      'column',            s.attname,
      'inherited',         s.inherited,
      'null_frac',         s.null_frac,
      'n_distinct',        s.n_distinct,
      'avg_width',         s.avg_width,
      'correlation',       s.correlation,
      'mcv_count',         array_length(s.most_common_freqs, 1),
      'mcv_total_freq',    (SELECT sum(f) FROM unnest(s.most_common_freqs) f),
      'most_common_freqs', to_jsonb(s.most_common_freqs[1:10]),
      'most_common_vals',  CASE
          WHEN :'values_mode' = 'all' THEN s.most_common_vals::text
          WHEN :'values_mode' = 'lowcard'
               AND array_length(s.most_common_freqs, 1) <= 64
               AND (SELECT sum(f) FROM unnest(s.most_common_freqs) f) + s.null_frac >= 0.999
            THEN s.most_common_vals::text
        END
    )), '[]')
    FROM pg_stats s
    WHERE s.schemaname NOT IN ('pg_catalog', 'information_schema')
      AND s.schemaname !~ '^pg_(toast|temp_)')
)::text;
