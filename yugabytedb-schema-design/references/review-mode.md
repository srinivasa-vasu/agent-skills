# Review Mode: Reviewing an Existing Schema

Use this when the user wants an existing YugabyteDB database reviewed, either **live** (you can
connect) or **offline** (the customer sends files). Both paths produce the same bundle and run the
same analyzer, so findings are identical and repeatable.

```
live:     collect.sh ──┐
offline:  customer ────┴──> bundle/ ──> analyze.py ──> findings.json ──> render.py ──> report.html
                                                          ▲                          changes.sql
                                                          └── you triage ────────────  rollback.sql
                                                              (status, review_note, edited SQL)
```

---

## Safety Rules (live mode)

- Connect with a **read-only role**. The export scripts also set `default_transaction_read_only`
  and a `statement_timeout`.
- **Never execute DDL** (CREATE/DROP/ALTER INDEX, ALTER TABLE). Recommendations go to
  `changes.sql` (with `rollback.sql`) for the customer to run after review.
- **Never run `ANALYZE` without asking.** It's a write and can be heavy on big tables.
- **No `EXPLAIN ANALYZE` on production without consent.** It executes the query. Plain `EXPLAIN` is fine.
- Export scripts never read table rows. `pg_stats` sample values are limited (see `values_mode`);
  remind the customer to review `stats.json` before sending it out.

---

## The Bundle

| File | Produced by | Needed for | Evidence level |
|---|---|---|---|
| `stats.json` | `scripts/export-stats.sql` (one node) | **required**: catalog, sharding, pg_stats, tablets | `schema`, `stats` |
| `usage/<node>.json` | `scripts/export-usage.sql` (**every** node) | unused indexes, pg_stat_statements with DocDB metrics | `workload` |
| `statements/*.json` | tserver `:13000/statements` per node, or a consolidated file | workload checks when `usage/` has no statements | `workload` |
| `schema.sql` | `ysql_dump --schema-only --include-yb-metadata` | reading DDL, replay; not parsed by the analyzer | — |

**Evidence levels** in every finding:
- **schema**: derived from DDL alone. A candidate, it needs confirmation.
- **stats**: backed by `pg_stats` / row counts. Likely.
- **workload**: backed by statements or usage counters. Confirmed by real traffic.

**Per-node data.** `pg_stat_user_indexes`, `pg_stat_user_tables` and `pg_stat_statements` are
kept per tserver. Usage from one node can call an index "unused" that another node scans all day.
The analyzer downgrades unused-index findings to `info` unless every node is present.

### Collecting

Live, from any machine that can reach the cluster:

```bash
scripts/collect.sh -h <host> -d <db> -U <readonly_user> -o bundle
#   --nodes h1,h2,h3     when yb_servers() addresses aren't reachable from here
#   --values-mode none   export no pg_stats sample values at all
#   --endpoint           also fetch http://<node>:13000/statements
#   --sslrootcert ca.crt TLS: verify the server against this CA (sslmode defaults to verify-ca)
#   --sslmode verify-full  when node host names match the certificate
#   --sslcert/--sslkey   client certificate authentication
#   --web-https          fetch :13000/statements over https (uses --sslrootcert as the CA)
```

For TLS-enabled clusters (yugabyted `--secure`, YBA "encryption in transit"), pass the cluster's
root CA, e.g. `--sslrootcert <base_dir>/certs/ca.crt` for yugabyted. The same settings are
exported as `PGSSLMODE` / `PGSSLROOTCERT` / `PGSSLCERT` / `PGSSLKEY`, so `ysql_dump` and every
`ysqlsh` call use them. Hand-run commands take the same env vars or a connection string:
`ysqlsh "host=<h> dbname=<db> sslmode=verify-ca sslrootcert=ca.crt" -f export-stats.sql ...`

Or by hand (what to send a customer):

```bash
ysql_dump -h <host> -d <db> --schema-only --include-yb-metadata -f schema.sql
ysqlsh -h <host> -d <db> -X -q -f export-stats.sql -o stats.json
for n in <every tserver>; do
  ysqlsh -h $n -d <db> -X -q -f export-usage.sql -o usage/$n.json
done
```

`values_mode` (export-stats.sql, `-v values_mode=...`):
- `lowcard` (default): sample values only for columns whose MCV list covers every value (≤ 64
  values, e.g. status/enum). Needed to generate `SPLIT AT VALUES`.
