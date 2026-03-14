---
name: yugabytedb-sizing
description: >
  Calculate and recommend optimal YugabyteDB cluster sizing based on workload inputs. Use this
  skill whenever the user asks about YugabyteDB cluster sizing, capacity planning, node count,
  vCPU requirements, storage sizing, memory recommendations, or wants to know how many nodes
  they need for a given workload. Trigger when user mentions "size my cluster", "how many nodes",
  "YugabyteDB capacity", "YugabyteDB hardware requirements", QPS with YugabyteDB, or asks about
  CPU/storage/memory for a YugabyteDB deployment. Also trigger when the user provides workload
  metrics (QPS, read/write ratio, table size) and asks for infrastructure recommendations.
---

# YugabyteDB Cluster Sizing Calculator

This skill performs accurate YugabyteDB cluster sizing from workload inputs, producing a complete hardware recommendation optimized for ≤65% CPU utilization. Always use the bundled python script to perform the calculations. Make sure that the RF is always an odd number (1, 3, 5 and 7); minimum 3 for production and do not go beyond 7. Do not recommend RF=1 for production. Always specify that the sizing is indicative and the user should test with their actual workload to fine-tune the sizing.

## Inputs

### User-Provided (ask if not given)
| Input | Required? | Description |
|---|---|---|
| QPS | Required | Total queries per second at peak |
| Write % | Required | Percentage of operations that are writes |
| Read % | Required | Percentage of operations that are reads |
| Average execution time (ms) | Optional | Average time per DB operation — see fallback below if unknown |
| vCPU/node | Required | Desired or candidate vCPU count per node |
| Replication Factor (RF) | Required | Typically 3 (odd number: 1, 3, 5) |
| Table size (GB) | Required | Raw/uncompressed data size |
| Avg row size (bytes) | Optional | Used for IOPS estimation; default 512 bytes if unknown |
| Data growth rate (%/yr) | Optional | Used to project storage over 1–2 years; default 30%/yr if unknown |

### Fixed / Default Values
| Parameter | Value | Notes |
|---|---|---|
| RPC, Retries and Index lookups | 1.2 | Covers joins, index lookups, retry overhead |
| Index Storage Overhead | 20% | Additional storage for indexes |
| Compression ratio | 30% reduction | YugabyteDB LZ4 compression applied to storage |
| WAL Overhead | 10% | Write-Ahead Log storage on top of compressed+replicated data |
| Compaction Free Space | 35% | Reserved headroom for LSM-tree compaction operations |
| Connection CPU overhead | ~0.2% CPU/connection | Background CPU per connection (auth, keepalive, memory mgmt) |
| Target CPU utilization | 65% | Maximum sustained CPU utilization (before connection overhead) |
| Memory ratio | 1:4 or 1:8 vCPU:RAM | Use 1:4 for write-heavy; 1:8 for read/cache-heavy |
| PG connections/node | 32 × vCPU/node | Standard sizing: 32 connections per vCPU |
| Memory per connection | 60 MB | RAM reserved per PostgreSQL connection |
| Max storage per node | 20 TB (20,480 GB) | Hard cap on disk density per node; extra nodes added if exceeded |
| Default avg row size | 512 bytes | Used for IOPS if row size not provided |
| Default data growth rate | 30% per year | Used for storage projection if not provided |
| IOPS per write op | RF writes to disk per op | Each write op = RF SSTable writes |
| Network overhead factor | 2× write Ops/s | Inter-node Raft traffic ≈ 2× write ops for RF=3 |

### Avg Execution Time — Fallback When Unknown

If the user cannot provide avg execution time, derive it from workload profile:

| Workload Type | Condition | Default Avg Exec Time |
|---|---|---|
| Simple key-value / point lookup | Write% ≥ 70% or very low complexity | 1–2 ms |
| Mixed OLTP (typical) | 40–70% reads, indexed queries | 3–5 ms |
| Read-heavy with joins/aggregations | Read% ≥ 70%, complex queries | 5–10 ms |
| Operational analytical / reporting queries | Read% ≥ 90%, scans | 10–30 ms |

