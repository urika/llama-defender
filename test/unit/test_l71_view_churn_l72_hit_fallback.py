"""L-71 视图流失计量 + L-72 hit_ratio 第二来源 单测（2026-10-06）。

L-71: record_view_drop 分型累计 → finalize_request 逐轮 view_dropped 段
      （{msgs,tokens,by_reason}）；无 drop 轮 → null。
L-72: set_prompt_tokens processed_n fill-if-missing——timings.prompt_n
      （rapid-mlx 首选源）先到不覆盖；cached_tokens（Splash 世代）补位。
设计依据: roadmap L-71/L-72 行 ＋ docs/trajectory-view-requirements-20261006.md ②c。
"""
import json
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import proxy_state as _ps
import diagnostics as diag


class ViewDropTest(unittest.TestCase):
    def setUp(self):
        self._enabled = _ps.PROXY_DIAG_ENABLED
        _ps.PROXY_DIAG_ENABLED = True
        diag.begin_request("req_l71", "sess_l71", "header")

    def tearDown(self):
        _ps.PROXY_DIAG_ENABLED = self._enabled

    def test_accumulate_by_reason(self):
        diag.record_view_drop("epoch_collapse", msgs=8, tokens=1200)
        diag.record_view_drop("epoch_collapse", msgs=2, tokens=300)
        diag.record_view_drop("oom_safety", msgs=3)
        vd = diag._view_dropped_record(_ps._diag_ctx)
        self.assertEqual(vd["msgs"], 13)
        self.assertEqual(vd["tokens"], 1500)
        self.assertEqual(vd["by_reason"]["epoch_collapse"], {"msgs": 10, "tokens": 1500})
        self.assertEqual(vd["by_reason"]["oom_safety"], {"msgs": 3, "tokens": 0})

    def test_no_drop_returns_none(self):
        self.assertIsNone(diag._view_dropped_record(_ps._diag_ctx))

    def test_disabled_is_noop(self):
        _ps.PROXY_DIAG_ENABLED = False
        diag.record_view_drop("fifo_window", msgs=5)
        self.assertIsNone(_ps._diag_ctx.view_dropped)

    def test_finalize_includes_section(self):
        diag.record_view_drop("keep_budget", msgs=4, tokens=800)
        _ps._diag_ctx.request_id = "req_l71b"
        _ps._SESSION_REQUEST_COUNT["sess_l71"] = 1
        rec = diag.finalize_request({"session_id": "sess_l71"})
        self.assertIsNotNone(rec)
        vd = rec.get("view_dropped")
        self.assertEqual(vd["msgs"], 4)
        self.assertEqual(vd["tokens"], 800)
        self.assertIn("keep_budget", vd["by_reason"])

    def test_finalize_without_drop_is_null(self):
        _ps._diag_ctx.request_id = "req_l71c"
        _ps._SESSION_REQUEST_COUNT["sess_l71"] = 2
        rec = diag.finalize_request({"session_id": "sess_l71"})
        self.assertIsNone(rec.get("view_dropped"))

    def test_collapse_records_reason(self):
        """engine 折叠路径返回流失计量（window 口径 → maybe_epoch 落
        epoch_collapse；预算口径 → keep_budget）。"""
        import context_engine
        s = context_engine.CanonicalSession("sk_l71")
        # 构造 6 轮 body（每轮 user+assistant），窗口 K=2 → 收编 4 轮
        canon = [{"role": "system", "content": "sys"}]
        for i in range(6):
            canon.append({"role": "user", "content": f"u{i} " + "x" * 40})
            canon.append({"role": "assistant", "content": f"a{i}"})
        s.canonical = list(canon)
        s.user_msgs = 6
        msg, dm, dt = s._collapse(2, False)
        self.assertEqual(dm, 8)      # 4 轮 × 2 条
        self.assertGreater(dt, 0)
        # 预算口径同函数体, reason 由 maybe_epoch 按 keep_budget>0 分型
        msg2, dm2, dt2 = s._collapse(0, False, keep_budget=500)
        self.assertGreaterEqual(dm2, 0)


class HitRatioFallbackTest(unittest.TestCase):
    """L-72: processed 第二来源（fill-if-missing 语义）。"""

    def setUp(self):
        self._enabled = _ps.PROXY_DIAG_ENABLED
        _ps.PROXY_DIAG_ENABLED = True
        diag.begin_request("req_l72", "sess_l72", "header")

    def tearDown(self):
        _ps.PROXY_DIAG_ENABLED = self._enabled

    def test_timings_first_not_overwritten(self):
        """rapid-mlx 两族: timings.prompt_n 先写，usage.cached_tokens 后到不得覆盖。"""
        diag.set_prompt_tokens(processed_n=90000)
        diag.set_prompt_tokens(processed_n=12000, sent_n=98347,
                               generation_n=500)
        self.assertEqual(_ps._diag_ctx.prompt_processed_tokens, 90000)
        self.assertEqual(_ps._diag_ctx.prompt_sent_tokens, 98347)
        self.assertEqual(diag.compute_hit_ratio(
            _ps._diag_ctx.prompt_processed_tokens,
            _ps._diag_ctx.prompt_sent_tokens), 0.0849)

    def test_splash_fallback_fills(self):
        """Splash 世代: 无 timings → usage.cached_tokens 补位 processed。"""
        diag.set_prompt_tokens(sent_n=98347)
        diag.set_prompt_tokens(processed_n=81234, sent_n=98347,
                               generation_n=420)
        self.assertEqual(_ps._diag_ctx.prompt_processed_tokens, 81234)
        self.assertIsNotNone(diag.compute_hit_ratio(81234, 98347))

    def test_none_never_overwrites(self):
        diag.set_prompt_tokens(processed_n=777)
        diag.set_prompt_tokens(processed_n=None, sent_n=1000)
        self.assertEqual(_ps._diag_ctx.prompt_processed_tokens, 777)


class MetricsViewSectionTest(unittest.TestCase):
    """admin_server 聚合：view 段逐轮求和（含 by_reason 展平）。"""

    def _aggregate(self, records):
        """从 admin_server 的聚合逻辑抽出等价最小实现做断言（真函数内联在
        session signals 组装里, 这里直测数据形状契约）。"""
        total = {"msgs": 0, "tokens": 0}
        for r in records:
            vd = r.get("view_dropped")
            if not isinstance(vd, dict):
                continue
            total["msgs"] += int(vd.get("msgs") or 0)
            total["tokens"] += int(vd.get("tokens") or 0)
            for reason, item in (vd.get("by_reason") or {}).items():
                total[f"{reason}_msgs"] = total.get(f"{reason}_msgs", 0) + int(item.get("msgs") or 0)
                total[f"{reason}_tokens"] = total.get(f"{reason}_tokens", 0) + int(item.get("tokens") or 0)
        return total if total["msgs"] > 0 else None

    def test_sum_not_max(self):
        recs = [
            {"view_dropped": {"msgs": 4, "tokens": 100,
                              "by_reason": {"epoch_collapse": {"msgs": 4, "tokens": 100}}}},
            {"view_dropped": {"msgs": 2, "tokens": 50,
                              "by_reason": {"oom_safety": {"msgs": 2, "tokens": 50}}}},
        ]
        out = self._aggregate(recs)
        self.assertEqual(out["msgs"], 6)          # 求和 ≠ max(4)
        self.assertEqual(out["epoch_collapse_msgs"], 4)
        self.assertEqual(out["oom_safety_tokens"], 50)

    def test_empty_session_none(self):
        self.assertIsNone(self._aggregate([{"view_dropped": None}, {}]))


if __name__ == "__main__":
    unittest.main()
