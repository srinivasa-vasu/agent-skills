# CPU Cost Model

CPU is sized from **measured CPU cost per operation**, not from latency. Latency includes waiting
on the network, Raft quorum and disk, so `ops × latency` can be wrong by ~10× in either direction:
it oversizes high-concurrency single-row workloads (YCSB) and can undersize transactional ones,
where commits and follower applies burn CPU outside the timed statement.

```
Workload cores = (Read QPS × ms/read + Write QPS × ms/write × RF scale) / 1000
                 × workload multiplier × RPC multiplier × (1 + CDC + xCluster)
                 × arch factor × hardware scale
RF scale       = (1 + (RF − 1) × 0.33) / (1 + 2 × 0.33)        (= 1.0 at RF=3; RF5 = 1.40)
```

Costs are for current-generation CPUs (m7i/m8i/m8g class) at RF=3, before the RPC multiplier.

## Sources

| API | Calibration source | Derived |
|---|---|---|
| YSQL `oltp` | TPC-C-style transactional benchmark, YB 2026.1, current-generation x86, RF=3 | 1.070 ms/read, 1.784 ms/write |
| YSQL `kv` | Point-select benchmark, YB 2026.1, RF=3 | multiplier 0.214 on the `oltp` cost |
| YCQL `point` | Public YCQL key-value benchmark (YugabyteDB docs): 150k reads/s and 90k writes/s at 60% CPU on 3 × 16 cores (i3.4xlarge, older release) | measured 0.178 / 0.296 ms → 0.107 / 0.178 ms after the ×0.6 hardware-generation factor |

Derivation: busy cores − connection CPU − tablet-maintenance CPU = workload cores, divided by the
RPC multiplier and the operation rate. A transactional mix can't separate read from write cost on
its own, so the YSQL split uses YCQL's measured write:read ratio (1.67). Cross-check: YSQL point
ops cost ≈ 2.1× YCQL point ops for both reads and writes.

The test suite checks the calibrated constants and reproduces the public YCQL key-value benchmark
(60% CPU with the hardware factor undone).

**YCQL hardware-generation factor (×0.6).** No current YCQL run is published. Per-vCPU throughput
from Broadwell to current Xeon/Graviton (3–4 generations of IPC gains plus higher clocks) is
roughly 1.6–1.8×, so YCQL costs are scaled by 0.6 (≈1.67×). Conservative: it ignores
YugabyteDB software improvements. Replace it with a current YCQL run when one exists.

## Workload profiles (`--workload`)

| API | Profile | Multiplier | Use for | Auto-selected when |
|---|---|---|---|---|
| YSQL | `kv` | 0.214× (measured) | Point lookups / single-row writes by primary key | Write% ≥ 70 |
| YSQL | `oltp` | 1.0× (measured baseline) | Transactional OLTP with indexes, multi-statement transactions (TPC-C-like) | otherwise |
| YSQL | `complex` | 2.0× | Joins / aggregations | Read% ≥ 70 |
| YSQL | `analytics` | 5.0× | Scans / reporting queries | Read% ≥ 90 |
| YSQL | `htap` | 1.0× + scan CPU | OLTP statements at the `oltp` cost, plus analytical scans (below) | never (pass it) |
| YCQL | `point` | 1.0× (measured baseline) | Reads/writes by full primary key | otherwise |
| YCQL | `range` | 2.7× | Single-partition range reads (clustering-key ranges) | Read% ≥ 90 |
| YCQL | `lwt` | 4.0× | Lightweight transactions (`IF NOT EXISTS`/`IF`) or writes to indexed tables | never (pass it) |

`complex`, `analytics`, `range` and `lwt` follow the relative cost of the original exec-time tiers
(YSQL 7–10 / 20 ms vs 4 ms mixed; YCQL 2 / 3 ms vs 0.75 ms point); they are not measured.
Auto-selection from the read/write mix is a weak signal — pass `--workload` whenever the user
describes their queries.

## Transactions (`--tps`, `--statements-per-txn`, `--distributed-txn-pct`)

```
QPS                 = TPS × statements per transaction
Write transactions  = TPS × (1 − (1 − Write%)^statements)      (share with ≥ 1 write)
Distributed overhead = distributed% × (write txns × 1.0 + write statements × 1.0)
                       × fast-path write cost                    (commit record + intents)
```
A distributed transaction writes a commit record to the transaction-status tablet (Raft-replicated)
and a provisional record (intent) per row that is applied later. A single-statement write inside
`BEGIN … COMMIT` therefore costs ≈3× a fast-path write — in line with the YB Friday Tech Talk
benchmark (ep. 107): 1.7 vs 4.7 ms and ~6,000 vs ~2,000 transactions in the same interval.

Any `BEGIN` block with writes is distributed, so `--distributed-txn-pct` defaults to 100 when
statements per transaction > 1, and 0 for autocommit. The overhead is **added only to fast-path
profiles** (YSQL `kv`, YCQL `point`/`range`): `oltp` was measured on a TPC-C-style workload, whose transactions are
distributed, so it — and `complex`/`analytics`, which scale from it — already include it, as does
YCQL `lwt`. The 1.0 / 1.0 unit costs are estimates; calibrate when the user has a cluster.

## Read offload and multi-region

- **Follower reads** (`--follower-read-pct`): the same CPU per read, but served by the nearest
  replica instead of the leader. Total CPU is unchanged with balanced leaders; the cached working
  set widens towards all replicas; cross-zone/region read traffic drops.