**Script flag**: `--avg-exec-ms` is required by default. Pass the estimated value based on the
table above, and clearly note in the output that it is an estimate. Always recommend the user
profile their actual workload with `EXPLAIN ANALYZE` or YugabyteDB's query stats view
(`pg_stat_statements`) to replace the estimate with measured data.

```sql
-- Enable and query pg_stat_statements for real execution times:
SELECT query,
       calls,
       round(mean_exec_time::numeric, 2) AS avg_ms,
       round(total_exec_time::numeric, 2) AS total_ms
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 20;
```

---

## Running the Sizing Calculator

A Python script is bundled at `scripts/sizing_calc.py`. **Always run this script** rather than
performing manual calculations — it handles the iterative node-sizing loop, connection overhead,
and all rounding automatically.

### Basic usage
```bash
python3 scripts/sizing_calc.py \
  --qps <value> \
  --write-pct <value> \
  --read-pct <value> \
  --avg-exec-ms <value> \
  --vcpu-per-node <value> \
  --rf <value> \
  --table-size-gb <value>
```

### Try multiple vCPU tiers to find the optimal fit
```bash
for vcpu in 8 16 32; do
  echo "=== ${vcpu} vCPU/node ===" 
  python3 scripts/sizing_calc.py --qps 10000 --write-pct 30 --read-pct 70 \
    --avg-exec-ms 5 --vcpu-per-node $vcpu --rf 3 --table-size-gb 500
done
```

### Override any fixed parameter if needed
```bash
python3 scripts/sizing_calc.py ... --target-cpu-util 0.70 --conn-per-vcpu 16
```

### JSON output (for programmatic use)
```bash
python3 scripts/sizing_calc.py ... --json
```

All fixed defaults (RPC overhead 1.2×, index 20%, compression 30%, WAL 10%, compaction 35%,
32 conn/vCPU, 60 MB/conn) are baked in and match the parameters table below. Override any fixed parameter if needed based on user inputs.

---

## Step-by-Step Calculation Reference

The steps below document what the script computes. Use this to explain results to the user
or to manually verify a specific figure.

### Step 1: Derive Ops/s
```
Write Ops/s = QPS × (Write% / 100)
Read Ops/s  = QPS × (Read%  / 100)
```

### Step 2: Apply Overhead Multiplier
YugabyteDB uses Raft replication + DocDB RPC internally. Account for this:
```
Effective Write Ops/s = Write Ops/s × RF × RPC_Overhead
Effective Read Ops/s  = Read Ops/s  × RPC_Overhead
Total Effective Ops/s = Effective Write Ops/s + Effective Read Ops/s
```
Note: Writes are multiplied by RF because each write is replicated to RF nodes.
Reads go to the leader by default (RF not multiplied unless using follower reads).

### Step 3: Calculate CPU Cores Required
Each active PostgreSQL connection consumes background CPU for authentication, memory management,
and idle keepalive work — approximately 0.5–1% of a CPU core per connection under load.

```
CPU seconds/s needed    = Total Effective Ops/s × (Avg Execution Time ms / 1000)
Raw vCPUs (workload)    = CPU seconds/s needed

PG connections/node     = 32 × vCPU/node   (fixed: 32 connections per vCPU)
Connection CPU overhead = PG connections/node × 0.008  (≈0.8% CPU core per connection)

Total Raw vCPUs needed  = Raw vCPUs (workload) + (Connection CPU overhead × Total nodes)
                          [iterate: re-check after Step 4 since node count affects this]
Adjusted vCPUs          = Total Raw vCPUs / Target CPU Utilization (0.65)
```
> **Iteration note**: Connection overhead depends on nodes and vCPU/node, which aren't known until
> Step 4. Use a first-pass estimate, then verify after Step 5 and adjust if utilization exceeds 65%.

