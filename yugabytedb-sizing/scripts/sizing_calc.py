#!/usr/bin/env python3
"""
YugabyteDB Cluster Sizing Calculator
-------------------------------------
Usage:
    python sizing_calc.py \
        --qps 10000 \
        --write-pct 30 \
        --read-pct 70 \
        --avg-exec-ms 5 \
        --vcpu-per-node 16 \
        --rf 3 \
        --table-size-gb 500

    # avg-exec-ms is optional; if omitted, it is estimated from workload profile.
    # Use --avg-row-bytes and --growth-rate-pct for IOPS/network/storage-growth output.

All fixed overhead parameters can be overridden via flags if needed.
"""

import argparse
import math
import json
import sys


# ─── Fixed Defaults ────────────────────────────────────────────────────────────
DEFAULTS = {
    "rpc_overhead":        0.20,   # 20% overhead fraction for RPC, retries, and indexes (applied as 1 + rpc_overhead)
    "index_overhead":      0.20,   # 20% extra storage for indexes
    "compression_ratio":   0.30,   # 30% compression reduction
    "wal_overhead":        0.10,   # 10% WAL storage overhead
    "compaction_reserve":  0.20,   # 20% free space for LSM compaction
    "target_cpu_util":     0.65,   # 65% max sustained CPU utilization
    "conn_per_vcpu":       16,     # YugabyteDB connections per vCPU
    "mem_mb_per_conn":     60,     # MB of RAM per connection
    "conn_cpu_overhead":   0.002,  # CPU cores consumed per connection (~0.2%)
    "write_amp_factor":    4,      # LSM write amplification factor for IOPS estimate
    "read_cache_miss":     0.30,   # Fraction of reads that miss the block cache (cold)
    "avg_row_bytes":       512,    # Default avg row size if not provided
    "growth_rate_pct":     30,     # Annual data growth rate % if not provided
    "max_storage_per_node_gb": 20480,  # 20 TB max disk density per node
    "cdc_overhead":         0.05,   # 5% extra CPU overhead when CDC is enabled
    "xcluster_overhead":    0.05,   # 5% extra CPU overhead when xCluster is enabled
}

# Estimated avg execution time (ms) by workload profile when not provided by user
EXEC_TIME_ESTIMATES = [
    # (condition_fn, estimated_ms, label)
    (lambda w, r: w >= 70,                   2,  "write-heavy / key-value (estimated)"),
    (lambda w, r: r >= 90,                   20, "read-heavy with scans/analytics (estimated)"),
    (lambda w, r: r >= 70,                   7,  "read-heavy OLTP with joins (estimated)"),
    (lambda w, r: True,                      4,  "mixed OLTP (estimated)"),
]

# Standard RAM tiers (GB) - round up to nearest
RAM_TIERS = [16, 32, 64, 128, 192, 256, 384, 512, 768, 1024]


def round_up_to_rf_multiple(n, rf):
    """Round n up to the nearest multiple of rf, minimum rf."""
    if n <= rf:
        return rf
    return math.ceil(n / rf) * rf


def round_up_to_ram_tier(gb):
    """Round up to the nearest standard RAM tier."""
    for tier in RAM_TIERS:
        if tier >= gb:
            return tier
    return math.ceil(gb / 256) * 256  # beyond known tiers, round to 256 GB