- `none`: no sample values.
- `all`: every column's MCVs (may contain customer data).

`histogram_bounds` are never exported.

### Statements file formats accepted

The analyzer reads any of these, in `statements/*.json` or a single `statements.json`:

- Raw tserver `:13000/statements` output: `{"ysql": {"queries": [ {...}, ... ]}, "ycql": ...}`
  (YCQL rows are ignored), one file per node or one already consolidated across nodes.
- `{"statements": [ {...}, ... ]}`.
- A JSON array of `pg_stat_statements` rows (e.g. `SELECT jsonb_agg(to_jsonb(s)) FROM pg_stat_statements s`).
- A **consolidated** file: a JSON array (or `{"statements": [...]}`) where each object has:

  | field | required | notes |
  |---|---|---|
  | `query` | yes | normalized text |
  | `calls` | yes | |
  | `total_exec_time` or `total_time` | yes | ms. PG15-based releases use `total_exec_time`, PG11-based `total_time` |
  | `rows` | recommended | |
  | `queryid` / `query_id` | recommended | merge key |
  | `dbid`, `userid` | recommended | merge key; `dbid` filters other databases |
  | `docdb_rows_scanned`, `docdb_read_rpcs`, `docdb_seeks` | optional | from `pg_stat_statements` on 2025.1+ |

Rows are merged on `(dbid, userid, queryid)` and counters summed. If the file is already
consolidated across nodes, ship it as the only statements file so nothing is double-counted. When
`usage/*.json` contains statements, statements files are ignored for the same reason.

When `dbid` is unknown (schema-only review), rows are filtered on `datname` instead: by default the
database `stats.json` was exported from, or the name given with `--database`. A schema-only review
of a file spanning several databases warns with the per-database row counts; rerun with
`--database NAME` so other databases' queries are not attributed to this schema.

### Schema-only review (only `schema.sql`)

When the customer can only share the DDL, point the analyzer at it. No database or cluster is
needed: `scripts/schema_sql.py` parses the file into the same catalog `export-stats.sql` produces.

```bash
python3 scripts/analyze.py schema.sql                   # writes review/ next to schema.sql
python3 scripts/analyze.py bundle/                      # a bundle with schema.sql and no stats.json
python3 scripts/analyze.py schema.sql --range-default   # override: keys without HASH/ASC are range (see below)
```

- **Best input:** `ysql_dump --schema-only --include-yb-metadata`. It spells out HASH/ASC/DESC,
  SPLIT clauses and colocation, so the review matches the live catalog. Tested: parsed dumps gave
  the same findings and the same `changes.sql` as the live catalog of the same schema.
- **Hand-written DDL / migration scripts** also work: inline `PRIMARY KEY` / `UNIQUE` /
  `REFERENCES`, `SERIAL`, `SET search_path`, enums and domains. Keys without HASH/ASC get
  YugabyteDB's default: first key column HASH, the rest ASC. If the cluster runs with
  `yb_use_hash_splitting_by_default = off` (e.g. Enhanced PostgreSQL Compatibility), pass
  `--range-default`. The report warns how many keys were assumed. Colocation and tablet counts are
  only known where the DDL states them.
- **What's lost without stats:** cardinality, NULL and skew checks (booleans and enums are still
  caught from their types; HASH-led `smallint` keys become low-confidence candidates), row counts,
  unused indexes and workload evidence. The report adds a `schema-only` info finding.
- Views, functions, materialized views (and their indexes), grants and comments are skipped and
  counted in the bundle warnings.

**Higher fidelity: load into a scratch cluster.** When exact catalog details matter (expression
types, operator classes, collations), or the DDL relies on extensions or features the parser
doesn't model, load the file into an empty database and export from there:

```bash
yugabyted start --base_dir=/tmp/yb-review
ysqlsh -c "CREATE DATABASE review"            # use WITH COLOCATION = true if the source DB is colocated
ysqlsh -d review -v ON_ERROR_STOP=1 -f schema.sql
ysqlsh -d review -X -q -f scripts/export-stats.sql -o bundle/stats.json
```

Use the same YugabyteDB major version as the source.

---

## Running the Analyzer

```bash
python3 scripts/analyze.py bundle            # writes bundle/review/{findings.json,report.html,changes.sql,rollback.sql}
python3 scripts/analyze.py bundle --format md   # report.md instead of report.html
python3 scripts/analyze.py bundle --out out/
```