### Step 4: Determine Node Count
```
Nodes (CPU-based) = ceil(Adjusted vCPUs / vCPU_per_node)
```
**Round up to nearest multiple of RF** to ensure even tablet distribution.
```
Total nodes = ceil(Nodes_CPU_based / RF) × RF
```
Minimum nodes = RF (never go below replication factor).

### Step 5: Verify and Compute Per-Node Metrics
```
vCPU/node             = chosen vCPU tier (e.g., 8, 16, 32)
Total vCPU            = Total nodes × vCPU/node
PG connections/node   = 32 × vCPU/node
Connection CPU/node   = PG connections/node × 0.008  (CPU cores consumed by connections)
Total connection CPU  = Connection CPU/node × Total nodes

Effective vCPUs used  = Adjusted vCPUs (workload) + Total connection CPU
CPU utilization       = Effective vCPUs used / Total vCPU   → must be ≤ 65%

DB Ops/node/s         = Total Effective Ops/s / Total nodes
```
If utilization exceeds 65% after adding connection overhead, increase node count by RF increment.

### Step 6: Storage Calculation
```
Index Size (GB)          = Table size × 0.20
Total Raw (GB)           = Table size + Index Size
After Compression (GB)   = Total Raw × 0.70          (30% LZ4 compression)
With Replication (GB)    = After Compression × RF
Base Storage/node (GB)   = With Replication / Total nodes

WAL overhead             = Base Storage/node × 0.10   (10% for Write-Ahead Log)
Compaction reserve       = Base Storage/node × 0.35   (35% free space for LSM compaction)

Recommended Storage/node = Base Storage/node + WAL overhead + Compaction reserve
                         = Base Storage/node × (1 + 0.10 + 0.35)
                         = Base Storage/node × 1.45

If Storage/node > 20,480 GB (20 TB cap):
    Add nodes in RF multiples until Storage/node ≤ 20,480 GB
    Flag in output how many nodes were added due to storage cap
```
> **Why 35% compaction reserve?** YugabyteDB's DocDB uses an LSM-tree (like RocksDB). Compaction
> merges SSTables in the background and requires temporary space for both old and new files to
> coexist. Without adequate free space, compaction stalls and write amplification spikes.
> A minimum of 30–40% free space is required for healthy compaction; 35% is the recommended default.

### Step 7: Memory Recommendation
Each PostgreSQL connection reserves 60 MB of RAM for its session state (working memory, sort
buffers, stack). This is additive on top of the shared block cache.

```
Connection memory/node  = PG connections/node × 60 MB
                        = (32 × vCPU/node) × 60 MB
                        = 1,920 MB per vCPU  →  ≈ 1.875 GB per vCPU

If Write% ≥ 50%:  Base memory/node = vCPU/node × 4 GB   (write-heavy, smaller read cache)
If Write% < 50%:  Base memory/node = vCPU/node × 8 GB   (read-heavy, large block cache)

Recommended memory/node = Base memory/node + Connection memory/node
                        = Base memory/node + (32 × vCPU/node × 0.06 GB)
                          [round up to standard RAM tier: 32, 64, 128, 256 GB]

Total Memory = Recommended memory/node × Total nodes
```

### Step 8: IOPS Estimation
IOPS requirements are driven by write ops (SSTable flushes) and compaction read/write amplification.

```
Write IOPS/node = (Write Ops/s × RF × RPC_Overhead) / Total nodes × write_amp_factor
                  where write_amp_factor ≈ 3–5 for LSM (use 4 as default)
Read IOPS/node  = (Read Ops/s × RPC_Overhead) / Total nodes
                  (reads may be cache-hit; assume 30% cache miss rate for cold estimate)
Cold Read IOPS  = Read IOPS/node × 0.30

Total IOPS/node = Write IOPS/node + Cold Read IOPS/node
```
Cloud SSD baseline: ~3,000 IOPS/TB provisioned. For NVMe: ~30 IOPS/GB.
If Total IOPS/node exceeds provisioned IOPS, increase storage volume or use NVMe.

### Step 9: Network Bandwidth Estimation
Inter-node Raft traffic can saturate NICs on write-heavy workloads.

