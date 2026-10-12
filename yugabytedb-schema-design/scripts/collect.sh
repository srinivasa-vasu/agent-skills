#!/usr/bin/env bash
# Collect a schema-review bundle from a live YugabyteDB cluster. Read-only.
#
#   collect.sh -h <host> -d <db> [-U user] [-p 5433] [-o bundle_dir]
#              [--nodes h1,h2,h3] [--values-mode lowcard|none|all] [--no-dump] [--endpoint]
#
# Produces:
#   bundle/schema.sql        ysql_dump --schema-only --include-yb-metadata   (skip with --no-dump)
#   bundle/stats.json        export-stats.sql against <host>
#   bundle/usage/<node>.json export-usage.sql against EVERY node (counters are per node)
#   bundle/statements/<node>.json  tserver :13000/statements          (only with --endpoint)
#
# Nodes default to yb_servers(); pass --nodes when those addresses aren't reachable from here.
# Password/TLS: use the usual libpq env vars (PGPASSWORD, PGSSLMODE, PGSSLROOTCERT, ...).
# Use a read-only role; the scripts also set default_transaction_read_only.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="" DB="yugabyte" USER_="${PGUSER:-yugabyte}" PORT="5433" OUT="bundle-$(date +%Y%m%d-%H%M%S)"
NODES="" VALUES_MODE="lowcard" DUMP=1 ENDPOINT=0 WEB_PORT=13000

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h) HOST="$2"; shift 2 ;;
    -d) DB="$2"; shift 2 ;;
    -U) USER_="$2"; shift 2 ;;
    -p) PORT="$2"; shift 2 ;;
    -o) OUT="$2"; shift 2 ;;
    --nodes) NODES="$2"; shift 2 ;;
    --values-mode) VALUES_MODE="$2"; shift 2 ;;
    --no-dump) DUMP=0; shift ;;
    --endpoint) ENDPOINT=1; shift ;;
    --web-port) WEB_PORT="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$HOST" ]] || { sed -n '2,17p' "$0"; exit 2; }

YSQLSH="${YSQLSH:-$(command -v ysqlsh || command -v psql || true)}"
YSQL_DUMP="${YSQL_DUMP:-$(command -v ysql_dump || true)}"
[[ -n "$YSQLSH" ]] || { echo "ysqlsh/psql not found; set YSQLSH=/path/to/ysqlsh" >&2; exit 1; }

sql() { "$YSQLSH" -X -q -v ON_ERROR_STOP=1 -h "$1" -p "$PORT" -U "$USER_" -d "$DB" "${@:2}"; }

mkdir -p "$OUT/usage"
echo "bundle: $OUT"

if [[ $DUMP -eq 1 ]]; then
  if [[ -n "$YSQL_DUMP" ]]; then
    echo "• schema.sql (ysql_dump --schema-only --include-yb-metadata)"
    "$YSQL_DUMP" -h "$HOST" -p "$PORT" -U "$USER_" -d "$DB" --schema-only --include-yb-metadata \
      -f "$OUT/schema.sql"
  else
    echo "• ysql_dump not found: skipping schema.sql (set YSQL_DUMP=/path/to/ysql_dump)" >&2
  fi
fi

echo "• stats.json (values_mode=$VALUES_MODE)"
sql "$HOST" -v values_mode="$VALUES_MODE" -f "$HERE/export-stats.sql" -o "$OUT/stats.json"

if [[ -z "$NODES" ]]; then
  NODES="$(sql "$HOST" -At -c "SELECT string_agg(host, ',' ORDER BY host) FROM yb_servers()")"
fi
IFS=',' read -r -a NODE_LIST <<< "$NODES"
echo "• usage from ${#NODE_LIST[@]} node(s): $NODES"
failed=0
for n in "${NODE_LIST[@]}"; do
  if sql "$n" -f "$HERE/export-usage.sql" -o "$OUT/usage/$n.json" 2> "$OUT/usage/$n.err"; then
    rm -f "$OUT/usage/$n.err"
  else
    echo "  ! $n failed (see usage/$n.err); unused-index findings will be informational" >&2
    rm -f "$OUT/usage/$n.json"; failed=$((failed + 1))
  fi
  if [[ $ENDPOINT -eq 1 ]]; then
    mkdir -p "$OUT/statements"
    curl -sf --max-time 30 "http://$n:$WEB_PORT/statements" -o "$OUT/statements/$n.json" \
      || echo "  ! $n:$WEB_PORT/statements not reachable" >&2
  fi
done

cat <<EOF
done ($failed node(s) failed).
next:  python3 $HERE/analyze.py $OUT
Review $OUT/stats.json before sharing it outside the customer's environment.
EOF
