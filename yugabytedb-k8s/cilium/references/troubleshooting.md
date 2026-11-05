# Troubleshooting

| Symptom | Likely cause | Check / fix |
|---|---|---|
| kind nodes `NotReady` after create | No CNI yet (expected until Cilium is installed) | `cilium status --wait` |
| `cilium clustermesh connect` errors on CA / cluster ID | Same `cluster.id` or `cluster.name` in both clusters | Reinstall Cilium with unique values |
| `cilium clustermesh connect` / `status` warns that CA certificates don't match, or cross-cluster Hubble/mTLS fails | c2 installed with its own CA instead of c1's `cilium-ca` | Compare `kubectl get secret -n kube-system cilium-ca -o jsonpath='{.data.ca\.crt}'` in both clusters; if they differ, `cilium uninstall` on c2, copy the secret, reinstall |
| `kubectl create -f -` of copied `cilium-ca` fails with `resourceVersion should not be set` | Server-set metadata copied along | Strip `resourceVersion`/`uid`/`creationTimestamp` (the `sed` in SKILL.md step 2) |
| `cilium install` on c2 fails: `cilium-ca ... exists and cannot be imported into the current release` | Secret was recreated without its Helm labels/annotations | Copy the full YAML (keep `meta.helm.sh/*` annotations and `app.kubernetes.io/managed-by` label) |
| Mesh connected but cross-cluster pods unreachable | Overlapping pod CIDRs, or Cilium pool ≠ kind `podSubnet` | `kubectl get ciliumnode -o yaml` → `spec.ipam.podCIDRs` |
| `*.svc.clusterset.local` NXDOMAIN | MCS-API off, CoreDNS < 1.12.2, or autoconfig off | `kubectl -n kube-system get cm coredns -o yaml` should contain `clusterset.local`; `kubectl get crd serviceimports.multicluster.x-k8s.io` |
| Service name resolves, per-pod name (`yb-master-0.c1.yb-masters…`) does not | EndpointSlice sync off, or headless pod hostname not exported | Ensure `clustermesh.enableEndpointSliceSynchronization=true`; `kubectl get endpointslices -n <ns>` in the remote cluster |
| No ServiceImport in the namespace | Chart not creating ServiceExport, or namespace not global | `kubectl get serviceexports -n <ns>` (status conditions); `clustermesh.cilium.io/global=true` on the namespace |
| yb-master stays not ready, logs `Unable to find self in master addresses` / election never completes | `masterAddresses` not using the broadcast (clusterset) names, or lists masters that don't exist | Make `masterAddresses` match the chart's broadcast address and real master count |
| Universe up but tables under-replicated / `Not enough live tablet servers` | `replicas.totalMasters` left at 3 with 1 master/1 tserver → RF 3 | Set `totalMasters` = master count; reinstall after deleting PVCs |
| Master still uses old addresses after `helm upgrade` | Raft config persisted on PVC | Delete PVCs / namespace and reinstall (demo), or `yb-admin change_master_config` (real) |
| `setup_xcluster_replication` / `setup_universe_replication` times out | Target can't reach **source tservers** (not just masters) | `scripts/check-mcs-dns.sh` from kind-c2; source tserver broadcast must be the clusterset name |
| Automatic-DDL mode: setup succeeds, first `CREATE TABLE` never appears on the target, `get_xcluster_safe_time` lag keeps growing, target master logs `AddTableToXClusterTargetTask … Waiting for xCluster safe time`, other groups stall too | Target database created by hand instead of restored from a source backup (OIDs don't match). Not a network problem | `src drop_xcluster_replication <group> $TGT`; redo with a backup/restore, or use semi-automatic mode |
| `get_replication_status` shows errors after setup | Schema mismatch (semi-automatic mode) or data present without bootstrap | Compare schemas; rerun `is_xcluster_bootstrap_required` |
| Pods `Pending` | Default chart resources (2 CPU / 4 GiB per tserver) too large for kind | Use the reduced `resource:` block in `assets/values-*.yaml` |