```
Avg row size (bytes)        = user-provided or default 512 bytes
Write throughput/node (MB/s) = (Write Ops/s / Total nodes) × RF × Avg row size / 1,048,576
Read throughput/node (MB/s)  = (Read Ops/s  / Total nodes) × Avg row size / 1,048,576
Total network/node (MB/s)    = Write throughput/node + Read throughput/node
```
Rule of thumb: keep total inter-node traffic below 40% of NIC capacity (e.g., 4 Gbps of a 10 Gbps NIC).
If exceeded, consider larger nodes (fewer, higher-bandwidth NICs) or dedicated replication NICs.

### Step 10: Storage Growth Projection
Size storage for 1–2 years, not just current data.

```
Growth rate (%/yr)          = user-provided or default 30%
Storage after 1 yr/node     = storage_per_node × (1 + growth_rate)
Storage after 2 yr/node     = storage_per_node × (1 + growth_rate)²
```
Present the 2-year projection alongside the current recommendation so the user can provision
storage volumes that won't require resizing soon after launch.

---

## Output Format

Present results in this structure:

```
════════════════════════════════════════════
  YugabyteDB Cluster Sizing Recommendation
════════════════════════════════════════════

INPUT SUMMARY
─────────────
  QPS:                    {value}
  Write / Read:           {write%} / {read%}
  Avg execution time:     {value} ms  ⚠️ estimated ({workload type}) ← only if estimated
  Replication Factor:     {RF}
  Table size:             {value} GB
  Avg row size:           {value} bytes  ({provided | default 512})
  Data growth rate:       {value}%/yr   ({provided | default 30%})

DERIVED WORKLOAD
────────────────
  Write Ops/s:            {value}
  Read Ops/s:             {value}
  Effective Ops/s (w/ RF + overhead): {value}
  vCPUs required (at 65% util):       {value}

CLUSTER RECOMMENDATION
──────────────────────
  Total Nodes:            {value}  (multiple of RF={RF})
  vCPU/node:              {value}
  Memory/node:            {value} GB
  Storage/node (now):     {value} GB
  Storage/node (1 yr):    {value} GB
  Storage/node (2 yr):    {value} GB

  Total vCPU:             {value}
  Total Memory:           {value} GB
  Total Storage (now):    {value} GB
  Total Storage (2 yr):   {value} GB

PER-NODE OPERATIONAL LIMITS
────────────────────────────
  DB Operations/node/s:   {value}
  PG Connections/node:    {value}  (32 × vCPU/node)
  CPU Utilization:        {value}%  ✅ (target ≤65%)
  Est. IOPS/node:         {value}  (provision ≥ this; NVMe recommended if >10,000)
  Est. Network/node:      {value} MB/s  (keep below 40% of NIC capacity)

NOTES
─────
  • Storage: LZ4 compression (30%) + 20% index overhead + 10% WAL + 35% compaction reserve (×1.45 total)
  • CPU: Includes ~0.2% core overhead per PG connection (32 connections/vCPU)
  • Memory: Base ratio (1:4 or 1:8) + 60 MB × connections, rounded to standard RAM tier
  • Scale horizontally by adding nodes in multiples of RF
  • For Kubernetes: use StatefulSets, one pod per node
  • ⚠️ Cap the vertical scaling at 32 or 64 cores per node. Use "latency-performance" tuned profile for 32/64 core machines
  • [If exec time estimated] ⚠️ Run pg_stat_statements to replace estimated execution time with real data
════════════════════════════════════════════
```

---

## Common vCPU Tiers and Rough Benchmarks

| vCPU/node | Approx Ops/s/node | Use Case |
|---|---|---|
| 4  | ~2,000–4,000  | Dev/test, small workloads |
| 8  | ~5,000–10,000 | Small production |
| 16 | ~10,000–20,000 | Standard production |
| 32 | ~20,000–40,000 | High-throughput production |
| 64 | ~40,000–80,000 | Very large scale |

