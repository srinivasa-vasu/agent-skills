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

    # --api ysql (default) or --api ycql selects the API-specific overhead profile.
    # avg-exec-ms is optional; if omitted, it is estimated from workload profile.
    # Use --avg-row-bytes and --growth-rate-pct for IOPS/network/storage-growth output.
    # --format text (default) | html | json  — html is a self-contained report page.
    # YCQL only: --ttl-days caps storage growth at the TTL steady state.

All fixed overhead parameters can be overridden via flags if needed.
"""

import argparse
import math
import json
import sys


# ─── Fixed Defaults ────────────────────────────────────────────────────────────
DEFAULTS = {
    "compression_ratio":   0.30,   # 30% compression reduction
    "wal_retention_secs":  900,    # log_min_seconds_to_retain: WAL kept ≥15 min after flush
    "cdc_wal_retention_secs": 28800,  # cdc_wal_retention_time_secs default: WAL kept 8 h for CDC streams
    "xcluster_wal_retention_secs": 86400,  # YB xCluster best practice: cdc_wal_retention_time_secs = 24 h
    # YSQL Connection Manager (enable_ysql_conn_mgr)
    "cm_backends_per_vcpu":  10,   # physical backends/vCPU (docs: ≤15, ~10 for latency-sensitive OLTP)
    "cm_client_ratio":       10,   # client (logical) connections per backend (Aeon default)
    "cm_mem_mb":             200,  # odyssey process RAM per node (docs: up to 200 MB)
    "pg_memory_share":       0.27, # PostgreSQL share of node RAM (use_memory_defaults_optimized_for_ysql, >16 GB)
    "compaction_reserve":  0.20,   # 20% free space for LSM compaction
    "target_cpu_util":     0.65,   # 65% max sustained CPU utilization
    "cache_fraction_of_ram": 0.5,  # RAM that ends up caching data (block cache + OS page cache)
    # Calibrated at the default 512-byte row from TPC-C 4k (3× m8i.4xlarge) and sysbench
    # point selects (3× m6i.2xlarge), YB 2026.1, gp3:
    "iops_per_replica_write": 0.245,  # disk IOPS per replicated write (WAL group commit + batched flushes)
    "disk_write_amp":      19,     # disk bytes per replicated written byte (WAL + flush + compaction + reads)
    "net_write_factor":    13.9,   # NIC bytes per row-byte per replicated write (Raft, intents, txn status, TLS)
    "net_read_factor":     3.2,    # NIC bytes per row-byte per read (client + tserver RPC, TLS)
    # Disk limits per node: gp3 maximum provisionable (baseline is 3,000 IOPS / 125 MiB/s)
    "disk_iops_limit":     16000,
    "disk_mibps_limit":    1000,
    "cross_az_cost_per_gb": 0.02,  # AWS inter-AZ transfer: $0.01/GB in each direction
    "cross_region_cost_per_gb": 0.02,  # typical AWS inter-region transfer within a continent (varies by pair)
    "region_rtt_ms":       30,     # cross-region round trip between neighbouring regions (adjust per pair)
    # Distributed (multi-shard) transactions, in units of a fast-path write: a commit record to the
    # transaction-status tablet plus a provisional record (intent) per written row that is later applied.
    # Together ≈3× a fast-path single-row write (YFTT ep. 107: 1.7 vs 4.7 ms, ~6,000 vs ~2,000 txns).
    "txn_commit_cost":     1.0,
    "txn_intent_cost":     1.0,
    "read_replica_rf":     1,
    "follower_write_fraction": 0.333,  # follower apply cost relative to the leader's write cost
    "avg_row_bytes":       512,    # Default avg row size if not provided
    "growth_rate_pct":     30,     # Annual data growth rate % if not provided
    "ram_tier_tolerance":  0.02,   # Stay on a RAM tier if raw need exceeds it by ≤2%
    "max_storage_per_node_gb": 20480,  # 20 TB max disk density per node
    "cdc_overhead":         0.05,   # 5% extra CPU overhead when CDC is enabled
    "xcluster_overhead":    0.10,   # 10% extra CPU overhead when xCluster is enabled
    "tablet_vcpu_per_1000":    0.4,    # vCPU consumed per 1,000 tablets (cluster-wide) for tablet maintenance
    "tablet_mem_mb_per_1000":  700,    # MB of RAM consumed per 1,000 tablets (cluster-wide) for tablet maintenance
    # Automatic tablet splitting phases (yb-master defaults)
    "split_low_phase_bytes":     128 * 1024 ** 2,   # tablet_split_low_phase_size_threshold_bytes
    "split_low_phase_per_node":  1,                 # tablet_split_low_phase_shard_count_per_node
    "split_high_phase_bytes":    10 * 1024 ** 3,    # tablet_split_high_phase_size_threshold_bytes
    "split_high_phase_per_node": 24,                # tablet_split_high_phase_shard_count_per_node
    "split_force_bytes":         100 * 1024 ** 3,   # tablet_force_split_threshold_bytes
    "max_tablets_per_ts":        50,                # max_create_tablets_per_ts (also caps splitting)
}

# API-specific defaults. Explicit CLI overrides always win over these.
API_PROFILES = {
    "ysql": {
        "rpc_overhead":      0.15,   # RPC, retries, joins, index lookups, auto-analyze (applied as 1 + rpc_overhead)
        "index_overhead":    0.20,   # 20% extra storage for indexes
        "conn_per_vcpu":     16,     # PostgreSQL connections per vCPU
        "mem_mb_per_conn":   15,     # MB per PostgreSQL backend (TPC-C: 1,952 MB PSS / 133 backends per node)
        "conn_cpu_overhead": 0.002,  # CPU cores consumed per connection (~0.2%)
    },
    "ycql": {
        "rpc_overhead":      0.08,   # retries + secondary-index maintenance; no joins/auto-analyze, no PG→tserver hop
        "index_overhead":    0.10,   # query-first, denormalized models carry fewer secondary indexes
        "conn_per_vcpu":     0,      # CQL connections are multiplexed by drivers; no per-connection backend
        "mem_mb_per_conn":   0,
        "conn_cpu_overhead": 0.0,
    },
}

# Estimated avg execution time (ms) by workload profile when not provided by user
EXEC_TIME_ESTIMATES = {
    # (condition_fn, estimated_ms, label)
    "ysql": [
        (lambda w, r: w >= 70,               2,    "write-heavy / key-value (estimated)"),
        (lambda w, r: r >= 90,               20,   "read-heavy with scans/analytics (estimated)"),
        (lambda w, r: r >= 70,               7,    "read-heavy OLTP with joins (estimated)"),
        (lambda w, r: True,                  4,    "mixed OLTP (estimated)"),
    ],
    "ycql": [
        (lambda w, r: w >= 70,               0.75, "write-heavy point writes (estimated)"),
        (lambda w, r: r >= 90,               2,    "read-heavy single-partition range reads (estimated)"),
        (lambda w, r: True,                  1,    "mixed point lookups/writes (estimated)"),
    ],
}

# CPU milliseconds per client operation at RF=3 on current-generation hardware (m8i/m8g class),
# before the RPC/retry multiplier. Derived from measured runs: busy cores − connection CPU −
# tablet CPU, ÷ RPC multiplier; YSQL's read/write split uses YCQL's write:read cost ratio (1.67).
# The YCQL run is from 2016-era i3.4xlarge (Broadwell) on an older YB release, so its measured
# costs are scaled by YCQL_HW_GENERATION_FACTOR (≈1.67× per-vCPU throughput from CPU generations
# alone; software gains not counted).
YCQL_MEASURED = {"read": 0.178, "write": 0.296}
YCQL_HW_GENERATION_FACTOR = 0.6
CPU_COST_PROFILES = {
    "ysql": {"read": 1.070, "write": 1.784,
             "source": "TPC-C 4k, YB 2026.1, 3× m8i.4xlarge, RF3, 400 conns: 14,985 stmt/s (56% writes) at 54.5% CPU"},
    "ycql": {"read": round(YCQL_MEASURED["read"] * YCQL_HW_GENERATION_FACTOR, 4),
             "write": round(YCQL_MEASURED["write"] * YCQL_HW_GENERATION_FACTOR, 4),
             "source": "YCQL key-value benchmark (3× i3.4xlarge, RF3: 150k reads/s, 90k writes/s at 60% CPU) "
                       f"× {YCQL_HW_GENERATION_FACTOR} for current-generation CPUs"},
}

# Workload profiles: CPU multiplier relative to the API's measured baseline. YSQL kv is measured
# (sysbench point selects: 0.229 ms/read current-gen vs 1.070 oltp); the others follow the
# exec-time tiers (YSQL 7–10/20 ms vs mixed 4 ms; YCQL 2/3 ms vs 0.75 ms point).
WORKLOAD_PROFILES = {
    "ysql": {
        "kv":        (0.214, "point lookups / single-row writes (sysbench-measured)"),
        "oltp":      (1.0, "transactional OLTP, indexed (TPC-C-like) — baseline"),
        "complex":   (2.0, "joins / aggregations"),
        "analytics": (5.0, "scans / reporting queries"),
    },
    "ycql": {
        "point":     (1.0, "point reads/writes by full primary key — baseline"),
        "range":     (2.7, "single-partition range reads"),
        "lwt":       (4.0, "lightweight transactions or writes to indexed tables"),
    },
}
WORKLOAD_AUTO = {  # used when --workload is not given; mirrors the exec-time fallback tiers
    "ysql": [(lambda w, r: w >= 70, "kv"), (lambda w, r: r >= 90, "analytics"),
             (lambda w, r: r >= 70, "complex"), (lambda w, r: True, "oltp")],
    "ycql": [(lambda w, r: r >= 90, "range"), (lambda w, r: True, "point")],
}
# Profiles measured on fast-path (single-shard) operations; distributed-transaction overhead is added
# on top of these. The other profiles (TPC-C-based oltp and its multiples, YCQL lwt) already include it.
FASTPATH_PROFILE = {"ysql": "kv", "ycql": "point"}
FASTPATH_BASED = {"ysql": {"kv"}, "ycql": {"point", "range"}}
CPU_ARCH_FACTORS = {"x86": 1.0, "arm": 1.10}   # Graviton (m8g) used 10% more CPU than m8i for the same TPC-C load
CPU_MODELS = ("per-op", "latency")

VALID_RF = (1, 3, 5, 7)
SIZE_FOR = ("normal", "node", "zone")
COMPARE_VCPU_BAND = 0.10   # tiers within 10% of the lowest total vCPU count as equivalent

# Standard RAM tiers (GB) - round up to nearest
RAM_TIERS = [16, 32, 64, 128, 192, 256, 384, 512, 768, 1024]


def round_up_to_rf_multiple(n, rf):
    """Round n up to the nearest multiple of rf, minimum rf."""
    if n <= rf:
        return rf
    return math.ceil(n / rf) * rf


def round_up_to_ram_tier(gb, tolerance=0.0):
    """Round up to the nearest standard RAM tier, staying on a tier exceeded by ≤ tolerance."""
    for tier in RAM_TIERS:
        if tier * (1 + tolerance) >= gb:
            return tier
    return math.ceil(gb / 256) * 256  # beyond known tiers, round to 256 GB


def _pick(value, default):
    return value if value is not None else default


def data_driven_tablets(data_gb, nodes):
    """Tablets (before RF) automatic splitting produces for `data_gb` of on-disk
    (compressed) data on `nodes` nodes, treating the data as one table:
    split at 128 MiB until 1 tablet/node, at 10 GiB until 24 tablets/node,
    then at 100 GiB, capped at max_tablets_per_ts per node."""
    data = data_gb * 1024 ** 3
    low_cap = DEFAULTS["split_low_phase_per_node"] * nodes
    tablets = math.ceil(data / DEFAULTS["split_low_phase_bytes"])
    if tablets <= low_cap:
        return max(tablets, 1)
    high_cap = DEFAULTS["split_high_phase_per_node"] * nodes
    tablets = max(low_cap, math.ceil(data / DEFAULTS["split_high_phase_bytes"]))
    if tablets <= high_cap:
        return tablets
    tablets = max(high_cap, math.ceil(data / DEFAULTS["split_force_bytes"]))
    return min(tablets, DEFAULTS["max_tablets_per_ts"] * nodes)


def _ops(qps, write_pct, read_pct, rf, rpc_multiplier):
    """(write_ops, read_ops, eff_write_ops, eff_read_ops) for a QPS and mix."""
    write_ops = qps * (write_pct / 100)
    read_ops  = qps * (read_pct  / 100)
    return write_ops, read_ops, write_ops * rf * rpc_multiplier, read_ops * rpc_multiplier


def calculate(qps, write_pct, read_pct, avg_exec_ms, vcpu_per_node, rf, table_size_gb, **options):
    """Size a cluster. See _Sizing for the options; returns the result dict."""
    return _Sizing(qps=qps, write_pct=write_pct, read_pct=read_pct, avg_exec_ms=avg_exec_ms,
                   vcpu_per_node=vcpu_per_node, rf=rf, table_size_gb=table_size_gb, **options).run()


class _Sizing:
    """One sizing run. Each step is a method; `run()` wires them together.

    Node-count-dependent quantities (tablets, connection CPU, storage, memory, I/O) are
    methods of `nodes`, so the sizing loops can re-evaluate them as nodes are added."""

    def __init__(
        self,
        qps,
        write_pct,
        read_pct,
        avg_exec_ms,        # latency: concurrency check (per-op model) or CPU driver (legacy model)
        vcpu_per_node,
        rf,
        table_size_gb,
        avg_row_bytes=None,
        growth_rate_pct=None,
        max_storage_per_node_gb=None,
        rpc_overhead=None,
        index_overhead=None,
        compression_ratio=None,
        wal_overhead=None,          # legacy: WAL as a fraction of data; default is the write-rate model
        wal_retention_secs=None,
        cdc_wal_retention_secs=None,
        xcluster_wal_retention_secs=None,
        compaction_reserve=None,
        target_cpu_util=None,
        conn_per_vcpu=None,
        mem_mb_per_conn=None,
        conn_cpu_overhead=None,
        read_cache_miss=None,
        iops_per_replica_write=None,
        disk_write_amp=None,
        net_write_factor=None,
        net_read_factor=None,
        disk_iops=None,             # per-node disk IOPS limit (default: gp3 max); nodes are added to fit
        disk_mibps=None,            # per-node disk throughput limit, MiB/s (default: gp3 max)
        zones=None,                 # availability zones: RF (default, one zone per replica) or 1 (single AZ)
        cross_az_cost_per_gb=None,
        ram_tier_tolerance=None,
        cdc_enabled=False,
        cdc_overhead=None,
        xcluster_enabled=False,
        xcluster_overhead=None,
        num_objects=None,           # tables + indexes count (optional)
        tablets_per_table=None,
        tablet_vcpu_per_1000=None,
        tablet_mem_mb_per_1000=None,
        api="ysql",
        ttl_days=None,              # YCQL only: row/table TTL caps steady-state data size
        size_for="normal",          # normal | node | zone — add nodes until that failure stays within target
        failure_cpu_target=None,    # CPU ceiling under failure (default: target_cpu_util)
        observed_qps=None,          # calibration: an existing cluster/PoC running the same workload mix
        observed_cpu_pct=None,
        observed_nodes=None,
        observed_vcpu_per_node=None,
        connection_manager=False,   # YSQL Connection Manager: multiplex client connections onto a backend pool
        cm_client_ratio=None,
        cpu_model="per-op",         # per-op (measured CPU per operation) | latency (ops × exec time, legacy)
        workload=None,              # workload profile (see WORKLOAD_PROFILES); None = auto from the mix
        cpu_ms_per_read=None,
        cpu_ms_per_write=None,
        cpu_arch="x86",
        cpu_cost_scale=None,        # hardware adjustment: 1.0 current gen, ~1.15 previous, ~1.5 older (m5/c5/i3)
        tps=None,                   # transactions/s — alternative to qps (qps = tps × statements_per_txn)
        statements_per_txn=None,    # statements per transaction (default 1 = autocommit)
        distributed_txn_pct=None,   # % of write transactions that are distributed (default: 100 if >1 stmt, else 0)
        follower_read_pct=None,     # % of reads served as follower reads (read-only, bounded staleness)
        read_replica_read_pct=None, # % of reads served by a read-replica cluster
        read_replica_rf=None,       # copies of the data in the read-replica cluster (default 1)
        read_replica_vcpu=None,     # vCPU per read-replica node (default: vcpu_per_node)
        regions=None,               # 1 (default) or RF: one replica per region (synchronous stretch cluster)
        region_rtt_ms=None,         # cross-region round trip (default 30 ms)
        preferred_region=False,     # all tablet leaders pinned to one region
        cross_region_cost_per_gb=None,
        write_pipelining=False,     # YSQL ysql_enable_write_pipelining: async Raft per write, wait at COMMIT
    ):
        # ── Validation ─────────────────────────────────────────────────────
        if api not in API_PROFILES:
            raise ValueError(f"api must be one of {sorted(API_PROFILES)}, got {api!r}")
        if rf not in VALID_RF:
            raise ValueError(f"rf must be one of {VALID_RF}, got {rf}")
        if cdc_enabled and api == "ycql":
            raise ValueError("CDC is not supported for YCQL in this sizing model")
        if ttl_days is not None and api != "ycql":
            raise ValueError("ttl_days applies to YCQL only")
        if size_for not in SIZE_FOR:
            raise ValueError(f"size_for must be one of {SIZE_FOR}, got {size_for!r}")
        if size_for != "normal" and rf == 1:
            raise ValueError("size_for node/zone needs RF ≥ 3 — RF=1 has no failure tolerance")
        if connection_manager and api != "ysql":
            raise ValueError("Connection Manager applies to YSQL only")
        # Transaction mix: QPS (statements/s) directly, or TPS × statements per transaction
        statements_per_txn = _pick(statements_per_txn, 1)
        if statements_per_txn < 1:
            raise ValueError("statements_per_txn must be ≥ 1")
        if tps is not None and qps is not None:
            raise ValueError("pass either qps (statements/s) or tps (transactions/s), not both")
        if tps is not None:
            qps = tps * statements_per_txn
        if qps is None:
            raise ValueError("qps or tps is required")
        if distributed_txn_pct is not None and not 0 <= distributed_txn_pct <= 100:
            raise ValueError("distributed_txn_pct must be between 0 and 100")
        # Read offload
        follower_read_frac = _pick(follower_read_pct, 0) / 100
        rr_frac = _pick(read_replica_read_pct, 0) / 100
        if not (0 <= follower_read_frac <= 1 and 0 <= rr_frac <= 1 and follower_read_frac + rr_frac <= 1):
            raise ValueError("follower_read_pct and read_replica_read_pct must be 0–100 and sum to ≤ 100")
        # Multi-region: one replica per region (regions = RF), so regions become the fault domain
        regions = _pick(regions, 1)
        if regions not in (1, rf):
            raise ValueError(f"regions must be 1 or {rf} (one replica per region), got {regions}")
        if regions > 1:
            if zones not in (None, rf):
                raise ValueError("with multiple regions, each region is a zone: leave zones unset")
            zones = rf
        if preferred_region and regions == 1:
            raise ValueError("preferred_region needs a multi-region layout (regions = RF)")
        if write_pipelining and api != "ysql":
            raise ValueError("write pipelining (ysql_enable_write_pipelining) applies to YSQL only")
        zones = _pick(zones, rf)
        if zones not in (1, rf):
            raise ValueError(f"zones must be {rf} (one per replica, multi-AZ) or 1 (single AZ), got {zones}")
        if zones == 1 and size_for == "zone":
            raise ValueError("size_for zone needs a multi-AZ layout (zones = RF)")
        # Calibration needs the two measurements; the observed cluster shape defaults to a
        # minimal RF-node cluster with the same vCPU/node as the target.
        self.calibrated = any(v is not None for v in
                              (observed_qps, observed_cpu_pct, observed_nodes, observed_vcpu_per_node))
        if self.calibrated and (observed_qps is None or observed_cpu_pct is None):
            raise ValueError("calibration needs at least observed_qps and observed_cpu_pct")
        if cpu_model not in CPU_MODELS:
            raise ValueError(f"cpu_model must be one of {CPU_MODELS}, got {cpu_model!r}")
        if cpu_arch not in CPU_ARCH_FACTORS:
            raise ValueError(f"cpu_arch must be one of {sorted(CPU_ARCH_FACTORS)}, got {cpu_arch!r}")
        if workload is not None and workload not in WORKLOAD_PROFILES[api]:
            raise ValueError(f"workload for {api} must be one of {sorted(WORKLOAD_PROFILES[api])}, got {workload!r}")

        # ── Inputs and provenance ──────────────────────────────────────────
        self.qps, self.write_pct, self.read_pct = qps, write_pct, read_pct
        self.avg_exec_ms, self.vcpu_per_node, self.rf = avg_exec_ms, vcpu_per_node, rf
        self.table_size_gb, self.api, self.ttl_days = table_size_gb, api, ttl_days
        self.cdc_enabled, self.xcluster_enabled = cdc_enabled, xcluster_enabled
        self.num_objects, self.tablets_per_table = num_objects, tablets_per_table
        self.size_for, self.connection_manager = size_for, connection_manager
        self.zones = zones
        self.read_cache_miss_given = read_cache_miss
        self.tps_given = tps
        self.statements_per_txn = statements_per_txn
        self.distributed_txn_pct = _pick(distributed_txn_pct, 100 if statements_per_txn > 1 else 0)
        self.follower_read_frac, self.rr_frac = follower_read_frac, rr_frac
        self.rr_rf = _pick(read_replica_rf, DEFAULTS["read_replica_rf"])
        self.rr_vcpu = _pick(read_replica_vcpu, vcpu_per_node)
        self.regions, self.preferred_region = regions, preferred_region
        self.write_pipelining = write_pipelining
        self.region_rtt_ms = _pick(region_rtt_ms, DEFAULTS["region_rtt_ms"]) if regions > 1 else 0
        self.cross_region_cost_per_gb = _pick(cross_region_cost_per_gb, DEFAULTS["cross_region_cost_per_gb"])
        self.cpu_model, self.workload, self.cpu_arch = cpu_model, workload, cpu_arch
        self.cpu_ms_per_read_given = cpu_ms_per_read
        self.cpu_ms_per_write_given = cpu_ms_per_write
        self.observed_qps, self.observed_cpu_pct = observed_qps, observed_cpu_pct
        self.observed_nodes_source = "provided" if observed_nodes is not None else f"default (RF={rf})"
        self.observed_vcpu_source = ("provided" if observed_vcpu_per_node is not None
                                     else "default (target vCPU/node)")
        self.observed_nodes = _pick(observed_nodes, rf)
        self.observed_vcpu_per_node = _pick(observed_vcpu_per_node, vcpu_per_node)
        self.avg_row_bytes_source = "provided" if avg_row_bytes is not None else "default"
        self.growth_rate_source = "provided" if growth_rate_pct is not None else "default"

        # ── Defaults (CLI > API profile > global defaults) ─────────────────
        profile = API_PROFILES[api]
        self.rpc_overhead       = _pick(rpc_overhead,       profile["rpc_overhead"])
        self.index_overhead     = _pick(index_overhead,     profile["index_overhead"])
        self.conn_per_vcpu      = _pick(conn_per_vcpu, DEFAULTS["cm_backends_per_vcpu"] if connection_manager
                                        else profile["conn_per_vcpu"])
        self.cm_client_ratio    = _pick(cm_client_ratio,    DEFAULTS["cm_client_ratio"])
        self.mem_mb_per_conn    = _pick(mem_mb_per_conn,    profile["mem_mb_per_conn"])
        self.conn_cpu_overhead  = _pick(conn_cpu_overhead,  profile["conn_cpu_overhead"])
        self.compression_ratio  = _pick(compression_ratio,  DEFAULTS["compression_ratio"])
        self.wal_overhead       = wal_overhead
        self.wal_pct_model      = wal_overhead is not None
        self.wal_retention_secs = _pick(wal_retention_secs, DEFAULTS["wal_retention_secs"])
        self.cdc_wal_retention_secs = _pick(cdc_wal_retention_secs, DEFAULTS["cdc_wal_retention_secs"])
        self.xcluster_wal_retention_secs = _pick(xcluster_wal_retention_secs,
                                                 DEFAULTS["xcluster_wal_retention_secs"])
        self.compaction_reserve = _pick(compaction_reserve, DEFAULTS["compaction_reserve"])
        self.target_cpu_util    = _pick(target_cpu_util,    DEFAULTS["target_cpu_util"])
        self.failure_cpu_target = _pick(failure_cpu_target, self.target_cpu_util)
        self.iops_per_replica_write = _pick(iops_per_replica_write, DEFAULTS["iops_per_replica_write"])
        self.disk_write_amp     = _pick(disk_write_amp,     DEFAULTS["disk_write_amp"])
        self.net_write_factor   = _pick(net_write_factor,   DEFAULTS["net_write_factor"])
        self.net_read_factor    = _pick(net_read_factor,    DEFAULTS["net_read_factor"])
        self.disk_iops_limit    = _pick(disk_iops,          DEFAULTS["disk_iops_limit"])
        self.disk_mibps_limit   = _pick(disk_mibps,         DEFAULTS["disk_mibps_limit"])
        self.cross_az_cost_per_gb = _pick(cross_az_cost_per_gb, DEFAULTS["cross_az_cost_per_gb"])
        self.ram_tier_tolerance = _pick(ram_tier_tolerance, DEFAULTS["ram_tier_tolerance"])
        self.avg_row_bytes      = _pick(avg_row_bytes,      DEFAULTS["avg_row_bytes"])
        self.growth_rate_pct    = _pick(growth_rate_pct,    DEFAULTS["growth_rate_pct"])
        self.max_storage_per_node_gb = _pick(max_storage_per_node_gb, DEFAULTS["max_storage_per_node_gb"])
        self.cdc_overhead       = _pick(cdc_overhead,       DEFAULTS["cdc_overhead"])
        self.xcluster_overhead  = _pick(xcluster_overhead,  DEFAULTS["xcluster_overhead"])
        self.tablet_vcpu_per_1000   = _pick(tablet_vcpu_per_1000,   DEFAULTS["tablet_vcpu_per_1000"])
        self.tablet_mem_mb_per_1000 = _pick(tablet_mem_mb_per_1000, DEFAULTS["tablet_mem_mb_per_1000"])
        self.cpu_cost_scale     = _pick(cpu_cost_scale, 1.0)

        # ── Step 1 + 2: Ops/s with RF and RPC overhead ─────────────────────
        self.rpc_multiplier = 1.0 + self.rpc_overhead
        (self.write_ops, self.read_ops,
         self.eff_write_ops, self.eff_read_ops) = _ops(qps, write_pct, read_pct, rf, self.rpc_multiplier)
        self.total_eff_ops = self.eff_write_ops + self.eff_read_ops

        # CDC / xCluster: each enabled feature adds a percentage on top of workload CPU
        self.feature_overhead_factor = (1.0 + (self.cdc_overhead if cdc_enabled else 0)
                                        + (self.xcluster_overhead if xcluster_enabled else 0))

        # Data volume (needed for tablet count and storage)
        self.index_size_gb     = table_size_gb * self.index_overhead
        self.total_raw_gb      = table_size_gb + self.index_size_gb
        self.after_compression = self.total_raw_gb * (1 - self.compression_ratio)
        self.with_replication  = self.after_compression * rf

        self.conn_per_node     = self.conn_per_vcpu * vcpu_per_node
        self.conn_cpu_per_node = self._conn_cpu_per_node(vcpu_per_node)

    # ── Tablet maintenance overhead ────────────────────────────────────────
    # Every 1,000 tablets costs a fixed cluster-wide amount of vCPU + RAM for background
    # tablet maintenance (Raft heartbeats, bootstrapping, etc.), independent of ops/s.
    # Tablets (before RF) = max(schema-driven, data-driven):
    #   schema (only if num_objects given):
    #     YSQL: 1 tablet per table/index (no pre-splitting) unless overridden.
    #     YCQL: every table is hash-sharded; with automatic tablet splitting,
    #           yb_num_shards_per_tserver = 1 → 1 tablet per tserver per table
    #           (1 per cluster for ≤2 cores, 2 per cluster for ≤4 cores).
    #   data: automatic tablet splitting as the data grows (data_driven_tablets).

    def _tablets_per_table(self, nodes, vcpu):
        if self.tablets_per_table is not None:
            return self.tablets_per_table
        if self.api == "ysql":
            return 1
        if vcpu <= 2:
            return 1
        if vcpu <= 4:
            return 2
        return nodes

    def _tablet_counts(self, nodes, vcpu=None):
        """(schema_tablets, data_tablets) before RF for `nodes` nodes."""
        vcpu = _pick(vcpu, self.vcpu_per_node)
        schema = (self.num_objects * self._tablets_per_table(nodes, vcpu)
                  if self.num_objects is not None else 0)
        return schema, data_driven_tablets(self.after_compression, nodes)

    def _tablet_overhead(self, nodes, vcpu=None):
        """(total_tablets, vcpu_overhead, mem_overhead_mb) cluster-wide for `nodes` nodes."""
        tablets = max(self._tablet_counts(nodes, vcpu)) * self.rf
        return (tablets, (tablets / 1000) * self.tablet_vcpu_per_1000,
                (tablets / 1000) * self.tablet_mem_mb_per_1000)

    def _conn_cpu_per_node(self, vcpu):
        return self.conn_per_vcpu * vcpu * self.conn_cpu_overhead

    # ── Step 3: Workload CPU — measured (calibration), per-op costs, or legacy latency
    # per-op:  cores = (reads × ms/read + writes × ms/write × RF scale) × workload × RPC × features × arch
    #          Costs are measured CPU per client op at RF=3; RF scale = leader + (RF−1) cheaper followers.
    # latency: cores = effective ops × exec time (legacy). Exec time is latency, which includes
    #          waiting, so this can be far off in either direction.

    def _resolve_cpu_model(self):
        self.latency_ms = self.avg_exec_ms     # what the user gave: a latency, for the concurrency check
        self.exec_time_label = None
        self.exec_time_estimated = False
        if self.cpu_model == "latency" and self.avg_exec_ms is None:
            self.exec_time_estimated = True
            for condition, est_ms, label in EXEC_TIME_ESTIMATES[self.api]:
                if condition(self.write_pct, self.read_pct):
                    self.avg_exec_ms, self.exec_time_label = est_ms, label
                    break

        self.workload_source = "provided" if self.workload is not None else "auto (from read/write mix)"
        if self.workload is None:
            self.workload = next(name for cond, name in WORKLOAD_AUTO[self.api]
                                 if cond(self.write_pct, self.read_pct))
        self.workload_multiplier, self.workload_label = WORKLOAD_PROFILES[self.api][self.workload]
        cost = CPU_COST_PROFILES[self.api]
        self.cpu_ms_read  = _pick(self.cpu_ms_per_read_given,  cost["read"])  * self.cpu_cost_scale
        self.cpu_ms_write = _pick(self.cpu_ms_per_write_given, cost["write"]) * self.cpu_cost_scale
        follower = DEFAULTS["follower_write_fraction"]
        self.rf_write_scale = (1 + (self.rf - 1) * follower) / (1 + 2 * follower)
        self.arch_factor = CPU_ARCH_FACTORS[self.cpu_arch]

    def _leader_write_portion(self):
        """Share of a write's CPU spent on the leader (the rest is RF − 1 follower applies)."""
        f = DEFAULTS["follower_write_fraction"]
        return 1 / (1 + (self.rf - 1) * f)

    def _txn(self, q):
        """Transaction mix for `q` statements/s: write transactions and distributed overhead (ms/s)."""
        spt = self.statements_per_txn
        w_frac = self.write_pct / 100
        tps = q / spt
        p_write = 1 - (1 - w_frac) ** spt            # share of transactions with ≥1 write
        write_txns = tps * p_write
        dist = self.distributed_txn_pct / 100
        applies = self.cpu_model == "per-op" and self.workload in FASTPATH_BASED[self.api]
        overhead_ms = 0.0
        if applies and dist:
            # In fast-path write units, at the fast-path profile's cost
            fast_mult = WORKLOAD_PROFILES[self.api][FASTPATH_PROFILE[self.api]][0]
            units = dist * (write_txns * DEFAULTS["txn_commit_cost"] + q * w_frac * DEFAULTS["txn_intent_cost"])
            overhead_ms = units * self.cpu_ms_write * self.rf_write_scale * fast_mult / self.workload_multiplier
        return {"tps": tps, "write_txns": write_txns, "p_write": p_write, "dist": dist,
                "applies": applies, "overhead_ms": overhead_ms}

    def _region_cores(self, q):
        """(leader-region cores, follower-region cores) per region when leaders are pinned."""
        w, r = q * self.write_pct / 100, q * self.read_pct / 100
        m = self.workload_multiplier * self.rpc_multiplier * self.feature_overhead_factor * self.arch_factor
        write_cost = w * self.cpu_ms_write * self.rf_write_scale
        lead = self._leader_write_portion()
        r_leader = r * (1 - self.rr_frac - self.follower_read_frac)
        r_follower_each = r * self.follower_read_frac / self.rf        # follower reads spread over replicas
        leader = ((r_leader + r_follower_each) * self.cpu_ms_read + write_cost * lead
                  + self._txn(q)["overhead_ms"]) * m / 1000
        follower = (r_follower_each * self.cpu_ms_read + write_cost * (1 - lead) / (self.rf - 1)) * m / 1000
        return leader, follower

    def _model_cores(self, q):
        """Workload CPU cores the model predicts for `q` QPS at this mix (primary cluster)."""
        w, r = q * self.write_pct / 100, q * self.read_pct / 100
        r_primary = r * (1 - self.rr_frac)                         # read replicas take the rest
        if self.cpu_model == "latency":
            return (w * self.rf + r_primary) * self.rpc_multiplier * self.avg_exec_ms / 1000 * self.feature_overhead_factor
        balanced = ((r_primary * self.cpu_ms_read + w * self.cpu_ms_write * self.rf_write_scale
                     + self._txn(q)["overhead_ms"]) / 1000
                    * self.workload_multiplier * self.rpc_multiplier * self.feature_overhead_factor * self.arch_factor)
        if self.preferred_region:
            # Every region is sized to carry the leader load, so it can take over on failover
            return max(balanced, self._region_cores(q)[0] * self.regions)
        return balanced

    def _workload_cpu(self):
        """Set cpu_seconds_needed (and calibration details when observed metrics are given)."""
        self.calibration = None
        if not self.calibrated:
            self.cpu_seconds_needed = self._model_cores(self.qps)
            return
        # Measured beats modeled: observed busy cores − connection CPU − tablet CPU = workload
        # cores, scaled linearly to the target QPS (same mix, RF, features and CPU arch).
        n, v = self.observed_nodes, self.observed_vcpu_per_node
        busy   = self.observed_cpu_pct / 100 * n * v
        conn   = self._conn_cpu_per_node(v) * n
        tablet = self._tablet_overhead(n, v)[1]
        work   = busy - conn - tablet
        if work <= 0 or self.observed_qps <= 0:
            raise ValueError("observed CPU is fully explained by connection/tablet overhead — check observed inputs")
        self.cpu_seconds_needed = work * self.qps / self.observed_qps
        self.calibration = {
            "observed_qps": self.observed_qps,
            "observed_cpu_pct": self.observed_cpu_pct,
            "observed_nodes": n,
            "observed_nodes_source": self.observed_nodes_source,
            "observed_vcpu_per_node": v,
            "observed_vcpu_source": self.observed_vcpu_source,
            "observed_busy_cores": round(busy, 2),
            "observed_conn_cores": round(conn, 2),
            "observed_tablet_cores": round(tablet, 2),
            "observed_workload_cores": round(work, 2),
            "cpu_ms_per_op": round(work / self.observed_qps * 1000, 4),
            "factor_vs_model": round(work / self._model_cores(self.observed_qps), 2),
        }

    # ── Steps 4–5: CPU at a node count ─────────────────────────────────────

    def _raw_vcpus(self, nodes):
        """Workload CPU (incl. CDC/xCluster) + tablet maintenance CPU for `nodes` nodes."""
        return self.cpu_seconds_needed + self._tablet_overhead(nodes)[1]

    def _util(self, active_nodes, sized_nodes):
        """CPU utilisation of `active_nodes` carrying a cluster sized at `sized_nodes`.
        Tablet count is fixed once tables exist, so it follows `sized_nodes`."""
        if active_nodes <= 0:
            return None
        used = self._raw_vcpus(sized_nodes) + self.conn_cpu_per_node * active_nodes
        return used / (active_nodes * self.vcpu_per_node)

    def _size_at(self, nodes):
        cpu_util = self._util(nodes, nodes)
        return {
            "nodes": nodes,
            "total_vcpu": nodes * self.vcpu_per_node,
            "effective_vcpus_used": round(self._raw_vcpus(nodes) + self.conn_cpu_per_node * nodes, 1),
            "cpu_util_pct": round(cpu_util * 100, 1),
            "pass": cpu_util <= self.target_cpu_util,
        }

    def _surviving(self, nodes, scenario):
        return nodes - 1 if scenario == "node" else nodes - nodes // self.zones

    def _effective_active(self, nodes, scenario):
        """Node count whose even share of the load equals the hottest surviving node's load.
        With pinned leaders the leader region is the hot spot: losing one of its nodes is like
        losing one node per region; losing a region moves leadership to an equal-sized region."""
        if not self.preferred_region:
            return self._surviving(nodes, scenario)
        if scenario == "node" and nodes // self.regions > 1:
            return nodes - self.regions
        return nodes            # region lost, or its only node: an equal-sized region takes over

    # ── Step 6: Storage ────────────────────────────────────────────────────
    # Data + compaction reserve scale with data size; WAL scales with write throughput ×
    # retention (longer when xCluster/CDC streams exist). Legacy --wal-overhead switches
    # WAL back to a percentage of data.

    def _resolve_storage(self):
        self.storage_multiplier = 1 + self.compaction_reserve + (self.wal_overhead if self.wal_pct_model else 0)
        self.effective_wal_retention = max(
            self.wal_retention_secs,
            self.cdc_wal_retention_secs if self.cdc_enabled else 0,
            self.xcluster_wal_retention_secs if self.xcluster_enabled else 0,
        )
        self.wal_bytes_per_s = (self.write_ops * self.rf * self.avg_row_bytes
                                * (1 + self.index_overhead))          # cluster-wide, all replicas

    def _wal_gb_per_node(self, nodes):
        if self.wal_pct_model:
            return 0.0
        return self.wal_bytes_per_s * self.effective_wal_retention / nodes / 1024 ** 3

    def _storage_per_node(self, nodes, data_ratio=1.0):
        return (self.with_replication * data_ratio / nodes * self.storage_multiplier
                + self._wal_gb_per_node(nodes))

    # ── Node count: CPU loop, then storage cap, then failure headroom ──────

    def _size_nodes(self):
        rf = self.rf
        first = self._raw_vcpus(rf) / self.target_cpu_util
        nodes = round_up_to_rf_multiple(math.ceil(first / self.vcpu_per_node), rf)
        self.iterations = []
        for _ in range(20):
            self.iterations.append(self._size_at(nodes))
            if self.iterations[-1]["pass"]:
                break
            nodes += rf

        # If storage/node exceeds the per-node cap, add nodes (in RF multiples) until it fits
        self.storage_nodes_added = 0
        while self._storage_per_node(nodes) > self.max_storage_per_node_gb:
            nodes += rf
            self.storage_nodes_added += rf

        # Disk limits: add nodes until per-node IOPS and throughput fit the volume
        self.disk_nodes_added = 0
        while not self._disk_fits(nodes):
            nodes += rf
            self.disk_nodes_added += rf

        # Failure-aware sizing: keep the chosen failure scenario within target
        self.failure_nodes_added = 0
        if self.size_for != "normal":
            while self._util(self._effective_active(nodes, self.size_for), nodes) > self.failure_cpu_target:
                nodes += rf
                self.failure_nodes_added += rf
        self.total_nodes = nodes

    # ── Step 7: Memory ─────────────────────────────────────────────────────

    def _serving_nodes(self, nodes):
        """Nodes holding tablet leaders: all of them, or only the preferred region's."""
        return nodes // self.regions if self.preferred_region else nodes

    def _leader_data_gb(self, nodes):
        """Compressed data a leader-holding node serves reads from (its tablet leaders)."""
        return self.after_compression / self._serving_nodes(nodes)

    def _hot_data_gb(self, nodes):
        """Data each node serves reads from: its leaders, widened towards all its replicas by the
        share of primary-cluster reads that are follower reads."""
        primary_reads = 1 - self.rr_frac
        fr_share = self.follower_read_frac / primary_reads if primary_reads else 0
        leader = self._leader_data_gb(nodes)
        return leader + fr_share * (self.with_replication / nodes - leader)

    def _memory(self, nodes):
        # 1:4 by default; read-heavy workloads whose hot data doesn't fit the cache a 1:4 node
        # provides get 1:8 (sysbench: 100% reads on 1:4 nodes, 8 GB leader data/node, no disk reads).
        cache_at_1_4 = self.vcpu_per_node * 4 * DEFAULTS["cache_fraction_of_ram"]
        read_heavy = self.write_pct < 50
        base_ratio = 8 if read_heavy and self._hot_data_gb(nodes) > cache_at_1_4 else 4
        base_gb    = self.vcpu_per_node * base_ratio
        conn_gb    = self.conn_per_node * self.mem_mb_per_conn / 1024
        # The base ratio is total node RAM, which already reserves PostgreSQL's share
        # (use_memory_defaults_optimized_for_ysql); only connection memory beyond it adds RAM.
        pg_budget  = base_gb * DEFAULTS["pg_memory_share"] if self.api == "ysql" else 0.0
        conn_extra = max(0.0, conn_gb - pg_budget)
        tablet_gb  = self._tablet_overhead(nodes)[2] / nodes / 1024
        cm_gb      = DEFAULTS["cm_mem_mb"] / 1024 if self.connection_manager else 0.0
        raw        = base_gb + conn_extra + cm_gb + tablet_gb
        tier       = round_up_to_ram_tier(raw, self.ram_tier_tolerance)
        return {
            "base_mem_ratio": f"1:{base_ratio}",
            "base_mem_gb": round(base_gb, 1),
            "conn_mem_gb": round(conn_gb, 1),
            "pg_budget_gb": round(pg_budget, 1),
            "conn_extra_gb": round(conn_extra, 1),
            "conn_mgr_mem_gb": round(cm_gb, 2),
            "tablet_mem_gb": round(tablet_gb, 2),
            "raw_mem_per_node_gb": round(raw, 1),
            "mem_per_node_gb": tier,
            "ram_tier_tolerance_pct": self.ram_tier_tolerance * 100,
            "ram_tier_tolerance_applied": raw > tier,
            "total_memory_gb": tier * nodes,
        }

    # ── Step 8: IOPS and disk throughput ───────────────────────────────────
    # Calibrated against TPC-C: WAL group commit and batched flushes keep IOPS well below one
    # I/O per write; disk throughput carries the LSM amplification instead.

    def _read_cache_miss(self, nodes):
        """Fraction of reads that go to disk: uniform access over the leader data, of which the
        cache (half the node's RAM) holds a share. A fixed --read-cache-miss overrides this."""
        if self.read_cache_miss_given is not None:
            return self.read_cache_miss_given
        cache_gb = self._memory(nodes)["mem_per_node_gb"] * DEFAULTS["cache_fraction_of_ram"]
        hot = self._hot_data_gb(nodes)
        return max(0.0, 1 - cache_gb / hot) if hot else 0.0

    def _io_raw(self, nodes):
        replica_writes = self.write_ops * self.rf / nodes
        miss = self._read_cache_miss(nodes)
        write_iops = replica_writes * self.iops_per_replica_write
        # Leader reads land on the leader-holding nodes; follower reads spread over every node
        leader_reads = self.read_ops * (1 - self.rr_frac - self.follower_read_frac)
        follower_reads = self.read_ops * self.follower_read_frac
        read_iops  = (leader_reads / self._serving_nodes(nodes) + follower_reads / nodes) * miss
        disk_mibps = (replica_writes * self.avg_row_bytes * (1 + self.index_overhead)
                      * self.disk_write_amp / 1_048_576)
        return write_iops, read_iops, disk_mibps, miss

    def _disk_fits(self, nodes):
        write_iops, read_iops, disk_mibps, _ = self._io_raw(nodes)
        return write_iops + read_iops <= self.disk_iops_limit and disk_mibps <= self.disk_mibps_limit

    def _io(self, nodes):
        write_iops, read_iops, disk_mibps, miss = self._io_raw(nodes)
        return {
            "write_iops_per_node": round(write_iops, 0),
            "read_iops_per_node": round(read_iops, 0),
            "total_iops_per_node": round(write_iops + read_iops, 0),
            "disk_mibps_per_node": round(disk_mibps, 1),
            "read_cache_miss_pct": round(miss * 100, 1),
            "read_cache_miss_source": ("provided" if self.read_cache_miss_given is not None
                                       else "cache vs leader data"),
            "leader_data_gb_per_node": round(self._leader_data_gb(nodes), 1),
            "hot_data_gb_per_node": round(self._hot_data_gb(nodes), 1),
            "disk_iops_limit": self.disk_iops_limit,
            "disk_mibps_limit": self.disk_mibps_limit,
            "disk_nodes_added": self.disk_nodes_added,
            "note": f"Write IOPS = {self.iops_per_replica_write} per replicated write; read IOPS = "
                    f"{miss*100:.0f}% cache miss. Limits: {self.disk_iops_limit:,.0f} IOPS, "
                    f"{self.disk_mibps_limit:,.0f} MiB/s.",
        }

    # ── Step 9: Network ────────────────────────────────────────────────────

    def _network(self, nodes):
        row = self.avg_row_bytes
        primary_reads = self.read_ops * (1 - self.rr_frac)
        write = (self.write_ops / nodes) * self.rf * row * self.net_write_factor / 1_048_576
        read  = (primary_reads / nodes) * row * self.net_read_factor / 1_048_576
        total = write + read
        # Multi-AZ / multi-region: with one zone (region) per replica, about (zones − 1)/zones of the
        # replication and leader-read traffic leaves the zone. Follower reads are served locally.
        cross_frac = (self.zones - 1) / self.zones
        leader_read_share = ((1 - self.rr_frac - self.follower_read_frac) / (1 - self.rr_frac)
                             if self.rr_frac < 1 else 0)
        cross = (write + read * leader_read_share) * cross_frac
        # Per-node traffic counts bytes sent and received, so each transfer appears on two nodes
        gb_month = cross * nodes / 2 * 86400 * 30.44 / 1024
        price = self.cross_region_cost_per_gb if self.regions > 1 else self.cross_az_cost_per_gb
        return {
            "cross_scope": "region" if self.regions > 1 else "AZ",
            "write_net_mbps_per_node": round(write, 2),
            "read_net_mbps_per_node": round(read, 2),
            "total_net_mbps_per_node": round(total, 2),
            "zones": self.zones,
            "cross_az_fraction": round(cross_frac, 3),
            "cross_az_mbps_per_node": round(cross, 2),
            "cross_az_gb_per_month": round(gb_month, 0),
            "cross_az_cost_per_month": round(gb_month * price, 0),
            "cross_cost_per_gb": price,
            "note": "Keep below 40% of NIC capacity (e.g. 500 MB/s on 10 GbE, 2,500 MB/s on 50 GbE).",
        }

    # ── Step 10: Storage (with growth projection) ──────────────────────────
    # YCQL TTL: expired rows are reclaimed, so data plateaus at roughly (ingest/day × TTL
    # days). Assumes every write is a new row (upper bound); never below today's size.

    def _storage(self, nodes):
        growth = self.growth_rate_pct / 100
        ttl_info, data_cap = None, None
        if self.ttl_days is not None:
            ingest = self.write_ops * self.avg_row_bytes * 86400 / 1024 ** 3
            steady = ingest * self.ttl_days
            data_cap = max(steady, self.table_size_gb)
            ttl_info = {
                "ttl_days": self.ttl_days,
                "ingest_gb_per_day": round(ingest, 1),
                "ttl_steady_state_gb": round(steady, 1),
                "data_cap_gb": round(data_cap, 1),
            }

        def projected(years, capped=True):
            # WAL follows write throughput, not data size, so it stays constant here
            data_gb = self.table_size_gb * (1 + growth) ** years
            if capped and data_cap is not None:
                data_gb = min(data_gb, data_cap)
            return self._storage_per_node(nodes, data_gb / self.table_size_gb if self.table_size_gb else 0.0)

        base = self.with_replication / nodes
        per_node = self._storage_per_node(nodes)
        wal = self._wal_gb_per_node(nodes) if not self.wal_pct_model else base * self.wal_overhead
        yr1, yr2 = projected(1), projected(2)
        if ttl_info:
            ttl_info["growth_capped"] = yr2 < projected(2, capped=False)
        storage = {
            "index_size_gb": round(self.index_size_gb, 1),
            "total_raw_gb": round(self.total_raw_gb, 1),
            "after_compression_gb": round(self.after_compression, 1),
            "with_replication_gb": round(self.with_replication, 1),
            "base_storage_per_node_gb": round(base, 1),
            "storage_multiplier": round(self.storage_multiplier, 2),
            "compaction_reserve_gb_per_node": round(base * self.compaction_reserve, 1),
            "wal_write_mb_per_s_per_node": round(self.wal_bytes_per_s / nodes / 1024 ** 2, 2),
            "wal_gb_per_node": round(wal, 1),
            "storage_per_node_gb": round(per_node, 1),
            "total_storage_gb": round(per_node * nodes, 1),
            "storage_per_node_1yr_gb": round(yr1, 1),
            "storage_per_node_2yr_gb": round(yr2, 1),
            "total_storage_2yr_gb": round(yr2 * nodes, 1),
            "max_storage_per_node_gb": self.max_storage_per_node_gb,
            "storage_cap_triggered": self.storage_nodes_added > 0,
            "storage_nodes_added": self.storage_nodes_added,
        }
        return storage, ttl_info

    # ── Step 11: Tablet summary ────────────────────────────────────────────

    def _tablets(self, nodes):
        total, vcpu_overhead, mem_mb = self._tablet_overhead(nodes)
        schema, data = self._tablet_counts(nodes)
        has_objects = self.num_objects is not None
        return {
            "num_objects": self.num_objects,
            "tablets_per_table": self._tablets_per_table(nodes, self.vcpu_per_node) if has_objects else None,
            "tablets_per_table_source": (
                None if not has_objects
                else "provided" if self.tablets_per_table is not None
                else "1 per table (no pre-split)" if self.api == "ysql"
                else "1 per tserver (hash-sharded)" if self.vcpu_per_node > 4
                else "fixed per cluster (≤4 cores)"
            ),
            "schema_tablets": schema,
            "data_tablets": data,
            "tablet_basis": "data size (auto-split)" if data >= schema else "schema",
            "total_tablets": total,
            "tablet_vcpu_overhead_total": round(vcpu_overhead, 2),
            "tablet_vcpu_overhead_per_node": round(vcpu_overhead / nodes, 3),
            "tablet_mem_overhead_total_mb": round(mem_mb, 1),
            "tablet_mem_overhead_per_node_gb": round(mem_mb / nodes / 1024, 2),
            "low_tablet_count_warning": nodes > total,
        }

    # ── Step 12: Failure scenarios ─────────────────────────────────────────
    # Zones are aligned to RF: one zone per RF replica, nodes spread evenly.

    def _failure(self, nodes):
        def scenario(name):
            surviving = self._surviving(nodes, name)
            util = self._util(self._effective_active(nodes, name), nodes)
            pct = round(util * 100, 1) if util is not None else None
            return {
                "surviving_nodes": surviving,
                "cpu_util_pct": pct,
                "exceeds_target": pct is not None and pct > self.failure_cpu_target * 100,
            }
        multi_az = self.zones > 1
        return {
            "fault_domain": "region" if self.regions > 1 else "zone",
            "num_zones": self.zones,
            "nodes_per_zone": nodes // self.zones,
            "failure_cpu_target_pct": self.failure_cpu_target * 100,
            "sized_for": self.size_for,
            "failure_nodes_added": self.failure_nodes_added,
            "node_failure": scenario("node"),
            "zone_failure": scenario("zone") if multi_az else None,
        }

    # ── Concurrency check (Little's law): in-flight = QPS × latency ─────────

    def _added_latency_ms(self, pipelining=None):
        """Extra latency per statement from cross-region round trips.

        Reads to a leader in another region (balanced leaders, clients in every region) cross
        regions unless served as follower/replica reads. For writes:
          without pipelining: every write statement waits for a remote quorum, and each
                              distributed commit adds one more round trip;
          with pipelining:    writes are acknowledged by the leader and replicated in the
                              background, so a distributed write transaction pays ~2 round trips
                              at COMMIT (drain + status-tablet commit) and a fast-path write 1."""
        rtt = self.region_rtt_ms
        if not rtt or not self.qps:
            return 0.0
        pipelining = self.write_pipelining if pipelining is None else pipelining
        w, r = self.write_pct / 100, self.read_pct / 100
        remote_leader = 0 if self.preferred_region else (self.regions - 1) / self.regions
        remote_reads = r * (1 - self.follower_read_frac - self.rr_frac) * remote_leader
        txn = self._txn(self.qps)
        if pipelining:
            per_write_txn = txn["dist"] * 2 + (1 - txn["dist"]) * 1
            writes = txn["write_txns"] * per_write_txn / self.qps
        else:
            writes = w + txn["write_txns"] * txn["dist"] / self.qps
        return rtt * (writes + remote_reads)

    def _concurrency(self, nodes):
        if self.latency_ms is None:
            return None
        added = self._added_latency_ms()
        latency = self.latency_ms + added
        in_flight = self.qps * latency / 1000
        per_node = in_flight / self._serving_nodes(nodes)       # clients use the leader region if pinned
        return {
            "latency_ms": self.latency_ms,
            "added_region_latency_ms": round(added, 2),
            "effective_latency_ms": round(latency, 2),
            "in_flight_total": round(in_flight, 1),
            "in_flight_per_node": round(per_node, 1),
            "backends_per_node": self.conn_per_node if self.api == "ysql" else None,
            "connection_bound": self.api == "ysql" and per_node > self.conn_per_node,
        }

    # ── Read-replica cluster ───────────────────────────────────────────────
    # Serves read_replica_read_pct of the reads (timeline-consistent, like follower reads) and
    # applies every write once per copy, at the follower apply cost. Not part of the RF quorum.

    def _read_replica(self):
        if not self.rr_frac:
            return None
        v, copies = self.rr_vcpu, self.rr_rf
        m = self.workload_multiplier * self.rpc_multiplier * self.arch_factor
        reads = self.read_ops * self.rr_frac
        follower_apply = self.cpu_ms_write * self.rf_write_scale * (1 - self._leader_write_portion()) / (self.rf - 1)
        cores = (reads * self.cpu_ms_read + self.write_ops * follower_apply * copies) * m / 1000
        conn_cpu = self._conn_cpu_per_node(v)
        nodes = round_up_to_rf_multiple(math.ceil(cores / self.target_cpu_util / v), copies)
        while (cores + conn_cpu * nodes) / (nodes * v) > self.target_cpu_util:
            nodes += copies
        util = (cores + conn_cpu * nodes) / (nodes * v)
        data_gb = self.after_compression * copies / nodes
        ratio = 8 if data_gb > v * 4 * DEFAULTS["cache_fraction_of_ram"] else 4
        mem = round_up_to_ram_tier(v * ratio, self.ram_tier_tolerance)
        miss = max(0.0, 1 - mem * DEFAULTS["cache_fraction_of_ram"] / data_gb) if data_gb else 0.0
        writes_per_node = self.write_ops * copies / nodes
        storage = data_gb * (1 + self.compaction_reserve) + (
            self.write_ops * copies * self.avg_row_bytes * (1 + self.index_overhead)
            * self.wal_retention_secs / nodes / 1024 ** 3)
        ingest = self.write_ops * copies * self.avg_row_bytes * self.net_write_factor / 1_048_576
        return {
            "read_pct": round(self.rr_frac * 100, 1),
            "copies": copies,
            "reads_per_s": round(reads, 1),
            "nodes": nodes,
            "vcpu_per_node": v,
            "total_vcpu": nodes * v,
            "workload_cores": round(cores, 2),
            "cpu_utilization_pct": round(util * 100, 1),
            "mem_per_node_gb": mem,
            "storage_per_node_gb": round(storage, 1),
            "iops_per_node": round(writes_per_node * self.iops_per_replica_write
                                   + reads / nodes * miss, 0),
            "replication_ingest_mbps": round(ingest, 2),
        }

    # ── Assemble ───────────────────────────────────────────────────────────

    def run(self):
        self._resolve_cpu_model()
        self._workload_cpu()
        self._resolve_storage()
        self._size_nodes()
        nodes = self.total_nodes
        final = self._size_at(nodes)
        storage, ttl_info = self._storage(nodes)
        return {
            "inputs": {
                "api": self.api,
                "qps": self.qps,
                "write_pct": self.write_pct,
                "read_pct": self.read_pct,
                "avg_exec_ms": self.avg_exec_ms,
                "exec_time_estimated": self.exec_time_estimated,
                "exec_time_label": self.exec_time_label,
                "exec_time_calibrated": self.calibrated,
                "cpu_model": self.cpu_model,
                "workload": self.workload,
                "workload_source": self.workload_source,
                "cpu_arch": self.cpu_arch,
                "vcpu_per_node": self.vcpu_per_node,
                "rf": self.rf,
                "table_size_gb": self.table_size_gb,
                "avg_row_bytes": self.avg_row_bytes,
                "avg_row_bytes_source": self.avg_row_bytes_source,
                "growth_rate_pct": self.growth_rate_pct,
                "growth_rate_source": self.growth_rate_source,
                "cdc_enabled": self.cdc_enabled,
                "xcluster_enabled": self.xcluster_enabled,
                "num_objects": self.num_objects,
                "ttl_days": self.ttl_days,
                "size_for": self.size_for,
                "connection_manager": self.connection_manager,
                "zones": self.zones,
                "tps": self.tps_given,
                "statements_per_txn": self.statements_per_txn,
                "distributed_txn_pct": self.distributed_txn_pct,
                "follower_read_pct": round(self.follower_read_frac * 100, 1),
                "read_replica_read_pct": round(self.rr_frac * 100, 1),
                "regions": self.regions,
                "region_rtt_ms": self.region_rtt_ms,
                "preferred_region": self.preferred_region,
                "write_pipelining": self.write_pipelining,
            },
            "parameters": {
                "rpc_overhead": self.rpc_overhead,
                "rpc_multiplier": self.rpc_multiplier,
                "index_overhead_pct": self.index_overhead * 100,
                "compression_pct": self.compression_ratio * 100,
                "wal_model": "percentage of data" if self.wal_pct_model else "write rate × retention",
                "wal_overhead_pct": self.wal_overhead * 100 if self.wal_pct_model else None,
                "wal_retention_secs": None if self.wal_pct_model else self.effective_wal_retention,
                "compaction_reserve_pct": self.compaction_reserve * 100,
                "target_cpu_util_pct": self.target_cpu_util * 100,
                "failure_cpu_target_pct": self.failure_cpu_target * 100,
                "conn_per_vcpu": self.conn_per_vcpu,
                "mem_mb_per_conn": self.mem_mb_per_conn,
                "conn_cpu_overhead": self.conn_cpu_overhead,
                "cm_client_ratio": self.cm_client_ratio if self.connection_manager else None,
                "cpu_ms_per_read": round(self.cpu_ms_read, 4),
                "cpu_ms_per_write": round(self.cpu_ms_write, 4),
                "cpu_cost_scale": self.cpu_cost_scale,
                "cpu_cost_source": ("provided" if self.cpu_ms_per_read_given is not None
                                    or self.cpu_ms_per_write_given is not None
                                    else CPU_COST_PROFILES[self.api]["source"]),
                "rf_write_scale": round(self.rf_write_scale, 3),
                "workload_multiplier": self.workload_multiplier,
                "workload_label": self.workload_label,
                "arch_factor": self.arch_factor,
                "iops_per_replica_write": self.iops_per_replica_write,
                "disk_write_amp": self.disk_write_amp,
                "net_write_factor": self.net_write_factor,
                "net_read_factor": self.net_read_factor,
                "cross_az_cost_per_gb": self.cross_az_cost_per_gb,
                "cdc_overhead_pct": self.cdc_overhead * 100 if self.cdc_enabled else None,
                "xcluster_overhead_pct": self.xcluster_overhead * 100 if self.xcluster_enabled else None,
                "feature_overhead_factor": round(self.feature_overhead_factor, 4),
            },
            "calibration": self.calibration,
            "workload": {
                "write_ops_per_s": round(self.write_ops, 1),
                "read_ops_per_s": round(self.read_ops, 1),
                "eff_write_ops_per_s": round(self.eff_write_ops, 1),
                "eff_read_ops_per_s": round(self.eff_read_ops, 1),
                "total_eff_ops_per_s": round(self.total_eff_ops, 1),
                "cpu_seconds_needed": round(self.cpu_seconds_needed, 2),
                "raw_vcpus_workload_incl_features_and_tablets": round(self._raw_vcpus(nodes), 1),
            },
            "sizing_iterations": self.iterations,
            "cluster": {
                "total_nodes": nodes,
                "vcpu_per_node": self.vcpu_per_node,
                "total_vcpu": final["total_vcpu"],
                "cpu_utilization_pct": final["cpu_util_pct"],
                "cpu_within_target": final["pass"],
                "conn_per_node": self.conn_per_node,
                "client_conn_per_node": (self.conn_per_node * self.cm_client_ratio if self.connection_manager
                                         else self.conn_per_node),
                "conn_cpu_per_node": round(self.conn_cpu_per_node, 2),
                "db_ops_per_node_per_s": round(self.total_eff_ops / nodes, 1),
            },
            "storage": storage,
            "memory": self._memory(nodes),
            "iops": self._io(nodes),
            "network": self._network(nodes),
            "failure_scenarios": self._failure(nodes),
            "tablets": self._tablets(nodes),
            "ttl": ttl_info,
            "concurrency": self._concurrency(nodes),
            "transactions": self._txn_summary(),
            "read_replica": self._read_replica(),
            "multi_region": self._multi_region(nodes),
        }

    def _txn_summary(self):
        t = self._txn(self.qps)
        m = self.workload_multiplier * self.rpc_multiplier * self.feature_overhead_factor * self.arch_factor
        return {
            "tps": round(t["tps"], 1),
            "statements_per_txn": self.statements_per_txn,
            "write_txn_pct": round(t["p_write"] * 100, 1),
            "distributed_txn_pct": self.distributed_txn_pct,
            "overhead_cores": round(t["overhead_ms"] * m / 1000, 2),
            "overhead_basis": ("added: commit + intents per distributed write transaction" if t["applies"]
                               else f"included in the {self.workload} profile"
                               if self.cpu_model == "per-op" else "not modeled (legacy CPU model)"),
        }

    def _multi_region(self, nodes):
        if self.regions == 1:
            return None
        per_region = nodes // self.regions
        info = {
            "regions": self.regions,
            "nodes_per_region": per_region,
            "region_rtt_ms": self.region_rtt_ms,
            "preferred_region": self.preferred_region,
            "added_latency_per_statement_ms": round(self._added_latency_ms(), 2),
            "write_pipelining": self.write_pipelining,
            "added_latency_with_pipelining_ms": (round(self._added_latency_ms(pipelining=True), 2)
                                                 if self.api == "ysql" else None),
            "added_latency_without_pipelining_ms": round(self._added_latency_ms(pipelining=False), 2),
        }
        if self.preferred_region and self.cpu_model == "per-op" and not self.calibrated:
            leader, follower = self._region_cores(self.qps)
            tablets = self._tablet_overhead(nodes)[1] / self.regions
            cap = per_region * self.vcpu_per_node
            conn = self.conn_cpu_per_node * per_region
            info["leader_region_cpu_pct"] = round((leader + tablets + conn) / cap * 100, 1)
            info["follower_region_cpu_pct"] = round((follower + tablets + conn) / cap * 100, 1)
        return info


