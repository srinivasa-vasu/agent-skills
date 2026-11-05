# yugabytedb-k8s-submariner

Connect two Kubernetes clusters with Submariner, deploy an independent YugabyteDB universe in each, and connect them with xCluster replication.

**Covers:** kind clusters with non-overlapping pod/service CIDRs, `subctl deploy-broker` / `subctl join` (cluster ID, CIDRs, cable driver, NAT-T, gateway labelling), Lighthouse `clusterset.local` service discovery, yugabyte Helm values for MCS (`multicluster.createServiceExports`, `kubernetesClusterId`, `masterAddresses`, `replicas.totalMasters`), cross-cluster DNS checks, `subctl verify` / `diagnose`, Globalnet for overlapping CIDRs, and `yb-admin` xCluster setup (database-scoped transactional or table-level).

**Triggers on:** *"Submariner with YugabyteDB"*, *"xCluster across Kubernetes clusters with Submariner"*, *"subctl join for YugabyteDB"*, Lighthouse / ServiceExport for `yb-masters`/`yb-tservers`, `clusterset.local` master addresses with Submariner.

**Does not cover:** Cilium Cluster Mesh (use `yugabytedb-k8s-cilium`), the YugabyteDB Kubernetes Operator or YBA-managed DR (use `yb-k8s-operator`), or a single universe stretched across clusters.

## Sample prompts

**Build it end to end**
- "Set up two kind clusters connected with Submariner and deploy YugabyteDB in each so I can test xCluster replication."
- "Create a source and a target YugabyteDB universe on separate Kubernetes clusters linked by Submariner, then set up xCluster for database `app`."
- "Run the Submariner setup but skip `subctl verify`. I just want the clusters up quickly."

**Configure pieces**
- "Give me the Helm values for an RF3 YugabyteDB universe on cluster `s1` that will be the xCluster source over Submariner."
- "What `subctl join` flags do I need for two EKS clusters behind NAT running YugabyteDB?"
- "My two clusters both use 10.244.0.0/16 for pods. How do I run xCluster between them with Submariner Globalnet?"
- "Set up transactional xCluster from `s1` to `s2` for database `orders`."

**Review a config**
- "Review my kind configs, subctl join commands and YugabyteDB values files for cross-cluster xCluster." *(paste the config)*
- "Here are my yugabyte Helm values for both clusters. Will the universes form, and will xCluster reach the source tservers through Submariner?"

**Troubleshoot**
- "`subctl show connections` stays at `connecting` between my kind clusters."
- "`yb-master-0.s1.yb-masters.yb.svc.clusterset.local` doesn't resolve from cluster s2."
- "`setup_xcluster_replication` times out even though Submariner shows the clusters connected."
- "My ServiceExport for `yb-tservers` isn't `Ready`."
- "Automatic DDL xCluster set up fine, but my new table never shows up on the target."

## Layout

```
yugabytedb-k8s/submariner/
├── SKILL.md
├── README.md
├── assets/
│   ├── kind-s1.yaml / kind-s2.yaml      # kind clusters, unique CIDRs, one gateway worker each
│   ├── rp-filter-ds.yaml                # DaemonSet keeping rp_filter loose on every node
│   └── values-s1.yaml / values-s2.yaml  # yugabyte Helm values (source / target)
├── scripts/
│   ├── submariner-up.sh                 # kind + broker + gateway labels + join, idempotent
│   └── check-mcs-dns.sh                 # cross-cluster DNS + TCP check to every master/tserver
└── references/
    └── troubleshooting.md
```

Verified end to end (semi-automatic xCluster) with yugabyte chart 2026.1.2, Submariner 0.25.0, and kind 0.33 on macOS.
