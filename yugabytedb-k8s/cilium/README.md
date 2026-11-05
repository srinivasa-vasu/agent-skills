# yugabytedb-k8s-cilium

Set up a Cilium Cluster Mesh with Multi-Cluster Services (MCS-API) between two Kubernetes clusters, deploy an independent YugabyteDB universe in each, and connect them with xCluster replication.

**Covers:** kind clusters with non-overlapping pod/service CIDRs, `cilium install` with `clustermesh.mcsapi`, `cilium clustermesh enable/connect`, yugabyte Helm values for MCS (`multicluster.createServiceExports`, `kubernetesClusterId`, `masterAddresses`, `replicas.totalMasters`), cross-cluster `svc.clusterset.local` DNS checks, and `yb-admin` xCluster setup (database-scoped transactional or table-level).

**Triggers on:** *"Cilium Cluster Mesh with YugabyteDB"*, *"xCluster on Kubernetes across clusters"*, *"YugabyteDB on kind with two clusters"*, ServiceExport/ServiceImport for `yb-masters`/`yb-tservers`, `clusterset.local` master addresses.

**Does not cover:** the YugabyteDB Kubernetes Operator or YBA-managed DR (use `yb-k8s-operator`), or a single universe stretched across clusters.

## Sample prompts

**Build it end to end**
- "Set up two kind clusters with Cilium Cluster Mesh and deploy YugabyteDB in each so I can test xCluster replication."
- "Create a source and a target YugabyteDB universe on separate Kubernetes clusters connected via Cilium MCS-API, then set up xCluster for database `app`."
- "Run the mesh setup but skip the Cilium connectivity test. I just want the clusters up quickly."

**Configure pieces**
- "Give me the Helm values for an RF3 YugabyteDB universe on cluster `c1` that will be the xCluster source over Cilium MCS-API."
- "What should `masterAddresses` be when `multicluster.createServiceExports` is true and the namespace is `yb-prod`?"
- "I have two EKS clusters already running Cilium. What do I change to mesh them and run xCluster between YugabyteDB universes?"
- "Set up transactional xCluster from `c1` to `c2` for database `orders`."

**Review a config**
- "Review this kind and Cilium config plus my YugabyteDB values file for cross-cluster xCluster." *(paste the config)*
- "Here are my yugabyte Helm values for both clusters. Will the universes form, and will xCluster be able to reach the source tservers?"

**Troubleshoot**
- "`yb-master-0.c1.yb-masters.yb.svc.clusterset.local` doesn't resolve from cluster c2. What's wrong?"
- "My yb-master pod never becomes ready after I set `replicas.master: 1` with `isMultiAz: true`."
- "`setup_xcluster_replication` times out even though the cluster mesh shows as connected."
- "There's no ServiceImport for `yb-tservers` in my namespace on the target cluster."
- "Automatic DDL xCluster set up fine, but my new table never shows up on the target."

## Layout

```
yugabytedb-k8s/cilium/
├── SKILL.md
├── README.md
├── assets/
│   ├── kind-c1.yaml / kind-c2.yaml      # kind clusters, unique CIDRs, no default CNI
│   └── values-c1.yaml / values-c2.yaml  # yugabyte Helm values (source / target)
├── scripts/
│   ├── mesh-up.sh                       # kind + Cilium + Cluster Mesh, idempotent
│   └── check-mcs-dns.sh                 # cross-cluster DNS + TCP check to every master/tserver
└── references/
    └── troubleshooting.md
```

Verified against yugabyte chart 2026.1.2, Cilium 1.20.3 (cilium-cli 0.20.x), and kind 0.33.
