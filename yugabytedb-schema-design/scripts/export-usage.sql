/*
 * Export per-node usage counters as JSON (usage/<node>.json).
 * Index/table counters and pg_stat_statements are kept PER NODE in YSQL: run this
 * against EVERY tserver (same database), otherwise "unused index" findings are unsafe.
 *
 *   ysqlsh -h <node> -d <db> -X -q -f export-usage.sql -o usage/<node>.json
 *   ysqlsh ... -v stmt_limit=5000 -f export-usage.sql ...
 *
 * Statement text is normalized by pg_stat_statements (constants become $n).
 * Read-only.
 */
\set ON_ERROR_STOP on
\if :{?stmt_limit}
\else
  \set stmt_limit 2000
\endif
\pset format unaligned
\pset tuples_only on
\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '120s';

SELECT to_regclass('pg_stat_statements') IS NOT NULL AS has_pss \gset

\if :has_pss
SELECT jsonb_agg(to_jsonb(s)) AS stmts FROM (
  SELECT * FROM pg_stat_statements
  WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  ORDER BY calls DESC
  LIMIT :stmt_limit
) s \gset
\else
\set stmts null
\endif

SELECT jsonb_build_object(
  'format', 'yb-schema-review/usage/v1',
  'meta', jsonb_build_object(
    'exported_at', now(),
    'database',    current_database(),
    'dbid',        (SELECT oid FROM pg_database WHERE datname = current_database()),
    'node',       COALESCE(host(inet_server_addr()), 'local'),
    'stats_reset', (SELECT stats_reset FROM pg_stat_database WHERE datname = current_database())
  ),
  'index_usage', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'schema', schemaname, 'table', relname, 'index', indexrelname,
      'idx_scan', idx_scan, 'idx_tup_read', idx_tup_read)), '[]')
    FROM pg_stat_user_indexes),
  'table_usage', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'schema', schemaname, 'table', relname,
      'seq_scan', seq_scan, 'seq_tup_read', seq_tup_read, 'idx_scan', idx_scan,
      'n_tup_ins', n_tup_ins, 'n_tup_upd', n_tup_upd, 'n_tup_del', n_tup_del)), '[]')
    FROM pg_stat_user_tables),
  'statements', :'stmts'::jsonb
)::text;
