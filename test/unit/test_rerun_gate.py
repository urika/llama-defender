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
             epoch_after=None, ctx_after=None, metal_log=0,
             metal_peak_bytes=None, **thresholds):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(before, f); f.close()
        after = {
            "ts": "2026-09-21T06:30:00",
            "metrics": after_metrics,
            "ctx_config": ctx_after or before["ctx_config"],
            "epoch_collapse_by_session": epoch_after or before["epoch_collapse_by_session"],
        }
        if metal_peak_bytes is not None:
            after["metrics"] = dict(after_metrics)
            after["metrics"]["metal_peak_bytes"] = metal_peak_bytes
        opts = dict(wall_threshold_ratio=1.5, max_pressure_evict=864,
                    max_lru_evict=864, max_epoch_collapse=0,
                    max_metal_peak_gb=34.0, max_metal_events_30min=8)
        opts.update(thresholds)
        with mock.patch.object(rg, "snapshot", return_value=after), \
             mock.patch.object(rg, "_wall_times_from_sessions",
                               return_value=[wall_after_ms]), \
             mock.patch.object(rg, "_log_evict_attribution",
                               return_value=(metal_log, 0, 0)):
            rc = rg.check(f.name, opts["wall_threshold_ratio"],
                          opts["max_pressure_evict"], opts["max_lru_evict"],
                          opts["max_epoch_collapse"], None,
                          max_metal_peak_gb=opts["max_metal_peak_gb"],
                          max_metal_events_30min=opts["max_metal_events_30min"])
        os.unlink(f.name)
        return rc

    def test_pass_when_all_clear(self):
        rc = self._run(self._before(),
                       {"pressure_evictions": 0, "lru_evictions": 150,
                        "snapdown_hits": 0, "prefix_cache_hits": 12})
        self.assertEqual(rc, 0)

    def test_stop_on_pressure_evict(self):
        rc = self._run(self._before(),
                       {"pressure_evictions": 900, "lru_evictions": 110,
                        "snapdown_hits": 0, "prefix_cache_hits": 12})
        self.assertEqual(rc, 1)

    def test_pass_on_few_metal_events(self):
        """判据 (1)「Metal 有界」：少量瞬时（<8）→ PASS（0922-3 口径）。"""
        rc = self._run(self._before(),
                       {"pressure_evictions": 100, "lru_evictions": 110,
                        "snapdown_hits": 0, "prefix_cache_hits": 12},
                       metal_log=2)
        self.assertEqual(rc, 0)

    def test_stop_on_metal_thrash(self):
        """1b thrash：Metal 事件 >= 8 → STOP。"""
        rc = self._run(self._before(),
                       {"pressure_evictions": 100, "lru_evictions": 110,
                        "snapdown_hits": 0, "prefix_cache_hits": 12},
                       metal_log=8)
        self.assertEqual(rc, 1)

    def test_stop_on_metal_peak_ceiling(self):
        """1a 天花板：metal_peak >= 34.0GB → STOP。"""
        rc = self._run(self._before(),
                       {"pressure_evictions": 0, "lru_evictions": 110,
                        "snapdown_hits": 0, "prefix_cache_hits": 12},
                       metal_log=0, metal_peak_bytes=int(34.5e9))
        self.assertEqual(rc, 1)

    def test_pass_when_pressure_bounded_no_metal(self):
        """cache-self 有界（≤864）且 Metal 有界（0 事件、peak<33）→ PASS。"""
        rc = self._run(self._before(),
                       {"pressure_evictions": 800, "lru_evictions": 800,
                        "snapdown_hits": 0, "prefix_cache_hits": 12},
                       metal_log=0, metal_peak_bytes=int(32.7e9))
        self.assertEqual(rc, 0)

    def test_stop_on_lru_over_threshold(self):
        rc = self._run(self._before(),
                       {"pressure_evictions": 0, "lru_evictions": 1000,
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
    """0921-15 B① 口径校准：改数 sessions.jsonl 的 epoch_count（折叠**事件数**），
    不再数 manifest 条目数（那对长会话恒真、无判别力）。"""

    def _write(self, rows):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "logs", "diag"))
        with open(os.path.join(d, "logs", "diag", "sessions.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return d

    def test_takes_latest_epoch_count_per_session(self):
        """各会话取末次 epoch_count（累计折叠事件数）。"""
        d = self._write([
            {"session_key": "sA", "epoch_count": 1},
            {"session_key": "sA", "epoch_count": 3},   # 后写覆盖 → 3
            {"session_key": "sB", "epoch_count": 2},
        ])
        with mock.patch.object(rg, "REPO", d):
            got = rg._epoch_collapse_by_session()
        self.assertEqual(got, {"sA": 3, "sB": 2})

    def test_zero_epoch_count_excluded(self):
        """epoch_count=0 不入表（无折叠的会话不产生噪声键）。"""
        d = self._write([
            {"session_key": "sA", "epoch_count": 0},
            {"session_key": "sB", "epoch_count": 2},
        ])
        with mock.patch.object(rg, "REPO", d):
            got = rg._epoch_collapse_by_session()
        self.assertEqual(got, {"sB": 2})

    def test_missing_epoch_count_ignored(self):
        d = self._write([
            {"session_key": "sA"},
            {"session_key": "sB", "epoch_count": 4},
        ])
        with mock.patch.object(rg, "REPO", d):
            got = rg._epoch_collapse_by_session()
        self.assertEqual(got, {"sB": 4})


if __name__ == "__main__":
    unittest.main(verbosity=2)
