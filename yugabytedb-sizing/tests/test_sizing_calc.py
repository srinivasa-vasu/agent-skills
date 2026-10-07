"""
Regression tests for scripts/sizing_calc.py (stdlib unittest, no dependencies).

Run from the skill directory:
    python3 -m unittest discover -s tests -v

Golden files in tests/golden/ pin the full JSON result of reference scenarios.
After an intentional model change, regenerate them and review the diff:
    UPDATE_GOLDEN=1 python3 -m unittest discover -s tests
    git diff tests/golden/
"""

import json
import os
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "scripts" / "sizing_calc.py"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"

sys.dont_write_bytecode = True   # keep scripts/ free of __pycache__
sys.path.insert(0, str(SCRIPT.parent))
import sizing_calc  # noqa: E402

# name -> CLI args. These double as the documented reference examples.
SCENARIOS = {
    "ysql_reference": "--qps 10000 --write-pct 30 --read-pct 70 --avg-exec-ms 5 --workload oltp "
                      "--vcpu-per-node 16 --rf 3 --table-size-gb 500",
    "ysql_oltp_write_heavy": "--qps 15000 --write-pct 55 --read-pct 45 --avg-exec-ms 1.5 --workload oltp "
                             "--vcpu-per-node 16 --rf 3 --table-size-gb 350 --zones 1",
    "ysql_point_reads": "--qps 50000 --write-pct 0 --read-pct 100 --avg-exec-ms 0.6 --workload kv "
                        "--vcpu-per-node 8 --rf 3 --table-size-gb 30 --zones 1 --target-cpu-util 0.80",
    "ycql_nvme": "--api ycql --qps 100000 --write-pct 30 --read-pct 70 --workload point --vcpu-per-node 16 "
                 "--rf 3 --table-size-gb 500 --disk-iops 200000 --disk-mibps 4000",
    "ysql_legacy_latency": "--qps 10000 --write-pct 30 --read-pct 70 --avg-exec-ms 5 --cpu-model latency "
                           "--vcpu-per-node 16 --rf 3 --table-size-gb 500",
    "ysql_features":  "--qps 50000 --write-pct 40 --read-pct 60 --vcpu-per-node 32 --rf 3 "
                      "--table-size-gb 2000 --cdc --xcluster --num-objects 1000",
    "ysql_storage_cap": "--qps 5000 --write-pct 20 --read-pct 80 --avg-exec-ms 3 "
                        "--vcpu-per-node 8 --rf 3 --table-size-gb 200000",
    "ycql_reference": "--api ycql --qps 100000 --write-pct 30 --read-pct 70 --avg-exec-ms 1 --workload point "
                      "--vcpu-per-node 16 --rf 3 --table-size-gb 500",
    "ycql_tablets_ttl": "--api ycql --qps 100000 --write-pct 40 --read-pct 60 --vcpu-per-node 8 "
                        "--rf 3 --table-size-gb 3000 --num-objects 100 --ttl-days 90 --xcluster",
    "ycql_small_cores": "--api ycql --qps 2000 --write-pct 50 --read-pct 50 --vcpu-per-node 4 "
                        "--rf 5 --table-size-gb 50 --num-objects 20",
    "ysql_size_for_zone": "--qps 10000 --write-pct 30 --read-pct 70 --avg-exec-ms 5 "
                          "--vcpu-per-node 16 --rf 3 --table-size-gb 500 --size-for zone",
    "ysql_calibrated": "--qps 20000 --write-pct 30 --read-pct 70 --vcpu-per-node 16 --rf 3 --table-size-gb 500 "
                       "--observed-qps 8000 --observed-cpu-pct 40 --observed-nodes 6 --observed-vcpu-per-node 8",
    "ycql_compare_zone": "--api ycql --qps 80000 --write-pct 40 --read-pct 60 --avg-exec-ms 1 --rf 3 "
                         "--table-size-gb 3000 --compare 8,16,32 --size-for zone",
    "ysql_conn_mgr": "--qps 10000 --write-pct 30 --read-pct 70 --avg-exec-ms 5 "
                     "--vcpu-per-node 16 --rf 3 --table-size-gb 500 --connection-manager",
    "ysql_txn_mix": "--tps 3000 --statements-per-txn 6 --write-pct 30 --read-pct 70 --avg-exec-ms 2 --workload kv "
                    "--vcpu-per-node 16 --rf 3 --table-size-gb 500",
    "ysql_read_offload": "--qps 20000 --write-pct 30 --read-pct 70 --workload oltp --vcpu-per-node 16 --rf 3 "
                         "--table-size-gb 500 --follower-read-pct 40 --read-replica-read-pct 20",
    "ysql_multi_region_preferred": "--qps 20000 --write-pct 30 --read-pct 70 --avg-exec-ms 2 --workload oltp "
                                   "--vcpu-per-node 16 --rf 3 --table-size-gb 500 --regions 3 --preferred-region "
                                   "--size-for zone",
    "ysql_htap_replica": "--qps 20000 --write-pct 30 --read-pct 70 --workload htap --analytics-qps 5 "
                         "--analytics-rows 2000000 --analytics-target read-replica --vcpu-per-node 16 --rf 3 "
                         "--table-size-gb 500",
    "ysql_legacy_wal": "--qps 10000 --write-pct 30 --read-pct 70 --avg-exec-ms 5 --cpu-model latency "
                       "--vcpu-per-node 16 --rf 3 --table-size-gb 500 --wal-overhead 0.10",
}


def run_cli(args, check=True):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args.split()],
        capture_output=True, text=True, check=check,
    )


def run_json(args):
    return json.loads(run_cli(args + " --format json").stdout)


