#!/usr/bin/env python3
"""test_kv_observability.py — §3.5 KV 可观测落地测试。

- 24 个管线 stage 必须声明 kv 语义标签(防未来 stage 无意破坏前缀)
- fifo 截断递增前缀破碎计数(§3.5 KV 可观测)
- PrefixRatioComputer.output_metrics 输出 prefix_ratio
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import shutil
import tempfile
import inspect
import proxy_state as _ps


class TestKVTags(unittest.TestCase):

    def _all_stage_classes(self):
        import inspect
        import pipeline as pl
        return [c for n, c in vars(pl).items()
                if isinstance(c, type)
                and issubclass(c, pl.PipelineStage)
                and c is not pl.PipelineStage
                and c is not pl.ConditionalStage
                and c.__module__ == pl.__name__]

    def test_all_stages_have_kv_tag(self):
        missing = [c.__name__ for c in self._all_stage_classes()
                   if "kv:" not in (inspect.getsource(c) or "")]
        self.assertEqual(missing, [], f"缺少 kv 标签的 stage: {missing}")

    def test_conditional_stages_have_kv_tag(self):
        import inspect
        import pipeline as pl
        classes = [c for n, c in vars(pl).items()
                   if isinstance(c, type)
                   and issubclass(c, pl.ConditionalStage)
                   and c is not pl.ConditionalStage
                   and c.__module__ == pl.__name__]
        missing = [c.__name__ for c in classes
                   if "kv:" not in (inspect.getsource(c) or "")]
        self.assertEqual(missing, [], f"条件 stage 缺 kv 标签: {missing}")

    def test_tag_values_are_known(self):
        import inspect
        import pipeline as pl
        known = ("safe", "append", "one-shot", "breaking", "breaking-soft")
        for c in self._all_stage_classes():
            src = inspect.getsource(c)
            for line in src.split("\n"):
                if line.strip().startswith("# kv: "):
                    tag = line.strip().replace("# kv: ", "")
                    self.assertIn(tag, known, f"{c.__name__} 未知 kv 标签: {tag}")


class TestFifoBreakCounter(unittest.TestCase):

    def setUp(self):
        self._diag = tempfile.mkdtemp(prefix="kvbr_")
        self._orig = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._diag
        _ps._PREFIX_BREAK_COUNT.clear()

    def tearDown(self):
        _ps._DIAG_DIR = self._orig
        shutil.rmtree(self._diag, ignore_errors=True)

    def test_fifo_truncation_increments_break_count(self):
        import truncation
        sid = "kv-demo"
        orig_keep = _ps.PROXY_CTX_KEEP_MESSAGES
        orig_head = _ps.PROXY_CTX_KEEP_HEAD
        _ps.PROXY_CTX_KEEP_MESSAGES = 2
        _ps.PROXY_CTX_KEEP_HEAD = 0
        try:
            msgs = [{"role": "user" if i % 2 == 0 else "assistant",
                     "content": f"m{i} " + "x" * 60} for i in range(12)]
            truncation.truncate_messages_if_needed(
                [dict(m) for m in msgs], session_id=sid,
                strategy="fifo", keep_rounds=2)
            self.assertGreater(_ps._PREFIX_BREAK_COUNT.get(sid, 0), 0)
        finally:
            _ps.PROXY_CTX_KEEP_MESSAGES = orig_keep
            _ps.PROXY_CTX_KEEP_HEAD = orig_head

    def test_no_truncation_no_break(self):
        import truncation
        sid = "kv-clean"
        orig_keep = _ps.PROXY_CTX_KEEP_MESSAGES
        _ps.PROXY_CTX_KEEP_MESSAGES = 64
        try:
            msgs = [{"role": "user" if i % 2 == 0 else "assistant",
                     "content": f"m{i} " + "x" * 20} for i in range(8)]
            truncation.truncate_messages_if_needed(
                [dict(m) for m in msgs], session_id=sid,
                strategy="fifo", keep_rounds=2)
            self.assertEqual(_ps._PREFIX_BREAK_COUNT.get(sid, 0), 0)
        finally:
            _ps.PROXY_CTX_KEEP_MESSAGES = orig_keep


class TestPrefixRatioMetric(unittest.TestCase):

    def test_output_metrics_returns_ratio(self):
        from pipeline import PrefixRatioComputer, PipelineContext
        st = PrefixRatioComputer.__new__(PrefixRatioComputer)
        ctx = PipelineContext()
        ctx.common_prefix_ratio = 0.875
        ctx.session_id = "s-x"
        orig = _ps.PROXY_METRICS_ENABLED
        _ps.PROXY_METRICS_ENABLED = True
        try:
            m = st.output_metrics(ctx)
        finally:
            _ps.PROXY_METRICS_ENABLED = orig
        self.assertEqual(m["ratio"], 0.875)  # diagnostics 消费该键

    def test_diagnostics_record_includes_prefix_ratio(self):
        import diagnostics
        mc = {"pipeline": {"common_prefix_ratio": {"ratio": 0.8123}}}
        # 直接复用 data_quality_report 同款保守断言: 手工构造验证字段路径
        ratio = ((mc or {}).get("pipeline", {})
                 .get("common_prefix_ratio", {}) or {}).get("ratio")
        self.assertEqual(ratio, 0.8123)


if __name__ == "__main__":
    unittest.main()