Stdlib only (Python 3.8+). It never connects to a database. The report is `report.html` by default: one self-contained file (no external CSS/JS), with severity filters and collapsible SQL, that can be emailed as is. `--format md` writes `report.md`; `render.py` takes the same option. Run it with `python3 -I` when the
bundle came from outside.

For a quick live check without a bundle, run the standalone queries in `scripts/checks/`:
- `hash_on_range_type.sql`: HASH key columns of a date/time type (PKs, domains, expressions included)
- `redundant_indexes.sql`: same logic as the analyzer's redundant-index rule

---

## Your Job After the Analyzer Runs

The analyzer is deterministic. Your value is judgment. With the report / `findings.json`:

1. **Check the bundle warnings first.** Missing nodes, schema-only, other databases, `values_mode=all`.
   State what the review could and couldn't see.
2. **Triage each finding** against the reference it cites. Drop or downgrade false positives the
   rules can't judge. For example: a "redundant" index named in a hint; an intentional
   `(a, b) HASH` for a huge-tenant table; a HASH timestamp only used for exact-match lookups.
3. **Prioritize by workload.** A medium finding on a table in the top-5 statements outranks a high
   finding on an empty table. Use `calls`, `total_time_ms` and `docdb_rows_scanned` vs `rows`.
4. **Read the relevant statements** for the top findings and check the access path the fix needs.
   For example, a range-converted index must still serve the existing equality lookups, and the
   reorder in low-cardinality Option 1 only helps if queries filter on the new leading column.
5. **Collect the questions** (the report lists them) and ask the customer instead of guessing.
6. **Record your triage in `findings.json`, then re-render** so the SQL matches the report:
   - `"status": "rejected"` + `"review_note": "why"` for false positives or changes the customer
     declines. Rejected findings move to a "Rejected in review" section and leave `changes.sql`.
   - `"status": "accepted"` for confirmed ones (`render.py --accepted-only` emits only these).
   - Edit an action's `"sql"` (and its `"rollback"`) to adjust a change: bucket count, INCLUDE
     columns, split points. To turn a manual item into a real change, set its `"phase"`.
   - Then: `python3 scripts/render.py bundle/review/findings.json`.
   Never hand-edit `changes.sql` alone: the next render overwrites it, and it drifts from the report.
7. **Write the customer report**: summary, top 3–5 changes with impact and risk, the questions,
   the rollout plan below. The report already lists each finding's changes and their phase.

### The generated SQL

`changes.sql` holds every change in the (triaged) report, grouped into phases, and runs **one
phase per invocation**. Without `-v phase=...` it does nothing:

```bash
ysqlsh -d <db> -v phase=prepare -f changes.sql   # ANALYZE tables without statistics
ysqlsh -d <db> -v phase=create  -f changes.sql   # FK indexes, replacement indexes (built online)
ysqlsh -d <db> -v phase=workload -f changes.sql  # indexes proposed from statement text (built online)
#   ... verify plans and latency ...
ysqlsh -d <db> -v phase=drop    -f changes.sql   # drop redundant indexes; swap replacements in
ysqlsh -d <db> -v phase=rewrite -f changes.sql   # PK / column-type changes: full table rewrites,
                                                 # maintenance window, writes stopped (#19860)
```

- **Replacements keep the original name.** A replacement index is built as `<name>_new` in
  `create`. In `drop`, the old index is dropped and the new one renamed, so hints and dashboards
  keep working. The swap only runs while `<name>_new` exists, so re-running a phase is safe.
- **New index names are unique.** New indexes are named `<table>_<cols>_idx`. Each name is
  checked against the table's existing indexes and every other name proposed in the same run. A
  name over 63 bytes is cut and gets a 6-character hash of the full column list, so two proposals
  that differ only past the cut still get different names. Manual option A/B rebuilds of one
  index share `<name>_new` on purpose: you run only one of them.
- **Overlaps are resolved.** A replacement of an index that is dropped as redundant is skipped. An
  index that covers a dropped one is flagged if another finding changes or drops it.
- **Workload indexes are runnable, not commented out.** The indexes the statement checks propose
  have their own `workload` phase, with a checklist at its top and, under each index, the statements
  it serves (calls, mean time). Check each with `EXPLAIN (ANALYZE, DIST)` first; skip one by setting
  its finding's `status` to `rejected` and re-rendering, not by editing `changes.sql`.