class GoldenTests(unittest.TestCase):
    """Full-result snapshots: any change to the model shows up as a diff here."""

    def test_scenarios_match_golden(self):
        GOLDEN_DIR.mkdir(exist_ok=True)
        for name, args in SCENARIOS.items():
            with self.subTest(scenario=name):
                result = run_json(args)
                path = GOLDEN_DIR / f"{name}.json"
                if UPDATE_GOLDEN or not path.exists():
                    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
                    continue
                expected = json.loads(path.read_text())
                self.assertEqual(
                    expected, result,
                    f"{name} differs from golden; if intentional, rerun with UPDATE_GOLDEN=1",
                )


class CalibrationTests(unittest.TestCase):
    """Behaviour of the calibrated CPU, cache, I/O and network constants, plus a reproduction of
    the public YCQL key-value benchmark from the YugabyteDB docs."""

    OLTP = dict(qps=15000, write_pct=55, read_pct=45, avg_exec_ms=1.5, workload="oltp",
                vcpu_per_node=16, rf=3, table_size_gb=350, zones=1)
    POINT_READS = dict(qps=50000, write_pct=0, read_pct=100, avg_exec_ms=0.6, workload="kv",
                       vcpu_per_node=8, rf=3, table_size_gb=30, zones=1, target_cpu_util=0.80)

    def test_arm_costs_ten_percent_more_cpu(self):
        x86 = sizing_calc.calculate(**self.OLTP)["workload"]["cpu_seconds_needed"]
        arm = sizing_calc.calculate(**self.OLTP, cpu_arch="arm")["workload"]["cpu_seconds_needed"]
        self.assertAlmostEqual(arm / x86, 1.10, places=3)

    def test_cached_point_reads_need_no_read_iops(self):
        r = sizing_calc.calculate(**self.POINT_READS)
        self.assertEqual(r["iops"]["read_cache_miss_pct"], 0)        # leader data fits half the RAM
        self.assertEqual(r["iops"]["total_iops_per_node"], 0)
        self.assertEqual(r["memory"]["base_mem_ratio"], "1:4")

    def test_reads_move_fewer_bytes_than_replicated_writes(self):
        reads = sizing_calc.calculate(**dict(self.OLTP, write_pct=0, read_pct=100))["network"]
        writes = sizing_calc.calculate(**dict(self.OLTP, write_pct=100, read_pct=0))["network"]
        self.assertLess(reads["total_net_mbps_per_node"], writes["total_net_mbps_per_node"])

    def test_ycql_kv_benchmark_reproduction(self):
        # Public YCQL key-value benchmark (docs): 150k reads/s and 90k writes/s at 60% CPU on
        # 3 × 16 cores (2016-era i3.4xlarge, NVMe). Undo the hardware-generation factor.
        old_hw = 1 / sizing_calc.YCQL_HW_GENERATION_FACTOR
        for qps, write_pct in ((150000, 0), (90000, 100)):
            with self.subTest(write_pct=write_pct):
                r = sizing_calc.calculate(api="ycql", qps=qps, write_pct=write_pct, read_pct=100 - write_pct,
                                          avg_exec_ms=None, workload="point", vcpu_per_node=16, rf=3,
                                          table_size_gb=100, cpu_cost_scale=old_hw,
                                          disk_iops=10 ** 6, disk_mibps=10 ** 5)
                self.assertEqual(r["cluster"]["total_nodes"], 3)
                self.assertAlmostEqual(r["cluster"]["cpu_utilization_pct"], 60, delta=1.0)

    def test_ycql_defaults_scaled_for_current_hardware(self):
        cost = sizing_calc.CPU_COST_PROFILES["ycql"]
        self.assertAlmostEqual(cost["read"], 0.178 * 0.6, places=4)
        self.assertAlmostEqual(cost["write"], 0.296 * 0.6, places=4)

    def test_cpu_cost_scale(self):
        a = sizing_calc.calculate(**self.OLTP)["workload"]["cpu_seconds_needed"]
        b = sizing_calc.calculate(**self.OLTP, cpu_cost_scale=1.5)["workload"]["cpu_seconds_needed"]
        self.assertAlmostEqual(b / a, 1.5, places=3)

    def test_concurrency_is_littles_law(self):
        cc = sizing_calc.calculate(**self.OLTP)["concurrency"]
        self.assertAlmostEqual(cc["in_flight_total"], 15000 * 1.5 / 1000, delta=0.1)
        self.assertFalse(cc["connection_bound"])


class ReferenceExampleTests(unittest.TestCase):
    """Numbers quoted in SKILL.md's worked examples."""

    def test_ysql_reference_example(self):
        r = run_json(SCENARIOS["ysql_reference"])
        self.assertEqual(r["parameters"]["rpc_multiplier"], 1.15)
        self.assertEqual(r["cluster"]["total_nodes"], 3)
        self.assertEqual(r["cluster"]["cpu_utilization_pct"], 34.1)
        self.assertEqual(r["memory"]["mem_per_node_gb"], 128)
        self.assertEqual(r["failure_scenarios"]["zone_failure"]["cpu_util_pct"], 49.5)

    def test_ycql_reference_example(self):
        r = run_json(SCENARIOS["ycql_reference"])
        self.assertEqual(r["parameters"]["rpc_multiplier"], 1.08)
        # CPU fits 3 nodes, but gp3's 16,000 IOPS doesn't: the disk limit adds 3 nodes
        self.assertEqual(r["cluster"]["total_nodes"], 6)
        self.assertEqual(r["iops"]["disk_nodes_added"], 3)
        self.assertEqual(r["cluster"]["cpu_utilization_pct"], 14.5)
        self.assertEqual(r["cluster"]["conn_per_node"], 0)
        self.assertEqual(r["memory"]["mem_per_node_gb"], 128)
        self.assertEqual(r["iops"]["total_iops_per_node"], 3705)

    def test_ycql_nvme_stays_at_three_nodes(self):
        r = run_json(SCENARIOS["ycql_nvme"])
        self.assertEqual(r["cluster"]["total_nodes"], 3)
        self.assertEqual(r["iops"]["disk_nodes_added"], 0)

    def test_legacy_latency_model_unchanged(self):
        # The pre-per-op model, kept behind --cpu-model latency
        r = run_json(SCENARIOS["ysql_legacy_latency"])
        self.assertEqual(r["workload"]["total_eff_ops_per_s"], 18400)
        self.assertEqual([it["nodes"] for it in r["sizing_iterations"]], [9, 12])
        self.assertEqual(r["cluster"]["cpu_utilization_pct"], 51.1)
        self.assertEqual(r["failure_scenarios"]["zone_failure"]["cpu_util_pct"], 75.1)