def compare_vcpu_tiers(tiers, **kwargs):
    """Run `calculate` for each vCPU/node tier and pick a recommendation.

    Eligible = tiers that meet the CPU target and keep a zone failure within the
    failure target. Among eligible tiers within COMPARE_VCPU_BAND of the lowest
    total vCPU, prefer fewer (larger) nodes, then lower zone-failure CPU.
    Falls back to tiers meeting only the CPU target if none survive a zone loss."""
    results = [calculate(vcpu_per_node=v, **kwargs) for v in tiers]
    rows = []
    for r in results:
        c, fs = r["cluster"], r["failure_scenarios"]
        rows.append({
            "vcpu_per_node": c["vcpu_per_node"],
            "total_nodes": c["total_nodes"],
            "total_vcpu": c["total_vcpu"],
            "mem_per_node_gb": r["memory"]["mem_per_node_gb"],
            "total_memory_gb": r["memory"]["total_memory_gb"],
            "storage_per_node_gb": r["storage"]["storage_per_node_gb"],
            "cpu_utilization_pct": c["cpu_utilization_pct"],
            "cpu_within_target": c["cpu_within_target"],
            "node_failure_cpu_pct": fs["node_failure"]["cpu_util_pct"],
            "zone_failure_cpu_pct": fs["zone_failure"]["cpu_util_pct"] if fs["zone_failure"] else None,
            "node_failure_ok": not fs["node_failure"]["exceeds_target"],
            "zone_failure_ok": not (fs["zone_failure"] and fs["zone_failure"]["exceeds_target"]),
        })

    pool = [x for x in rows if x["cpu_within_target"] and x["zone_failure_ok"]]
    basis = "meets the CPU target and survives a zone failure"
    if not pool:
        pool = [x for x in rows if x["cpu_within_target"]] or rows
        basis = "meets the CPU target (no tier survives a zone failure within target)"
    floor = min(x["total_vcpu"] for x in pool)
    band = [x for x in pool if x["total_vcpu"] <= floor * (1 + COMPARE_VCPU_BAND)]
    best = min(band, key=lambda x: (x["total_nodes"], x["zone_failure_cpu_pct"] or 0))
    basis += (f"; fewest nodes within {COMPARE_VCPU_BAND:.0%} of the lowest total vCPU ({floor})")
    for x in rows:
        x["recommended"] = x is best
    return {
        "comparison": rows,
        "recommended_vcpu_per_node": best["vcpu_per_node"],
        "recommendation_basis": basis,
        "results": results,
    }


