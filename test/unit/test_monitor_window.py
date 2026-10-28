"""监控滚动窗口外推数学的单测（tools/monitor_window.py）。

背景：原实现用「相邻两次快照差分」外推 headroom 耗尽时间，5 分钟采样
噪声大 → 两次误报（0.3h/0.4h 耗尽均未发生）。改 30 分钟滚动窗口最小
二乘，且样本跨度不足时不外推。本文件锁死这些行为。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools"))

import monitor_window as mw


def series(start_ts, n, step_s, headroom0, drain_per_h, denied0, denied_rate_per_h):
    """构造线性样本序列（headroom 按 drain_per_h GB/h 下降）。"""
    out = []
    for i in range(n):
        dt_h = (step_s * i) / 3600.0
        out.append((start_ts + step_s * i,
                    headroom0 - drain_per_h * dt_h,
                    denied0 + denied_rate_per_h * dt_h))
    return out


class MonitorWindowTest(unittest.TestCase):

    def test_slope_linear_series_exact(self):
        """线性序列斜率＝真实速率（每小时）。"""
        s = series(1000.0, 7, 300, 20.0, 0.5, 3000, 600)
        slope, span = mw.slope(s, 1)
        self.assertAlmostEqual(slope, -0.5, places=6)   # headroom 每小时 -0.5GB
        self.assertAlmostEqual(span, 30.0, places=6)    # 6 段 × 5 分钟
        slope2, _ = mw.slope(s, 2)
        self.assertAlmostEqual(slope2, 600.0, places=3)  # denied 每小时 +600

    def test_slope_insufficient_samples(self):
        self.assertEqual(mw.slope([], 1), (None, 0.0))
        self.assertEqual(mw.slope([(1.0, 2.0, 3.0)], 1), (None, 0.0))

    def test_slope_degenerate_all_same_ts(self):
        """同一时间戳的重复样本 → 斜率 None（不外推）。"""
        s = [(100.0, 20.0, 100), (100.0, 20.0, 100)]
        slope, span = mw.slope(s, 1)
        self.assertIsNone(slope)

    def test_update_history_appends_and_trims(self):
        prev = [[0.0, 20.0, 100], [100.0, 19.9, 200]]
        hist = mw.update_history(prev, 200.0, 19.8, 300, window_min=30)
        self.assertEqual(len(hist), 3)
        self.assertEqual(hist[-1][2], 300)

    def test_update_history_drops_out_of_window(self):
        now = 10_000.0
        prev = [[now - 7200, 20.0, 100], [now - 300, 19.0, 200]]
        hist = mw.update_history(prev, now, 18.9, 250, window_min=30)
        self.assertEqual(len(hist), 2)   # 2h 前的样本被裁掉
        self.assertAlmostEqual(hist[0][0], now - 300, places=3)

    def test_update_history_tolerates_garbage(self):
        hist = mw.update_history([["x", None], [1.0, 2.0, 3.0]], 10.0, 4.0, 5.0)
        self.assertEqual(len(hist), 2)

    def test_update_history_resets_on_counter_restart(self):
        """引擎重启 → denied 归零 → 跨复位样本必须丢弃（否则斜率荒谬）。"""
        now = 10_000.0
        prev = [[now - 600, 18.0, 7000], [now - 300, 18.0, 7100]]
        hist = mw.update_history(prev, now, 18.0, 7)      # 7100 → 7 = 复位
        self.assertEqual(len(hist), 1)                    # 只留本轮样本
        self.assertAlmostEqual(hist[0][2], 7.0, places=6)
        r = mw.denied_rate(hist, min_span_min=15)
        self.assertIsNone(r["rate_per_h"])                # 样本不足 → 不外推

    def test_headroom_eta_insufficient_span_no_extrapolation(self):
        """跨度不足 → 不外推（eta=None）——两次误报的教训。"""
        s = series(1000.0, 3, 300, 20.0, 5.0, 100, 0)   # 10 分钟跨度
        out = mw.headroom_eta(s, 18.0, min_span_min=15)
        self.assertFalse(out["sufficient"])
        self.assertIsNone(out["eta_h"])

    def test_headroom_eta_linear_drain(self):
        s = series(1000.0, 7, 300, 20.0, 0.5, 100, 0)
        out = mw.headroom_eta(s, 17.5, min_span_min=15)
        self.assertTrue(out["sufficient"])
        self.assertAlmostEqual(out["drain_per_h"], 0.5, places=2)
        self.assertAlmostEqual(out["eta_h"], 35.0, places=1)   # 17.5 / 0.5

    def test_headroom_eta_flat_no_drain(self):
        """基本没掉 → sufficient=True 但不报 eta（不误报）。"""
        s = series(1000.0, 7, 300, 20.0, 0.0, 100, 0)
        out = mw.headroom_eta(s, 20.0, min_span_min=15)
        self.assertTrue(out["sufficient"])
        self.assertIsNone(out["eta_h"])

    def test_denied_rate(self):
        s = series(1000.0, 7, 300, 20.0, 0.5, 3000, 1200)
        r = mw.denied_rate(s)
        self.assertAlmostEqual(r["rate_per_h"], 1200.0, places=3)
        self.assertAlmostEqual(r["span_min"], 30.0, places=6)

    def test_denied_rate_single_sample(self):
        r = mw.denied_rate([(1.0, 2.0, 3.0)])
        self.assertIsNone(r["rate_per_h"])

    def test_denied_rate_short_span_no_extrapolation(self):
        """跨度不足 → 不外推（denied 增长是突发的，短窗会算出上万/h）。"""
        s = series(1000.0, 2, 60, 20.0, 0.0, 3000, 0)   # 1 分钟跨度
        r = mw.denied_rate(s, min_span_min=15)
        self.assertIsNone(r["rate_per_h"])
        self.assertAlmostEqual(r["span_min"], 1.0, places=6)

    def test_sev_max_not_lexicographic(self):
        """等级取严重度更高者——不能用字符串 max()（CRIT 被 WARN 覆盖）。"""
        self.assertEqual(mw.sev_max("OK", "WARN"), "WARN")
        self.assertEqual(mw.sev_max("CRIT", "WARN"), "CRIT")
        self.assertEqual(mw.sev_max("WARN", "CRIT"), "CRIT")
        self.assertEqual(mw.sev_max("INFO", "OK"), "INFO")
        self.assertEqual(mw.sev_max("CRIT", "CRIT"), "CRIT")
        self.assertNotEqual(mw.sev_max("CRIT", "WARN"), max("CRIT", "WARN"))

    # ---- 重启决策（清场 / 劣化 / 冷却 / swap）----

    def test_degraded_now_uses_rate_not_cumulative(self):
        """关键区分：累计 denied 高但当前速率 0 ⇒ **不算当前劣化**（那是清场判据）。"""
        snap = {"denied_rate_per_h": 0.0, "memory_denied": 765,
                "memory_headroom_gb": 17.7, "ttft_p95_ms": 9793.0}
        bad, why = mw.degraded_now(snap)
        self.assertFalse(bad)
        self.assertEqual(why, "")

    def test_degraded_now_rate_headroom_ttft(self):
        snap = {"denied_rate_per_h": 1200.0, "memory_headroom_gb": 17.0,
                "ttft_p95_ms": 1000.0}
        self.assertEqual(mw.degraded_now(snap)[1], "denied_rate")
        snap2 = {"denied_rate_per_h": 0.0, "memory_headroom_gb": 5.0,
                 "ttft_p95_ms": 1000.0}
        self.assertEqual(mw.degraded_now(snap2)[1], "headroom")
        snap3 = {"denied_rate_per_h": 0.0, "memory_headroom_gb": 17.0,
                 "ttft_p95_ms": 45000.0}
        self.assertEqual(mw.degraded_now(snap3)[1], "ttft_p95")

    def test_recycle_needed_on_batch_end(self):
        """批跑收官一次性清场：连续缺席 ≥2 轮 + 累计拒绝超阈。"""
        self.assertTrue(mw.recycle_needed(False, False, 765, th=500,
                                          idle_cycles=2))
        # 间隙误杀防护：上一轮还在批跑（idle_cycles=1）→ 不清场
        self.assertFalse(mw.recycle_needed(True, False, 765, th=500,
                                           idle_cycles=1))
        # 缺席轮数不足 → 再观察
        self.assertFalse(mw.recycle_needed(False, False, 765, th=500,
                                           idle_cycles=1))
        # 累计量未超阈 → 不清场
        self.assertFalse(mw.recycle_needed(False, False, 100, th=500,
                                           idle_cycles=3))
        # 批跑中 → 绝不清场
        self.assertFalse(mw.recycle_needed(False, True, 765, th=500,
                                           idle_cycles=2))

    def test_restart_allowed_cooldown_and_cap(self):
        """冷却 + 次数上限（防重启风暴）。"""
        now = 100000.0
        ok, _ = mw.restart_allowed(now, None, 0)
        self.assertTrue(ok)
        ok, why = mw.restart_allowed(now, now - 60, 0, cooldown_s=900)
        self.assertFalse(ok)
        self.assertIn("冷却", why)
        ok, why = mw.restart_allowed(now, now - 3600, 3, max_attempts=3)
        self.assertFalse(ok)
        self.assertIn("最大", why)

    def test_swap_alert_levels(self):
        """swap 判据：>90% CRIT、>阈值 WARN、其余静默。"""
        lvl, msg = mw.swap_alert(10444, 11264, warn_mb=6144)
        self.assertEqual(lvl, "CRIT")
        self.assertIn("重启系统", msg)
        lvl, _ = mw.swap_alert(7000, 11264, warn_mb=6144)
        self.assertEqual(lvl, "WARN")
        self.assertIsNone(mw.swap_alert(3000, 11264, warn_mb=6144)[0])
        self.assertIsNone(mw.swap_alert(0, 0, warn_mb=6144)[0])


if __name__ == "__main__":
    unittest.main()