class ModelBehaviourTests(unittest.TestCase):

    def base(self, **kw):
        args = dict(qps=10000, write_pct=30, read_pct=70, avg_exec_ms=5,
                    vcpu_per_node=16, rf=3, table_size_gb=500)
        args.update(kw)
        return sizing_calc.calculate(**args)

    def test_nodes_are_rf_multiples(self):
        for rf in (3, 5, 7):
            with self.subTest(rf=rf):
                self.assertEqual(self.base(rf=rf)["cluster"]["total_nodes"] % rf, 0)

    def test_cpu_within_target(self):
        r = self.base()
        self.assertLessEqual(r["cluster"]["cpu_utilization_pct"], 65)
        self.assertTrue(r["cluster"]["cpu_within_target"])

    def test_ycql_has_no_connection_overhead(self):
        r = self.base(api="ycql")
        self.assertEqual(r["cluster"]["conn_cpu_per_node"], 0)
        self.assertEqual(r["memory"]["conn_mem_gb"], 0)

    def test_ycql_tablets_scale_with_nodes(self):
        r = self.base(api="ycql", num_objects=10, qps=200000, avg_exec_ms=1)
        t = r["tablets"]
        self.assertEqual(t["tablets_per_table"], r["cluster"]["total_nodes"])

    def test_explicit_override_beats_api_profile(self):
        r = self.base(api="ycql", rpc_overhead=0.30)
        self.assertEqual(r["parameters"]["rpc_multiplier"], 1.3)

    def test_ram_tier_tolerance(self):
        self.assertEqual(sizing_calc.round_up_to_ram_tier(128.1, 0.02), 128)
        self.assertEqual(sizing_calc.round_up_to_ram_tier(128.1, 0.0), 192)
        self.assertEqual(sizing_calc.round_up_to_ram_tier(131.0, 0.02), 192)

    def test_ttl_caps_growth(self):
        capped = self.base(api="ycql", table_size_gb=300, ttl_days=3)
        uncapped = self.base(api="ycql", table_size_gb=300)
        self.assertTrue(capped["ttl"]["growth_capped"])
        self.assertLess(capped["storage"]["storage_per_node_2yr_gb"],
                        uncapped["storage"]["storage_per_node_2yr_gb"])

    def test_input_source_labels(self):
        r = self.base(avg_row_bytes=512)
        self.assertEqual(r["inputs"]["avg_row_bytes_source"], "provided")
        self.assertEqual(r["inputs"]["growth_rate_source"], "default")

    def test_exec_time_auto_estimate_legacy_model(self):
        legacy = dict(avg_exec_ms=None, write_pct=80, read_pct=20, cpu_model="latency")
        self.assertEqual(self.base(**legacy)["inputs"]["avg_exec_ms"], 2)
        self.assertEqual(self.base(api="ycql", **legacy)["inputs"]["avg_exec_ms"], 0.75)

    def test_workload_auto_selection(self):
        pick = lambda **kw: self.base(**kw)["inputs"]["workload"]
        self.assertEqual(pick(write_pct=80, read_pct=20), "kv")
        self.assertEqual(pick(write_pct=50, read_pct=50), "oltp")
        self.assertEqual(pick(write_pct=30, read_pct=70), "complex")
        self.assertEqual(pick(write_pct=5, read_pct=95), "analytics")
        self.assertEqual(pick(api="ycql", write_pct=50, read_pct=50), "point")
        self.assertEqual(self.base(workload="oltp")["inputs"]["workload_source"], "provided")

    def test_per_op_cpu_scales_with_workload_and_arch(self):
        oltp = self.base(workload="oltp")["workload"]["cpu_seconds_needed"]
        self.assertAlmostEqual(self.base(workload="complex")["workload"]["cpu_seconds_needed"], oltp * 2, delta=0.02)
        self.assertAlmostEqual(self.base(workload="oltp", cpu_arch="arm")["workload"]["cpu_seconds_needed"],
                               oltp * 1.1, delta=0.02)

    def test_rf_scales_write_cost_sublinearly(self):
        r3 = self.base(workload="oltp", write_pct=100, read_pct=0)["workload"]["cpu_seconds_needed"]
        r5 = self.base(workload="oltp", write_pct=100, read_pct=0, rf=5)["workload"]["cpu_seconds_needed"]
        self.assertAlmostEqual(r5 / r3, (1 + 4 * 0.333) / (1 + 2 * 0.333), places=3)   # ≈1.40, not 5/3

    def test_latency_does_not_change_per_op_cpu(self):
        a = self.base(workload="oltp", avg_exec_ms=1)["workload"]["cpu_seconds_needed"]
        b = self.base(workload="oltp", avg_exec_ms=50)["workload"]["cpu_seconds_needed"]
        self.assertEqual(a, b)

    def test_connection_memory_uses_postgres_share(self):
        r = self.base(workload="oltp")                           # 256 × 15 MB = 3.75 GB ≪ 27% of 128 GB
        self.assertEqual(r["memory"]["conn_extra_gb"], 0)
        heavy = self.base(workload="oltp", mem_mb_per_conn=200)  # 256 × 200 MB = 50 GB > 34.6 GB share
        self.assertAlmostEqual(heavy["memory"]["conn_extra_gb"], 50 - 34.56, delta=0.1)
        self.assertEqual(self.base(api="ycql")["memory"]["pg_budget_gb"], 0)

    def test_connection_bound_flag(self):
        cc = self.base(workload="oltp", qps=50000, avg_exec_ms=100)["concurrency"]
        self.assertTrue(cc["connection_bound"])                  # 5,000 in flight > backends
        self.assertIsNone(self.base(avg_exec_ms=None)["concurrency"])

    def test_invalid_workload_rejected(self):
        with self.assertRaises(ValueError):
            self.base(api="ycql", workload="oltp")


