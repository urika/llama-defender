"""Unit tests for context_engine.py — R8.1-R8.3 上下文工程引擎 Phase 1.

设计依据: llama-defender-context-engineering-design §4.3(写入期压缩模板/
最小阈值/句柄) §4.9(epoch 状态机/回退保护/硬上限) §4.10(canonical 两套账);
Phase 0 §12.3 结论 4(击穿根因=回溯改写)——引擎开启时 7/14/17 stage 跳过。
"""
import os
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import context_engine as ce  # noqa: E402
import proxy_state as _ps   # noqa: E402


def _tu(text):
    return [{"role": "user", "content": [{"type": "text", "text": text}]}]


def _tool_round(tid, tool, args, result, result_key="content"):
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": tool, "input": args}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, result_key: result}]},
    ]


class TestCompressObservation(unittest.TestCase):
    """§4.3 写入期压缩模板。"""

    def test_small_result_verbatim(self):
        out, handle, kind = ce.compress_observation("Bash", {"command": "ls"}, "ok\nfile1")
        self.assertEqual(out, "ok\nfile1")
        self.assertEqual(kind, "verbatim")
        self.assertEqual(handle, "ls")

    def test_error_full_preserved(self):
        text = "Traceback (most recent call last):\nValueError: boom\n" * 50
        out, _, kind = ce.compress_observation("Bash", {}, text)
        self.assertEqual(out, text)          # 错误全文保留(负反馈稀缺信号)
        self.assertEqual(kind, "error_full")

    def test_large_result_head_tail_handle(self):
        text = "x" * 20000
        out, handle, kind = ce.compress_observation("WebSearch", {"query": "rust async"}, text)
        self.assertLess(len(out), 8000)
        self.assertEqual(kind, "search")
        self.assertEqual(handle, "rust async")
        self.assertIn("[ctx-engine: search result compressed", out)
        self.assertIn("handle: rust async", out)
        self.assertTrue(out.startswith("x"))
        self.assertTrue(out.rstrip().endswith("x"))

    def test_min_threshold_no_wrap(self):
        # 介于 MIN(512) 与类别预算之间的结果逐字保留
        text = "y" * 700
        out, _, kind = ce.compress_observation("Read", {"file_path": "/a"}, text)
        self.assertEqual(out, text)
        self.assertEqual(kind, "verbatim")

    def test_tool_classification(self):
        self.assertEqual(ce._classify_tool("WebSearch")[0], "search")
        self.assertEqual(ce._classify_tool("mcp__serper__google_search")[0], "search")
        self.assertEqual(ce._classify_tool("Read")[0], "file")
        self.assertEqual(ce._classify_tool("Bash")[0], "command")
        self.assertEqual(ce._classify_tool("WebFetch")[0], "http")


