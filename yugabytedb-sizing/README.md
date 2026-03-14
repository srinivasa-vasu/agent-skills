# YugabyteDB Cluster Sizing Skill

An AI agent skill that **calculates optimal YugabyteDB cluster sizing** from workload inputs producing an indicative hardware recommendation optimized for ≤65% CPU utilization.

## Overview

Choosing the right cluster size for YugabyteDB requires balancing CPU, memory, storage, IOPS, and network bandwidth while accounting for Raft replication overhead, LSM-tree compaction reserves, connection memory, and data growth. This skill automates that entire process with a structured methodology.

## What It Covers

| Area | What's Computed |
|---|---|
| **CPU Sizing** | Effective ops/s with RF and RPC overhead, iterative node count with connection CPU overhead, target ≤65% utilization |
| **Storage** | LZ4 compression, RF replication, 20% index overhead, 10% WAL, 35% compaction reserve, 20 TB/node cap |
| **Memory** | 1:4 (write-heavy) or 1:8 (read-heavy) vCPU:RAM ratio + 60 MB per PG connection, rounded to standard RAM tiers |
| **IOPS** | Write IOPS with ×4 LSM write amplification, read IOPS with 30% cache miss rate |
| **Network** | Inter-node Raft write traffic + read throughput per node (keep below 40% NIC capacity) |
| **Growth Projection** | 1-year and 2-year storage forecasts based on configurable annual growth rate |

## When to Use

This skill activates when a user:

- Asks to **size a YugabyteDB cluster** or plan capacity
- Wants to know **how many nodes** they need for a given workload
- Provides workload metrics (**QPS, read/write ratio, table size**) and asks for infrastructure recommendations
- Mentions *"YugabyteDB hardware requirements"*, *"vCPU sizing"*, *"storage sizing"*, or *"memory recommendations"*
- Needs to **compare vCPU tiers** (e.g., 8 vs 16 vs 32 vCPU/node)

## Prompt Examples

**Basic sizing** — provide QPS, read/write split, node tier, and data size:

> *"Size my YugabyteDB cluster for 10,000 QPS, 30% writes / 70% reads, 16 vCPU nodes, RF 3, with 500 GB of data and ~5 ms avg query time."*

**Comparing vCPU tiers** — ask the agent to evaluate multiple node sizes:

> *"Compare 8, 16, and 32 vCPU node configurations for a YugabyteDB cluster handling 25,000 QPS at 50/50 read-write with 1 TB of data."*

**Storage growth planning** — include a growth rate for multi-year projections:

> *"I need a YugabyteDB cluster for 5,000 QPS (80% reads), 200 GB table, RF 3. Data grows ~40% per year — show me storage needs for the next 2 years."*

**Minimal info** — the skill auto-estimates execution time when omitted:

> *"How many YugabyteDB nodes do I need for 50,000 QPS with a 60/40 read-write split on 2 TB of data?"*

**Kubernetes deployment** — mention the platform for tailored guidance:

> *"Size a YugabyteDB cluster on Kubernetes for 15,000 QPS, 70% reads, 8 vCPU pods, 300 GB data, RF 3."*

**Custom CPU target** — override the default 65% utilization ceiling:

> *"Size my YugabyteDB cluster for 20,000 QPS, 40% writes, 16 vCPU nodes, RF 3, 800 GB data. Target 70% CPU utilization instead of the default 65%."*

**Tuned connection density** — reduce connections per vCPU for latency-sensitive workloads:

> *"I need a YugabyteDB cluster for 12,000 QPS (90% reads), 500 GB, RF 3, 32 vCPU nodes. Use 16 connections per vCPU instead of the default 32."*

**Custom compression and compaction** — adjust storage assumptions for a known workload:

> *"Size a YugabyteDB cluster for 8,000 QPS, 50/50 read-write, 16 vCPU, RF 3, 1 TB data. My data compresses ~50% with LZ4 and I want 40% compaction reserve."*

## Required Inputs

| Input | Description |
|---|---|
| `qps` | Total queries per second at peak |
| `write-pct` / `read-pct` | Write and read percentages (must sum to 100) |
| `vcpu-per-node` | Desired vCPU count per node (e.g., 8, 16, 32) |
| `rf` | Replication factor — typically 3 |
| `table-size-gb` | Raw uncompressed data size |


## Key Defaults

| Parameter | Default | Notes |
|---|---|---|
| RPC overhead | 1.2× | Joins, index lookups, retry overhead |
| Compression | 30% reduction | LZ4 compression |
| Compaction reserve | 35% | LSM-tree compaction free space |
| Target CPU utilization | 65% | Max sustained utilization |
| Connections/vCPU | 32 | PostgreSQL connections per vCPU |
| Memory/connection | 60 MB | RAM reserved per connection |
| Max storage/node | 20 TB | Hard cap; extra nodes added if exceeded |

All defaults can be overridden via prompts.

## Project Structure

```
yugabytedb-sizing/
├── SKILL.md                  # Skill definition, full calculation methodology, and output format
├── README.md                 # This file
└── scripts/
    └── sizing_calc.py        # Python sizing calculator (CLI + JSON output)
```