class CalibrationTests(unittest.TestCase):
    MIX = dict(write_pct=30, read_pct=70, rf=3, table_size_gb=500)

    def test_round_trip_recovers_model(self):
        # Size a cluster, then "observe" it at the resulting CPU: calibration must reproduce
        # the same workload CPU (factor ≈ 1.0 vs the profile) and the same node count.
        sized = sizing_calc.calculate(qps=10000, avg_exec_ms=None, workload="oltp", vcpu_per_node=16, **self.MIX)
        c = sized["cluster"]
        cal = sizing_calc.calculate(
            qps=10000, avg_exec_ms=None, workload="oltp", vcpu_per_node=16, **self.MIX,
            observed_qps=10000, observed_cpu_pct=c["cpu_utilization_pct"],
            observed_nodes=c["total_nodes"], observed_vcpu_per_node=16,
        )
        self.assertAlmostEqual(cal["calibration"]["factor_vs_model"], 1.0, delta=0.01)
        self.assertAlmostEqual(cal["workload"]["cpu_seconds_needed"], sized["workload"]["cpu_seconds_needed"],
                               delta=0.1)
        self.assertEqual(cal["cluster"]["total_nodes"], c["total_nodes"])
        self.assertTrue(cal["inputs"]["exec_time_calibrated"])

    def test_latency_allowed_with_calibration(self):
        r = sizing_calc.calculate(qps=10000, avg_exec_ms=2, vcpu_per_node=16, **self.MIX,
                                  observed_qps=5000, observed_cpu_pct=30)
        self.assertIsNotNone(r["concurrency"])
        self.assertIsNotNone(r["calibration"])

    def test_manual_derivation(self):
        r = sizing_calc.calculate(
            qps=20000, avg_exec_ms=None, vcpu_per_node=16, **self.MIX,
            observed_qps=8000, observed_cpu_pct=40, observed_nodes=6, observed_vcpu_per_node=8,
        )
        busy = 0.40 * 6 * 8
        conn = 16 * 8 * 0.002 * 6
        tablets = sizing_calc.data_driven_tablets(500 * 1.2 * 0.7, 6) * 3 / 1000 * 0.4
        self.assertAlmostEqual(r["calibration"]["cpu_ms_per_op"], (busy - conn - tablets) / 8000 * 1000, places=3)
        self.assertAlmostEqual(r["workload"]["cpu_seconds_needed"], (busy - conn - tablets) * 20000 / 8000, delta=0.01)

    def test_observed_shape_defaults(self):
        # Only QPS + CPU% given: observed cluster = RF nodes × target vCPU/node
        r = sizing_calc.calculate(qps=20000, avg_exec_ms=None, vcpu_per_node=16, **self.MIX,
                                  observed_qps=8000, observed_cpu_pct=40)
        cal = r["calibration"]
        self.assertEqual((cal["observed_nodes"], cal["observed_vcpu_per_node"]), (3, 16))
        self.assertTrue(cal["observed_nodes_source"].startswith("default"))
        explicit = sizing_calc.calculate(qps=20000, avg_exec_ms=None, vcpu_per_node=16, **self.MIX,
                                         observed_qps=8000, observed_cpu_pct=40,
                                         observed_nodes=3, observed_vcpu_per_node=16)
        self.assertEqual(cal["cpu_ms_per_op"], explicit["calibration"]["cpu_ms_per_op"])

    def test_cli_compare_requires_observed_vcpu(self):
        p = run_cli("--qps 20000 --write-pct 30 --read-pct 70 --rf 3 --table-size-gb 500 "
                    "--compare 8,16 --observed-qps 8000 --observed-cpu-pct 40", check=False)
        self.assertIn("--observed-vcpu-per-node", p.stderr)

    def test_partial_observed_inputs_rejected(self):
        with self.assertRaises(ValueError):
            sizing_calc.calculate(qps=1000, avg_exec_ms=None, vcpu_per_node=8, **self.MIX, observed_qps=500)
        with self.assertRaises(ValueError):
            sizing_calc.calculate(qps=1000, avg_exec_ms=None, vcpu_per_node=8, **self.MIX, observed_nodes=3)