class TestCanonicalSession(unittest.TestCase):
    """append-only 吸收 + 冻结 + epoch 状态机。"""

    def setUp(self):
        self.sess = ce.CanonicalSession("t")

    def test_absorb_incremental_append_only(self):
        r1 = _tu("q1") + _tool_round("t1", "WebSearch", {"query": "a"}, "r" * 20000)
        canon, mism, n = self.sess.absorb(r1)
        self.assertFalse(mism)
        self.assertEqual(n, 3)
        # 大结果已写入期压缩
        tr = canon[2]["content"][0]["content"]
        self.assertLess(len(tr), 8000)
        # 第二轮: 前缀一致, 只追加新消息; 既往压缩不再重写(frozen)
        r2 = r1 + [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
        canon2, mism2, n2 = self.sess.absorb(r2)
        self.assertFalse(mism2)
        self.assertEqual(n2, 1)
        self.assertEqual(len(canon2), 4)
        self.assertIs(canon2[2], canon[2])  # 前缀对象引用稳定(未重建)

    def test_absorb_prefix_mismatch_rebuild(self):
        r1 = _tu("q1") + _tool_round("t1", "Bash", {"command": "ls"}, "out")
        self.sess.absorb(r1)
        # 客户端裁剪/重写: 前缀失配 → 全量重建
        r2 = [{"role": "user", "content": "trimmed"}] + r1[1:]
        canon, mism, n = self.sess.absorb(r2)
        self.assertTrue(mism)
        self.assertEqual(len(canon), len(r2))

    def test_frozen_copy_not_polluted(self):
        msgs = _tu("q1") + _tool_round("t1", "Bash", {"command": "ls"}, "keep me")
        canon, _, _ = self.sess.absorb(msgs)
        # 下游 stage 拿到的是发送副本(stage 出口 _frozen_copy)——
        # 就地改写发送副本/客户端重放都不回污染 canonical
        sent = [ce._frozen_copy(m) for m in canon]
        sent[0]["content"] = [{"type": "text", "text": "MUTATED"}]
        msgs[0]["content"] = [{"type": "text", "text": "CLIENT_MUTATED"}]
        msgs2 = _tu("q1") + _tool_round("t1", "Bash", {"command": "ls"}, "keep me")
        canon2, mism, _ = self.sess.absorb(msgs2)
        self.assertFalse(mism)
        self.assertEqual(canon2[0]["content"][0]["text"], "q1")

    def test_epoch_not_triggered_small(self):
        msgs = _tu("q") + _tool_round("t1", "Bash", {"command": "ls"}, "x" * 100)
        self.sess.absorb(msgs)
        triggered, final = self.sess.maybe_epoch(100000, 24)
        self.assertFalse(triggered)
        self.assertEqual(len(final), len(self.sess.canonical))
        self.assertEqual(self.sess.epoch_count, 0)

    def test_epoch_collapse_keeps_system_and_k_window(self):
        msgs = [{"role": "system", "content": "SYS"}]
        for i in range(30):
            msgs += _tu("q%d" % i) + _tool_round(
                "t%d" % i, "Bash", {"command": "cmd%d" % i}, "o" * 4000)
        self.sess.absorb(msgs)
        before = self.sess.est_tokens()
        # 低预算强制 epoch; K=5
        triggered, final = self.sess.maybe_epoch(before // 4, 5)
        self.assertTrue(triggered)
        self.assertEqual(self.sess.epoch_count, 1)
        self.assertLess(ce.CanonicalSession.est_tokens(self.sess, final), before)
        # system 保留在最前
        self.assertEqual(final[0]["role"], "system")
        # 压缩区消息存在且带 epoch 标记
        region = [m for m in final if m.get("_ctx_engine_epoch")]
        self.assertEqual(len(region), 1)
        self.assertIn("action ledger", region[0]["content"][0]["text"])
        # 重切后 canonical 已回写(后续轮在其上 append)
        self.assertEqual(len(self.sess.canonical), len(final))
        # 保留轮数 = K 轮(user/assistant 对 ≈ 2K 条消息 + 压缩区 1 条)
        kept_msgs = len(final) - 1 - 1  # 减 system 与压缩区
        self.assertLessEqual(kept_msgs, 5 * 2 + 1)

    def test_epoch_hard_cap_returns_none(self):
        # 极端: 单轮即超预算且无更老轮可收编 → 回退穷尽 → None
        msgs = _tu("q") + [{"role": "assistant", "content": [
            {"type": "text", "text": "z" * 400000}]}]
        self.sess.absorb(msgs)
        triggered, final = self.sess.maybe_epoch(1000, 24)
        self.assertTrue(triggered)
        self.assertIsNone(final)   # 调用方映射 413 context_window_exceeded

    def test_engine_store_epoch_turn_tracking(self):
        store = ce.EngineStore()
        sess = store.get_or_create("s1")
        sess.absorb(_tu("q"))
        self.assertFalse(store.is_epoch_turn("s1", 1))
        store.mark_epoch_turn("s1", 1)
        self.assertTrue(store.is_epoch_turn("s1", 1))
        self.assertFalse(store.is_epoch_turn("s1", 2))
        self.assertEqual(store.epoch_count("s1"), 0)


class TestEffectiveParams(unittest.TestCase):
    def test_auto_trigger_tokens(self):
        saved = _ps.PROXY_CTX_EPOCH_TRIGGER_TOKENS
        saved_ctx = _ps.PROXY_CTX_CHARS_LIMIT
        try:
            _ps.PROXY_CTX_EPOCH_TRIGGER_TOKENS = 0  # auto
            # ctx 400K → 65% = 260K chars /4 = 65K < 70K 默认上限
            _ps.PROXY_CTX_CHARS_LIMIT = 400000
            self.assertEqual(ce.effective_trigger_tokens(), 65000)
            # ctx 小 → 取 65% 推导值(< 70K 上限)
            _ps.PROXY_CTX_CHARS_LIMIT = 180000
            self.assertEqual(ce.effective_trigger_tokens(), 29250)
            # 显式值优先
            _ps.PROXY_CTX_EPOCH_TRIGGER_TOKENS = 5000
            self.assertEqual(ce.effective_trigger_tokens(), 5000)
        finally:
            _ps.PROXY_CTX_EPOCH_TRIGGER_TOKENS = saved
            _ps.PROXY_CTX_CHARS_LIMIT = saved_ctx

    def test_auto_window_k(self):
        saved = _ps.PROXY_CTX_WINDOW_K
        try:
            _ps.PROXY_CTX_WINDOW_K = 0
            self.assertEqual(ce.effective_window_k(), ce.DEFAULT_WINDOW_K)
            _ps.PROXY_CTX_WINDOW_K = 8
            self.assertEqual(ce.effective_window_k(), 8)
        finally:
            _ps.PROXY_CTX_WINDOW_K = saved


class TestPipelineWiring(unittest.TestCase):
    """引擎开关联动: on 时 0.5 运行且 7/14/17 跳过; off 时全保持旧行为。"""

    def _ctx(self, msgs, sid="s-pipe"):
        from pipeline import PipelineContext
        return PipelineContext(body={"model": "claude-sonnet-4-6", "messages": msgs}, request_id="r")

    def test_engine_off_old_stages_run(self):
        from pipeline import ContentCompressor, ContextTruncator, OOMSafetyFIFO, ContextEngineStage
        saved = _ps.PROXY_CTX_ENGINE_ENABLED
        try:
            _ps.PROXY_CTX_ENGINE_ENABLED = False
            ctx = self._ctx([_tu("hi")])
            ctx.session_id = "s1"
            self.assertFalse(ContextEngineStage().should_run(ctx))
            # 路由 local 时旧 stage 保持运行
            ctx._route_target = "local"
            self.assertTrue(ContentCompressor().should_run(ctx))
            self.assertTrue(ContextTruncator().should_run(ctx)
                            == _ps.PROXY_CTX_LIMIT_ENABLED)
        finally:
            _ps.PROXY_CTX_ENGINE_ENABLED = saved

    def test_engine_on_old_stages_skip(self):
        from pipeline import (ContentCompressor, ContextTruncator, OOMSafetyFIFO,
                              ContextEngineStage)
        saved = _ps.PROXY_CTX_ENGINE_ENABLED
        try:
            _ps.PROXY_CTX_ENGINE_ENABLED = True
            ctx = self._ctx([_tu("hi")])
            ctx.session_id = "s2"
            ctx._route_target = "local"
            self.assertTrue(ContextEngineStage().should_run(ctx))
            self.assertFalse(ContentCompressor().should_run(ctx))
            self.assertFalse(ContextTruncator().should_run(ctx))
            self.assertFalse(OOMSafetyFIFO().should_run(ctx))
            # 无 session_id → 引擎跳过(canonical 无处安放)
            ctx2 = self._ctx([_tu("hi")])
            self.assertFalse(ContextEngineStage().should_run(ctx2))
        finally:
            _ps.PROXY_CTX_ENGINE_ENABLED = saved

    def test_stage_process_swaps_messages(self):
        import pipeline as pl
        saved = _ps.PROXY_CTX_ENGINE_ENABLED
        try:
            _ps.PROXY_CTX_ENGINE_ENABLED = True
            ce.ENGINE._sessions.pop("s3", None)
            ce.ENGINE._epoch_last_turn.pop("s3", None)
            msgs = _tu("q") + _tool_round("t1", "WebSearch", {"query": "x"}, "y" * 30000)
            ctx = self._ctx(msgs)
            ctx.session_id = "s3"
            ctx = pl.ContextEngineStage().process(ctx)
            self.assertLess(len(str(ctx.messages)), len(str(msgs)))
            # 未见 tool_use 配对回看的 generic 压缩也生效(30000 → <8K)
        finally:
            _ps.PROXY_CTX_ENGINE_ENABLED = saved

    def test_classify_overflow_413(self):
        from loop_detection import _classify_exception
        err = ce.ContextOverflowError("canonical history exceeds epoch budget")
        status, etype, retryable = _classify_exception(err)
        self.assertEqual((status, etype, retryable), (413, "context_window_exceeded", False))


class TestDiagnosticsEpochFields(unittest.TestCase):
    def test_finalize_epoch_backfill_when_engine_on(self):
        import diagnostics as diag
        saved_engine = _ps.PROXY_CTX_ENGINE_ENABLED
        saved_path = _ps._DIAG_SESSIONS_PATH
        import tempfile
        tmp = tempfile.mkdtemp()
        _ps._DIAG_SESSIONS_PATH = os.path.join(tmp, "sessions.jsonl")
        try:
            _ps.PROXY_CTX_ENGINE_ENABLED = True
            ce.ENGINE._sessions.pop("s-diag", None)
            _ps._SESSION_REQUEST_COUNT["s-diag"] = 1  # 生产中由 stage 1 递增
            sess = ce.ENGINE.get_or_create("s-diag")
            sess.absorb(_tu("q"))
            ce.ENGINE.mark_epoch_turn("s-diag", 1)
            diag.begin_request("req_x", "s-diag")
            rec = diag.finalize_request({"session_id": "s-diag", "duration_ms": 10})
            self.assertEqual(rec["epoch_count"], 0)   # epoch 未触发但引擎开启 → 0(非 None)
            self.assertTrue(rec["is_epoch_turn"])     # 本轮被 mark
            # 引擎关闭 → 保持 None(消费方编码兼容)
            _ps.PROXY_CTX_ENGINE_ENABLED = False
            _ps._DIAG_SESSIONS_PATH = os.path.join(tmp, "s2.jsonl")
            diag.begin_request("req_y", "s-diag")
            rec2 = diag.finalize_request({"session_id": "s-diag"})
            self.assertIsNone(rec2["epoch_count"])
        finally:
            _ps.PROXY_CTX_ENGINE_ENABLED = saved_engine
            _ps._DIAG_SESSIONS_PATH = saved_path
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