def calculate(
    qps,
    write_pct,
    read_pct,
    avg_exec_ms,        # None = auto-estimate from workload profile
    vcpu_per_node,
    rf,
    table_size_gb,
    avg_row_bytes=None,
    growth_rate_pct=None,
    max_storage_per_node_gb=None,
    rpc_overhead=None,
    index_overhead=None,
    compression_ratio=None,
    wal_overhead=None,
    compaction_reserve=None,
    target_cpu_util=None,
    conn_per_vcpu=None,
    mem_mb_per_conn=None,
    conn_cpu_overhead=None,
    write_amp_factor=None,
    read_cache_miss=None,
    cdc_enabled=False,
    cdc_overhead=None,
    xcluster_enabled=False,
    xcluster_overhead=None,
):
    # Apply defaults for any unset overrides
    rpc_overhead       = rpc_overhead       if rpc_overhead       is not None else DEFAULTS["rpc_overhead"]
    index_overhead     = index_overhead     if index_overhead     is not None else DEFAULTS["index_overhead"]
    compression_ratio  = compression_ratio  if compression_ratio  is not None else DEFAULTS["compression_ratio"]
    wal_overhead       = wal_overhead       if wal_overhead       is not None else DEFAULTS["wal_overhead"]
    compaction_reserve = compaction_reserve if compaction_reserve is not None else DEFAULTS["compaction_reserve"]
    target_cpu_util    = target_cpu_util    if target_cpu_util    is not None else DEFAULTS["target_cpu_util"]
    conn_per_vcpu      = conn_per_vcpu      if conn_per_vcpu      is not None else DEFAULTS["conn_per_vcpu"]
    mem_mb_per_conn    = mem_mb_per_conn    if mem_mb_per_conn    is not None else DEFAULTS["mem_mb_per_conn"]
    conn_cpu_overhead  = conn_cpu_overhead  if conn_cpu_overhead  is not None else DEFAULTS["conn_cpu_overhead"]
    write_amp_factor   = write_amp_factor   if write_amp_factor   is not None else DEFAULTS["write_amp_factor"]
    read_cache_miss    = read_cache_miss    if read_cache_miss    is not None else DEFAULTS["read_cache_miss"]
    avg_row_bytes      = avg_row_bytes      if avg_row_bytes      is not None else DEFAULTS["avg_row_bytes"]
    growth_rate_pct    = growth_rate_pct    if growth_rate_pct    is not None else DEFAULTS["growth_rate_pct"]
    max_storage_per_node_gb = max_storage_per_node_gb if max_storage_per_node_gb is not None else DEFAULTS["max_storage_per_node_gb"]
    cdc_overhead       = cdc_overhead       if cdc_overhead       is not None else DEFAULTS["cdc_overhead"]
    xcluster_overhead  = xcluster_overhead  if xcluster_overhead  is not None else DEFAULTS["xcluster_overhead"]

    # ── Exec time: estimate if not provided ────────────────────────────────
    exec_time_estimated = avg_exec_ms is None
    exec_time_label = None
    if exec_time_estimated:
        for condition, est_ms, label in EXEC_TIME_ESTIMATES:
            if condition(write_pct, read_pct):
                avg_exec_ms = est_ms
                exec_time_label = label
                break

    # ── Step 1: Derive ops/s ────────────────────────────────────────────────
    write_ops = qps * (write_pct / 100)
    read_ops  = qps * (read_pct  / 100)

    # ── Step 2: Apply overhead multiplier ──────────────────────────────────
    rpc_multiplier = 1.0 + rpc_overhead
    eff_write_ops = write_ops * rf * rpc_multiplier
    eff_read_ops  = read_ops  * rpc_multiplier
    total_eff_ops = eff_write_ops + eff_read_ops

    # ── Step 3: CPU required for workload ──────────────────────────────────
    cpu_seconds_needed = total_eff_ops * (avg_exec_ms / 1000)

    # ── CDC / xCluster overhead ────────────────────────────────────────────
    # Each enabled feature adds a percentage overhead on top of the base CPU workload.
    feature_overhead_factor = 1.0
    if cdc_enabled:
        feature_overhead_factor += cdc_overhead
    if xcluster_enabled:
        feature_overhead_factor += xcluster_overhead
    cpu_seconds_needed = cpu_seconds_needed * feature_overhead_factor

    # ── Step 4 + 5: Iterative node sizing with connection overhead ─────────
    conn_per_node     = conn_per_vcpu * vcpu_per_node
    conn_cpu_per_node = conn_per_node * conn_cpu_overhead

    raw_vcpus_workload = cpu_seconds_needed
    adj_vcpus_first    = raw_vcpus_workload / target_cpu_util
    nodes_first        = round_up_to_rf_multiple(
                             math.ceil(adj_vcpus_first / vcpu_per_node), rf
                         )

    total_nodes = nodes_first
    iterations  = []
    for _ in range(20):
        total_vcpu           = total_nodes * vcpu_per_node
        total_conn_cpu       = conn_cpu_per_node * total_nodes
        effective_vcpus_used = raw_vcpus_workload + total_conn_cpu
        cpu_util             = effective_vcpus_used / total_vcpu

        iterations.append({
            "nodes": total_nodes,
            "total_vcpu": total_vcpu,
            "effective_vcpus_used": round(effective_vcpus_used, 1),
            "cpu_util_pct": round(cpu_util * 100, 1),
            "pass": cpu_util <= target_cpu_util,
        })

        if cpu_util <= target_cpu_util:
            break
        total_nodes += rf

    final = iterations[-1]

    # ── Step 6: Storage ────────────────────────────────────────────────────
    index_size_gb      = table_size_gb * index_overhead
    total_raw_gb       = table_size_gb + index_size_gb
    after_compression  = total_raw_gb * (1 - compression_ratio)
    with_replication   = after_compression * rf
    storage_multiplier = 1 + wal_overhead + compaction_reserve

    # Compute storage/node with current CPU-driven node count, then check cap
    base_storage_node  = with_replication / total_nodes
    storage_per_node   = base_storage_node * storage_multiplier

    # If storage/node exceeds 20TB cap, add nodes (in RF multiples) until it fits
    storage_cap_triggered = False
    storage_nodes_added   = 0
    while storage_per_node > max_storage_per_node_gb:
        storage_cap_triggered = True
        total_nodes      += rf
        storage_nodes_added += rf
        base_storage_node = with_replication / total_nodes
        storage_per_node  = base_storage_node * storage_multiplier
        # Recalculate CPU utilization for the new node count
        total_vcpu           = total_nodes * vcpu_per_node
        total_conn_cpu       = conn_cpu_per_node * total_nodes
        effective_vcpus_used = raw_vcpus_workload + total_conn_cpu
        cpu_util             = effective_vcpus_used / total_vcpu
        final = {
            "nodes": total_nodes,
            "total_vcpu": total_vcpu,
            "effective_vcpus_used": round(effective_vcpus_used, 1),
            "cpu_util_pct": round(cpu_util * 100, 1),
            "pass": cpu_util <= target_cpu_util,
        }

    total_storage = storage_per_node * total_nodes

    # ── Step 7: Memory ─────────────────────────────────────────────────────
    base_mem_ratio    = 4 if write_pct >= 50 else 8
    base_mem_gb       = vcpu_per_node * base_mem_ratio
    conn_mem_gb       = conn_per_node * mem_mb_per_conn / 1024
    raw_mem_per_node  = base_mem_gb + conn_mem_gb
    mem_per_node      = round_up_to_ram_tier(raw_mem_per_node)
    total_memory      = mem_per_node * total_nodes

    # ── Step 8: IOPS estimation ────────────────────────────────────────────
    write_iops_per_node = (eff_write_ops / total_nodes) * write_amp_factor
    read_iops_per_node  = (eff_read_ops  / total_nodes) * read_cache_miss
    total_iops_per_node = write_iops_per_node + read_iops_per_node

    # ── Step 9: Network bandwidth ──────────────────────────────────────────
    write_net_mbps = (write_ops / total_nodes) * rf * avg_row_bytes / 1_048_576
    read_net_mbps  = (read_ops  / total_nodes) * avg_row_bytes / 1_048_576
    total_net_mbps = write_net_mbps + read_net_mbps

    # ── Step 10: Storage growth projection ────────────────────────────────
    growth = growth_rate_pct / 100
    storage_1yr = storage_per_node * (1 + growth)
    storage_2yr = storage_per_node * (1 + growth) ** 2
    total_storage_2yr = storage_2yr * total_nodes

    # ── Step 11: Failure scenario CPU projections ──────────────────────────
    # Zones are aligned to RF: one zone per RF replica, nodes spread evenly.
    num_zones        = rf                                         # zones = RF
    nodes_per_zone   = total_nodes // num_zones                  # evenly distributed

    def _cpu_util_with_nodes(surviving):
        """CPU utilisation when only `surviving` nodes remain (same workload)."""
        if surviving <= 0:
            return None
        sv_total_vcpu      = surviving * vcpu_per_node
        sv_total_conn_cpu  = conn_cpu_per_node * surviving
        sv_eff_vcpus       = raw_vcpus_workload + sv_total_conn_cpu
        return round(sv_eff_vcpus / sv_total_vcpu * 100, 1)

    nodes_after_node_failure = total_nodes - 1
    nodes_after_zone_failure = total_nodes - nodes_per_zone

    cpu_util_node_failure = _cpu_util_with_nodes(nodes_after_node_failure)
    cpu_util_zone_failure = _cpu_util_with_nodes(nodes_after_zone_failure)

    failure_scenarios = {
        "num_zones": num_zones,
        "nodes_per_zone": nodes_per_zone,
        "node_failure": {
            "surviving_nodes": nodes_after_node_failure,
            "cpu_util_pct": cpu_util_node_failure,
            "exceeds_target": cpu_util_node_failure is not None and cpu_util_node_failure > target_cpu_util * 100,
        },
        "zone_failure": {
            "surviving_nodes": nodes_after_zone_failure,
            "cpu_util_pct": cpu_util_zone_failure,
            "exceeds_target": cpu_util_zone_failure is not None and cpu_util_zone_failure > target_cpu_util * 100,
        },
    }

    # ── Assemble result ────────────────────────────────────────────────────
    result = {
        "inputs": {
            "qps": qps,
            "write_pct": write_pct,
            "read_pct": read_pct,
            "avg_exec_ms": avg_exec_ms,
            "exec_time_estimated": exec_time_estimated,
            "exec_time_label": exec_time_label,
            "vcpu_per_node": vcpu_per_node,
            "rf": rf,
            "table_size_gb": table_size_gb,
            "avg_row_bytes": avg_row_bytes,
            "avg_row_bytes_source": "provided" if avg_row_bytes != DEFAULTS["avg_row_bytes"] else "default",
            "growth_rate_pct": growth_rate_pct,
            "growth_rate_source": "provided" if growth_rate_pct != DEFAULTS["growth_rate_pct"] else "default",
            "cdc_enabled": cdc_enabled,
            "xcluster_enabled": xcluster_enabled,
        },
        "parameters": {
            "rpc_overhead": rpc_overhead,
            "rpc_multiplier": rpc_multiplier,
            "index_overhead_pct": index_overhead * 100,
            "compression_pct": compression_ratio * 100,
            "wal_overhead_pct": wal_overhead * 100,
            "compaction_reserve_pct": compaction_reserve * 100,
            "target_cpu_util_pct": target_cpu_util * 100,
            "conn_per_vcpu": conn_per_vcpu,
            "mem_mb_per_conn": mem_mb_per_conn,
            "write_amp_factor": write_amp_factor,
            "read_cache_miss_pct": read_cache_miss * 100,
            "cdc_overhead_pct": cdc_overhead * 100 if cdc_enabled else None,
            "xcluster_overhead_pct": xcluster_overhead * 100 if xcluster_enabled else None,
            "feature_overhead_factor": round(feature_overhead_factor, 4),
        },
        "workload": {
            "write_ops_per_s": round(write_ops, 1),
            "read_ops_per_s": round(read_ops, 1),
            "eff_write_ops_per_s": round(eff_write_ops, 1),
            "eff_read_ops_per_s": round(eff_read_ops, 1),
            "total_eff_ops_per_s": round(total_eff_ops, 1),
            "cpu_seconds_needed": round(cpu_seconds_needed, 2),
            "raw_vcpus_workload_incl_features": round(raw_vcpus_workload, 1),
        },
        "sizing_iterations": iterations,
        "cluster": {
            "total_nodes": total_nodes,
            "vcpu_per_node": vcpu_per_node,
            "total_vcpu": final["total_vcpu"],
            "cpu_utilization_pct": final["cpu_util_pct"],
            "conn_per_node": conn_per_node,
            "conn_cpu_per_node": round(conn_cpu_per_node, 2),
            "db_ops_per_node_per_s": round(total_eff_ops / total_nodes, 1),
        },
        "storage": {
            "index_size_gb": round(index_size_gb, 1),
            "total_raw_gb": round(total_raw_gb, 1),
            "after_compression_gb": round(after_compression, 1),
            "with_replication_gb": round(with_replication, 1),
            "base_storage_per_node_gb": round(base_storage_node, 1),
            "storage_multiplier": round(storage_multiplier, 2),
            "storage_per_node_gb": round(storage_per_node, 1),
            "total_storage_gb": round(total_storage, 1),
            "storage_per_node_1yr_gb": round(storage_1yr, 1),
            "storage_per_node_2yr_gb": round(storage_2yr, 1),
            "total_storage_2yr_gb": round(total_storage_2yr, 1),
            "max_storage_per_node_gb": max_storage_per_node_gb,
            "storage_cap_triggered": storage_cap_triggered,
            "storage_nodes_added": storage_nodes_added,
        },
        "memory": {
            "base_mem_ratio": f"1:{base_mem_ratio}",
            "base_mem_gb": round(base_mem_gb, 1),
            "conn_mem_gb": round(conn_mem_gb, 1),
            "raw_mem_per_node_gb": round(raw_mem_per_node, 1),
            "mem_per_node_gb": mem_per_node,
            "total_memory_gb": total_memory,
        },
        "iops": {
            "write_iops_per_node": round(write_iops_per_node, 0),
            "read_iops_per_node": round(read_iops_per_node, 0),
            "total_iops_per_node": round(total_iops_per_node, 0),
            "note": f"Write IOPS include LSM write amplification ×{write_amp_factor}. Read IOPS assume {read_cache_miss*100:.0f}% cache miss.",
        },
        "network": {
            "write_net_mbps_per_node": round(write_net_mbps, 2),
            "read_net_mbps_per_node": round(read_net_mbps, 2),
            "total_net_mbps_per_node": round(total_net_mbps, 2),
            "note": "Keep below 40% of NIC capacity (e.g. 500 MB/s on 10 GbE, 2,500 MB/s on 50 GbE).",
        },
        "failure_scenarios": failure_scenarios,
    }
    return result