class ConnectionManagerTests(unittest.TestCase):
    BASE = dict(qps=10000, write_pct=30, read_pct=70, avg_exec_ms=5, vcpu_per_node=16, rf=3, table_size_gb=500)

    def test_backend_pool_and_memory(self):
        direct = sizing_calc.calculate(**self.BASE)
        cm = sizing_calc.calculate(**self.BASE, connection_manager=True)
        self.assertEqual(direct["cluster"]["conn_per_node"], 256)
        self.assertEqual(cm["cluster"]["conn_per_node"], 160)              # 10 backends/vCPU
        self.assertEqual(cm["cluster"]["client_conn_per_node"], 1600)      # 10 clients/backend
        self.assertEqual(cm["memory"]["conn_mem_gb"], round(160 * 15 / 1024, 1))
        self.assertAlmostEqual(cm["memory"]["conn_mgr_mem_gb"], 200 / 1024, places=2)
        # Connection memory fits in the PostgreSQL share either way, so CM only adds odyssey RAM
        self.assertLess(cm["memory"]["conn_mem_gb"], direct["memory"]["conn_mem_gb"])
        self.assertAlmostEqual(cm["memory"]["raw_mem_per_node_gb"] - direct["memory"]["raw_mem_per_node_gb"],
                               200 / 1024, delta=0.1)
        self.assertLess(cm["cluster"]["conn_cpu_per_node"], direct["cluster"]["conn_cpu_per_node"])

    def test_overrides(self):
        r = sizing_calc.calculate(**self.BASE, connection_manager=True, conn_per_vcpu=15, cm_client_ratio=20)
        self.assertEqual(r["cluster"]["conn_per_node"], 240)
        self.assertEqual(r["cluster"]["client_conn_per_node"], 4800)

    def test_ycql_rejected(self):
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**self.BASE, api="ycql", connection_manager=True)


class FailureSizingTests(unittest.TestCase):
    # Pinned to the legacy CPU model: these test the sizing loop, not CPU constants
    BASE = dict(qps=10000, write_pct=30, read_pct=70, avg_exec_ms=5, vcpu_per_node=16, rf=3, table_size_gb=500,
                cpu_model="latency")

    def test_size_for_zone_keeps_zone_loss_within_target(self):
        normal = sizing_calc.calculate(**self.BASE)
        zone = sizing_calc.calculate(**self.BASE, size_for="zone")
        self.assertTrue(normal["failure_scenarios"]["zone_failure"]["exceeds_target"])
        self.assertLessEqual(zone["failure_scenarios"]["zone_failure"]["cpu_util_pct"], 65)
        self.assertEqual(zone["cluster"]["total_nodes"],
                         normal["cluster"]["total_nodes"] + zone["failure_scenarios"]["failure_nodes_added"])
        self.assertEqual(zone["cluster"]["total_nodes"] % 3, 0)

    def test_custom_failure_ceiling(self):
        r = sizing_calc.calculate(**self.BASE, size_for="zone", failure_cpu_target=0.80)
        self.assertEqual(r["failure_scenarios"]["failure_nodes_added"], 0)   # 75.1% ≤ 80%
        self.assertFalse(r["failure_scenarios"]["zone_failure"]["exceeds_target"])

    def test_size_for_node(self):
        r = sizing_calc.calculate(**dict(self.BASE, qps=12000), size_for="node")
        self.assertLessEqual(r["failure_scenarios"]["node_failure"]["cpu_util_pct"], 65)

    def test_size_for_needs_rf3(self):
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**dict(self.BASE, rf=1), size_for="zone")


class CompareTests(unittest.TestCase):
    BASE = dict(qps=10000, write_pct=30, read_pct=70, avg_exec_ms=5, rf=3, table_size_gb=500, cpu_model="latency")

    def test_compare_rows_and_recommendation(self):
        cmp = sizing_calc.compare_vcpu_tiers([4, 8, 16, 32, 64], size_for="zone", **self.BASE)
        self.assertEqual([x["vcpu_per_node"] for x in cmp["comparison"]], [4, 8, 16, 32, 64])
        self.assertEqual(sum(x["recommended"] for x in cmp["comparison"]), 1)
        # 4 vCPU has the lowest total vCPU (228); 8 and 16 are within 10% → fewest nodes wins
        self.assertEqual(cmp["recommended_vcpu_per_node"], 16)

    def test_cli_compare_json(self):
        out = run_json("--qps 10000 --write-pct 30 --read-pct 70 --avg-exec-ms 5 --rf 3 "
                       "--table-size-gb 500 --compare 8,16")
        self.assertIn("recommended_vcpu_per_node", out)
        self.assertEqual(len(out["results"]), 2)

    def test_cli_compare_and_vcpu_are_exclusive(self):
        p = run_cli("--qps 1 --write-pct 50 --read-pct 50 --table-size-gb 1 --vcpu-per-node 8 --compare 8,16", check=False)
        self.assertNotEqual(p.returncode, 0)

    def test_cli_needs_vcpu_or_compare(self):
        p = run_cli("--qps 1 --write-pct 50 --read-pct 50 --table-size-gb 1", check=False)
        self.assertIn("--vcpu-per-node is required", p.stderr)


