# Worked Examples

## YSQL

**Input**: 10,000 QPS, 30% write / 70% read, `--workload oltp`, 5 ms latency, RF=3,
16 vCPU/node, 500 GB table, multi-AZ (default)

```
Reads = 7,000/s | Writes = 3,000/s

CPU (per-op, oltp ×1.0, RF scale 1.0):
  Reads  = 7,000 × 1.070 ms = 7.49 core-s/s
  Writes = 3,000 × 1.784 ms = 5.35 core-s/s
  × 1.15 RPC                = 14.77 cores, + tablets (126 × 0.4/1000) ≈ 0.05 → Raw vCPUs ≈ 14.8

First pass: 14.8 / 0.65 = 22.8 vCPU → 2 → 3 nodes (minimum RF)
Verify: connection CPU = 256 × 0.002 × 3 = 1.54 → 16.4 / 48 = 34.1% ✅

Concurrency: 10,000 × 5 ms = 50 in flight → 16.7/node of 256 backends ✅

Storage: 600 GB raw → 420 GB compressed → 1,260 GB with RF3
  Base/node 420.0 + compaction 84.0 + WAL 1.5 (1.76 MiB/s × 900 s) = 505.5 GB

Memory: leader data 140 GB/node > 32 GB cache of a 1:4 node → 1:8 = 128 GB;
  256 backends × 15 MB = 3.8 GB fits the 34.6 GB PostgreSQL share → 128 GB

I/O per node:
  Read cache miss = 1 − 64 GB cache / 140 GB leader data = 54%
  IOPS = 3,000 replicated writes × 0.245 + 7,000/3 × 0.54 = 735 + 1,267 = 2,002   (gp3 ✅)
  Disk = 3,000 × 512 B × 1.20 × 19 = 33.4 MiB/s

Network per node: writes 20.4 + reads 3.7 = 24.0 MiB/s
  Cross-AZ ≈ 2/3 = 16.0 MiB/s/node ≈ 61,700 GB/month ≈ $1,230/month

Failure (3 zones × 1 node): 1-node / 1-zone (2 nodes) = (14.8 + 1.02) / 32 = 49.5% ✅
```

The legacy latency model (`--cpu-model latency`, 5 ms × effective ops) sizes the same workload at
12 nodes — it treats 5 ms of latency as 5 ms of CPU on every replica.

## YCQL

**Input**: `--api ycql`, 100,000 QPS, 30% write / 70% read, `--workload point`, 1 ms latency,
RF=3, 16 vCPU/node, 500 GB table, gp3 (default disk limits)

```
Reads = 70,000/s | Writes = 30,000/s

CPU (per-op, point ×1.0, current-gen costs):
  Reads  = 70,000 × 0.107 ms = 7.48 core-s/s
  Writes = 30,000 × 0.178 ms = 5.33 core-s/s
  × 1.08 RPC                 = 13.83 cores, + tablets ≈ 0.05 → Raw vCPUs ≈ 13.9
  → 3 nodes at 28.9% CPU would be enough …

… but disk doesn't fit at 3 nodes:
  Leader data 128 GB/node vs 64 GB cache → 50% read miss
  IOPS = 30,000 × 0.245 + 23,333 × 0.50 = 7,350 + 11,667 ≈ 19,000 > 16,000 (gp3 max)
  → disk limit adds 3 nodes → 6 nodes

At 6 nodes:
  CPU 14.5%; zone failure (4 nodes) 21.7% ✅
  IOPS = 15,000 × 0.245 + 11,667 × 0.003 = 3,705 (leader data 64 GB now fits the cache)
  Disk = 15,000 × 512 B × 1.10 × 19 = 153 MiB/s
  Network 120 MiB/s/node; cross-AZ 80 MiB/s/node ≈ 617,000 GB/month ≈ $12,300/month
  Storage 238 GB/node; memory 128 GB (1:8)
```

With NVMe or io2 (`--disk-iops 200000 --disk-mibps 4000`) the same workload stays at 3 nodes and
28.9% CPU. This is the case to call out: CPU is not the constraint — disk is — so present the
choice between more gp3 nodes and fewer NVMe/io2 nodes. Note the cross-AZ transfer cost, which
is significant at this throughput.

## YSQL, multi-region with a preferred region

**Input**: 20,000 QPS, 30% write / 70% read, `--workload oltp`, 2 ms latency, RF=3, 16 vCPU/node,
500 GB, `--regions 3 --preferred-region --size-for zone` (30 ms RTT default)

```
Balanced leaders (--regions 3):  3 nodes, 64.8% CPU; +23 ms per statement (writes + 2/3 of reads remote)

Preferred region:
  Leader region carries all leader reads + leader writes; follower regions apply writes only
  → every region sized for the leader load → 9 nodes (3 per region)
  Leader region CPU 54.5%, follower regions 8.4%
  Region failure: an equal-sized region takes leadership → 54.5% ✅
  Node failure in the leader region: 80.2% ⚠️ (its share moves to 2 nodes)
  Added latency: +9 ms per statement (writes only); 73 in-flight requests per leader-region node

With --follower-read-pct 60: 6 nodes — follower reads move reads to the other regions
```

Write pipelining matters once transactions have several statements. At 3,000 TPS × 6 statements
(`kv`, 30% writes) with leaders pinned: +13.4 ms per statement without pipelining (connection-bound:
277 in flight vs 256 backends on the leader-region node) and +8.8 ms with `--write-pipelining`
(195 in flight) — same CPU.

Pinning leaders buys lower read latency for the app's region at the cost of idle capacity in the
other regions; follower reads (if staleness is acceptable) recover much of it.
