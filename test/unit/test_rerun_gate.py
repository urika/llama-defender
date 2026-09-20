#!/usr/bin/env python3
"""test_rerun_gate.py — 重跑前置门测试（0921-4 交付）。

覆盖：计数器增量解析 / 四件套判据（pressure/lru/墙钟/epoch_collapse）/
since 过滤 + 中位数抗长尾（自查修正）/ ctx_config 漂移 / PASS-STOP 两路径 /
exit 码语义。纯逻辑测试，不触网（_fetch/_backend_metrics 打桩）。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import rerun_gate as rg  # noqa: E402


class TestMetricParsing(unittest.TestCase):
    def test_metric_value_extracts_counter(self):
        txt = ("# HELP rapid_mlx_prefix_cache_pressure_evictions_total ...\n"
               "# TYPE rapid_mlx_prefix_cache_pressure_evictions_total counter\n"
               "rapid_mlx_prefix_cache_pressure_evictions_total 221\n")
        self.assertEqual(
            rg._metric_value(txt, "rapid_mlx_prefix_cache_pressure_evictions_total"),
            221.0)

    def test_metric_value_absent_returns_none(self):
        self.assertIsNone(rg._metric_value("other_metric 1\n", "rapid_mlx_x_total"))

    def test_backend_metrics_maps_counters_and_gauges(self):
        fake = ("rapid_mlx_prefix_cache_pressure_evictions_total 5\n"
                "rapid_mlx_prefix_cache_evictions_total 9\n"
                "rapid_mlx_prefix_cache_snapdown_hits_total 2\n"
                "rapid_mlx_prefix_cache_hits_total 7\n"
                "rapid_mlx_metal_active_memory_bytes 100\n"
                "rapid_mlx_metal_peak_memory_bytes 200\n"
                "rapid_mlx_prefix_cache_current_bytes 300\n"
                "rapid_mlx_prefix_cache_cap_bytes 400\n")
        with mock.patch.object(rg, "_fetch", return_value=fake):
            m = rg._backend_metrics()
        self.assertEqual(m["pressure_evictions"], 5)
        self.assertEqual(m["lru_evictions"], 9)
        self.assertEqual(m["snapdown_hits"], 2)
        self.assertEqual(m["metal_active_bytes"], 100)
        self.assertEqual(m["prefix_cache_cap_bytes"], 400)


class TestWallFiltering(unittest.TestCase):
    def _write_sessions(self, rows):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "logs", "diag"))
        p = os.path.join(d, "logs", "diag", "sessions.jsonl")
        with open(p, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return d, p

    def test_since_filter_excludes_history(self):
        """0921-4 修正：必须按 since 过滤，否则历史长尾（15min/run）污染。"""
        d, p = self._write_sessions([
            {"ts": "2026-09-19T10:00:00", "duration_ms": 900000},   # 灾难期
            {"ts": "2026-09-21T06:00:00", "duration_ms": 30000},    # 本批
            {"ts": "2026-09-21T06:01:00", "duration_ms": 32000},
        ])
        with mock.patch.object(rg, "REPO", d):
            got = rg._wall_times_from_sessions(since_ts="2026-09-21T00:00:00")
        self.assertEqual(got, [30000.0, 32000.0])   # 900000 被排除

    def test_since_none_reads_all(self):
        d, p = self._write_sessions([
            {"ts": "2026-09-19T10:00:00", "duration_ms": 900000},
            {"ts": "2026-09-21T06:00:00", "duration_ms": 30000},
        ])
        with mock.patch.object(rg, "REPO", d):
            got = rg._wall_times_from_sessions()
        self.assertEqual(len(got), 2)


class TestCheckVerdict(unittest.TestCase):
    def _before(self, **kw):
        base = {
            "ts": "2026-09-21T06:00:00",
            "epoch_utc": 1.0,
            "metrics": {"pressure_evictions": 0, "lru_evictions": 100,
                        "snapdown_hits": 0, "prefix_cache_hits": 10},
            "ctx_config": {"epoch_S": 70000, "window_K": 24, "engine_enabled": True},
            "epoch_collapse_by_session": {"s1": 5},
            "wall_median_ms": 30000,
        }
        base.update(kw)
        return base

    def _run(self, before, after_metrics, wall_after_ms=30000,
             epoch_after=None, ctx_after=None, **thresholds):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(before, f); f.close()
        after = {
            "ts": "2026-09-21T06:30:00",
            "metrics": after_metrics,
            "ctx_config": ctx_after or before["ctx_config"],
            "epoch_collapse_by_session": epoch_after or before["epoch_collapse_by_session"],
        }
        opts = dict(wall_threshold_ratio=1.5, max_pressure_evict=0,
                    max_lru_evict=200, max_epoch_collapse=0)
        opts.update(thresholds)
        with mock.patch.object(rg, "snapshot", return_value=after), \
             mock.patch.object(rg, "_wall_times_from_sessions",
                               return_value=[wall_after_ms]):
            rc = rg.check(f.name, opts["wall_threshold_ratio"],
                          opts["max_pressure_evict"], opts["max_lru_evict"],
                          opts["max_epoch_collapse"], None)
        os.unlink(f.name)
        return rc

    def test_pass_when_all_clear(self):
        rc = self._run(self._before(),
                       {"pressure_evictions": 0, "lru_evictions": 150,
                        "snapdown_hits": 0, "prefix_cache_hits": 12})
        self.assertEqual(rc, 0)

    def test_stop_on_pressure_evict(self):
        rc = self._run(self._before(),
                       {"pressure_evictions": 3, "lru_evictions": 110,
                        "snapdown_hits": 0, "prefix_cache_hits": 12})
        self.assertEqual(rc, 1)

    def test_stop_on_lru_over_threshold(self):
        rc = self._run(self._before(),
                       {"pressure_evictions": 0, "lru_evictions": 350,
                        "snapdown_hits": 0, "prefix_cache_hits": 12})
        self.assertEqual(rc, 1)

    def test_stop_on_wall_degradation(self):
        rc = self._run(self._before(),
                       {"pressure_evictions": 0, "lru_evictions": 110,
                        "snapdown_hits": 0, "prefix_cache_hits": 12},
                       wall_after_ms=60000)   # 60000/30000 = 2.0 > 1.5
        self.assertEqual(rc, 1)

    def test_stop_on_epoch_collapse(self):
        rc = self._run(self._before(),
                       {"pressure_evictions": 0, "lru_evictions": 110,
                        "snapdown_hits": 0, "prefix_cache_hits": 12},
                       epoch_after={"s1": 5, "s2": 1})   # 新增 s2:1
        self.assertEqual(rc, 1)

    def test_stop_on_ctx_config_drift(self):
        rc = self._run(self._before(),
                       {"pressure_evictions": 0, "lru_evictions": 110,
                        "snapdown_hits": 0, "prefix_cache_hits": 12},
                       ctx_after={"epoch_S": 50000, "window_K": 24, "engine_enabled": True})
        self.assertEqual(rc, 1)


class TestEpochCollapseScan(unittest.TestCase):
    def test_counts_epoch_collapse_lines_per_session(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "sA.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "epoch_collapse", "turn": 1}) + "\n")
            f.write(json.dumps({"kind": "other"}) + "\n")
            f.write(json.dumps({"kind": "epoch_collapse", "turn": 2}) + "\n")
        with open(os.path.join(d, "sB.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "other"}) + "\n")
        with mock.patch.object(rg, "MANIFEST_DIR", d):
            got = rg._epoch_collapse_by_session()
        self.assertEqual(got, {"sA": 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)