class TabletAndWalTests(unittest.TestCase):

    def test_data_driven_tablet_phases(self):
        f = sizing_calc.data_driven_tablets
        self.assertEqual(f(0.1, 3), 1)            # 100 MB: below the 128 MiB low-phase threshold
        self.assertEqual(f(0.3, 3), 3)            # low phase: 128 MiB splits until 1 tablet/node
        self.assertEqual(f(420, 12), 42)          # high phase: 10 GiB tablets
        self.assertEqual(f(10_000, 3), 100)       # past 24/node → 100 GiB tablets (and ≥ 72)
        self.assertEqual(f(168_000, 30), 1500)    # capped at 50 tablets per tserver

    def test_schema_floor_vs_data(self):
        base = dict(qps=10000, write_pct=30, read_pct=70, avg_exec_ms=5, vcpu_per_node=16, rf=3, table_size_gb=500)
        data_only = sizing_calc.calculate(**base)["tablets"]
        many_objects = sizing_calc.calculate(**base, num_objects=1000)["tablets"]
        self.assertEqual(data_only["tablet_basis"], "data size (auto-split)")
        self.assertEqual(many_objects["tablet_basis"], "schema")
        self.assertEqual(many_objects["total_tablets"], 1000 * 3)

    def test_wal_follows_write_rate_and_replication_retention(self):
        base = dict(qps=10000, write_pct=30, read_pct=70, avg_exec_ms=5, vcpu_per_node=16, rf=3, table_size_gb=500)
        plain = sizing_calc.calculate(**base)
        xcl = sizing_calc.calculate(**base, xcluster_enabled=True)
        cdc = sizing_calc.calculate(**base, cdc_enabled=True)
        both = sizing_calc.calculate(**base, cdc_enabled=True, xcluster_enabled=True)
        self.assertEqual(plain["parameters"]["wal_retention_secs"], 900)
        self.assertEqual(xcl["parameters"]["wal_retention_secs"], 86400)     # 24 h for xCluster
        self.assertEqual(cdc["parameters"]["wal_retention_secs"], 28800)     # 8 h for CDC
        self.assertEqual(both["parameters"]["wal_retention_secs"], 86400)    # longer wins
        expected = 3000 * 3 * 512 * 1.2 * 900 / plain["cluster"]["total_nodes"] / 1024 ** 3
        self.assertAlmostEqual(plain["storage"]["wal_gb_per_node"], expected, places=1)
        self.assertGreater(xcl["storage"]["wal_gb_per_node"], 30 * plain["storage"]["wal_gb_per_node"])

    def test_legacy_wal_percentage(self):
        r = sizing_calc.calculate(qps=10000, write_pct=30, read_pct=70, avg_exec_ms=5, cpu_model="latency",
                                  vcpu_per_node=16, rf=3, table_size_gb=500, wal_overhead=0.10)
        self.assertEqual(r["storage"]["storage_per_node_gb"], 136.5)   # pre-change model
        self.assertIsNone(r["parameters"]["wal_retention_secs"])

    def test_wal_not_scaled_by_growth(self):
        r = sizing_calc.calculate(qps=10000, write_pct=30, read_pct=70, avg_exec_ms=5, vcpu_per_node=16,
                                  rf=3, table_size_gb=500, xcluster_enabled=True)
        s = r["storage"]
        data_now = s["storage_per_node_gb"] - s["wal_gb_per_node"]
        self.assertAlmostEqual(s["storage_per_node_1yr_gb"], data_now * 1.3 + s["wal_gb_per_node"], delta=0.2)


class TopologyAndDiskTests(unittest.TestCase):
    BASE = dict(qps=10000, write_pct=30, read_pct=70, avg_exec_ms=5, workload="oltp",
                vcpu_per_node=16, rf=3, table_size_gb=500)

    def test_multi_az_is_default(self):
        r = sizing_calc.calculate(**self.BASE)
        self.assertEqual(r["inputs"]["zones"], 3)
        self.assertIsNotNone(r["failure_scenarios"]["zone_failure"])
        nw = r["network"]
        self.assertAlmostEqual(nw["cross_az_mbps_per_node"], nw["total_net_mbps_per_node"] * 2 / 3, delta=0.01)
        self.assertGreater(nw["cross_az_cost_per_month"], 0)

    def test_single_az(self):
        r = sizing_calc.calculate(**self.BASE, zones=1)
        self.assertIsNone(r["failure_scenarios"]["zone_failure"])
        self.assertEqual(r["network"]["cross_az_mbps_per_node"], 0)
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**self.BASE, zones=1, size_for="zone")
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**self.BASE, zones=2)

    def test_disk_limit_adds_nodes(self):
        tight = sizing_calc.calculate(**self.BASE, disk_iops=500)
        self.assertGreater(tight["iops"]["disk_nodes_added"], 0)
        self.assertLessEqual(tight["iops"]["total_iops_per_node"], 500)
        self.assertEqual(tight["cluster"]["total_nodes"] % 3, 0)

    def test_read_cache_miss_follows_leader_data(self):
        small = sizing_calc.calculate(**dict(self.BASE, table_size_gb=20))["iops"]
        large = sizing_calc.calculate(**dict(self.BASE, table_size_gb=5000))["iops"]
        self.assertEqual(small["read_cache_miss_pct"], 0)
        self.assertGreater(large["read_cache_miss_pct"], 50)
        fixed = sizing_calc.calculate(**self.BASE, read_cache_miss=0.3)["iops"]
        self.assertEqual(fixed["read_cache_miss_pct"], 30)

    def test_memory_ratio_is_data_aware(self):
        small = sizing_calc.calculate(**dict(self.BASE, table_size_gb=20))["memory"]
        large = sizing_calc.calculate(**self.BASE)["memory"]
        self.assertEqual(small["base_mem_ratio"], "1:4")   # leader data fits a 1:4 node's cache
        self.assertEqual(large["base_mem_ratio"], "1:8")   # 140 GB leader data/node does not

    def test_xcluster_default_overhead(self):
        r = sizing_calc.calculate(**self.BASE, xcluster_enabled=True)
        self.assertEqual(r["parameters"]["xcluster_overhead_pct"], 10)