def format_report(r):
    c  = r["cluster"]
    s  = r["storage"]
    m  = r["memory"]
    w  = r["workload"]
    i  = r["inputs"]
    io = r["iops"]
    nw = r["network"]
    p  = r["parameters"]
    is_ycql = i["api"] == "ycql"
    has_conn = c["conn_per_node"] > 0

    exec_label = ""
    if i.get("exec_time_estimated"):
        tool = "<tserver>:12000/statements" if is_ycql else "pg_stat_statements"
        exec_label = f"  ⚠️  ESTIMATED ({i['exec_time_label']}) — measure with {tool}"

    lines = [
        "═" * 56,
        f"  YugabyteDB Cluster Sizing Recommendation ({i['api'].upper()})",
        "═" * 56,
        "",
        "INPUT SUMMARY",
        "─" * 44,
        f"  API:                         {i['api'].upper()}",
        f"  QPS:                         {i['qps']:,}",
        f"  Write / Read:                {i['write_pct']}% / {i['read_pct']}%",
    ]
    per_op = i["cpu_model"] == "per-op"
    if not per_op:
        lines.append(f"  Avg execution time:          {i['avg_exec_ms']} ms  (legacy latency CPU model)")
    elif i["avg_exec_ms"] is not None:
        lines.append(f"  Avg latency:                 {i['avg_exec_ms']} ms  (used for the concurrency check)")
    lines += [
        f"  Workload profile:            {i['workload']} — {p['workload_label']}  ({i['workload_source']})",
        f"  CPU architecture:            {i['cpu_arch']}" + (f"  (×{p['arch_factor']:g} CPU)" if p["arch_factor"] != 1 else ""),
    ]
    if exec_label:
        lines.append(f"  {exec_label}")
    lines += [
        f"  Replication Factor:          {i['rf']}",
        f"  Topology:                    " + (
            f"multi-region, {i['regions']} regions (one replica each, {i['region_rtt_ms']:g} ms RTT"
            + (", leaders pinned to one region)" if i["preferred_region"] else ", balanced leaders)")
            if i["regions"] > 1 else f"multi-AZ, {i['zones']} zones (one per replica)" if i["zones"] > 1
            else "single AZ"),
        f"  Table size:                  {i['table_size_gb']:,} GB",
        f"  Avg row size:                {i['avg_row_bytes']} bytes  ({i['avg_row_bytes_source']})",
        f"  Data growth rate:            {i['growth_rate_pct']}%/yr  ({i['growth_rate_source']})",
    ]
    ttl = r.get("ttl")
    if ttl:
        lines.append(f"  TTL:                         {ttl['ttl_days']:g} days")
    tx = r["transactions"]
    if i["tps"] is not None or i["statements_per_txn"] > 1 or i["distributed_txn_pct"]:
        lines.append(f"  Transactions:                {tx['tps']:,.0f} TPS × {tx['statements_per_txn']:g} statements "
                     f"({tx['write_txn_pct']:g}% with writes, {tx['distributed_txn_pct']:g}% of those distributed)")
    if i["follower_read_pct"] or i["read_replica_read_pct"]:
        lines.append(f"  Read offload:                {i['follower_read_pct']:g}% follower reads, "
                     f"{i['read_replica_read_pct']:g}% read replicas")

    t = r["tablets"]
    if t["num_objects"] is not None:
        lines.append(
            f"  Objects (tables+indexes):    {t['num_objects']:,}  → {t['total_tablets']:,} tablets @ RF={i['rf']}"
        )

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
        f"  Effective Write Ops/s:       {w['eff_write_ops_per_s']:,}  (×RF×{p['rpc_multiplier']:g} RPC overhead)",
        f"  Effective Read Ops/s:        {w['eff_read_ops_per_s']:,}  (×{p['rpc_multiplier']:g} RPC overhead)",
        f"  Total Effective Ops/s:       {w['total_eff_ops_per_s']:,}",
    ]
    if r.get("calibration"):
        lines.append("  CPU model:                   calibrated from observed cluster (see below)")
    elif per_op:
        lines += [
            f"  CPU model:                   per-op — {p['cpu_ms_per_read']} ms/read, {p['cpu_ms_per_write']} ms/write"
            f" (×{p['rf_write_scale']:g} for RF={i['rf']})",
            f"                               × {p['workload_multiplier']:g} workload × {p['rpc_multiplier']:g} RPC"
            + (f" × {p['feature_overhead_factor']:g} features" if p["feature_overhead_factor"] != 1 else "")
            + (f" × {p['arch_factor']:g} arch" if p["arch_factor"] != 1 else ""),
            f"  CPU cost source:             {p['cpu_cost_source']}"
            + (f"  (× {p['cpu_cost_scale']:g} hardware scale)" if p["cpu_cost_scale"] != 1 else ""),
        ]
    else:
        lines.append("  CPU model:                   legacy latency (effective ops × exec time)")
    if i["tps"] is not None or i["statements_per_txn"] > 1 or i["distributed_txn_pct"]:
        lines.append(f"  Distributed-txn CPU:         {tx['overhead_cores']} cores  ({tx['overhead_basis']})")
    lines += [
        f"  vCPUs needed (workload+tablets): {w['raw_vcpus_workload_incl_features_and_tablets']}",
        "",
    ]
    cal = r.get("calibration")
    if cal:
        lines += [
            "CPU CALIBRATION (from observed cluster)",
            "─" * 44,
            f"  Observed:                    {cal['observed_qps']:,.0f} QPS @ {cal['observed_cpu_pct']:g}% CPU on "
            f"{cal['observed_nodes']} × {cal['observed_vcpu_per_node']} vCPU",
            f"  Observed cluster shape:      nodes {cal['observed_nodes_source']}, vCPU {cal['observed_vcpu_source']}",
            f"  Busy cores:                  {cal['observed_busy_cores']}  − conn {cal['observed_conn_cores']}"
            f"  − tablets {cal['observed_tablet_cores']}  = workload {cal['observed_workload_cores']}",
            f"  CPU cost:                    {cal['cpu_ms_per_op']} ms per client op  "
            f"({cal['factor_vs_model']}× the {i['workload']} profile)",
            "  Assumes the observed cluster runs the same read/write mix, RF and features.",
            "",
        ]
    lines += [
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
    ]
    conn_kind = "CQL" if is_ycql else "PG"
    if has_conn and i.get("connection_manager"):
        lines += [
            f"  PG backends / node:          {c['conn_per_node']}  ({p['conn_per_vcpu']} × {i['vcpu_per_node']} vCPU, via Connection Manager)",
            f"  Client connections / node:   ~{c['client_conn_per_node']:,}  ({p['cm_client_ratio']} per backend)",
            f"  Connection CPU / node:       {c['conn_cpu_per_node']} cores",
        ]
    elif has_conn:
        lines += [
            f"  {conn_kind} Connections / node:       {c['conn_per_node']}  ({p['conn_per_vcpu']} × {i['vcpu_per_node']} vCPU)",
            f"  Connection CPU / node:       {c['conn_cpu_per_node']} cores",
        ]
    else:
        lines.append("  CQL Connections:             multiplexed by drivers — no per-connection overhead modeled")
    cc = r.get("concurrency")
    if cc:
        bound = ""
        if cc["backends_per_node"] is not None:
            bound = (f"  ⚠️  exceeds {cc['backends_per_node']} backends — connection-bound" if cc["connection_bound"]
                     else f"  (of {cc['backends_per_node']} backends)")
        lat = (f"{cc['latency_ms']:g} + {cc['added_region_latency_ms']:g} ms cross-region"
               if cc["added_region_latency_ms"] else f"{cc['latency_ms']:g} ms")
        lines.append(f"  In-flight requests / node:   {cc['in_flight_per_node']:,}  (QPS × {lat} latency){bound}")
    cpu_badge = "✅" if c["cpu_within_target"] else "⚠️  EXCEEDS TARGET"
    lines += [
        f"  CPU Utilization:             {c['cpu_utilization_pct']}%  {cpu_badge}  (target ≤{p['target_cpu_util_pct']:.0f}%)",
        f"  Est. IOPS / node:            {int(io['total_iops_per_node']):,}",
        f"    → Write IOPS:              {int(io['write_iops_per_node']):,}  ({p['iops_per_replica_write']:g} per replicated write)",
        f"    → Read IOPS (cold):        {int(io['read_iops_per_node']):,}  ({io['read_cache_miss_pct']:g}% cache miss: "
        f"{io['leader_data_gb_per_node']:,} GB leader data/node vs ~{m['mem_per_node_gb'] // 2} GB cache)",
        f"  Est. disk throughput / node: {io['disk_mibps_per_node']:,} MiB/s  (gp3 baseline 125 MiB/s, up to 1,000)",
        f"  Disk limits / node:          {io['disk_iops_limit']:,.0f} IOPS, {io['disk_mibps_limit']:,.0f} MiB/s"
        + (f"  (+{io['disk_nodes_added']} node(s) added to fit)" if io["disk_nodes_added"] else "  ✅ fits"),
        f"  Est. Network / node:         {nw['total_net_mbps_per_node']:.1f} MiB/s",
        f"    → Write (Raft):            {nw['write_net_mbps_per_node']:.1f} MiB/s",
        f"    → Read:                    {nw['read_net_mbps_per_node']:.1f} MiB/s",
    ]
    if nw["zones"] > 1:
        lines.append(f"  Cross-{nw['cross_scope']} traffic / node:".ljust(31) + f"{nw['cross_az_mbps_per_node']:.1f} MiB/s  "
                     f"(≈{nw['cross_az_gb_per_month']:,.0f} GB/month cluster ≈ ${nw['cross_az_cost_per_month']:,.0f}/month at "
                     f"${nw['cross_cost_per_gb']:g}/GB)")
    lines += [
        "",
        "STORAGE BREAKDOWN",
        "─" * 44,
        f"  Raw data + indexes:          {s['total_raw_gb']} GB",
        f"  After LZ4 compression:       {s['after_compression_gb']} GB",
        f"  After RF={i['rf']} replication:       {s['with_replication_gb']} GB",
        f"  Per node (data):             {s['base_storage_per_node_gb']} GB",
        f"  + Compaction reserve ({p['compaction_reserve_pct']:.0f}%):  {s['compaction_reserve_gb_per_node']} GB",
        "  " + (f"+ WAL ({s['wal_write_mb_per_s_per_node']} MB/s × {p['wal_retention_secs'] / 3600:g} h):"
               if p["wal_retention_secs"] is not None
               else f"+ WAL ({p['wal_overhead_pct']:.0f}% of data):").ljust(29) + f"{s['wal_gb_per_node']} GB",
        f"  Per node (total):            {s['storage_per_node_gb']} GB",
        "",
        "MEMORY BREAKDOWN",
        "─" * 44,
        f"  Base ratio:                  {m['base_mem_ratio']} vCPU:RAM = {m['base_mem_gb']} GB",
    ]
    if has_conn:
        lines.append(f"  Connection memory:           {c['conn_per_node']} conns × {p['mem_mb_per_conn']:.0f} MB = {m['conn_mem_gb']} GB"
                     + (f"  (within {m['pg_budget_gb']} GB PostgreSQL share)" if not m["conn_extra_gb"]
                        else f"  (+{m['conn_extra_gb']} GB beyond the {m['pg_budget_gb']} GB PostgreSQL share)"))
    if m["conn_mgr_mem_gb"]:
        lines.append(f"  Conn Manager (odyssey):      {m['conn_mgr_mem_gb']} GB")
    if t["total_tablets"]:
        lines.append(f"  Tablet maintenance memory:   {m['tablet_mem_gb']} GB  ({t['total_tablets']:,} tablets / {c['total_nodes']} nodes)")
    lines += [
        f"  Raw total / node:            {m['raw_mem_per_node_gb']} GB",
        f"  Recommended / node:          {m['mem_per_node_gb']} GB  (standard tier"
        + (f"; within {m['ram_tier_tolerance_pct']:g}% tolerance)" if m["ram_tier_tolerance_applied"] else ")"),
        "",
    ]

    if t:
        lines += ["TABLET MAINTENANCE OVERHEAD", "─" * 44]
        if t["num_objects"] is not None:
            lines.append(f"  Schema tablets:              {t['schema_tablets']:,}  ({t['num_objects']:,} objects × "
                         f"{t['tablets_per_table']} tablet/table [{t['tablets_per_table_source']}])")
        lines += [
            f"  Data-driven tablets:         {t['data_tablets']:,}  (auto-split of {s['after_compression_gb']:,} GB compressed)",
            f"  Total tablets (cluster):     {t['total_tablets']:,}  (max of the above, by {t['tablet_basis']}, × RF={i['rf']})",
            f"  vCPU overhead (cluster):     {t['tablet_vcpu_overhead_total']}  (≈{t['tablet_vcpu_overhead_per_node']}/node)",
            f"  RAM overhead (cluster):      {t['tablet_mem_overhead_total_mb']:,.0f} MB  (≈{m['tablet_mem_gb']} GB/node)",
            "",
        ]
        if t.get("low_tablet_count_warning"):
            lines += [
                f"⚠️  LOW TABLET COUNT: {c['total_nodes']} nodes but only {t['total_tablets']:,} tablets — "
                f"some nodes may host no leader tablets for these objects, underusing cluster capacity.",
                "",
            ]

    if io["disk_nodes_added"]:
        lines += [
            f"⚠️  DISK LIMIT: per-node IOPS/throughput exceeded {io['disk_iops_limit']:,.0f} IOPS / "
            f"{io['disk_mibps_limit']:,.0f} MiB/s.",
            f"    {io['disk_nodes_added']} extra node(s) added. For fewer nodes, use NVMe instances or io2 and pass "
            "--disk-iops/--disk-mibps.",
            "",
        ]
    if s.get("storage_cap_triggered"):
        lines += [
            f"⚠️  STORAGE CAP TRIGGERED: Storage/node exceeded {s['max_storage_per_node_gb']:,} GB "
            f"({s['max_storage_per_node_gb']//1024} TB limit).",
            f"    {s['storage_nodes_added']} extra node(s) added to bring storage/node within cap.",
            "",
        ]

    if ttl:
        lines += [
            "TTL STEADY STATE (YCQL)",
            "─" * 44,
            f"  Ingest (writes × row size):  {ttl['ingest_gb_per_day']:,} GB/day raw",
            f"  Steady state @ {ttl['ttl_days']:g} days TTL:  {ttl['ttl_steady_state_gb']:,} GB raw  (cap used: {ttl['data_cap_gb']:,} GB)",
            f"  Growth projection capped:    {'yes' if ttl['growth_capped'] else 'no (cap not reached within 2 yr)'}",
            "  Assumes every write is a new row (upper bound); updates/overwrites lower it.",
            "  Expired data is reclaimed by compaction — prefer table-level default_time_to_live.",
            "",
        ]

    if per_op and not r.get("calibration") and i["workload_source"] != "provided":
        lines += [
            f"⚠️  Workload profile auto-selected ({i['workload']}) from the read/write mix. Pass --workload, or",
            "   calibrate with --observed-qps/--observed-cpu-pct from a running cluster for best accuracy.",
            "",
        ]
    if i.get("exec_time_estimated"):
        if is_ycql:
            lines.append("⚠️  IMPORTANT: Execution time was estimated. Measure real YCQL statement latencies at:")
            lines.append("   http://<tserver>:12000/statements   (or ycql_stat_statements via the yb_ycql_utils YSQL extension)")
        else:
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

    ftarget = fs["failure_cpu_target_pct"]
    dom = fs.get("fault_domain", "zone")
    layout = (f"{dom} layout: {fs['num_zones']} {dom}s × {fs['nodes_per_zone']} nodes/{dom}" if zf
              else "single AZ — a zone outage takes down the whole cluster")
    lines += [
        f"FAILURE RESILIENCE  ({layout})",
        "─" * 44,
        f"  Failure CPU ceiling:         {ftarget:.0f}%",
    ]
    if fs["sized_for"] != "normal":
        sized = fs.get("fault_domain", "zone") if fs["sized_for"] == "zone" else fs["sized_for"]
        lines.append(f"  Sized for:                   {sized} failure  (+{fs['failure_nodes_added']} node(s) added for headroom)")
    lines += [
        f"  Normal (all {c['total_nodes']} nodes):          {c['cpu_utilization_pct']}%  ✅",
        "",
        f"  1-node failure  ({nf['surviving_nodes']} nodes survive):",
        f"    CPU Utilization:           {nf['cpu_util_pct']}%  {_util_badge(nf['exceeds_target'])}",
        "",
    ]
    if zf:
        lines += [
            f"  1-{dom} failure  ({zf['surviving_nodes']} nodes survive, {fs['nodes_per_zone']} node(s) lost):",
            f"    CPU Utilization:           {zf['cpu_util_pct']}%  {_util_badge(zf['exceeds_target'])}",
            "",
        ]

    if nf["exceeds_target"] or (zf and zf["exceeds_target"]):
        lines.append(f"  💡 Consider adding nodes (in multiples of RF) to stay within {ftarget:.0f}% under failure"
                     " — rerun with --size-for zone to size for it.")
        lines.append("")

    mr = r.get("multi_region")
    if mr:
        lines += ["MULTI-REGION", "─" * 44,
                  f"  Regions:                     {mr['regions']} × {mr['nodes_per_region']} nodes, {mr['region_rtt_ms']:g} ms RTT",
                  f"  Added latency / statement:   ~{mr['added_latency_per_statement_ms']:g} ms (remote quorum for writes"
                  + (", remote leaders for reads" if not mr["preferred_region"] else "") + ", distributed commits)"]
        if mr["added_latency_with_pipelining_ms"] is not None:
            if mr["write_pipelining"]:
                lines.append(f"  Write pipelining:            on  (without it: ~{mr['added_latency_without_pipelining_ms']:g} ms)")
            elif mr["added_latency_with_pipelining_ms"] < mr["added_latency_per_statement_ms"]:
                lines.append(f"  Write pipelining:            off — with it: ~{mr['added_latency_with_pipelining_ms']:g} ms "
                             "(--ysql_enable_write_pipelining=true)")
        if "leader_region_cpu_pct" in mr:
            lines += [f"  Leader region CPU:           {mr['leader_region_cpu_pct']}%  (all leaders; sized so any region can take over)",
                      f"  Follower region CPU:         {mr['follower_region_cpu_pct']}%"]
        lines += ["  • Writes always cross regions; follower reads and read replicas keep reads local.", ""]
    rr = r.get("read_replica")
    if rr:
        lines += ["READ REPLICA CLUSTER", "─" * 44,
                  f"  Serves:                      {rr['read_pct']:g}% of reads ({rr['reads_per_s']:,.0f}/s), "
                  f"{rr['copies']} cop{'y' if rr['copies'] == 1 else 'ies'} of the data",
                  f"  Nodes:                       {rr['nodes']} × {rr['vcpu_per_node']} vCPU  "
                  f"({rr['cpu_utilization_pct']}% CPU, incl. applying every write)",
                  f"  Memory / node:               {rr['mem_per_node_gb']} GB",
                  f"  Storage / node:              {rr['storage_per_node_gb']:,} GB",
                  f"  IOPS / node:                 {int(rr['iops_per_node']):,}",
                  f"  Replication ingest:          {rr['replication_ingest_mbps']:,} MiB/s (usually cross-region)",
                  "  • Timeline-consistent reads; not part of the RF quorum. YSQL reads need",
                  "    yb_read_from_followers and read-only transactions.", ""]

    if is_ycql:
        lines += [
            "YCQL DEPLOYMENT NOTES",
            "─" * 44,
            "  • YCQL-only cluster: set --use_memory_defaults_optimized_for_ysql=false",
            "    (TServer gets ~85% of RAM instead of ~60%; no PostgreSQL share) and",
            "    optionally --enable_ysql=false to drop the postgres processes.",
            "  • No colocation in YCQL: many small tables each cost tablets.",
            "  • CDC is not supported for YCQL in this model; xCluster is supported.",
            "",
        ]
    if i.get("connection_manager"):
        lines += [
            "YSQL CONNECTION MANAGER",
            "─" * 44,
            "  • Set --enable_ysql_conn_mgr=true (YBA/Aeon: turn on Connection Pooling).",
            f"  • Set --ysql_max_connections={c['conn_per_node']} to cap backends per node; raise",
            "    --ysql_conn_mgr_max_client_connections (default 10000) if apps need more.",
            "  • Avoid session state that makes connections sticky (SQL-level PREPARE, temp tables).",
            "",
        ]

    lines.append("═" * 56)
    return "\n".join(lines)


