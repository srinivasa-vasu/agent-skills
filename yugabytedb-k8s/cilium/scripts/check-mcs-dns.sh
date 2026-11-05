#!/usr/bin/env bash
# Verify that each cluster can resolve and reach the other cluster's YugabyteDB pods through
# MCS-API (clusterset.local). Run after both Helm releases are up.
#
# Usage: check-mcs-dns.sh <namespace> [masters] [tservers]
set -euo pipefail

NS="${1:?namespace required}"
MASTERS="${2:-1}"
TSERVERS="${3:-1}"
FAIL=0

check() {  # from_ctx  remote_cluster_id
  local from="$1" remote="$2" names=()
  for ((i = 0; i < MASTERS; i++));  do names+=("yb-master-$i.$remote.yb-masters.$NS.svc.clusterset.local:7100"); done
  for ((i = 0; i < TSERVERS; i++)); do names+=("yb-tserver-$i.$remote.yb-tservers.$NS.svc.clusterset.local:9100"); done

  echo "== from $from -> $remote"
  kubectl --context "$from" -n "$NS" get serviceimports 2>/dev/null || echo "   (no ServiceImports in $NS)"
  for hp in "${names[@]}"; do
    host="${hp%:*}" port="${hp##*:}"
    # The yugabyte image ships bash, so /dev/tcp gives a TCP check without extra tools.
    if kubectl --context "$from" -n "$NS" exec yb-tserver-0 -c yb-tserver -- \
         bash -c "getent hosts $host >/dev/null && timeout 3 bash -c '</dev/tcp/$host/$port'" 2>/dev/null; then
      echo "   OK   $hp"
    else
      echo "   FAIL $hp"; FAIL=1
    fi
  done
}

check kind-c1 c2
check kind-c2 c1
exit $FAIL