class TransactionMixTests(unittest.TestCase):
    BASE = dict(write_pct=30, read_pct=70, avg_exec_ms=2, vcpu_per_node=16, rf=3, table_size_gb=500)

    def test_tps_converts_to_statements(self):
        r = sizing_calc.calculate(qps=None, tps=1000, statements_per_txn=5, workload="kv", **self.BASE)
        self.assertEqual(r["inputs"]["qps"], 5000)
        self.assertEqual(r["transactions"]["tps"], 1000)
        with self.assertRaises(ValueError):
            sizing_calc.calculate(qps=5000, tps=1000, **self.BASE)
        with self.assertRaises(ValueError):
            sizing_calc.calculate(qps=None, **self.BASE)

    def test_distributed_default_follows_statement_count(self):
        single = sizing_calc.calculate(qps=1000, workload="kv", **self.BASE)
        multi = sizing_calc.calculate(qps=1000, statements_per_txn=4, workload="kv", **self.BASE)
        self.assertEqual(single["inputs"]["distributed_txn_pct"], 0)
        self.assertEqual(multi["inputs"]["distributed_txn_pct"], 100)
        p_write = 1 - (1 - 0.3) ** 4
        self.assertAlmostEqual(multi["transactions"]["write_txn_pct"], p_write * 100, places=1)

    def test_distributed_single_write_costs_3x_fast_path(self):
        base = dict(self.BASE, write_pct=100, read_pct=0)
        fast = sizing_calc.calculate(qps=1000, workload="kv", **base)["workload"]["cpu_seconds_needed"]
        dist = sizing_calc.calculate(qps=1000, workload="kv", distributed_txn_pct=100,
                                     **base)["workload"]["cpu_seconds_needed"]
        self.assertAlmostEqual(dist / fast, 3.0, places=2)    # commit + intent ≈ 2 extra write units

    def test_overhead_included_in_oltp_profile(self):
        r = sizing_calc.calculate(qps=1000, statements_per_txn=8, workload="oltp", **self.BASE)
        self.assertEqual(r["transactions"]["overhead_cores"], 0)
        self.assertIn("included", r["transactions"]["overhead_basis"])

    def test_cli_tps(self):
        out = run_json("--tps 500 --statements-per-txn 4 --write-pct 50 --read-pct 50 --workload kv "
                       "--vcpu-per-node 8 --rf 3 --table-size-gb 50")
        self.assertEqual(out["inputs"]["qps"], 2000)


class ReadOffloadTests(unittest.TestCase):
    BASE = dict(qps=20000, write_pct=30, read_pct=70, avg_exec_ms=None, workload="oltp",
                vcpu_per_node=16, rf=3, table_size_gb=500)

    def test_follower_reads_move_load_not_cpu(self):
        plain = sizing_calc.calculate(**self.BASE)
        fr = sizing_calc.calculate(**self.BASE, follower_read_pct=50)
        self.assertEqual(fr["workload"]["cpu_seconds_needed"], plain["workload"]["cpu_seconds_needed"])
        self.assertGreater(fr["iops"]["hot_data_gb_per_node"], plain["iops"]["hot_data_gb_per_node"])
        self.assertLess(fr["network"]["cross_az_mbps_per_node"], plain["network"]["cross_az_mbps_per_node"])

    def test_read_replicas_offload_primary(self):
        plain = sizing_calc.calculate(**self.BASE)
        rr = sizing_calc.calculate(**self.BASE, read_replica_read_pct=40, read_replica_rf=3)
        self.assertLess(rr["workload"]["cpu_seconds_needed"], plain["workload"]["cpu_seconds_needed"])
        cluster = rr["read_replica"]
        self.assertEqual(cluster["nodes"] % 3, 0)
        self.assertLessEqual(cluster["cpu_utilization_pct"], 65)
        self.assertGreater(cluster["replication_ingest_mbps"], 0)
        self.assertIsNone(plain["read_replica"])

    def test_offload_bounds(self):
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**self.BASE, follower_read_pct=70, read_replica_read_pct=40)


class MultiRegionTests(unittest.TestCase):
    BASE = dict(qps=20000, write_pct=30, read_pct=70, avg_exec_ms=2, workload="oltp",
                vcpu_per_node=16, rf=3, table_size_gb=500)

    def test_balanced_regions(self):
        r = sizing_calc.calculate(**self.BASE, regions=3)
        self.assertEqual(r["failure_scenarios"]["fault_domain"], "region")
        self.assertEqual(r["network"]["cross_scope"], "region")
        # writes wait for a remote quorum; 2/3 of reads hit a remote leader
        self.assertAlmostEqual(r["multi_region"]["added_latency_per_statement_ms"], 30 * (0.3 + 0.7 * 2 / 3), places=2)

    def test_preferred_region_concentrates_leaders(self):
        balanced = sizing_calc.calculate(**self.BASE, regions=3)
        pinned = sizing_calc.calculate(**self.BASE, regions=3, preferred_region=True)
        mr = pinned["multi_region"]
        self.assertGreater(pinned["cluster"]["total_nodes"], balanced["cluster"]["total_nodes"])
        self.assertGreater(mr["leader_region_cpu_pct"], mr["follower_region_cpu_pct"])
        self.assertLessEqual(mr["leader_region_cpu_pct"], 65)
        # losing a region moves leadership to an equal-sized region: same CPU as normal
        self.assertEqual(pinned["failure_scenarios"]["zone_failure"]["cpu_util_pct"],
                         pinned["cluster"]["cpu_utilization_pct"])
        self.assertAlmostEqual(mr["added_latency_per_statement_ms"], 30 * 0.3, places=2)

    def test_follower_reads_relieve_leader_region(self):
        pinned = sizing_calc.calculate(**self.BASE, regions=3, preferred_region=True)
        fr = sizing_calc.calculate(**self.BASE, regions=3, preferred_region=True, follower_read_pct=60)
        self.assertLess(fr["cluster"]["total_nodes"], pinned["cluster"]["total_nodes"])

    def test_write_pipelining(self):
        txn = dict(self.BASE, qps=None, tps=3000, statements_per_txn=6, workload="kv", regions=3,
                   preferred_region=True)
        off = sizing_calc.calculate(**txn)
        on = sizing_calc.calculate(**txn, write_pipelining=True)
        # without: every write statement + each distributed commit = (0.3 + 0.147) RTT per statement
        p_write = 1 - 0.7 ** 6
        self.assertAlmostEqual(off["multi_region"]["added_latency_per_statement_ms"],
                               30 * (0.3 + p_write / 6), places=2)
        # with: ~2 RTT per distributed write transaction
        self.assertAlmostEqual(on["multi_region"]["added_latency_per_statement_ms"],
                               30 * p_write * 2 / 6, places=2)
        self.assertEqual(off["multi_region"]["added_latency_with_pipelining_ms"],
                         on["multi_region"]["added_latency_per_statement_ms"])
        self.assertEqual(on["workload"]["cpu_seconds_needed"], off["workload"]["cpu_seconds_needed"])
        # autocommit single-row writes still pay one round trip each
        auto = dict(self.BASE, regions=3, preferred_region=True)
        self.assertEqual(sizing_calc.calculate(**auto)["multi_region"]["added_latency_per_statement_ms"],
                         sizing_calc.calculate(**auto, write_pipelining=True)["multi_region"]["added_latency_per_statement_ms"])
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**dict(self.BASE, api="ycql", workload="point"), write_pipelining=True)

    def test_validation(self):
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**self.BASE, regions=2)
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**self.BASE, preferred_region=True)
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**self.BASE, regions=3, zones=1)


