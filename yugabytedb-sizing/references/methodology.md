# Calculation Methodology

What `scripts/sizing_calc.py` computes, step by step. Use this to explain a result or to verify a
figure by hand. Defaults are listed in [defaults.md](defaults.md); the CPU model is in
[cpu-model.md](cpu-model.md).

## Step 1–2: Operations
```
QPS (statements/s) = --qps, or --tps × --statements-per-txn
Write ops/s = QPS × Write% / 100          Read ops/s = QPS × Read% / 100
RPC multiplier = 1 + rpc_overhead          (YSQL 1.15, YCQL 1.08)
Effective ops/s = Write ops × RF × RPC + Read ops × RPC   (reported; drives the legacy model only)
```

## Step 3: Workload CPU
Per-op model (default) — see [cpu-model.md](cpu-model.md):
```
Workload cores = (Reads × ms/read + Writes × ms/write × RF scale) / 1000
                 × workload × RPC × (1 + CDC + xCluster) × arch × hardware scale
```
Plus, for fast-path profiles (YSQL `kv`, YCQL `point`/`range`), the distributed-transaction
overhead (commit + intents) — see [cpu-model.md](cpu-model.md#transactions---tps---statements-per-txn---distributed-txn-pct).
Reads served by a read-replica cluster are removed from the primary cluster's reads.

With `--preferred-region`, the primary's CPU demand is `max(balanced, leader-region cores × regions)`,
because every region must be able to carry the leader load:
```
Leader region   = (leader reads + follower reads / RF) × ms/read + writes × ms/write × leader share
                  + distributed overhead                  (leader share = 1 / (1 + (RF − 1) × 0.33))
Follower region = follower reads / RF × ms/read + writes × ms/write × follower share / (RF − 1)
```

Calibration replaces this with `observed workload cores × target QPS / observed QPS`.

Connection CPU (YSQL): `backends/node × 0.002` cores per node; backends = 16 × vCPU (10 × vCPU with
Connection Manager). YCQL connections are multiplexed by drivers and not modeled.

```
Raw vCPUs = Workload cores + tablet maintenance vCPU (Step 11)
```

## Step 4–5: Node count (CPU)
```
First pass   = Raw vCPUs / 0.65 → ceil(÷ vCPU/node) → round up to a multiple of RF (min RF)
Verify       = (Raw vCPUs + connection CPU/node × nodes) / (nodes × vCPU/node) ≤ 65%
               otherwise add RF nodes and re-check
```
Then, in order, more nodes are added in RF multiples when:
1. storage/node exceeds the 20 TB cap (Step 6),
2. per-node IOPS or disk throughput exceeds the disk limits (Step 8),
3. `--size-for node|zone` and that failure scenario exceeds the failure CPU ceiling (Step 12).

## Step 6: Storage
```
Index           = Table size × index overhead            (YSQL 20%, YCQL 10%)
Compressed      = (Table + Index) × 0.70                 (30% LZ4)
Replicated      = Compressed × RF
Base/node       = Replicated / nodes
Compaction      = Base/node × 0.20
WAL rate        = Write ops × RF × row size × (1 + index overhead)          (cluster-wide)
WAL retention   = 900 s; 24 h with xCluster; 8 h with CDC (longer applies)
WAL/node        = WAL rate × retention / nodes
Storage/node    = Base/node + Compaction + WAL/node       (cap 20 TB/node)
```
WAL follows write throughput, not data size. `--wal-overhead 0.10` restores the legacy "10% of
data" WAL model. Why 20% compaction reserve: LSM compaction needs room for old and new SSTables to
coexist; without 15–25% free space, compaction stalls and write amplification spikes.

## Step 7: Memory
```
Base ratio      = 1:4 vCPU:RAM; read-heavy (Write% < 50) and leader data/node > cache of a
                  1:4 node (half its RAM) → 1:8
Base memory     = vCPU/node × ratio                       (total node RAM)
PostgreSQL share = Base memory × 0.27                     (use_memory_defaults_optimized_for_ysql)
Connection mem  = backends/node × 15 MB                   (TPC-C PSS per backend)
Connection extra = max(0, Connection mem − PostgreSQL share)
Memory/node     = Base + Connection extra + 200 MB odyssey (Connection Manager) + tablet RAM/node
                  → round up to RAM tier (16, 32, 64, 128, 192, 256, 384, 512 …),
                    staying on a tier exceeded by ≤ 2%
```
YCQL has no PostgreSQL share or connection memory; recommend
`--use_memory_defaults_optimized_for_ysql=false` (TServer gets ~85% of RAM) and optionally
`--enable_ysql=false` for YCQL-only clusters. Raise `--mem-mb-per-conn` for queries with large
sorts/hash joins (`work_mem`).

## Step 8: IOPS and disk throughput
```
Leader data/node    = Compressed / leader-holding nodes   (all nodes; preferred region: its nodes)
Hot data/node       = Leader data + follower-read share × (all replica data/node − Leader data)
Cache/node          = Memory/node × 0.5                   (block cache + OS page cache)
Read cache miss     = max(0, 1 − Cache / Hot data)        (uniform access; --read-cache-miss fixes it)
Replicated writes/node = Write ops × RF / nodes
IOPS/node           = Replicated writes × 0.245 + Reads/node × miss
Disk MiB/s/node     = Replicated writes × row size × (1 + index overhead) × 19 / 1,048,576
```
Calibrated against TPC-C (3,503 IOPS, 92.6 MiB/s per node; 94 GB leader data vs 32 GB cache) and
sysbench (≈0 read IOPS with 8 GB leader data in a 16 GB cache). Uniform access is conservative:
skewed workloads with a hot subset miss less.

**Disk limits** (`--disk-iops`, `--disk-mibps`; default 16,000 IOPS / 1,000 MiB/s = gp3 maximum):
nodes are added until both fit. gp3 includes 3,000 IOPS / 125 MiB/s; anything above is
provisioned. For NVMe (i4i, i8g) or io2, pass their limits to avoid adding nodes for I/O.

## Step 9: Network
```
Write MiB/s/node = Write ops / nodes × RF × row size × 13.9 / 1,048,576
Read MiB/s/node  = Read ops / nodes × row size × 3.2 / 1,048,576
Cross-zone/node  = (Write + leader-read traffic) × (zones − 1) / zones   (≈ 2/3; follower reads stay local)
Cross GB/month   = Cross/node × nodes / 2 × seconds/month   (each transfer counted once)
Cross cost       = GB/month × $0.02                       (inter-AZ $0.01 each way; inter-region ~$0.02)
```
With `--regions`, each region is a zone and the cross traffic is inter-region.
Factors calibrated against TPC-C (60.2 MiB/s/node) and sysbench (29.7 MiB/s/node), at the default
512-byte row. Keep total traffic below ~40% of NIC capacity.

## Step 10: Storage growth
```
Storage after N yr/node = Base/node × (1 + growth)^N × 1.20 + WAL/node
```
**YCQL TTL** (`--ttl-days`): data plateaus at `max(ingest/day × TTL days, table size)`, where
ingest/day = Write ops × row size × 86,400 (every write a new row — upper bound). Growth
projections are capped at that steady state. Expired data is reclaimed by compaction; prefer a
table-level `default_time_to_live`.

## Step 11: Tablets
```
Total tablets = max(Data-driven, Schema) × RF
Data-driven   = automatic splitting of the compressed data (as one table):
                128 MiB until 1/node, 10 GiB until 24/node, then 100 GiB; ≤ 50 per tserver
Schema        = objects × tablets/table   (YSQL 1; YCQL 1 per tserver, or 1–2 per cluster at ≤4 cores)
Tablet CPU    = tablets / 1000 × 0.4 vCPU     Tablet RAM = tablets / 1000 × 700 MB  (cluster-wide)
```
YCQL tablet counts grow with node count and are recomputed on every sizing pass. YCQL has no
colocation, so many small tables cost tablets. Many large tables split independently and can
exceed the data-driven estimate — pass `--num-objects` with `--tablets-per-table` if known.

## Multi-region latency and concurrency
```
Without write pipelining:
  Added latency/statement = RTT × (Write% + remote leader reads + distributed commits per statement)
With write pipelining (--write-pipelining, YSQL):
  Added latency/statement = RTT × (write txns × (2 if distributed else 1) / QPS + remote leader reads)
remote leader reads     = Read% × (1 − follower − replica read share) × (regions − 1)/regions
                          (0 with --preferred-region: clients in the leader region)
In-flight/node          = QPS × (latency + added) / leader-holding nodes
```
Writes always wait for a quorum in another region. With `ysql_enable_write_pipelining`, a write is
acknowledged once the leader has applied it and replicated in the background; COMMIT waits for
all of them and then commits at the status tablet, so a transaction pays ~2 round trips instead of
one per write (documented as 10–30% lower TPC-C latency; larger across regions). Autocommit
single-row writes still pay one round trip. CPU is unchanged; lower latency means fewer requests
in flight. Pass a latency measured in the target topology with `--region-rtt-ms 0` to avoid adding
it twice.

## HTAP analytics
```
Scan cores        = analytical q/s × rows/query × 2 µs/row × arch × hardware scale
Scanned MiB/s     = q/s × rows × row size × 0.70 (compressed) / serving nodes
Scan disk MiB/s   = Scanned MiB/s × max(0, 1 − cache / scanned data per node)
Scan IOPS         = Scan disk MiB/s × 1024 / 256            (256 KiB sequential I/Os)
```
Added to the primary (`primary`, `followers`) or to the read-replica cluster (`read-replica`), and
included in the disk-limit checks of whichever cluster runs them. On the primary, analytics also
make the memory rule treat the workload as read-heavy (1:8 when its data doesn't fit a 1:4 cache).

## Read-replica cluster
```
CPU     = replica reads × ms/read + writes × follower apply × copies       (× workload × RPC × arch)
Nodes   = smallest multiple of copies with (CPU + connection CPU) ≤ 65%,
          then more until IOPS and disk MiB/s (applies + scans) fit the disk limits
Storage = Compressed × copies / nodes × 1.20 + WAL
Memory  = 1:4, or 1:8 if its data/node doesn't fit a 1:4 node's cache
```

## Step 12: Failure resilience
Multi-AZ by default: zones = RF, one replica per zone, nodes spread evenly. `--zones 1` models a
single AZ (no zone-failure scenario; a zone outage takes the whole cluster down). With
`--regions`, the fault domain is a region. With `--preferred-region`, losing the leader region
moves leadership to an equal-sized region (same CPU as normal), while losing one leader-region
node concentrates its share on the remaining leader-region nodes.
```
1-node failure: surviving = nodes − 1
1-zone failure: surviving = nodes − nodes/zones
CPU% = (Raw vCPUs + connection CPU × surviving) / (surviving × vCPU/node)
```
Flag any scenario above the failure CPU ceiling (default 65%). `--size-for zone` keeps adding RF
nodes until a zone loss fits; recommend it for production.