- **Read replicas** (`--read-replica-read-pct`): those reads leave the primary cluster. The replica
  cluster is sized separately: its reads plus applying every write once per copy, at the follower
  apply cost (0.33 of a write's leader cost).
- **Preferred region** (`--regions RF --preferred-region`): all leaders in one region, so that
  region carries every leader read and leader write while the others only apply follower writes.
  Each region is sized for the leader load (it must take over on failover), which can multiply
  node count; follower reads move reads back to the other regions.

## HTAP analytics (`--workload htap`, `--analytics-*`)

Analytical queries are sized by rows scanned, not per statement:
```
Scan cores = analytical queries/s × rows scanned per query × 2 µs/row × arch × hardware scale
```
The 2 µs/row assumes filters and aggregates are pushed down to DocDB (`Remote Filter`,
`Partial Aggregate`). A single-stream filtered scan of 38.9M wide (320-byte) rows took ~81 s of
storage time (≈2.1 µs/row, GitHub #32973); parallel `COUNT(*)` scans reach 1.5–7.5M rows/s on
3-node clusters. Queries that pull rows into PostgreSQL (joins, sorts, hash aggregates) cost more —
raise `--scan-cpu-us-per-row` or calibrate.

Where the scans run (`--analytics-target`):
- `primary` (default): scan CPU is added to the OLTP cluster; scanned data that misses the cache
  (leader data vs half the RAM) becomes disk reads at 256 KiB per IOP. Scans also evict the OLTP
  working set.
- `followers`: the same CPU, spread over all replicas (read-only, bounded staleness); each node now
  serves all of its replicas, so the cached working set widens and more scans hit disk.
- `read-replica`: scans move to the read-replica cluster, sized for scan CPU, follower applies and
  its own disk limits — the OLTP cluster is untouched.

Without inputs, `--workload htap` assumes 1 query/s × 1M rows and flags it.

## Adjustments

| Flag | Default | Use |
|---|---|---|
| `--cpu-arch arm` | x86 | Graviton used ~10% more CPU than x86 for the same OLTP load (×1.10) |
| `--cpu-cost-scale` | 1.0 | Older target hardware: ~1.15 previous gen (m6i/c6i/m6g), ~1.5 for m5/c5/i3 |
| `--cpu-ms-per-read`, `--cpu-ms-per-write` | profile | The user's own measured costs |
| `--rpc-overhead` | YSQL 0.15, YCQL 0.08 | Retries and real-world variance on top of the measured costs (costs were derived net of it) |

## Calibration (`--observed-*`) — preferred whenever a cluster exists

```
Observed busy cores  = observed CPU% × observed nodes × observed vCPU/node
Workload cores       = busy cores − connection CPU − tablet CPU   (at the observed size)
CPU seconds/s needed = Workload cores × (target QPS / observed QPS)
```
Only `--observed-qps` and `--observed-cpu-pct` are required; the observed cluster defaults to RF
nodes × the target vCPU/node. Assumes the same read/write mix, RF, features and CPU architecture.
The report shows `factor_vs_model` — how the user's workload compares with the chosen profile.

## Latency and the concurrency check

`--avg-exec-ms` is a latency. It feeds Little's law: in-flight requests per node = QPS × latency /
nodes. For YSQL, if that exceeds the backend pool (16/vCPU, or 10/vCPU with Connection Manager),
the report flags the workload as connection-bound.

Multi-AZ (the default topology) adds a cross-AZ round trip to writes — typically well under a
millisecond within a region — and leaves CPU per op essentially unchanged.

## Legacy model (`--cpu-model latency`)

`Effective ops × exec time`, with the old exec-time fallback tiers (YSQL 2 / 4 / 7 / 20 ms; YCQL
0.75 / 1 / 2 ms). Kept for comparison only.

## Measuring real numbers

- **YSQL**: `pg_stat_statements` (calls and mean latency per statement) plus node CPU% from YBA,
  Prometheus or `top`:
  ```sql
  SELECT query, calls, round(mean_exec_time::numeric, 2) AS avg_ms
  FROM pg_stat_statements ORDER BY calls DESC LIMIT 20;
  ```
- **YCQL**: `http://<tserver>:12000/statements` per node; with YSQL enabled, the
  `ycql_stat_statements` view (`CREATE EXTENSION yb_ycql_utils;`).

## Capacity per node (sanity check)

Client QPS one node sustains at the 65% CPU target — 30% writes / 70% reads, RF=3, x86,
current-gen CPUs, default connections, disk limits ignored:

| API / profile | 8 vCPU | 16 vCPU | 32 vCPU |
|---|---|---|---|
| YSQL `kv` | ~15,600 | ~31,000 | ~62,500 |
| YSQL `oltp` | ~3,300 | ~6,700 | ~13,400 |
| YSQL `complex` | ~1,700 | ~3,300 | ~6,700 |
| YSQL `analytics` | ~670 | ~1,300 | ~2,700 |
| YCQL `point` | ~37,500 | ~75,000 | ~150,000 |
| YCQL `range` | ~13,900 | ~27,800 | ~55,700 |
| YCQL `lwt` | ~9,400 | ~18,800 | ~37,600 |

For comparison, the public YCSB runs on 2017-era c5.4xlarge reached ≈26k reads/s per node (YSQL
workload C) and ≈63k (YCQL workload C) near saturation.
