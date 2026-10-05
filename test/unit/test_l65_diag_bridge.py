#!/usr/bin/env python3
"""test_l65_diag_bridge.py — L-65 桥接回归：管线统计 → 诊断 per-turn 记录。

出典（L-65，2026-10-05 b2 批中守望审计）：P0(ii) 的 `ctx_recall`/`reread`
两段统计落在管线的 `PipelineContext` 上，而 `diagnostics.finalize_request`
的 per-turn 记录读的是 `_ps._diag_ctx`（诊断专属对象）——两个对象不共事，
sessions.jsonl 两段恒空、端点聚合恒 null（数据面静默断链）。

修复=候选①：统计赋值点同址镜像到 `_diag_ctx`（`pipeline._mirror_diag_ctx_stats`）。
本测试钉死桥接契约：stage 8 赋值后 `_diag_ctx` 两段非空；stage 20 改写刷新后
`_diag_ctx.ctx_recall_stats` 含本轮结局；`_diag_ctx` 缺席时镜像静默跳过。
"""
import unittest
from unittest.mock import patch

import proxy_state as _ps

import ctx_recall as cr
from pipeline import PipelineContext, ToolLoopDetector, FormatConverter

_ERR = "<tool_use_error>No such tool available: ctx_recall</tool_use_error>"


class _FakeDiagCtx:
    """最小 _diag_ctx 替身：finalize 只按属性名取值。"""
    session_key = "s-l65"
    request_id = "req-l65"
    key_source = "test"


def _recall_round(tid, query):
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "ctx_recall",
             "input": {"query": query}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": [{"type": "text", "text": _ERR}]}]},
    ]


class TestL65DiagBridge(unittest.TestCase):
    def setUp(self):
        cr.CTX_RECALL_STATS.reset()
        self._orig_dc = getattr(_ps, "_diag_ctx", None)
        _ps._diag_ctx = _FakeDiagCtx()

    def tearDown(self):
        _ps._diag_ctx = self._orig_dc
        cr.CTX_RECALL_STATS.reset()

    def test_stage8_mirrors_both_sections(self):
        messages = _recall_round("c1", "needle")
        messages += [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "r1", "name": "Read",
             "input": {"file_path": "/a.py"}}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "r2", "name": "Read",
                 "input": {"file_path": "/a.py"}}]}]
        cr.CTX_RECALL_STATS.record("s-l65", "c1", "miss", turn=3)
        ctx = ToolLoopDetector().process(
            PipelineContext(messages=messages, session_id="s-l65"))
        dc = _ps._diag_ctx
        self.assertIsNotNone(dc.ctx_recall_stats, "桥接后 ctx_recall 段不得为空")
        self.assertEqual(dc.ctx_recall_stats["attempts"], 1)
        self.assertEqual(dc.ctx_recall_stats["misses"], 1)
        self.assertIsNotNone(dc.reread_stats, "桥接后 reread 段不得为空")
        self.assertEqual(dc.reread_stats["no_edit_rereads"], 1)

    def test_stage20_refresh_mirrors_fresh_outcome(self):
        # stage 8 先跑一轮（快照不含本轮结局），代答路径记账，stage 20 刷新
        messages = _recall_round("c1", "needle")
        cr.CTX_RECALL_STATS.record("s-l65", "c1", "miss", turn=2)
        ctx = PipelineContext(messages=messages, session_id="s-l65")
        ctx = ToolLoopDetector().process(ctx)
        _ps._diag_ctx.ctx_recall_stats = None   # 模拟修复前形态：段为空
        with patch("pipeline._ctx_engine_on", return_value=True), \
             patch.object(_ps, "PROXY_PD_ENABLED", True):
            FormatConverter().process(ctx)
        dc = _ps._diag_ctx
        self.assertIsNotNone(dc.ctx_recall_stats)
        self.assertEqual(dc.ctx_recall_stats["misses"], 1,
                         "刷新镜像必须含本轮结局（代答台账）")

    def test_mirror_silent_when_diag_ctx_absent(self):
        _ps._diag_ctx = None   # 诊断关闭早期形态：不抛错即可
        ctx = ToolLoopDetector().process(
            PipelineContext(messages=_recall_round("c1", "q"),
                            session_id="s-l65"))
        self.assertIsNotNone(ctx.ctx_recall_stats)


if __name__ == "__main__":
    unittest.main()