These are approximate — actual throughput depends on operation complexity, row size, index count.

---

## Edge Cases & Guidance

- **Minimum cluster**: Always RF nodes (e.g., 3 for RF=3). Never fewer.
- **Read replicas**: If read% is very high (>80%), consider adding read replicas to offload; they don't count toward the RF quorum.
- **IOPS**: For NVMe SSDs, plan for ~30 IOPS/GB; for cloud SSDs, check provider limits. Write-heavy workloads benefit from higher IOPS provisioning.
- **Tablet count**: Default 3 tablets/table. For large tables (>100GB/node), increase tablet count to improve parallelism.
- **Growth headroom**: Sizing above assumes peak load. For bursty workloads, add 1–2 extra nodes as buffer or plan for horizontal scaling.
- **YugabyteDB Managed (cloud)**: Match to available instance types (e.g., AWS r6g.4xlarge = 16 vCPU / 128 GB RAM).

---

## Reference: Calculation Example

**Input**: 10,000 QPS, 30% write / 70% read, 5ms avg execution, RF=3, 16 vCPU/node, 500 GB table

```
Write Ops/s = 3,000 | Read Ops/s = 7,000
Eff. Writes = 3,000 × 3 × 1.2 = 10,800
Eff. Reads  = 7,000 × 1.2     =  8,400
Total Eff.  = 19,200 ops/s

CPU s/s (workload) = 19,200 × 0.005 = 96 CPU-seconds/s

Connections/node (16 vCPU) = 32 × 16 = 512
Connection CPU/node        = 512 × 0.008 = 4.1 cores

First-pass (ignore connection overhead):
  vCPUs needed = 96 / 0.65 ≈ 148 → 12 nodes × 16 vCPU = 192 total vCPU (multiple of RF=3)

Add connection overhead (12 nodes):
  Total connection CPU = 4.1 × 12 = 49.2 cores
  Effective vCPUs used = 148 + 49.2 = 197.2
  CPU util = 197.2 / 192 = 103% ❌ → increase to 15 nodes

  15 nodes × 16 vCPU = 240 total vCPU
  Total connection CPU = 4.1 × 15 = 61.5
  Effective vCPUs = 148 + 61.5 = 209.5
  CPU util = 209.5 / 240 = 87% ❌ → increase to 18 nodes

  18 nodes × 16 vCPU = 288 total vCPU
  Total connection CPU = 4.1 × 18 = 73.8
  Effective vCPUs = 148 + 73.8 = 221.8
  CPU util = 221.8 / 288 = 77% ❌ → increase to 21 nodes

  21 nodes × 16 vCPU = 336 total vCPU
  Total connection CPU = 4.1 × 21 = 86.1
  Effective vCPUs = 148 + 86.1 = 234.1
  CPU util = 234.1 / 336 = 70% ❌ → increase to 24 nodes

  24 nodes × 16 vCPU = 384 total vCPU
  Total connection CPU = 4.1 × 24 = 98.4
  Effective vCPUs = 148 + 98.4 = 246.4
  CPU util = 246.4 / 384 = 64% ✅

→ Recommendation: 24 nodes, 16 vCPU/node

Storage (24 nodes):
  Index = 500 × 0.20 = 100 GB → Total Raw = 600 GB
  Compressed = 600 × 0.70 = 420 GB
  With RF3 = 420 × 3 = 1,260 GB
  Base/node = 1,260 / 24 = 52.5 GB
  Storage/node = 52.5 × 1.45 = 76.1 GB → round to 80 GB provisioned
  Total Storage = 80 × 24 = 1,920 GB

Memory (read-heavy 70%, 16 vCPU, 512 connections/node):
  Base = 16 × 8 = 128 GB
  Connection memory = 512 × 0.06 GB = 30.7 GB
  Total/node = 128 + 30.7 = 158.7 GB → use 192 GB tier

PG Connections/node: 32 × 16 = 512
DB Ops/node: 19,200 / 24 = 800 ops/s/node
Total Memory: 192 × 24 = 4,608 GB
```