- **Manual decisions** (choices, unique-index reshapes, unused-index drops, range-tail options,
  templates with `<placeholders>`) are written commented out at the end, each with the questions to
  answer (`decide:`) before running it by hand.
- **Inside each phase, changes run most severe first** (high → medium → low), under a `-- ---- high`
  / `medium` / `low` divider, also after triage edits reorder `findings.json`. Phases themselves keep
  their order: replacements are swapped in only after they are built, and rewrites run last.
- `rollback.sql` undoes the phases in reverse (`rewrite` → `drop` → `workload` → `create`). Recreated indexes
  come from `pg_get_indexdef`, which omits `SPLIT AT` clauses: re-add them from `schema.sql`.
- All statements are idempotent (`IF [NOT] EXISTS`). YSQL has no `DROP INDEX CONCURRENTLY`; plain
  `CREATE INDEX` already builds online outside a transaction block.

Tested end to end: applying every phase on a test schema cleared all non-manual findings on
re-review, a second run was a no-op, and `rollback.sql` restored the exact original schema.

### Rule catalog

| Rule | Evidence | Reference |
|---|---|---|
| `no-primary-key` | schema | [primary-keys.md](primary-keys.md) |
| `invalid-index` | schema | [index-design.md](index-design.md) |
| `hash-on-range-type` | schema → workload when range filters/ORDER BY on the column are seen | [hotspots.md](hotspots.md), [sharding.md](sharding.md) |
| `monotonic-range-key` | schema (identity, sequence, `now()` default, or timestamp-like name) | [hotspots.md](hotspots.md) |
| `low-cardinality-hash` | stats (n_distinct ≤ 64) or schema (boolean/enum); a HASH-led `smallint` without stats is a low-confidence candidate. Not raised when the HASH columns are the whole key of the PK or a unique index (one hash value per row), when the estimated distinct count is at least half the rows (unique in practice), or for tables under 1,000 rows. Replacement: `(col ASC)` + `SPLIT AT VALUES`, with an A1 bucket on a following monotonic column | [low-cardinality.md](low-cardinality.md) |
| `null-heavy-hash`, `skewed-hash` | stats (null_frac, MCV freq) | [partial-indexes.md](partial-indexes.md) |
| `range-hot-tails` | schema/stats: low-cardinality or skewed (one value ≥ 30% of rows) leading range column followed by a monotonic column (identity, sequence, `now()` default, or a created/modified/updated/`_date` name, snake_case or camelCase), or by a range PK suffix. Options bucket on the PK | [low-cardinality.md](low-cardinality.md) |
| `range-single-tablet` | stats: range-sharded, 1 tablet, ≥ 100k rows | [low-cardinality.md](low-cardinality.md) |
| `partial-keyed-on-predicate` | schema | [partial-indexes.md](partial-indexes.md) |
| `partial-index-parameterized` | workload, **low confidence** (see limits) | [partial-indexes.md](partial-indexes.md) |
| `redundant-index` | schema | [index-design.md](index-design.md#redundant-indexes) |
| `pk-range-twin` | schema → workload: a secondary index on exactly the PK columns, range-sharded where the PK is HASH (`(tenant_id, id) HASH` PK + `(tenant_id ASC, id ASC)` index). Every row is written twice. If every statement on those columns binds the full key, drop the index (`drop`). Otherwise reshape the PK and drop the index (`rewrite`, full table rewrite): the leading column is decided like any proposal (stats, then existing indexes other than these twins): low-cardinality → `(tenant_id ASC, id ASC)`, high-cardinality → `(tenant_id HASH, id ASC)`, which serves both access paths. Other indexes the new PK covers are dropped with it; an index dropped as redundant *because of the twin* keeps its drop only if the new PK covers it too. Manual when FKs reference the PK, the table is partitioned, or another finding also reshapes that PK. Notes a per-tenant insert tail when the second column is monotonic | [index-design.md](index-design.md#redundant-indexes) |
| `unused-index` | workload (per table; `info` unless all nodes collected) | [index-design.md](index-design.md) |
| `fk-without-index`, `fk-type-mismatch` | schema/stats | [foreign-keys.md](foreign-keys.md) |
| `int-sequence` | schema | [hotspots.md](hotspots.md) |
| `multi-column-gin` | schema | [index-design.md](index-design.md) |
| `scan-heavy-statement` | workload (`docdb_rows_scanned` ≥ 50× rows returned) | [index-design.md](index-design.md) |
| `statement-no-index` | workload: a statement's equality/range conditions match no leading PK or index column (full scan). Names an index that would serve if a leading column such as `tenant_id` were added to the query | [index-design.md](index-design.md) |
| `hash-key-partly-bound` | workload: a multi-column HASH key with only some HASH columns bound by equality, and no other index serves the statement | [sharding.md](sharding.md) |
| `range-on-hash-column` | workload: range filter or `ORDER BY ... LIMIT` on a non-date/time HASH column that no index has as a range column (date/time: `hash-on-range-type`) | [sharding.md](sharding.md) |
| `statement-weak-index` | workload: the best index binds only part of the equality conditions; reported only for statements at medium cost or above | [index-design.md](index-design.md) |
| `missing-statistics`, `schema-only`, `range-default` | stats / settings | this file, [sharding.md](sharding.md) |

**Statement access checks.** The four `statement-*` / `*-hash-*` rules parse each statement's
top-level `AND`-ed conditions in `WHERE` and `JOIN ... ON` (conditions under `OR` or `NOT` are
filters, not access paths; `INSERT` is skipped) and compare them with every valid LSM index and the
PK: all HASH columns must be bound by equality, then range columns are used as a leading prefix.
Severity comes from the workload, so an unindexed lookup that costs nothing yet stays low:

| severity | when (any statement in the finding) |
|---|---|
| high | mean ≥ 100 ms over ≥ 100 calls, or ≥ 20% of all statement time with mean ≥ 10 ms |
| medium | mean ≥ 20 ms, or ≥ 5% of all statement time |
| low | otherwise; also when stats.json shows the table under 1,000 rows (a large table, ≥ 1M rows, is at least medium) |

Each finding lists its statements with calls, total and mean time and share of statement time, and a
proposed index as a `manual` (commented-out) change: verify it with `EXPLAIN (ANALYZE, DIST)` first. A
subquery is analysed as its own query block. A proposal is dropped (the finding points to the other
index instead) when an existing index that no change drops or replaces, another finding's change,
or a longer proposal on the same table has the same HASH columns and starts with its range columns.
Equality columns that more statements on the table filter on go first, so proposals tend to share a
leading key.

The leading column of a proposed index is HASH or range (ASC), decided in this order: a column
that increases on insert (identity, sequence, `now()` default, time-like name) is HASH; otherwise
`pg_stats` n_distinct ≤ 64 means range (a HASH key spreads a low-cardinality column over only that
many hash values, while a range index splits inside a value as it grows) and more means HASH;
without statistics, follow how existing secondary indexes lead with that column (on the table,
then anywhere in the schema), then the schema's convention (≥ 80% of ≥ 10 secondary indexes
range-led), then the column type (boolean/smallint/enum: range). The recommendation states the
reason.

Equality columns are ordered the same way for every table, so similar queries get similar keys:
columns existing indexes on the table lead with (such as `tenant_id`) first; then selective columns
before low-cardinality ones (by `pg_stats`, else by type or by a name such as `flag`, `*_status`,
`*_type`, `active`, `is_*`); then columns more of the table's statements filter on; then
single-value `=` before `IN (...)` lists; then query order. When folding duplicates, equality
columns count in any order within the key prefix: `(a, b, c)` serves `WHERE b = $1 AND a = $2`.
Because that order is free, a proposal whose equality columns contain every key column of an
existing secondary index starts with that index's key (the longest such index), so it supersedes
it: `(tenant_id, loggedin_session_id)` existing + `tenant_id, campaign_id, loggedin_session_id,
event` bound → `(tenant_id, loggedin_session_id, campaign_id, event, ...)`, and the old index is
dropped. Statements are grouped into one proposal only when they bind the same equality columns,
so a statement that binds `agent_id` instead of `loggedin_session_id` gets its own index.
A proposal whose leading key columns are exactly the columns of a HASH primary key (for example
`(tenant_id ASC, lob_id DESC)` for `PRIMARY KEY ((tenant_id, lob_id) HASH)`) would be a range twin of
the PK: the finding reshapes the PK to that shape instead (`rewrite` phase; the PK holds every
column, so no INCLUDE is needed), unless FKs reference the PK, the table is partitioned or another
finding already changes that PK, in which case it keeps the index and says why. `hash-on-range-type`
is not raised for a PK whose date/time column an existing index already scans as a range column.
A proposal also counts as covered by a primary key another finding reshapes (the new shape, not
the old one). When a proposal fully covers an existing plain secondary index (same HASH part, the
old key a prefix, its INCLUDE columns contained), the finding drops the old index in the `drop`
phase, after the new one is built, so no access path ends up with two indexes.
A range-led index starts on one tablet and splits as it grows; pre-split it if it will be large
from day one.

A statement whose time is explained by one block scanning a whole table (`statement-no-index`,
`hash-key-partly-bound`) does not also raise `statement-weak-index` for its other blocks.

When every statement behind a proposal is a SELECT block that doesn't select `*`, the proposal adds
`INCLUDE` for the other columns the block reads (at most 4, never json/jsonb/bytea/xml/arrays), so
it can run as an Index Only Scan. Primary key columns count: a YSQL index entry stores the primary
key only in encoded form (`ybctid`), which the query can't read back. When a statement binds only
part of a HASH primary key but a subquery on the same table binds the rest (`UPDATE t ... WHERE id IN
(SELECT id FROM t WHERE tenant_id = $1 ...)`), the finding recommends binding it in the outer
statement instead of proposing an index.
Share of statement time is computed over the statements kept for this database, so filter
multi-database files with `--database`.

**Partitioned tables.** `pg_stats` holds a partitioned parent's statistics only after `ANALYZE`
on the parent itself (inherited stats); analyzing partitions doesn't fill them. When the parent has
none, the analyzer derives them from its partitions: distinct values = the largest per-partition
count (a lower bound for the whole table), `null_frac` weighted by rows, most common values from the
largest partition, rows = the partitions' sum. Recommendations say when a statistic came from
partitions. `missing-statistics` lists parents without inherited stats and adds `ANALYZE <parent>`
to the `prepare` phase.

### Known limits

- **Literals vs bind parameters are indistinguishable.** `pg_stat_statements` normalizes
  `status = 'active'` to `status = $1`, the same as a prepared statement. Partial-index checks
  against parameters always need confirmation from application code.
- **Query-text matching is heuristic.** Range usage is matched by table and column name; aliases,
  views and functions can hide or fake a match. The statement access checks resolve aliases and
  unqualified columns (when only one referenced table has the column), but don't see through views,
  functions, or the planner's choice of join order: a join condition counts as an equality on both
  sides.
- **`pg_stats.correlation` is not used.** Rows are stored in key order (often hash order), so a
  perfectly monotonic `created_at` shows ~0. Monotonic columns come from identity/sequence/`now()`
  defaults and names.
- **`num_tablets` can be stale.** Confirm tablet counts with `yb-admin list_tablets` before acting
  on `range-single-tablet`.
- **Expression keys** get no column statistics, so only type-based checks apply.
- **Reversed indexes** (`a DESC` vs `a ASC`) are conservatively not reported as redundant.

---

## Rollout Runbook (after the review)

Offline review produces hypotheses, not proven fixes.

1. **Review with the customer.** Answer the open questions; confirm which queries matter and which
   tables will grow.
2. **Validate on staging** with representative data. For each change, compare plans for the
   affected top statements with `EXPLAIN (ANALYZE, DIST)`: rows scanned, read requests, sort
   removed. For hotspot fixes, run a write load and confirm writes spread across tablet leaders.
3. **Roll out in order of risk** (these are the `changes.sql` phases):
   1. **prepare / create**: new and replacement indexes. `CREATE INDEX` builds online by default
      in YSQL (outside a transaction block); schedule large builds off-peak.
   2. **Verify** production plans pick them up.
   3. **drop**: redundant indexes, plus swapping each replacement in for the index it replaces.
      Unused-index drops stay manual until a full business cycle on every node shows no use.
      YSQL has no `DROP INDEX CONCURRENTLY` and no multi-object `DROP INDEX a, b`.
   4. **rewrite**: PK and column-type changes are full table rewrites. Do them last, in a
      maintenance window with writes stopped, or as a migration to a new table.
4. **Re-measure.** Run `SELECT pg_stat_statements_reset()` on every node after the rollout, let a
   representative window pass, collect a new bundle and re-run the analyzer. Compare the two
   reports to see what improved and catch regressions.
