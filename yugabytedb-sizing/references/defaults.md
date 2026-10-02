# Defaults

Every default can be overridden with the flag shown. Precedence: CLI flag > API profile > global
default.

## API-specific

| Parameter | YSQL | YCQL | Flag / notes |
|---|---|---|---|
| CPU per read | 1.070 ms | 0.107 ms | `--cpu-ms-per-read`; RF=3, current-gen CPUs, before RPC — see [cpu-model.md](cpu-model.md) |
| CPU per write | 1.784 ms | 0.178 ms | `--cpu-ms-per-write`; includes replication to 2 followers |
| Baseline workload | `oltp` | `point` | `--workload`; YSQL `kv` is 0.214× (sysbench-measured) |
| RPC overhead | 0.15 → 1.15× | 0.08 → 1.08× | `--rpc-overhead`; retries and variance on top of measured costs |
| Index storage | 20% | 10% | `--index-overhead`; YCQL models are query-first/denormalized |
| Backends per node | 16 × vCPU (10 × vCPU with Connection Manager) | not modeled | `--conn-per-vcpu`; CQL drivers multiplex requests |
| Memory per backend | 15 MB | — | `--mem-mb-per-conn`; TPC-C PSS. Counted against PostgreSQL's 27% RAM share first |
| Connection CPU | 0.2% core per backend | — | `--conn-cpu-overhead` |
| Tablets per table | 1 (no pre-split) | 1 per tserver | `--tablets-per-table`; YCQL ≤2 cores: 1 per cluster, ≤4 cores: 2 |
| CDC | +5% CPU, 8 h WAL | not supported | `--cdc`, `--cdc-overhead`, `--cdc-wal-retention-secs` |
| Statement profiling | `pg_stat_statements` | `<tserver>:12000/statements` | |

## Common

| Parameter | Default | Flag / notes |
|---|---|---|
| Topology | multi-AZ, zones = RF | `--zones` (RF or 1). Zone-failure scenario and cross-AZ traffic follow from it |
| Regions | 1 | `--regions RF` = one replica per region; `--preferred-region` pins leaders |
| Cross-region RTT | 30 ms | `--region-rtt-ms` (set per region pair) |
| Cross-region price | $0.02/GB | `--cross-region-cost-per-gb` (varies by provider and region pair) |
| Statements per transaction | 1 (autocommit) | `--statements-per-txn`; `--tps` gives QPS = TPS × statements |
| Distributed transactions | 100% of write txns if > 1 statement, else 0% | `--distributed-txn-pct` |
| Distributed-txn cost | commit 1.0 + intent 1.0 fast-path writes | Added to fast-path profiles only (`kv`, `point`, `range`) |
| Write pipelining | off (YSQL default) | `--write-pipelining`; latency only — the report always shows the alternative |
| Follower reads | 0% | `--follower-read-pct` |
| Read replicas | 0% of reads, 1 copy, same vCPU | `--read-replica-read-pct`, `--read-replica-rf`, `--read-replica-vcpu` |
| Target CPU utilization | 65% | `--target-cpu-util`; includes connection and tablet CPU |
| Failure CPU ceiling | = target (65%) | `--failure-cpu-target`; used for warnings and `--size-for` |
| xCluster | +10% CPU, 24 h WAL retention | `--xcluster`, `--xcluster-overhead`, `--xcluster-wal-retention-secs` |
| WAL retention | 900 s | `--wal-retention-secs` (`log_min_seconds_to_retain`) |
| Compression | 30% | `--compression-ratio` (LZ4) |
| Compaction reserve | 20% | `--compaction-reserve` |
| Max storage per node | 20 TB | `--max-storage-per-node-gb`; nodes added if exceeded |
| Disk limits per node | 16,000 IOPS, 1,000 MiB/s (gp3 max) | `--disk-iops`, `--disk-mibps`; nodes added if exceeded |
| IOPS per replicated write | 0.245 | `--iops-per-replica-write` (TPC-C) |
| Disk write amplification | 19× | `--disk-write-amp` (TPC-C) |
| Read cache miss | computed: 1 − (½ RAM / leader data) | `--read-cache-miss` fixes it |
| Network factors | 13.9× per replicated write, 3.2× per read (per row byte) | `--net-write-factor`, `--net-read-factor` |
| Cross-AZ price | $0.02/GB | `--cross-az-cost-per-gb` (AWS $0.01 each direction) |
| Memory ratio | 1:4; 1:8 for read-heavy data that doesn't fit a 1:4 node's cache | — |
| PostgreSQL RAM share | 27% | `use_memory_defaults_optimized_for_ysql` |
| RAM tier tolerance | 2% | `--ram-tier-tolerance` |
| Connection Manager | off; 10 backends/vCPU, ~10 clients/backend, +200 MB/node | `--connection-manager`, `--cm-client-ratio` |
| Tablet maintenance | 0.4 vCPU + 700 MB per 1,000 tablets | `--tablet-vcpu-per-1000`, `--tablet-mem-mb-per-1000` |
| Tablet split thresholds | 128 MiB → 10 GiB → 100 GiB, ≤ 50/tserver | yb-master defaults |
| Avg row size | 512 bytes | `--avg-row-bytes`; drives WAL, disk and network |
| Data growth | 30%/yr | `--growth-rate-pct` |
| CPU architecture | x86 | `--cpu-arch arm` (×1.10 CPU) |
| Hardware scale | 1.0 (current gen) | `--cpu-cost-scale` (~1.15 previous gen, ~1.5 older) |
| YCQL hardware-generation factor | 0.6 | Measured YCQL costs are from 2016-era i3 hardware |
