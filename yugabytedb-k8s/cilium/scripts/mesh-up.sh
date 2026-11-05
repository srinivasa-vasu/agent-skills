#!/usr/bin/env bash
# Create two kind clusters, install Cilium with MCS-API, and connect them in a Cluster Mesh.
# Safe to re-run: existing clusters and installs are skipped.
#
# Usage: mesh-up.sh [--skip-connectivity-test]
# Env:   ASSETS (default: ../assets next to this script)
set -euo pipefail

ASSETS="${ASSETS:-$(cd "$(dirname "$0")/../assets" && pwd)}"
RUN_TEST=1
[[ "${1:-}" == "--skip-connectivity-test" ]] && RUN_TEST=0

# name  id  pod CIDR (must match the kind config podSubnet)
CLUSTERS=("c1 1 10.10.0.0/16" "c2 2 10.20.0.0/16")

for bin in docker kind kubectl cilium helm; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done

# Copy cilium-ca, keeping its Helm labels/annotations (so `cilium install` adopts it) but
# dropping server-set metadata that the API server rejects on create.
copy_ca() {  # from_ctx to_ctx
  echo "== copying cilium-ca $1 -> $2"
  kubectl --context "$1" -n kube-system get secret cilium-ca -o yaml \
    | sed -E '/^  (resourceVersion|uid|creationTimestamp):/d' \
    | kubectl --context "$2" create -f -
}

for row in "${CLUSTERS[@]}"; do
  read -r name id cidr <<<"$row"
  ctx="kind-$name"

  if kind get clusters 2>/dev/null | grep -qx "$name"; then
    echo "== $name: kind cluster exists"
  else
    echo "== $name: creating kind cluster"
    kind create cluster --name "$name" --config "$ASSETS/kind-$name.yaml"
  fi

  if kubectl --context "$ctx" -n kube-system get ds cilium >/dev/null 2>&1; then
    echo "== $name: Cilium already installed"
  else
    # Every cluster after the first must share the first cluster's CA, copied before
    # `cilium install` so Cilium adopts it instead of generating its own.
    if [[ "$name" != "${CLUSTERS[0]%% *}" ]] && ! kubectl --context "$ctx" -n kube-system get secret cilium-ca >/dev/null 2>&1; then
      copy_ca "kind-${CLUSTERS[0]%% *}" "$ctx"
    fi
    echo "== $name: installing Cilium (cluster.id=$id)"
    cilium install \
      --context "$ctx" \
      --set cluster.name="$name" \
      --set cluster.id="$id" \
      --set ipam.operator.clusterPoolIPv4PodCIDRList="$cidr" \
      --set clustermesh.mcsapi.enabled=true \
      --set clustermesh.mcsapi.corednsAutoConfigure.enabled=true \
      --set clustermesh.enableEndpointSliceSynchronization=true
  fi
  cilium status --context "$ctx" --wait
done

for row in "${CLUSTERS[@]}"; do
  read -r name _ _ <<<"$row"
  ctx="kind-$name"
  if kubectl --context "$ctx" -n kube-system get deploy clustermesh-apiserver >/dev/null 2>&1; then
    echo "== $name: clustermesh already enabled"
  else
    # NodePort is fine on kind (both clusters share the docker "kind" network).
    cilium clustermesh enable --context "$ctx" --service-type NodePort
  fi
  cilium clustermesh status --context "$ctx" --wait
done

# connect is bidirectional by default.
cilium clustermesh connect --context kind-c1 --destination-context kind-c2 || true
cilium clustermesh status --context kind-c1 --wait
cilium clustermesh status --context kind-c2 --wait

if (( RUN_TEST )); then
  cilium connectivity test --context kind-c1 --multi-cluster kind-c2
fi

echo "== Cluster Mesh ready: kind-c1 <-> kind-c2"