class HtapTests(unittest.TestCase):
    BASE = dict(qps=20000, write_pct=30, read_pct=70, avg_exec_ms=None, vcpu_per_node=16, rf=3,
                table_size_gb=500)
    SCANS = dict(workload="htap", analytics_qps=5, analytics_rows=2_000_000)

    def test_defaults_are_flagged(self):
        a = sizing_calc.calculate(**self.BASE, workload="htap")["analytics"]
        self.assertTrue(a["assumed"])
        self.assertAlmostEqual(a["cores"], 1 * 1_000_000 * 2e-6, places=3)

    def test_scan_cores_and_scaling(self):
        a = sizing_calc.calculate(**self.BASE, **self.SCANS)["analytics"]
        self.assertAlmostEqual(a["cores"], 5 * 2_000_000 * 2e-6, places=2)
        self.assertFalse(a["assumed"])
        arm = sizing_calc.calculate(**self.BASE, **self.SCANS, cpu_arch="arm")["analytics"]
        self.assertAlmostEqual(arm["cores"], a["cores"] * 1.10, places=2)

    def test_primary_cpu_includes_scans(self):
        oltp = sizing_calc.calculate(**self.BASE, workload="oltp")["workload"]["cpu_seconds_needed"]
        htap = sizing_calc.calculate(**self.BASE, **self.SCANS)["workload"]["cpu_seconds_needed"]
        self.assertAlmostEqual(htap - oltp, 20.0, places=1)

    def test_read_replica_isolates_analytics(self):
        oltp = sizing_calc.calculate(**self.BASE, workload="oltp")
        rr = sizing_calc.calculate(**self.BASE, **self.SCANS, analytics_target="read-replica")
        self.assertEqual(rr["workload"]["cpu_seconds_needed"], oltp["workload"]["cpu_seconds_needed"])
        self.assertEqual(rr["read_replica"]["analytics_cores"], 20.0)
        self.assertGreater(rr["read_replica"]["scan_mibps_per_node"], 0)
        tight = sizing_calc.calculate(**self.BASE, **self.SCANS, analytics_target="read-replica", disk_mibps=300)
        self.assertGreater(tight["read_replica"]["disk_nodes_added"], 0)
        self.assertLessEqual(tight["read_replica"]["disk_mibps_per_node"], 300)

    def test_followers_spread_cpu_but_widen_cache(self):
        primary = sizing_calc.calculate(**self.BASE, **self.SCANS)
        followers = sizing_calc.calculate(**self.BASE, **self.SCANS, analytics_target="followers")
        self.assertEqual(followers["workload"]["cpu_seconds_needed"], primary["workload"]["cpu_seconds_needed"])
        self.assertGreater(followers["iops"]["scan_mibps_per_node"], primary["iops"]["scan_mibps_per_node"])

    def test_ysql_only(self):
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**self.BASE, api="ycql", workload="htap")
        with self.assertRaises(ValueError):
            sizing_calc.calculate(**self.BASE, api="ycql", analytics_qps=1)


class ValidationTests(unittest.TestCase):
    COMMON = "--qps 1000 --write-pct 50 --read-pct 50 --vcpu-per-node 8 --table-size-gb 10"

    def assert_cli_error(self, extra, needle):
        p = run_cli(f"{self.COMMON} {extra}", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(needle, p.stderr)

    def test_even_rf_rejected(self):
        self.assert_cli_error("--rf 4", "--rf must be one of")

    def test_rf_above_7_rejected(self):
        self.assert_cli_error("--rf 9", "--rf must be one of")

    def test_rf1_warns(self):
        p = run_cli(f"{self.COMMON} --rf 1")
        self.assertIn("RF=1", p.stderr)

    def test_ycql_cdc_rejected(self):
        self.assert_cli_error("--api ycql --cdc", "CDC is not supported for YCQL")

    def test_ttl_requires_ycql(self):
        self.assert_cli_error("--ttl-days 5", "--ttl-days applies to --api ycql only")

    def test_read_write_must_sum_to_100(self):
        p = run_cli("--qps 1 --write-pct 50 --read-pct 40 --vcpu-per-node 8 --table-size-gb 1", check=False)
        self.assertNotEqual(p.returncode, 0)


class OutputFormatTests(unittest.TestCase):
    ARGS = SCENARIOS["ysql_reference"]

    def test_text_is_default(self):
        self.assertEqual(run_cli(self.ARGS).stdout, run_cli(self.ARGS + " --format text").stdout)

    def test_json_alias(self):
        self.assertEqual(run_cli(self.ARGS + " --json").stdout,
                         run_cli(self.ARGS + " --format json").stdout)

    def test_html_is_well_formed_and_self_contained(self):
        for args in (self.ARGS, SCENARIOS["ycql_tablets_ttl"]):
            with self.subTest(args=args):
                html = run_cli(args + " --format html").stdout
                self.assertTrue(html.startswith("<!doctype html>"))
                HTMLParser().feed(html)
                self.assertNotIn("http://", html.split("</style>")[0])
                self.assertNotIn("<script", html)


if __name__ == "__main__":
    unittest.main()
