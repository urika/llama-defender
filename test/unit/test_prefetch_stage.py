#!/usr/bin/env python3
"""test_prefetch_stage.py — R-CM-5 预取推送（BM25 排序 / 门控 / 注入）。"""
import unittest
from unittest.mock import patch

import proxy_state as _ps

from pipeline import PipelineContext, PrefetchStage, _bm25_rank_lines


def _line(anchor, text):
    return {"anchor": anchor, "kind": "tool_result", "turn": 1, "tool": "Read",
            "handle": {"type": "path", "value": text}, "head": text,
            "size_chars": len(text), "reason": "epoch_collapse"}


class TestPrefetchStage(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(_ps, k, None) for k in (
            "PROXY_PD_ENABLED", "PROXY_PD_PREFETCH_ENABLED", "PROXY_PD_PREFETCH_TOP_K",
            "PROXY_PD_PREFETCH_MAX_CHARS", "PROXY_PD_PREFETCH_PER_SESSION")}
        _ps.PROXY_PD_ENABLED = True
        with _ps._state_lock:
            _ps._SESSION_PREFETCH_STATE.clear()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                setattr(_ps, k, v)
        with _ps._state_lock:
            _ps._SESSION_PREFETCH_STATE.clear()

    def _ctx(self):
        return PipelineContext(session_id="s1", messages=[
            {"role": "user", "content": "please look at load_config usage"}])

    # ---- _bm25_rank_lines ----
    def test_bm25_ranks_matching_line(self):
        lines = [_line("r:a", "def unrelated_function(): pass"),
                 _line("r:b", "def load_config(path): return config")]
        ranked = _bm25_rank_lines(lines, "load_config usage", top_k=2)
        self.assertTrue(ranked)
        self.assertEqual(ranked[0]["anchor"], "r:b")

    def test_bm25_empty_query(self):
        self.assertEqual(_bm25_rank_lines([_line("r:a", "x")], "", top_k=2), [])

    # ---- should_run ----
    def test_disabled(self):
        _ps.PROXY_PD_PREFETCH_ENABLED = False
        self.assertFalse(PrefetchStage().should_run(self._ctx()))

    def test_aux_skipped(self):
        _ps.PROXY_PD_PREFETCH_ENABLED = True
        c = self._ctx(); c.session_id = "s1::aux-haiku"
        self.assertFalse(PrefetchStage().should_run(c))

    def test_dedup_with_auto_recall(self):
        _ps.PROXY_PD_PREFETCH_ENABLED = True
        c = self._ctx(); c.auto_recall_info = {"injected": 1}
        self.assertFalse(PrefetchStage().should_run(c))

    def test_per_session_cap(self):
        _ps.PROXY_PD_PREFETCH_ENABLED = True
        _ps.PROXY_PD_PREFETCH_PER_SESSION = 1
        with _ps._state_lock:
            _ps._SESSION_PREFETCH_STATE["s1"] = {"r:x"}
        self.assertFalse(PrefetchStage().should_run(self._ctx()))

    # ---- process ----
    def test_process_injects_and_marks(self):
        _ps.PROXY_PD_PREFETCH_ENABLED = True
        _ps.PROXY_PD_PREFETCH_TOP_K = 2
        _ps.PROXY_PD_PREFETCH_MAX_CHARS = 2000
        st = PrefetchStage()
        with patch("memory_stores.MANIFEST.lines",
                   return_value=[_line("r:b", "def load_config(path): return config")]), \
             patch("ctx_recall.recover_full_content",
                   return_value="def load_config(path):\n    return config"):
            ctx = st.process(self._ctx())
        self.assertEqual(ctx.prefetch_info["injected"], 1)
        # 尾部注入
        self.assertTrue(any("System: PREFETCH" in str(m.get("content"))
                            for m in ctx.messages))
        # 已去重：同会话再跑，锚点已 seen → 不注入
        with patch("memory_stores.MANIFEST.lines",
                   return_value=[_line("r:b", "def load_config(path): return config")]), \
             patch("ctx_recall.recover_full_content", return_value="x"):
            ctx2 = st.process(self._ctx())
        self.assertEqual(ctx2.prefetch_info["injected"], 0)

    def test_process_no_manifest(self):
        _ps.PROXY_PD_PREFETCH_ENABLED = True
        with patch("memory_stores.MANIFEST.lines", return_value=[]):
            ctx = PrefetchStage().process(self._ctx())
        self.assertEqual(ctx.prefetch_info["injected"], 0)


if __name__ == "__main__":
    unittest.main()
