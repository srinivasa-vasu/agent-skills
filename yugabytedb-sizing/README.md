# YugabyteDB Cluster Sizing Skill

An AI agent skill that **calculates optimal YugabyteDB cluster sizing** from workload inputs producing an indicative hardware recommendation optimized for ≤65% CPU utilization. Supports both the **YSQL** (PostgreSQL-compatible) and **YCQL** (Cassandra-compatible) APIs, each with its own overhead profile.

## Overview

Choosing the right cluster size for YugabyteDB requires balancing CPU, memory, storage, IOPS, and network bandwidth while accounting for Raft replication overhead, LSM-tree compaction reserves, connection memory, and data growth. This skill automates that entire process with a structured methodology.

## What It Covers

| Area | What's Computed |
|---|---|
| **CPU Sizing** | Measured CPU cost per read/write (YSQL from transactional and point-select benchmarks on YB 2026.1, YCQL from the public key-value benchmark) × workload profile, plus connection and tablet CPU; iterative node count, target ≤65% utilization |
| **Storage** | LZ4 compression, RF replication, index overhead (20% YSQL / 10% YCQL), 20% compaction reserve, WAL from write rate × retention (longer with xCluster/CDC), 20 TB/node cap |
| **Memory** | 1:4 vCPU:RAM, or 1:8 for read-heavy data that doesn't fit a 1:4 node's cache, as total node RAM; YSQL connections (15 MB each, from TPC-C) fit in PostgreSQL's 27% share before adding RAM; rounded to standard RAM tiers |
| **IOPS / Disk** | IOPS per replicated write and disk MiB/s with LSM amplification; read IOPS from cache vs leader data (benchmark-calibrated); nodes added to fit gp3 (or your NVMe/io2) limits |
| **Network** | Separate read and replicated-write traffic factors (benchmark-calibrated); multi-AZ cross-zone traffic and monthly transfer cost |
| **Topology** | Multi-AZ by default (one zone per replica); single-AZ; multi-region with balanced or pinned (preferred-region) leaders, cross-region latency and transfer cost |
| **Transactions** | TPS × statements per transaction; distributed-transaction overhead (commit + intents) on fast-path profiles; YSQL write pipelining for cross-region latency |
| **Read offload** | Follower reads and a separately sized read-replica cluster |
| **HTAP** | OLTP plus analytical scans (queries/s × rows scanned), on the primary, followers or an isolated read replica |
| **Growth Projection** | 1-year and 2-year storage forecasts based on configurable annual growth rate; YCQL TTL caps growth at the steady state |
| **Tablet Overhead** | CPU/RAM for tablet maintenance — tablets from automatic splitting of the data, or from schema (YSQL 1 tablet/table, YCQL 1 tablet/tserver/table) if larger |
| **Failure Resilience** | CPU utilization after losing one node or one zone; optionally sizes the cluster so a zone loss stays within target |
| **CPU Calibration** | Derives CPU cost per operation from an existing cluster's QPS and CPU% instead of guessing execution time |
| **vCPU Tier Comparison** | Sizes several node shapes in one run and recommends one |

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

**Minimal info** — the skill picks a workload profile from the read/write mix when you don't describe your queries:

> *"How many YugabyteDB nodes do I need for 50,000 QPS with a 60/40 read-write split on 2 TB of data?"*

**Kubernetes deployment** — mention the platform for tailored guidance:

> *"Size a YugabyteDB cluster on Kubernetes for 15,000 QPS, 70% reads, 8 vCPU pods, 300 GB data, RF 3."*

**Custom CPU target** — override the default 65% utilization ceiling:

> *"Size my YugabyteDB cluster for 20,000 QPS, 40% writes, 16 vCPU nodes, RF 3, 800 GB data. Target 70% CPU utilization instead of the default 65%."*

**Tuned connection density** — reduce connections per vCPU for latency-sensitive workloads:

> *"I need a YugabyteDB cluster for 12,000 QPS (90% reads), 500 GB, RF 3, 32 vCPU nodes. Use 8 connections per vCPU instead of the default 16."*

**Custom compression and compaction** — adjust storage assumptions for a known workload:

> *"Size a YugabyteDB cluster for 8,000 QPS, 50/50 read-write, 16 vCPU, RF 3, 1 TB data. My data compresses ~50% with LZ4 and I want 40% compaction reserve."*

**YCQL workload** — mention Cassandra/CQL to get the YCQL profile (no connection overhead, lighter RPC overhead):

> *"Size a YugabyteDB YCQL cluster for 80,000 QPS of point reads/writes (60% reads), 3 TB of data, 16 vCPU nodes, RF 3."*

**YCQL with TTL** — storage growth plateaus at ingest × TTL:

> *"YCQL time-series workload: 20,000 writes/s and 5,000 reads/s, 1 KB rows, 90-day TTL, 2 TB today, RF 3, 16 vCPU nodes."*

**Calibrated from a running cluster** — the most accurate input when available:

> *"Our PoC handles 8,000 QPS at 40% CPU on 6 × 8 vCPU nodes (30% writes). Size YugabyteDB for 20,000 QPS on 16 vCPU nodes, 500 GB, RF 3."*

Only observed QPS and CPU% are required; if the PoC shape isn't given, it's assumed to be RF nodes with the target vCPU/node.

**YSQL Connection Manager** — for apps with many connections:

