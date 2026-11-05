#!/usr/bin/env bash
# Create two kind clusters, deploy a Submariner broker on s1, and join both clusters to it.
# Safe to re-run: existing clusters, broker and joins are skipped.
#
# subctl runs from the quay.io/submariner/subctl image on the docker "kind" network, using kind's
# internal kubeconfigs. A host-run subctl would write the broker URL as https://127.0.0.1:<port>,
# which pods in the other cluster can't reach (and macOS may refuse the connection or the cert).
#
# Usage: submariner-up.sh [--skip-verify]
# Env:   ASSETS          (default: ../assets next to this script)
#        SUBCTL_VERSION  (default: 0.25.0; image tag quay.io/submariner/subctl:<version>)
#        CABLE_DRIVER    (default: libreswan; use vxlan if IPsec won't come up on your Docker host)
#        WORKDIR         (default: current directory; broker-info.subm and kubeconfig-internal go here)
set -euo pipefail

ASSETS="${ASSETS:-$(cd "$(dirname "$0")/../assets" && pwd)}"
SUBCTL_VERSION="${SUBCTL_VERSION:-0.25.0}"
CABLE_DRIVER="${CABLE_DRIVER:-libreswan}"
WORKDIR="${WORKDIR:-$PWD}"
RUN_VERIFY=1
[[ "${1:-}" == "--skip-verify" ]] && RUN_VERIFY=0

# name  pod CIDR  service CIDR (must match the kind config)
CLUSTERS=("s1 10.30.0.0/16 10.130.0.0/16" "s2 10.40.0.0/16 10.140.0.0/16")
BROKER_CTX=kind-s1

for bin in docker kind kubectl helm; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done

for row in "${CLUSTERS[@]}"; do
  read -r name _ _ <<<"$row"
  if kind get clusters 2>/dev/null | grep -qx "$name"; then
    echo "== $name: kind cluster exists"
  else
    echo "== $name: creating kind cluster"
    kind create cluster --name "$name" --config "$ASSETS/kind-$name.yaml" --wait 5m
  fi
done

# Loose reverse-path filtering on every node (see assets/rp-filter-ds.yaml for why). A DaemonSet
# rather than a one-off sysctl, so it survives node restarts and covers new nodes.
for row in "${CLUSTERS[@]}"; do
  read -r name _ _ <<<"$row"
  kubectl --context "kind-$name" apply -f "$ASSETS/rp-filter-ds.yaml" >/dev/null
  kubectl --context "kind-$name" -n kube-system rollout status ds/rp-filter-loose --timeout=3m
done

cd "$WORKDIR"
# One kubeconfig with both clusters' internal API addresses (https://<name>-control-plane:6443).
tmp=$(mktemp -d)
for row in "${CLUSTERS[@]}"; do
  read -r name _ _ <<<"$row"
  kind get kubeconfig --internal --name "$name" > "$tmp/$name"
done
KUBECONFIG=$(ls -d "$tmp"/* | paste -sd: -) kubectl config view --flatten > kubeconfig-internal
command rm -rf "$tmp"

subctl() {
  docker run --rm --network kind --user "$(id -u)" \
    -v "$PWD:/work" -w /work -e KUBECONFIG=/work/kubeconfig-internal -e HOME=/work \
    "quay.io/submariner/subctl:$SUBCTL_VERSION" /subctl "$@"
}

if kubectl --context "$BROKER_CTX" get ns submariner-k8s-broker >/dev/null 2>&1 && [[ -f broker-info.subm ]]; then
  echo "== broker already deployed on $BROKER_CTX"
else
  echo "== deploying broker on $BROKER_CTX"
  subctl deploy-broker --context "$BROKER_CTX"     # writes ./broker-info.subm
fi

for row in "${CLUSTERS[@]}"; do
  read -r name podcidr svccidr <<<"$row"
  ctx="kind-$name"

  # Label the gateway up front so join doesn't prompt.
  kubectl --context "$ctx" label node "$name-worker" submariner.io/gateway=true --overwrite >/dev/null

  if kubectl --context "$ctx" -n submariner-operator get submariner submariner >/dev/null 2>&1; then
    echo "== $name: already joined"
  else
    echo "== $name: joining (clusterid=$name, cable=$CABLE_DRIVER)"
    # --natt=false: kind nodes share one docker network, so there's no NAT between gateways.
    subctl join broker-info.subm \
      --context "$ctx" \
      --clusterid "$name" \
      --clustercidr "$podcidr" \
      --servicecidr "$svccidr" \
      --cable-driver "$CABLE_DRIVER" \
      --natt=false \
      --label-gateway=false
  fi
done

# The operator creates the gateway/route-agent/lighthouse pods after join returns; images take a while.
for row in "${CLUSTERS[@]}"; do
  read -r name _ _ <<<"$row"
  echo "== $name: waiting for Submariner pods"
  until kubectl --context "kind-$name" -n submariner-operator get ds submariner-gateway >/dev/null 2>&1; do sleep 5; done
  kubectl --context "kind-$name" -n submariner-operator wait --for=condition=Ready pod --all --timeout=10m
done

echo "== waiting for the gateway connection"
for i in {1..60}; do
  if subctl show connections --context kind-s1 2>/dev/null | grep -q ' connected'; then break; fi
  (( i == 60 )) && { subctl show all --context kind-s1; echo "gateway never connected" >&2; exit 1; }
  sleep 5
done
subctl show connections --context kind-s1
subctl show connections --context kind-s2

if (( RUN_VERIFY )); then
  subctl verify --context kind-s1 --tocontext kind-s2 --only connectivity,service-discovery --verbose
fi

echo "== Submariner ready: kind-s1 <-> kind-s2"