def format_report(r):
    c  = r["cluster"]
    s  = r["storage"]
    m  = r["memory"]
    w  = r["workload"]
    i  = r["inputs"]
    io = r["iops"]
    nw = r["network"]

    exec_label = ""
    if i.get("exec_time_estimated"):
        exec_label = f"  ⚠️  ESTIMATED ({i['exec_time_label']}) — measure with pg_stat_statements"

    lines = [
        "═" * 56,
        "  YugabyteDB Cluster Sizing Recommendation",
        "═" * 56,
        "",
        "INPUT SUMMARY",
        "─" * 44,
        f"  QPS:                         {i['qps']:,}",
        f"  Write / Read:                {i['write_pct']}% / {i['read_pct']}%",
        f"  Avg execution time:          {i['avg_exec_ms']} ms",
    ]
    if exec_label:
        lines.append(f"  {exec_label}")
    lines += [
        f"  Replication Factor:          {i['rf']}",
        f"  Table size:                  {i['table_size_gb']:,} GB",
        f"  Avg row size:                {i['avg_row_bytes']} bytes  ({i['avg_row_bytes_source']})",
        f"  Data growth rate:            {i['growth_rate_pct']}%/yr  ({i['growth_rate_source']})",
    ]

    feature_lines = []
    if i.get("cdc_enabled"):
        pct = r["parameters"]["cdc_overhead_pct"]
        feature_lines.append(f"  CDC:                         enabled  (+{pct:.0f}% CPU overhead)")
    if i.get("xcluster_enabled"):
        pct = r["parameters"]["xcluster_overhead_pct"]
        feature_lines.append(f"  xCluster:                    enabled  (+{pct:.0f}% CPU overhead)")
    if feature_lines:
        lines += feature_lines

    lines += [
        "",
        "DERIVED WORKLOAD",
        "─" * 44,
        f"  Write Ops/s:                 {w['write_ops_per_s']:,}",
        f"  Read Ops/s:                  {w['read_ops_per_s']:,}",
        f"  Effective Write Ops/s:       {w['eff_write_ops_per_s']:,}  (×RF×{r['parameters']['rpc_multiplier']} RPC overhead)",
        f"  Effective Read Ops/s:        {w['eff_read_ops_per_s']:,}  (×{r['parameters']['rpc_multiplier']} RPC overhead)",
        f"  Total Effective Ops/s:       {w['total_eff_ops_per_s']:,}",
        f"  vCPUs needed (workload):     {w['raw_vcpus_workload_incl_features']}",
        "",
        "NODE SIZING ITERATIONS",
        "─" * 44,
    ]

    for idx, it in enumerate(r["sizing_iterations"]):
        status = "✅" if it["pass"] else "❌"
        lines.append(
            f"  [{idx+1}] {it['nodes']} nodes × {i['vcpu_per_node']} vCPU = "
            f"{it['total_vcpu']} vCPU | used {it['effective_vcpus_used']} "
            f"→ {it['cpu_util_pct']}% {status}"
        )

    lines += [
        "",
        "CLUSTER RECOMMENDATION",
        "─" * 44,
        f"  Total Nodes:                 {c['total_nodes']}  (multiple of RF={i['rf']})",
        f"  vCPU / node:                 {c['vcpu_per_node']}",
        f"  Memory / node:               {m['mem_per_node_gb']} GB",
        f"  Storage / node (now):        {s['storage_per_node_gb']} GB",
        f"  Storage / node (1 yr):       {s['storage_per_node_1yr_gb']} GB",
        f"  Storage / node (2 yr):       {s['storage_per_node_2yr_gb']} GB",
        "",
        f"  Total vCPU:                  {c['total_vcpu']}",
        f"  Total Memory:                {m['total_memory_gb']:,} GB",
        f"  Total Storage (now):         {s['total_storage_gb']:,} GB",
        f"  Total Storage (2 yr):        {s['total_storage_2yr_gb']:,} GB",
        "",
        "PER-NODE OPERATIONAL LIMITS",
        "─" * 44,
        f"  DB Operations / node / s:    {c['db_ops_per_node_per_s']:,}",
        f"  PG Connections / node:       {c['conn_per_node']}  ({r['parameters']['conn_per_vcpu']} × {i['vcpu_per_node']} vCPU)",
        f"  Connection CPU / node:       {c['conn_cpu_per_node']} cores",
        f"  CPU Utilization:             {c['cpu_utilization_pct']}%  ✅  (target ≤{r['parameters']['target_cpu_util_pct']:.0f}%)",
        f"  Est. IOPS / node:            {int(io['total_iops_per_node']):,}",
        f"    → Write IOPS:              {int(io['write_iops_per_node']):,}  (incl. ×{r['parameters']['write_amp_factor']} write amp)",
        f"    → Read IOPS (cold):        {int(io['read_iops_per_node']):,}  ({r['parameters']['read_cache_miss_pct']:.0f}% cache miss assumed)",
        f"  Est. Network / node:         {nw['total_net_mbps_per_node']:.1f} MB/s",
        f"    → Write (Raft):            {nw['write_net_mbps_per_node']:.1f} MB/s",
        f"    → Read:                    {nw['read_net_mbps_per_node']:.1f} MB/s",
        "",
        "STORAGE BREAKDOWN",
        "─" * 44,
        f"  Raw data + indexes:          {s['total_raw_gb']} GB",
        f"  After LZ4 compression:       {s['after_compression_gb']} GB",
        f"  After RF={i['rf']} replication:       {s['with_replication_gb']} GB",
        f"  Per node (base):             {s['base_storage_per_node_gb']} GB",
        f"  Per node (×{s['storage_multiplier']} WAL+compact):  {s['storage_per_node_gb']} GB",
        "",
        "MEMORY BREAKDOWN",
        "─" * 44,
        f"  Base ratio:                  {m['base_mem_ratio']} vCPU:RAM = {m['base_mem_gb']} GB",
        f"  Connection memory:           {c['conn_per_node']} conns × {r['parameters']['mem_mb_per_conn']:.0f} MB = {m['conn_mem_gb']} GB",
        f"  Raw total / node:            {m['raw_mem_per_node_gb']} GB",
        f"  Recommended / node:          {m['mem_per_node_gb']} GB  (standard tier)",
        "",
    ]

    if s.get("storage_cap_triggered"):
        lines += [
            f"⚠️  STORAGE CAP TRIGGERED: Storage/node exceeded {s['max_storage_per_node_gb']:,} GB "
            f"({s['max_storage_per_node_gb']//1024} TB limit).",
            f"    {s['storage_nodes_added']} extra node(s) added to bring storage/node within cap.",
            "",
        ]

    if i.get("exec_time_estimated"):
        lines.append("⚠️  IMPORTANT: Execution time was estimated. Use pg_stat_statements to measure real values:")
        lines.append("   SELECT query, round(mean_exec_time::numeric,2) AS avg_ms")
        lines.append("   FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 20;")
        lines.append("")

    # ── Failure resilience section ─────────────────────────────────────────
    fs  = r["failure_scenarios"]
    nf  = fs["node_failure"]
    zf  = fs["zone_failure"]

    def _util_badge(exceeds):
        return "⚠️  EXCEEDS TARGET" if exceeds else "✅ within target"

    lines += [
        "FAILURE RESILIENCE  (zone layout: RF={rf} zones × {npz} nodes/zone)".format(
            rf=fs["num_zones"], npz=fs["nodes_per_zone"]
        ),
        "─" * 44,
        f"  Normal (all {c['total_nodes']} nodes):          {c['cpu_utilization_pct']}%  ✅",
        "",
        f"  1-node failure  ({nf['surviving_nodes']} nodes survive):",
        f"    CPU Utilization:           {nf['cpu_util_pct']}%  {_util_badge(nf['exceeds_target'])}",
        "",
        f"  1-zone failure  ({zf['surviving_nodes']} nodes survive, {fs['nodes_per_zone']} node(s) lost):",
        f"    CPU Utilization:           {zf['cpu_util_pct']}%  {_util_badge(zf['exceeds_target'])}",
        "",
    ]

    if nf["exceeds_target"] or zf["exceeds_target"]:
        lines.append(f"  💡 Consider adding nodes (in multiples of RF) to stay within {r['parameters']['target_cpu_util_pct']:.0f}% under failure.")
        lines.append("")

    lines.append("═" * 56)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="YugabyteDB Cluster Sizing Calculator"
    )

    # Required inputs
    parser.add_argument("--qps",            type=float, required=True,  help="Total queries per second at peak")
    parser.add_argument("--write-pct",      type=float, required=True,  help="Write percentage (e.g. 30 for 30%%)")
    parser.add_argument("--read-pct",       type=float, required=True,  help="Read percentage (e.g. 70 for 70%%)")
    parser.add_argument("--avg-exec-ms",    type=float, default=None,   help="Avg execution time per op (ms). Omit to auto-estimate from workload profile.")
    parser.add_argument("--vcpu-per-node",  type=int,   required=True,  help="vCPUs per node (e.g. 8, 16, 32)")
    parser.add_argument("--rf",             type=int,   default=3,      help="Replication factor (default: 3)")
    parser.add_argument("--table-size-gb",  type=float, required=True,  help="Raw uncompressed table size (GB)")

    # Optional workload inputs
    parser.add_argument("--avg-row-bytes",         type=int,   default=None,   help=f"Avg row size in bytes (default: {DEFAULTS['avg_row_bytes']})")
    parser.add_argument("--growth-rate-pct",       type=float, default=None,   help=f"Annual data growth rate %% (default: {DEFAULTS['growth_rate_pct']})")
    parser.add_argument("--max-storage-per-node-gb", type=float, default=None, help=f"Max disk per node in GB (default: {DEFAULTS['max_storage_per_node_gb']} = 20 TB)")

    # Optional overrides for fixed parameters
    parser.add_argument("--rpc-overhead",        type=float, help=f"RPC overhead fraction added to base 1.0 (default: {DEFAULTS['rpc_overhead']} → multiplier 1.20)")
    parser.add_argument("--index-overhead",      type=float, help=f"Index overhead fraction (default: {DEFAULTS['index_overhead']})")
    parser.add_argument("--compression-ratio",   type=float, help=f"Compression reduction fraction (default: {DEFAULTS['compression_ratio']})")
    parser.add_argument("--wal-overhead",        type=float, help=f"WAL overhead fraction (default: {DEFAULTS['wal_overhead']})")
    parser.add_argument("--compaction-reserve",  type=float, help=f"Compaction reserve fraction (default: {DEFAULTS['compaction_reserve']})")
    parser.add_argument("--target-cpu-util",     type=float, help=f"Target CPU utilization (default: {DEFAULTS['target_cpu_util']})")
    parser.add_argument("--conn-per-vcpu",       type=int,   help=f"Connections per vCPU (default: {DEFAULTS['conn_per_vcpu']})")
    parser.add_argument("--mem-mb-per-conn",     type=float, help=f"MB RAM per connection (default: {DEFAULTS['mem_mb_per_conn']})")
    parser.add_argument("--conn-cpu-overhead",   type=float, help=f"CPU core overhead per connection (default: {DEFAULTS['conn_cpu_overhead']})")
    parser.add_argument("--write-amp-factor",    type=float, help=f"LSM write amplification factor (default: {DEFAULTS['write_amp_factor']})")
    parser.add_argument("--read-cache-miss",     type=float, help=f"Read cache miss fraction (default: {DEFAULTS['read_cache_miss']})")

    # CDC / xCluster feature flags
    parser.add_argument("--cdc",                 action="store_true", default=False, help="Enable CDC (Change Data Capture): adds 5%% CPU overhead by default")
    parser.add_argument("--cdc-overhead",        type=float, default=None,           help=f"Override CDC CPU overhead fraction (default: {DEFAULTS['cdc_overhead']})")
    parser.add_argument("--xcluster",            action="store_true", default=False, help="Enable xCluster replication: adds 5%% CPU overhead by default")
    parser.add_argument("--xcluster-overhead",   type=float, default=None,           help=f"Override xCluster CPU overhead fraction (default: {DEFAULTS['xcluster_overhead']})")

    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of formatted report")

    args = parser.parse_args()

    if abs(args.write_pct + args.read_pct - 100) > 0.01:
        print(f"ERROR: write-pct ({args.write_pct}) + read-pct ({args.read_pct}) must equal 100", file=sys.stderr)
        sys.exit(1)

    result = calculate(
        qps=args.qps,
        write_pct=args.write_pct,
        read_pct=args.read_pct,
        avg_exec_ms=args.avg_exec_ms,
        vcpu_per_node=args.vcpu_per_node,
        rf=args.rf,
        table_size_gb=args.table_size_gb,
        avg_row_bytes=args.avg_row_bytes,
        growth_rate_pct=args.growth_rate_pct,
        max_storage_per_node_gb=args.max_storage_per_node_gb,
        rpc_overhead=args.rpc_overhead,
        index_overhead=args.index_overhead,
        compression_ratio=args.compression_ratio,
        wal_overhead=args.wal_overhead,
        compaction_reserve=args.compaction_reserve,
        target_cpu_util=args.target_cpu_util,
        conn_per_vcpu=args.conn_per_vcpu,
        mem_mb_per_conn=args.mem_mb_per_conn,
        conn_cpu_overhead=args.conn_cpu_overhead,
        write_amp_factor=args.write_amp_factor,
        read_cache_miss=args.read_cache_miss,
        cdc_enabled=args.cdc,
        cdc_overhead=args.cdc_overhead,
        xcluster_enabled=args.xcluster,
        xcluster_overhead=args.xcluster_overhead,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()