> *"Size for 15,000 QPS, 40% writes, 1 TB, RF 3, 16 vCPU. We'll use YSQL Connection Manager — around 2,000 app connections per node."*

**Survive a zone outage** — size so losing a full zone stays within target:

> *"Size for 10,000 QPS, 30% writes, 5 ms, 500 GB, RF 3 across 3 AZs — it must stay under 65% CPU if a zone goes down. Compare 8, 16 and 32 vCPU nodes."*

**Transactions** — give transactions per second and statements per transaction:

> *"We run 3,000 transactions/s, about 6 statements each (30% writes), point lookups, 500 GB, RF 3 on 16 vCPU nodes."*

**Multi-region with follower reads** — leaders pinned near the app, stale-tolerant reads served locally:

> *"Size a 3-region YugabyteDB cluster for 20,000 QPS (70% reads), 500 GB, with leaders in us-east and 40% of reads as follower reads."*

**HTAP** — OLTP plus analytical queries, isolated on a read replica:

> *"Size for 20,000 QPS of OLTP (70% reads, 500 GB) plus reporting queries — about 5 per second, each scanning ~2M rows. Keep analytics off the transactional nodes."*

**Sizing with CDC and xCluster** (CDC is YSQL only) — the skill accounts for additional CPU overhead when Change Data Capture (CDC) or cross-cluster replication (xCluster) is enabled:

> *"How many YugabyteDB nodes do I need for 50,000 QPS with a 60/40 read-write split on 2 TB of data with CDC and xCluster enabled?"*


## Required Inputs

| Input | Description |
|---|---|
| `api` | `ysql` (default) or `ycql` |
| `qps` | Total queries per second at peak |
| `write-pct` / `read-pct` | Write and read percentages (must sum to 100) |
| `vcpu-per-node` | Desired vCPU count per node (e.g., 8, 16, 32) |
| `rf` | Replication factor — typically 3 |
| `table-size-gb` | Raw uncompressed data size |


## Key Defaults

| Parameter | YSQL | YCQL | Notes |
|---|---|---|---|
| CPU per read / write | 1.070 / 1.784 ms | 0.107 / 0.178 ms | Measured at RF=3 (YCQL scaled ×0.6 from 2016-era hardware to current CPUs); scaled by workload profile and `--cpu-cost-scale` |
| RPC overhead | 15% | 8% | Retries and real-world variance on top of the measured costs |
| Index storage | 20% | 10% | YCQL models are typically denormalized |
| Connections/vCPU | 16 | not modeled | CQL drivers multiplex requests |
| Memory/connection | 15 MB | — | Per PostgreSQL backend (PSS under OLTP load); counted against PostgreSQL's 27% RAM share first |
| Tablets | auto-split / 1 per table | auto-split / 1 per tserver per table | Data-driven count always; schema count when object count is given |
| WAL retention | 15 min (24 h xCluster, 8 h CDC) | 15 min (24 h xCluster) | WAL = write MB/s × retention |
| Connection Manager | off; when on: 10 backends/vCPU, ~10 clients/backend, +200 MB/node | n/a | `--connection-manager` |
| Failure CPU ceiling | 65% | 65% | Used by `--size-for node\|zone` |
| CDC | +5% CPU | not supported | xCluster +10% CPU for both |
| Topology | multi-AZ (zones = RF) | multi-AZ (zones = RF) | `--zones 1` for single AZ |
| Disk limits | 16,000 IOPS / 1,000 MiB/s | same | gp3 max; `--disk-iops` / `--disk-mibps` for NVMe or io2 |
| Compression | 30% | 30% | LZ4 compression |
| Compaction reserve | 20% | 20% | LSM-tree compaction free space |
| Target CPU utilization | 65% | 65% | Max sustained utilization |
| Max storage/node | 20 TB | 20 TB | Hard cap; extra nodes added if exceeded |

All defaults can be overridden via prompts. Memory rounds up to standard RAM tiers, staying on a tier if exceeded by ≤2%.

## Output Formats

The calculator prints a text report by default. Ask for an **HTML report** for a shareable, self-contained page (KPI tiles, breakdowns, failure-resilience bars; light/dark aware), or **JSON** for programmatic use:

```bash
python3 scripts/sizing_calc.py ... --format text|html|json   # default: text
```

> *"Size my YCQL cluster for 40,000 QPS, 70% reads, 1 TB, RF 3, 16 vCPU — give me an HTML report."*

## Project Structure

```
yugabytedb-sizing/
├── SKILL.md                  # Workflow, inputs, commands, how to present results
├── README.md                 # This file
├── references/
│   ├── cpu-model.md          # CPU cost model, measured sources, workload profiles, calibration
│   ├── methodology.md        # Step-by-step formulas for every output
│   ├── defaults.md           # Every default and its flag
│   └── examples.md           # Worked YSQL and YCQL examples
├── scripts/
│   └── sizing_calc.py        # Python sizing calculator (CLI; text / HTML / JSON output)
└── tests/
    ├── test_sizing_calc.py   # stdlib unittest suite (no dependencies)
    └── golden/               # pinned JSON results for reference scenarios and benchmark runs
```

## Testing

```bash
python3 -m unittest discover -s tests -v                 # run from this directory
UPDATE_GOLDEN=1 python3 -m unittest discover -s tests   # after an intentional model change, then review git diff tests/golden/
```