def format_comparison(cmp):
    header = (f"  {'vCPU':>4}  {'Nodes':>5}  {'Tot vCPU':>8}  {'RAM/node':>8}  {'Disk/node':>10}"
              f"  {'CPU':>6}  {'Node-fail':>9}  {'Zone-fail':>9}")
    lines = [
        "═" * 80,
        "  vCPU TIER COMPARISON",
        "═" * 80,
        header,
        "  " + "─" * 76,
    ]
    for x in cmp["comparison"]:
        mark = "  ◀ recommended" if x["recommended"] else ""
        zone = ("n/a" if x["zone_failure_cpu_pct"] is None
                else f"{x['zone_failure_cpu_pct']}%" + ("" if x["zone_failure_ok"] else " ⚠"))
        cpu = f"{x['cpu_utilization_pct']}%" + ("" if x["cpu_within_target"] else " ⚠")
        node = f"{x['node_failure_cpu_pct']}%" + ("" if x["node_failure_ok"] else " ⚠")
        lines.append(
            f"  {x['vcpu_per_node']:>4}  {x['total_nodes']:>5}  {x['total_vcpu']:>8}  {str(x['mem_per_node_gb']) + ' GB':>8}"
            f"  {format(x['storage_per_node_gb'], ',.0f') + ' GB':>10}  {cpu:>6}  {node:>9}"
            f"  {zone:>9}{mark}"
        )
    lines += [
        "",
        f"  Recommended: {cmp['recommended_vcpu_per_node']} vCPU/node — {cmp['recommendation_basis']}.",
        "  Full report for the recommended tier follows."
        + ("" if cmp["results"][0]["inputs"]["size_for"] != "normal"
           else "\n  Tip: add --size-for zone to size every tier for a zone loss before comparing."),
    ]
    return "\n".join(lines)


HTML_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2128;--muted:#5d6672;--line:#e3e6ea;
--accent:#3b5bdb;--ok:#2b8a3e;--ok-bg:#ebfbee;--warn:#b35c00;--warn-bg:#fff4e6;--bar:#dfe3e8}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f25;--fg:#e6e9ed;--muted:#9aa4af;
--line:#2a3139;--accent:#7b93f5;--ok:#69db7c;--ok-bg:#16291b;--warn:#ffa94d;--warn-bg:#2e2113;--bar:#2a3139}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 10px}
.sub{color:var(--muted);margin:0 0 20px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--accent);color:var(--card);font-size:12px;font-weight:600;vertical-align:middle;margin-left:6px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.kpi .l{color:var(--muted);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0}
.wide{grid-column:1/-1}
table{width:100%;border-collapse:collapse}
td,th{padding:5px 0;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;padding-left:12px}
th{color:var(--muted);font-weight:500;font-size:12px}
.note{color:var(--muted);font-size:12px}
.ok{color:var(--ok)} .bad{color:var(--warn)}
.alert{border-radius:8px;padding:10px 12px;margin:0 0 12px;background:var(--warn-bg);color:var(--fg);border-left:3px solid var(--warn)}
.bar{position:relative;height:10px;background:var(--bar);border-radius:5px;margin:4px 0 2px}
.bar>span{position:absolute;left:0;top:0;bottom:0;border-radius:5px;background:var(--ok)}
.bar>span.bad{background:var(--warn)}
.bar>i{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--fg);opacity:.6}
.scn{margin-bottom:12px}
.scn .h{display:flex;justify-content:space-between;gap:8px}
ul{margin:0;padding-left:18px}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}
footer{margin-top:20px;color:var(--muted);font-size:12px}
"""


def format_html(r, comparison=None):
    from html import escape as e

    c  = r["cluster"]
    s  = r["storage"]
    m  = r["memory"]
    w  = r["workload"]
    i  = r["inputs"]
    io = r["iops"]
    nw = r["network"]
    p  = r["parameters"]
    t  = r.get("tablets")
    ttl = r.get("ttl")
    fs = r["failure_scenarios"]
    api = i["api"].upper()
    is_ycql = i["api"] == "ycql"
    has_conn = c["conn_per_node"] > 0
    target = p["target_cpu_util_pct"]
    ftarget = fs["failure_cpu_target_pct"]
    cal = r.get("calibration")
    cc = r.get("concurrency")

    def kv(rows):
        body = "".join(
            f"<tr><td>{e(str(k))}</td><td class='n'>{v}</td></tr>" for k, v in rows
        )
        return f"<table>{body}</table>"

    def card(title, inner, wide=False):
        cls = "card wide" if wide else "card"
        return f"<section class='{cls}'><h2>{e(title)}</h2>{inner}</section>"

    def badge(ok, ok_text="within target", bad_text="exceeds target"):
        return f"<span class='ok'>✓ {ok_text}</span>" if ok else f"<span class='bad'>⚠ {bad_text}</span>"

    def util_bar(label, pct, ok, mark):
        width = min(pct or 0, 100)
        return (
            f"<div class='scn'><div class='h'><span>{e(label)}</span>"
            f"<span><b>{pct}%</b> {badge(ok)}</span></div>"
            f"<div class='bar'><span class='{'' if ok else 'bad'}' style='width:{width}%'></span>"
            f"<i style='left:{mark}%' title='target {mark:.0f}%'></i></div></div>"
        )

    # ── Alerts ──
    alerts = []
    if i.get("exec_time_estimated"):
        tool = "<code>&lt;tserver&gt;:12000/statements</code>" if is_ycql else "<code>pg_stat_statements</code>"
        profile = i["exec_time_label"].replace(" (estimated)", "")
        alerts.append(f"Execution time was <b>estimated</b> at {i['avg_exec_ms']:g} ms ({e(profile)}). "
                      f"Measure real latencies with {tool}.")
    if i["cpu_model"] == "per-op" and not cal and i["workload_source"] != "provided":
        alerts.append(f"Workload profile <b>auto-selected</b> ({e(i['workload'])}) from the read/write mix — pass "
                      "<code>--workload</code>, or calibrate with <code>--observed-qps</code>/<code>--observed-cpu-pct</code>.")
    if cc and cc["connection_bound"]:
        alerts.append(f"{cc['in_flight_per_node']:,} in-flight requests/node exceed {cc['backends_per_node']} backends — "
                      "the workload is connection-bound; add nodes or raise backends.")
    if not c["cpu_within_target"]:
        alerts.append(f"CPU utilization {c['cpu_utilization_pct']}% exceeds the {target:.0f}% target.")
    if s.get("storage_cap_triggered"):
        alerts.append(f"Storage cap triggered: {s['storage_nodes_added']} node(s) added to keep storage/node "
                      f"≤ {s['max_storage_per_node_gb']:,} GB.")
    if t and t.get("low_tablet_count_warning"):
        alerts.append(f"Low tablet count: {c['total_nodes']} nodes but only {t['total_tablets']:,} tablets — "
                      f"some nodes may host no leader tablets.")
    if io["disk_nodes_added"]:
        alerts.append(f"Disk limit: {io['disk_nodes_added']} node(s) added to keep IOPS/throughput within "
                      f"{io['disk_iops_limit']:,.0f} IOPS / {io['disk_mibps_limit']:,.0f} MiB/s per node — "
                      "NVMe or io2 (<code>--disk-iops</code>/<code>--disk-mibps</code>) would need fewer.")
    if fs["node_failure"]["exceeds_target"] or (fs["zone_failure"] and fs["zone_failure"]["exceeds_target"]):
        alerts.append(f"CPU exceeds {ftarget:.0f}% under failure — consider adding nodes in multiples of RF={i['rf']} "
                      "(or rerun with <code>--size-for zone</code>).")

    # ── KPI tiles ──
    kpis = [
        (f"{c['total_nodes']}", f"nodes (RF={i['rf']})"),
        (f"{c['vcpu_per_node']}", "vCPU / node"),
        (f"{m['mem_per_node_gb']} GB", "memory / node"),
        (f"{s['storage_per_node_gb']:,.0f} GB", f"storage / node (2 yr: {s['storage_per_node_2yr_gb']:,.0f} GB)"),
        (f"{c['cpu_utilization_pct']}%", f"CPU utilization (target ≤{target:.0f}%)"),
    ]
    kpi_html = "".join(f"<div class='kpi'><div class='v'>{e(v)}</div><div class='l'>{e(l)}</div></div>" for v, l in kpis)

    # ── Sections ──
    inputs = [
        ("API", api),
        ("QPS", f"{i['qps']:,.0f}"),
        ("Write / Read", f"{i['write_pct']:g}% / {i['read_pct']:g}%"),
        (("Avg execution time" if i["cpu_model"] == "latency" else "Avg latency"),
         ("—" if i["avg_exec_ms"] is None else f"{i['avg_exec_ms']:g} ms")
         + (" (estimated)" if i.get("exec_time_estimated") else "")
         + (" (legacy CPU model)" if i["cpu_model"] == "latency" else "")),
        ("Workload profile", f"{e(i['workload'])} — {e(p['workload_label'])} ({e(i['workload_source'])})"),
        ("CPU architecture", f"{i['cpu_arch']}" + (f" (×{p['arch_factor']:g})" if p["arch_factor"] != 1 else "")),
        ("Replication factor", i["rf"]),
        ("Topology", (f"multi-region, {i['regions']} regions, {i['region_rtt_ms']:g} ms RTT"
                      + (", leaders pinned" if i["preferred_region"] else "")) if i["regions"] > 1
         else f"multi-AZ, {i['zones']} zones" if i["zones"] > 1 else "single AZ"),
        ("Table size", f"{i['table_size_gb']:,.0f} GB"),
        ("Avg row size", f"{i['avg_row_bytes']} bytes ({i['avg_row_bytes_source']})"),
        ("Data growth rate", f"{i['growth_rate_pct']:g}%/yr ({i['growth_rate_source']})"),
    ]
    if ttl:
        inputs.append(("TTL", f"{ttl['ttl_days']:g} days"))
    if t["num_objects"] is not None:
        inputs.append(("Objects (tables+indexes)", f"{t['num_objects']:,}"))
    tx = r["transactions"]
    if i["tps"] is not None or i["statements_per_txn"] > 1 or i["distributed_txn_pct"]:
        inputs.append(("Transactions", f"{tx['tps']:,.0f} TPS × {tx['statements_per_txn']:g} statements, "
                                       f"{tx['distributed_txn_pct']:g}% distributed"))
    if i["follower_read_pct"] or i["read_replica_read_pct"]:
        inputs.append(("Read offload", f"{i['follower_read_pct']:g}% follower reads, "
                                       f"{i['read_replica_read_pct']:g}% read replicas"))
    if i.get("connection_manager"):
        inputs.append(("Connection Manager", "enabled"))
    if i.get("cdc_enabled"):
        inputs.append(("CDC", f"enabled (+{p['cdc_overhead_pct']:.0f}% CPU)"))
    if i.get("xcluster_enabled"):
        inputs.append(("xCluster", f"enabled (+{p['xcluster_overhead_pct']:.0f}% CPU)"))

    workload = [
        ("Write ops/s", f"{w['write_ops_per_s']:,.0f}"),
        ("Read ops/s", f"{w['read_ops_per_s']:,.0f}"),
        (f"Effective write ops/s (×RF×{p['rpc_multiplier']:g})", f"{w['eff_write_ops_per_s']:,.0f}"),
        (f"Effective read ops/s (×{p['rpc_multiplier']:g})", f"{w['eff_read_ops_per_s']:,.0f}"),
        ("Total effective ops/s", f"{w['total_eff_ops_per_s']:,.0f}"),
        ("CPU model", "calibrated from observed cluster" if cal
         else (f"per-op: {p['cpu_ms_per_read']} ms/read, {p['cpu_ms_per_write']} ms/write (×{p['rf_write_scale']:g} RF), "
               f"× {p['workload_multiplier']:g} workload × {p['rpc_multiplier']:g} RPC") if i["cpu_model"] == "per-op"
         else "legacy latency (effective ops × exec time)"),
        ("vCPUs needed (workload + tablets)", w["raw_vcpus_workload_incl_features_and_tablets"]),
    ]

    cluster = [
        ("Total nodes", c["total_nodes"]),
        ("Total vCPU", c["total_vcpu"]),
        ("Total memory", f"{m['total_memory_gb']:,} GB"),
        ("Storage / node — now", f"{s['storage_per_node_gb']:,} GB"),
        ("Storage / node — 1 yr", f"{s['storage_per_node_1yr_gb']:,} GB"),
        ("Storage / node — 2 yr", f"{s['storage_per_node_2yr_gb']:,} GB"),
        ("Total storage — now", f"{s['total_storage_gb']:,} GB"),
        ("Total storage — 2 yr", f"{s['total_storage_2yr_gb']:,} GB"),
    ]

    limits = [("DB operations / node / s", f"{c['db_ops_per_node_per_s']:,.0f}")]
    if has_conn and i.get("connection_manager"):
        limits += [
            ("PG backends / node", f"{c['conn_per_node']} ({p['conn_per_vcpu']} × {i['vcpu_per_node']} vCPU, Connection Manager)"),
            ("Client connections / node", f"~{c['client_conn_per_node']:,} ({p['cm_client_ratio']} per backend)"),
            ("Connection CPU / node", f"{c['conn_cpu_per_node']} cores"),
        ]
    elif has_conn:
        kind = "CQL" if is_ycql else "PG"
        limits += [
            (f"{kind} connections / node", f"{c['conn_per_node']} ({p['conn_per_vcpu']} × {i['vcpu_per_node']} vCPU)"),
            ("Connection CPU / node", f"{c['conn_cpu_per_node']} cores"),
        ]
    else:
        limits.append(("CQL connections", "multiplexed — not modeled"))
    if cc:
        in_flight = f"{cc['in_flight_per_node']:,} (QPS × {cc['latency_ms']:g} ms)"
        if cc["backends_per_node"] is not None:
            pool = f"of {cc['backends_per_node']} backends"
            in_flight += " " + badge(not cc["connection_bound"], pool, "connection-bound")
        limits.append(("In-flight requests / node", in_flight))
    limits += [
        ("CPU utilization", f"{c['cpu_utilization_pct']}% {badge(c['cpu_within_target'])}"),
        ("Est. IOPS / node", f"{int(io['total_iops_per_node']):,}"),
        (f"  write ({p['iops_per_replica_write']:g} per replicated write)", f"{int(io['write_iops_per_node']):,}"),
        (f"  read cold ({io['read_cache_miss_pct']:g}% miss)", f"{int(io['read_iops_per_node']):,}"),
        ("Disk limits / node", f"{io['disk_iops_limit']:,.0f} IOPS, {io['disk_mibps_limit']:,.0f} MiB/s"
         + (f" (+{io['disk_nodes_added']} nodes)" if io["disk_nodes_added"] else "")),
        ("Est. disk throughput / node", f"{io['disk_mibps_per_node']:,} MiB/s"),
        ("Est. network / node", f"{nw['total_net_mbps_per_node']:.1f} MiB/s"),
        ("  write (Raft)", f"{nw['write_net_mbps_per_node']:.1f} MiB/s"),
        ("  read", f"{nw['read_net_mbps_per_node']:.1f} MiB/s"),
    ]
    if nw["zones"] > 1:
        limits.append((f"Cross-{nw['cross_scope']} traffic / node", f"{nw['cross_az_mbps_per_node']:.1f} MiB/s "
                       f"(≈{nw['cross_az_gb_per_month']:,.0f} GB/mo, ${nw['cross_az_cost_per_month']:,.0f}/mo)"))
    limits += [
    ]

    storage = [
        (f"Raw data + indexes ({p['index_overhead_pct']:.0f}% index)", f"{s['total_raw_gb']:,} GB"),
        (f"After LZ4 compression (−{p['compression_pct']:.0f}%)", f"{s['after_compression_gb']:,} GB"),
        (f"After RF={i['rf']} replication", f"{s['with_replication_gb']:,} GB"),
        ("Per node (data)", f"{s['base_storage_per_node_gb']:,} GB"),
        (f"+ Compaction reserve ({p['compaction_reserve_pct']:.0f}%)", f"{s['compaction_reserve_gb_per_node']:,} GB"),
        ((f"+ WAL ({s['wal_write_mb_per_s_per_node']} MB/s × {p['wal_retention_secs'] / 3600:g} h)"
          if p["wal_retention_secs"] is not None else f"+ WAL ({p['wal_overhead_pct']:.0f}% of data)"),
         f"{s['wal_gb_per_node']:,} GB"),
        ("Per node (total)", f"<b>{s['storage_per_node_gb']:,} GB</b>"),
    ]

    memory = [("Base ratio", f"{m['base_mem_ratio']} vCPU:RAM = {m['base_mem_gb']} GB")]
    if has_conn:
        memory.append(("Connection memory", f"{c['conn_per_node']} × {p['mem_mb_per_conn']:.0f} MB = {m['conn_mem_gb']} GB"
                       + (f" (within {m['pg_budget_gb']} GB PostgreSQL share)" if not m["conn_extra_gb"]
                          else f" (+{m['conn_extra_gb']} GB beyond {m['pg_budget_gb']} GB PostgreSQL share)")))
    if m["conn_mgr_mem_gb"]:
        memory.append(("Connection Manager (odyssey)", f"{m['conn_mgr_mem_gb']} GB"))
    if t["total_tablets"]:
        memory.append(("Tablet maintenance", f"{m['tablet_mem_gb']} GB"))
    memory += [
        ("Raw total / node", f"{m['raw_mem_per_node_gb']} GB"),
        ("Recommended / node (tier)", f"<b>{m['mem_per_node_gb']} GB</b>"
         + (f" <span class='note'>within {m['ram_tier_tolerance_pct']:g}% tolerance</span>" if m["ram_tier_tolerance_applied"] else "")),
    ]

    iter_rows = "".join(
        f"<tr><td>{idx + 1}</td><td class='n'>{it['nodes']}</td><td class='n'>{it['total_vcpu']}</td>"
        f"<td class='n'>{it['effective_vcpus_used']}</td><td class='n'>{it['cpu_util_pct']}%</td>"
        f"<td class='n'>{badge(it['pass'], 'pass', 'fail')}</td></tr>"
        for idx, it in enumerate(r["sizing_iterations"])
    )
    iterations = (
        "<table><tr><th>#</th><th class='n'>Nodes</th><th class='n'>Total vCPU</th>"
        f"<th class='n'>vCPU used</th><th class='n'>CPU</th><th class='n'>≤{target:.0f}%</th></tr>{iter_rows}</table>"
    )

    nf, zf = fs["node_failure"], fs["zone_failure"]
    sized = (f" Sized for {fs['sized_for']} failure (+{fs['failure_nodes_added']} node(s))."
             if fs["sized_for"] != "normal" else "")
    failure = (
        f"<p class='note'>Zone layout: {fs['num_zones']} zones × {fs['nodes_per_zone']} node(s)/zone. "
        f"Markers: {target:.0f}% normal, {ftarget:.0f}% under failure.{sized}</p>"
        + util_bar(f"Normal ({c['total_nodes']} nodes)", c["cpu_utilization_pct"], c["cpu_within_target"], target)
        + util_bar(f"1-node failure ({nf['surviving_nodes']} survive)", nf["cpu_util_pct"], not nf["exceeds_target"], ftarget)
        + (util_bar(f"1-{fs.get('fault_domain', 'zone')} failure ({zf['surviving_nodes']} survive)", zf["cpu_util_pct"], not zf["exceeds_target"], ftarget)
           if zf else "<p class='note'>Single AZ: a zone outage takes down the whole cluster.</p>")
    )

    sections = []
    if comparison:
        head = ("<tr><th>vCPU/node</th><th class='n'>Nodes</th><th class='n'>Total vCPU</th><th class='n'>RAM/node</th>"
                "<th class='n'>Disk/node</th><th class='n'>CPU</th><th class='n'>Node-fail</th><th class='n'>Zone-fail</th></tr>")
        body = "".join(
            f"<tr><td>{'<b>' if x['recommended'] else ''}{x['vcpu_per_node']}{' ✓ recommended</b>' if x['recommended'] else ''}</td>"
            f"<td class='n'>{x['total_nodes']}</td><td class='n'>{x['total_vcpu']}</td>"
            f"<td class='n'>{x['mem_per_node_gb']} GB</td><td class='n'>{x['storage_per_node_gb']:,.0f} GB</td>"
            f"<td class='n {'' if x['cpu_within_target'] else 'bad'}'>{x['cpu_utilization_pct']}%</td>"
            f"<td class='n {'' if x['node_failure_ok'] else 'bad'}'>{x['node_failure_cpu_pct']}%</td>"
            f"<td class='n {'' if x['zone_failure_ok'] else 'bad'}'>"
            f"{'n/a' if x['zone_failure_cpu_pct'] is None else str(x['zone_failure_cpu_pct']) + '%'}</td></tr>"
            for x in comparison["comparison"]
        )
        sections.append(card("vCPU tier comparison", f"<table>{head}{body}</table>"
                             f"<p class='note'>Recommended: {comparison['recommended_vcpu_per_node']} vCPU/node — "
                             f"{e(comparison['recommendation_basis'])}. Details below are for that tier.</p>", wide=True))
    sections += [
        card("Inputs", kv(inputs)),
        card("Derived workload", kv(workload)),
        card("Cluster recommendation", kv(cluster)),
        card("Per-node operational limits", kv(limits)),
        card("Storage breakdown", kv(storage)),
        card("Memory breakdown", kv(memory)),
        card("Failure resilience", failure),
        card("Node sizing iterations", iterations),
    ]
    if cal:
        sections.append(card("CPU calibration", kv([
            ("Observed", f"{cal['observed_qps']:,.0f} QPS @ {cal['observed_cpu_pct']:g}% on "
                         f"{cal['observed_nodes']} × {cal['observed_vcpu_per_node']} vCPU"),
            ("Observed cluster shape", f"nodes {e(cal['observed_nodes_source'])}, vCPU {e(cal['observed_vcpu_source'])}"),
            ("Busy cores", cal["observed_busy_cores"]),
            ("− connection CPU", cal["observed_conn_cores"]),
            ("− tablet maintenance CPU", cal["observed_tablet_cores"]),
            ("= workload cores", cal["observed_workload_cores"]),
            ("CPU cost per client op", f"<b>{cal['cpu_ms_per_op']} ms</b> ({cal['factor_vs_model']}× the {e(i['workload'])} profile)"),
        ]) + "<p class='note'>Assumes the observed cluster runs the same read/write mix, RF and features.</p>"))
    if t:
        schema_row = ([("Schema tablets", f"{t['schema_tablets']:,} ({t['num_objects']:,} objects × "
                                          f"{t['tablets_per_table']} — {e(t['tablets_per_table_source'])})")]
                      if t["num_objects"] is not None else [])
        sections.append(card("Tablet maintenance overhead", kv(schema_row + [
            ("Data-driven tablets (auto-split)", f"{t['data_tablets']:,}"),
            (f"Total tablets (by {t['tablet_basis']}, × RF)", f"{t['total_tablets']:,}"),
            ("vCPU overhead (cluster)", f"{t['tablet_vcpu_overhead_total']} (≈{t['tablet_vcpu_overhead_per_node']}/node)"),
            ("RAM overhead (cluster)", f"{t['tablet_mem_overhead_total_mb']:,.0f} MB (≈{m['tablet_mem_gb']} GB/node)"),
        ])))
    if ttl:
        sections.append(card("TTL steady state", kv([
            ("Ingest (writes × row size)", f"{ttl['ingest_gb_per_day']:,} GB/day raw"),
            (f"Steady state @ {ttl['ttl_days']:g} days", f"{ttl['ttl_steady_state_gb']:,} GB raw"),
            ("Data cap used", f"{ttl['data_cap_gb']:,} GB"),
            ("Growth projection capped", "yes" if ttl["growth_capped"] else "no (not within 2 yr)"),
        ]) + "<p class='note'>Assumes every write is a new row (upper bound). Expired data is reclaimed by "
             "compaction — prefer table-level <code>default_time_to_live</code>.</p>"))
    if i.get("connection_manager"):
        sections.append(card("YSQL Connection Manager", "<ul>"
            "<li>Set <code>--enable_ysql_conn_mgr=true</code> (YBA/Aeon: turn on Connection Pooling).</li>"
            f"<li>Set <code>--ysql_max_connections={c['conn_per_node']}</code> to cap backends per node; raise "
            "<code>--ysql_conn_mgr_max_client_connections</code> (default 10000) if apps need more.</li>"
            "<li>Avoid session state that makes connections sticky (SQL-level PREPARE, temp tables).</li></ul>"))
    mr = r.get("multi_region")
    if mr:
        rows = [("Regions", f"{mr['regions']} × {mr['nodes_per_region']} nodes, {mr['region_rtt_ms']:g} ms RTT"),
                ("Added latency / statement", f"~{mr['added_latency_per_statement_ms']:g} ms")]
        if mr["added_latency_with_pipelining_ms"] is not None:
            rows.append(("Write pipelining", "on" if mr["write_pipelining"] else
                         f"off — with it ~{mr['added_latency_with_pipelining_ms']:g} ms"))
        if "leader_region_cpu_pct" in mr:
            rows += [("Leader region CPU", f"{mr['leader_region_cpu_pct']}% (sized so any region can take over)"),
                     ("Follower region CPU", f"{mr['follower_region_cpu_pct']}%")]
        sections.append(card("Multi-region", kv(rows)))
    rr = r.get("read_replica")
    if rr:
        sections.append(card("Read replica cluster", kv([
            ("Serves", f"{rr['read_pct']:g}% of reads ({rr['reads_per_s']:,.0f}/s), {rr['copies']} copies"),
            ("Nodes", f"{rr['nodes']} × {rr['vcpu_per_node']} vCPU ({rr['cpu_utilization_pct']}% CPU)"),
            ("Memory / node", f"{rr['mem_per_node_gb']} GB"),
            ("Storage / node", f"{rr['storage_per_node_gb']:,} GB"),
            ("IOPS / node", f"{int(rr['iops_per_node']):,}"),
            ("Replication ingest", f"{rr['replication_ingest_mbps']:,} MiB/s"),
        ]) + "<p class='note'>Timeline-consistent reads; outside the RF quorum.</p>"))
    if is_ycql:
        sections.append(card("YCQL deployment notes", "<ul>"
            "<li>YCQL-only cluster: set <code>--use_memory_defaults_optimized_for_ysql=false</code> "
            "(TServer gets ~85% of RAM instead of ~60%) and optionally <code>--enable_ysql=false</code>.</li>"
            "<li>No colocation in YCQL: many small tables each cost tablets.</li>"
            "<li>CDC is not supported for YCQL in this model; xCluster is supported.</li></ul>"))

    alert_html = "".join(f"<div class='alert'>{a}</div>" for a in alerts)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YugabyteDB Sizing ({api})</title>
<style>{HTML_CSS}</style></head>
<body><main>
<h1>YugabyteDB Cluster Sizing <span class="pill">{api}</span></h1>
<p class="sub">{i['qps']:,.0f} QPS · {i['write_pct']:g}% write / {i['read_pct']:g}% read · RF={i['rf']} · {i['table_size_gb']:,.0f} GB</p>
{alert_html}
<div class="kpis">{kpi_html}</div>
<div class="grid">{''.join(sections)}</div>
<footer>Indicative sizing only — test with your actual workload to fine-tune production sizing.
Scale horizontally by adding nodes in multiples of RF; cap vertical scaling at 32–64 cores per node.</footer>
</main></body></html>
"""


def main():
    parser = argparse.ArgumentParser(
        description="YugabyteDB Cluster Sizing Calculator"
    )

    parser.add_argument("--api", choices=sorted(API_PROFILES), default="ysql",
                        help="YugabyteDB API: ysql (default) or ycql — selects API-specific overhead defaults")

    # Required inputs
    parser.add_argument("--qps",            type=float, default=None,   help="Peak statements (operations) per second — or use --tps")
    parser.add_argument("--tps",            type=float, default=None,   help="Peak transactions per second (QPS = TPS × --statements-per-txn)")
    parser.add_argument("--statements-per-txn", type=float, default=None, help="Statements per transaction (default 1 = autocommit)")
    parser.add_argument("--distributed-txn-pct", type=float, default=None,
                        help="%% of write transactions that are distributed/multi-shard (default: 100 if >1 statement, else 0)")
    parser.add_argument("--write-pct",      type=float, required=True,  help="Write percentage (e.g. 30 for 30%%)")
    parser.add_argument("--read-pct",       type=float, required=True,  help="Read percentage (e.g. 70 for 70%%)")
    parser.add_argument("--avg-exec-ms",    type=float, default=None,   help="Avg statement latency (ms). Per-op CPU model: used for the concurrency check only. "
                                                                         "Legacy --cpu-model latency: drives CPU (auto-estimated if omitted).")
    parser.add_argument("--vcpu-per-node",  type=int,   default=None,   help="vCPUs per node (e.g. 8, 16, 32); required unless --compare is used")
    parser.add_argument("--rf",             type=int,   default=3,      help="Replication factor: 1, 3, 5 or 7 (default: 3)")
    parser.add_argument("--table-size-gb",  type=float, required=True,  help="Raw uncompressed table size (GB)")

    # Optional workload inputs
    parser.add_argument("--avg-row-bytes",         type=int,   default=None,   help=f"Avg row size in bytes (default: {DEFAULTS['avg_row_bytes']})")
    parser.add_argument("--growth-rate-pct",       type=float, default=None,   help=f"Annual data growth rate %% (default: {DEFAULTS['growth_rate_pct']})")
    parser.add_argument("--max-storage-per-node-gb", type=float, default=None, help=f"Max disk per node in GB (default: {DEFAULTS['max_storage_per_node_gb']} = 20 TB)")

    # Optional overrides for fixed parameters
    ysql, ycql = API_PROFILES["ysql"], API_PROFILES["ycql"]
    parser.add_argument("--rpc-overhead",        type=float, help=f"RPC overhead fraction added to base 1.0 (default: YSQL {ysql['rpc_overhead']}, YCQL {ycql['rpc_overhead']})")
    parser.add_argument("--index-overhead",      type=float, help=f"Index overhead fraction (default: YSQL {ysql['index_overhead']}, YCQL {ycql['index_overhead']})")
    parser.add_argument("--compression-ratio",   type=float, help=f"Compression reduction fraction (default: {DEFAULTS['compression_ratio']})")
    parser.add_argument("--wal-overhead",        type=float, help="Legacy: model WAL as this fraction of data (e.g. 0.10) instead of write rate × retention")
    parser.add_argument("--wal-retention-secs",  type=float, help=f"WAL retention for the write-rate model (default: {DEFAULTS['wal_retention_secs']} = log_min_seconds_to_retain)")
    parser.add_argument("--cdc-wal-retention-secs", type=float, help=f"WAL retention with CDC (default: {DEFAULTS['cdc_wal_retention_secs']} = cdc_wal_retention_time_secs)")
    parser.add_argument("--xcluster-wal-retention-secs", type=float, help=f"WAL retention with xCluster (default: {DEFAULTS['xcluster_wal_retention_secs']} = 24 h, YB best practice)")
    parser.add_argument("--compaction-reserve",  type=float, help=f"Compaction reserve fraction (default: {DEFAULTS['compaction_reserve']})")
    parser.add_argument("--target-cpu-util",     type=float, help=f"Target CPU utilization (default: {DEFAULTS['target_cpu_util']})")
    parser.add_argument("--conn-per-vcpu",       type=int,   help=f"Connections (backends) per vCPU (default: YSQL {ysql['conn_per_vcpu']}, "
                                                                  f"{DEFAULTS['cm_backends_per_vcpu']} with --connection-manager; YCQL not modeled)")
    parser.add_argument("--connection-manager",  action="store_true", default=False,
                        help="YSQL only: size for YSQL Connection Manager (backend pool of "
                             f"{DEFAULTS['cm_backends_per_vcpu']}/vCPU, +{DEFAULTS['cm_mem_mb']} MB/node for odyssey)")
    parser.add_argument("--cm-client-ratio",     type=int,   help=f"Client connections per backend with Connection Manager (default: {DEFAULTS['cm_client_ratio']})")
    parser.add_argument("--mem-mb-per-conn",     type=float, help=f"MB RAM per connection (default: YSQL {ysql['mem_mb_per_conn']}, YCQL not modeled)")
    parser.add_argument("--conn-cpu-overhead",   type=float, help=f"CPU core overhead per connection (default: YSQL {ysql['conn_cpu_overhead']}, YCQL not modeled)")
    parser.add_argument("--iops-per-replica-write", type=float, help=f"Disk IOPS per replicated write (default: {DEFAULTS['iops_per_replica_write']}, TPC-C calibrated)")
    parser.add_argument("--disk-write-amp",      type=float, help=f"Disk bytes per replicated written byte (default: {DEFAULTS['disk_write_amp']}, TPC-C calibrated)")
    parser.add_argument("--net-write-factor",    type=float, help=f"NIC bytes per row-byte per replicated write (default: {DEFAULTS['net_write_factor']})")
    parser.add_argument("--net-read-factor",     type=float, help=f"NIC bytes per row-byte per read (default: {DEFAULTS['net_read_factor']})")
    parser.add_argument("--disk-iops",           type=float, help=f"Per-node disk IOPS limit; nodes are added to fit (default: {DEFAULTS['disk_iops_limit']:,.0f} = gp3 max)")
    parser.add_argument("--disk-mibps",          type=float, help=f"Per-node disk throughput limit in MiB/s (default: {DEFAULTS['disk_mibps_limit']:,.0f} = gp3 max)")
    parser.add_argument("--zones",               type=int,   help="Availability zones: RF (default, multi-AZ with one zone per replica) or 1 (single AZ)")
    parser.add_argument("--cross-az-cost-per-gb", type=float, help=f"Inter-AZ transfer price per GB (default: {DEFAULTS['cross_az_cost_per_gb']}, AWS)")
    parser.add_argument("--read-cache-miss",     type=float, help="Fixed read cache-miss fraction (default: computed from leader data vs cache)")
    parser.add_argument("--ram-tier-tolerance",  type=float, help=f"Stay on a RAM tier if raw memory exceeds it by at most this fraction (default: {DEFAULTS['ram_tier_tolerance']})")

    # CDC / xCluster feature flags
    parser.add_argument("--cdc",                 action="store_true", default=False, help="Enable CDC (Change Data Capture, YSQL only): adds 5%% CPU overhead by default")
    parser.add_argument("--cdc-overhead",        type=float, default=None,           help=f"Override CDC CPU overhead fraction (default: {DEFAULTS['cdc_overhead']})")
    parser.add_argument("--xcluster",            action="store_true", default=False, help="Enable xCluster replication: adds 10%% CPU overhead and 24 h WAL retention by default")
    parser.add_argument("--xcluster-overhead",   type=float, default=None,           help=f"Override xCluster CPU overhead fraction (default: {DEFAULTS['xcluster_overhead']})")

    # Tablet maintenance overhead (optional, driven by schema object count)
    parser.add_argument("--num-objects",         type=int,   default=None, help="Number of tables + indexes (optional). Sets a schema-driven tablet floor; data-driven tablets are always included.")
    parser.add_argument("--tablets-per-table",   type=int,   default=None, help="Tablets per table/index before RF (default: YSQL 1 = no pre-split; YCQL 1 per tserver)")
    parser.add_argument("--tablet-vcpu-per-1000", type=float, default=None, help=f"vCPU overhead per 1,000 tablets, cluster-wide (default: {DEFAULTS['tablet_vcpu_per_1000']})")
    parser.add_argument("--tablet-mem-mb-per-1000", type=float, default=None, help=f"RAM (MB) overhead per 1,000 tablets, cluster-wide (default: {DEFAULTS['tablet_mem_mb_per_1000']})")

    parser.add_argument("--ttl-days",            type=float, default=None, help="YCQL only: row/table TTL in days; caps storage growth at the TTL steady state")

    # CPU model
    parser.add_argument("--cpu-model",           choices=CPU_MODELS, default="per-op",
                        help="per-op (default): measured CPU per operation; latency: legacy ops × exec time")
    parser.add_argument("--workload",            default=None,
                        help="Workload profile — YSQL: kv | oltp | complex | analytics; YCQL: point | range | lwt "
                             "(default: auto from the read/write mix)")
    parser.add_argument("--cpu-ms-per-read",     type=float, default=None,
                        help=f"Override CPU ms per read (default: YSQL {CPU_COST_PROFILES['ysql']['read']}, YCQL {CPU_COST_PROFILES['ycql']['read']})")
    parser.add_argument("--cpu-ms-per-write",    type=float, default=None,
                        help=f"Override CPU ms per write at RF=3 (default: YSQL {CPU_COST_PROFILES['ysql']['write']}, YCQL {CPU_COST_PROFILES['ycql']['write']})")
    parser.add_argument("--cpu-arch",            choices=sorted(CPU_ARCH_FACTORS), default="x86",
                        help="CPU architecture: x86 (default) or arm (Graviton: ×1.10 CPU)")
    parser.add_argument("--cpu-cost-scale",      type=float, default=None,
                        help="Scale CPU cost per op for the target hardware: 1.0 current gen (m7i/m8i/m8g, default), "
                             "~1.15 previous gen (m6i/c6i/m6g), ~1.5 older (m5/c5/i3)")

    # Read offload and multi-region
    parser.add_argument("--follower-read-pct",   type=float, default=None, help="%% of reads served as follower reads (read-only, bounded staleness)")
    parser.add_argument("--read-replica-read-pct", type=float, default=None, help="%% of reads served by a read-replica cluster (sized separately)")
    parser.add_argument("--read-replica-rf",     type=int,   default=None, help="Copies of the data in the read-replica cluster (default 1)")
    parser.add_argument("--read-replica-vcpu",   type=int,   default=None, help="vCPU per read-replica node (default: --vcpu-per-node)")
    parser.add_argument("--regions",             type=int,   default=None, help="1 (default) or RF: one replica per region (synchronous multi-region cluster)")
    parser.add_argument("--region-rtt-ms",       type=float, default=None, help=f"Cross-region round trip in ms (default {DEFAULTS['region_rtt_ms']})")
    parser.add_argument("--preferred-region",    action="store_true", default=False, help="Pin all tablet leaders to one region")
    parser.add_argument("--write-pipelining",    action="store_true", default=False,
                        help="YSQL: ysql_enable_write_pipelining — writes replicate in the background; ~2 round trips per transaction")
    parser.add_argument("--cross-region-cost-per-gb", type=float, default=None, help=f"Inter-region transfer price per GB (default {DEFAULTS['cross_region_cost_per_gb']})")

    # Failure-aware sizing and vCPU tier comparison
    parser.add_argument("--size-for",            choices=SIZE_FOR, default="normal",
                        help="Add nodes until this scenario stays within --failure-cpu-target: normal (default), node or zone failure")
    parser.add_argument("--failure-cpu-target",  type=float, default=None, help="CPU ceiling under failure (default: same as --target-cpu-util)")
    parser.add_argument("--compare",             type=str,   default=None, help="Comma-separated vCPU/node tiers to compare, e.g. 8,16,32 (replaces --vcpu-per-node)")

    # Calibration from an existing cluster / PoC running the same workload mix
    parser.add_argument("--observed-qps",        type=float, default=None, help="Calibration: QPS observed on an existing cluster with the same read/write mix")
    parser.add_argument("--observed-cpu-pct",    type=float, default=None, help="Calibration: average CPU %% observed at that QPS")
    parser.add_argument("--observed-nodes",      type=int,   default=None, help="Calibration: node count of the observed cluster (default: RF)")
    parser.add_argument("--observed-vcpu-per-node", type=int, default=None, help="Calibration: vCPU per node of the observed cluster (default: --vcpu-per-node; required with --compare)")

    parser.add_argument("--format", choices=("text", "html", "json"), default="text",
                        help="Output format: text (default), html (self-contained report page) or json")
    parser.add_argument("--json", action="store_true", help="Alias for --format json")

    args = parser.parse_args()

    if abs(args.write_pct + args.read_pct - 100) > 0.01:
        print(f"ERROR: write-pct ({args.write_pct}) + read-pct ({args.read_pct}) must equal 100", file=sys.stderr)
        sys.exit(1)
    if args.rf not in VALID_RF:
        print(f"ERROR: --rf must be one of {VALID_RF} (odd, max 7); got {args.rf}", file=sys.stderr)
        sys.exit(1)
    if args.rf == 1:
        print("WARNING: RF=1 has no fault tolerance — do not use for production.", file=sys.stderr)
    if args.api == "ycql" and args.cdc:
        print("ERROR: CDC is not supported for YCQL; drop --cdc (xCluster is supported)", file=sys.stderr)
        sys.exit(1)
    if args.ttl_days is not None and args.api != "ycql":
        print("ERROR: --ttl-days applies to --api ycql only", file=sys.stderr)
        sys.exit(1)
    tiers = None
    if args.compare:
        try:
            tiers = sorted({int(v) for v in args.compare.split(",") if v.strip()})
        except ValueError:
            print(f"ERROR: --compare expects comma-separated integers, got {args.compare!r}", file=sys.stderr)
            sys.exit(1)
        if args.vcpu_per_node is not None:
            print("ERROR: use either --vcpu-per-node or --compare, not both", file=sys.stderr)
            sys.exit(1)
    elif args.vcpu_per_node is None:
        print("ERROR: --vcpu-per-node is required (or use --compare 8,16,32)", file=sys.stderr)
        sys.exit(1)
    if tiers and args.observed_qps is not None and args.observed_vcpu_per_node is None:
        print("ERROR: with --compare, pass --observed-vcpu-per-node (the observed cluster's shape is fixed)", file=sys.stderr)
        sys.exit(1)

    kwargs = dict(
        qps=args.qps,
        write_pct=args.write_pct,
        read_pct=args.read_pct,
        avg_exec_ms=args.avg_exec_ms,
        rf=args.rf,
        table_size_gb=args.table_size_gb,
        avg_row_bytes=args.avg_row_bytes,
        growth_rate_pct=args.growth_rate_pct,
        max_storage_per_node_gb=args.max_storage_per_node_gb,
        rpc_overhead=args.rpc_overhead,
        index_overhead=args.index_overhead,
        compression_ratio=args.compression_ratio,
        wal_overhead=args.wal_overhead,
        wal_retention_secs=args.wal_retention_secs,
        cdc_wal_retention_secs=args.cdc_wal_retention_secs,
        xcluster_wal_retention_secs=args.xcluster_wal_retention_secs,
        compaction_reserve=args.compaction_reserve,
        target_cpu_util=args.target_cpu_util,
        conn_per_vcpu=args.conn_per_vcpu,
        mem_mb_per_conn=args.mem_mb_per_conn,
        conn_cpu_overhead=args.conn_cpu_overhead,
        iops_per_replica_write=args.iops_per_replica_write,
        disk_write_amp=args.disk_write_amp,
        net_write_factor=args.net_write_factor,
        net_read_factor=args.net_read_factor,
        disk_iops=args.disk_iops,
        disk_mibps=args.disk_mibps,
        zones=args.zones,
        cross_az_cost_per_gb=args.cross_az_cost_per_gb,
        read_cache_miss=args.read_cache_miss,
        ram_tier_tolerance=args.ram_tier_tolerance,
        cdc_enabled=args.cdc,
        cdc_overhead=args.cdc_overhead,
        xcluster_enabled=args.xcluster,
        xcluster_overhead=args.xcluster_overhead,
        num_objects=args.num_objects,
        tablets_per_table=args.tablets_per_table,
        tablet_vcpu_per_1000=args.tablet_vcpu_per_1000,
        tablet_mem_mb_per_1000=args.tablet_mem_mb_per_1000,
        api=args.api,
        ttl_days=args.ttl_days,
        size_for=args.size_for,
        failure_cpu_target=args.failure_cpu_target,
        observed_qps=args.observed_qps,
        observed_cpu_pct=args.observed_cpu_pct,
        observed_nodes=args.observed_nodes,
        observed_vcpu_per_node=args.observed_vcpu_per_node,
        connection_manager=args.connection_manager,
        cm_client_ratio=args.cm_client_ratio,
        cpu_model=args.cpu_model,
        workload=args.workload,
        cpu_ms_per_read=args.cpu_ms_per_read,
        cpu_ms_per_write=args.cpu_ms_per_write,
        cpu_arch=args.cpu_arch,
        cpu_cost_scale=args.cpu_cost_scale,
        tps=args.tps,
        statements_per_txn=args.statements_per_txn,
        distributed_txn_pct=args.distributed_txn_pct,
        follower_read_pct=args.follower_read_pct,
        read_replica_read_pct=args.read_replica_read_pct,
        read_replica_rf=args.read_replica_rf,
        read_replica_vcpu=args.read_replica_vcpu,
        regions=args.regions,
        region_rtt_ms=args.region_rtt_ms,
        preferred_region=args.preferred_region,
        cross_region_cost_per_gb=args.cross_region_cost_per_gb,
        write_pipelining=args.write_pipelining,
    )

    try:
        if tiers:
            comparison = compare_vcpu_tiers(tiers, **kwargs)
            result = next(r for r in comparison["results"]
                          if r["cluster"]["vcpu_per_node"] == comparison["recommended_vcpu_per_node"])
        else:
            comparison = None
            result = calculate(vcpu_per_node=args.vcpu_per_node, **kwargs)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    output_format = "json" if args.json else args.format
    if output_format == "json":
        print(json.dumps(comparison or result, indent=2))
    elif output_format == "html":
        print(format_html(result, comparison))
    else:
        if comparison:
            print(format_comparison(comparison))
            print()
        print(format_report(result))


if __name__ == "__main__":
    main()