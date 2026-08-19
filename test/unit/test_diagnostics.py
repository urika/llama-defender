"""Unit tests for diagnostics.py — R13/R16 diagnostics data plane.

Covers: hit_ratio math, SSE tail serialization, header contract (absent fields
when unknown — P1 时序诚实), per-request accumulation (injections/timings
probe/token facts), sessions.jsonl persistence + read-back, lifecycle events.
设计依据: docs/02-architecture-design/diagnostics-dataplane-design-20260819.md
"""
import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import proxy_state as _ps
import diagnostics as diag


class TestHitRatio(unittest.TestCase):
    """compute_hit_ratio — 上游设计 §10.2① 公式 + 边界。"""

    def test_basic(self):
        self.assertEqual(diag.compute_hit_ratio(412, 98347), 0.9958)

    def test_full_cache_hit(self):
        self.assertEqual(diag.compute_hit_ratio(0, 1000), 1.0)

    def test_full_recompute(self):
        self.assertEqual(diag.compute_hit_ratio(1000, 1000), 0.0)

    def test_missing_inputs_return_none(self):
        self.assertIsNone(diag.compute_hit_ratio(None, 100))
        self.assertIsNone(diag.compute_hit_ratio(10, None))
        self.assertIsNone(diag.compute_hit_ratio("x", 10))

    def test_zero_or_negative_sent_returns_none(self):
        self.assertIsNone(diag.compute_hit_ratio(0, 0))
        self.assertIsNone(diag.compute_hit_ratio(10, 0))
        self.assertIsNone(diag.compute_hit_ratio(-1, 100))

    def test_processed_exceeds_sent_clamps_to_zero(self):
        """后端语义异常保护（processed > sent → 0 命中，不产生 >1 的假命中率）。"""
        self.assertEqual(diag.compute_hit_ratio(200, 100), 0.0)


class TestSseTail(unittest.TestCase):
    """sse_tail_line — R13 流式通道（SSE 注释行，规范保证被忽略）。"""

    def test_format(self):
        line = diag.sse_tail_line({"prompt_processed_n": 412, "hit_ratio": 0.9958})
        self.assertTrue(line.startswith(": x-proxy-diag "))
        self.assertTrue(line.endswith("\n\n"))
        payload = json.loads(line[len(": x-proxy-diag "):].strip())
        self.assertEqual(payload["prompt_processed_n"], 412)

    def test_chinese_content_serializes(self):
        line = diag.sse_tail_line({"note": "中文诊断"})
        self.assertIn("中文诊断", line)


class TestDiagHeaders(unittest.TestCase):
    """diag_headers — 值未知的字段不出现（设计 P1：永不发占位假值）。"""

    def test_minimal(self):
        self.assertEqual(diag.diag_headers("req_1", []),
                         {"X-Proxy-Diag-Request-Id": "req_1"})

    def test_with_injections(self):
        h = diag.diag_headers("req_1", ["loop_l1", "blocker"])
        self.assertEqual(h["X-Proxy-Feedback-Injected"], "loop_l1,blocker")

    def test_with_processed_n(self):
        h = diag.diag_headers("req_1", [], 412)
        self.assertEqual(h["X-Proxy-Prompt-Processed-N"], "412")

    def test_processed_n_none_absent(self):
        self.assertNotIn("X-Proxy-Prompt-Processed-N", diag.diag_headers("req_1", [], None))

    def test_no_request_id_no_header(self):
        self.assertEqual(diag.diag_headers("", ["blocker"]),
                         {"X-Proxy-Feedback-Injected": "blocker"})


class TestPerRequestAccumulation(unittest.TestCase):
    """thread-local 累积：injection 登记 / timings 探测 / token 事实。"""

    def setUp(self):
        self._saved_enabled = _ps.PROXY_DIAG_ENABLED
        _ps.PROXY_DIAG_ENABLED = True
        diag.begin_request("req_test", "sess_a", "header")

    def tearDown(self):
        _ps.PROXY_DIAG_ENABLED = self._saved_enabled

    def test_record_and_peek_injections_dedup(self):
        diag.record_injection("loop_l1")
        diag.record_injection("blocker")
        diag.record_injection("loop_l1")  # 重复登记去重
        self.assertEqual(diag.peek_injections(), ["loop_l1", "blocker"])

    def test_build_diag_payload_fields(self):
        diag.record_injection("blocker")
        diag.set_prompt_tokens(sent_n=98347)
        diag.set_prompt_tokens(processed_n=412, prompt_eval_ms=610.0)
        payload = diag.build_diag_payload()
        self.assertEqual(payload["request_id"], "req_test")
        self.assertEqual(payload["prompt_sent_n"], 98347)
        self.assertEqual(payload["prompt_processed_n"], 412)
        self.assertEqual(payload["hit_ratio"], 0.9958)
        self.assertEqual(payload["feedback_injected"], ["blocker"])

    def test_build_diag_payload_contains_session_key(self):
        """G-B: 恒含 session_key——metering 归因落会话无需自行实现 8 字符截断。"""
        payload = diag.build_diag_payload()
        self.assertEqual(payload.get("session_key"), "sess_a")

    def test_build_diag_payload_omits_unknown(self):
        payload = diag.build_diag_payload()
        self.assertNotIn("hit_ratio", payload)
        self.assertNotIn("feedback_injected", payload)

    def test_set_prompt_tokens_no_none_overwrite(self):
        """流式场景先 timings 后 usage 两次回填——usage 不得清掉 timings 阶段的 ms。"""
        diag.set_prompt_tokens(processed_n=412, prompt_eval_ms=610.0,
                               generation_n=10, gen_ms=45.6)
        diag.set_prompt_tokens(sent_n=98347, generation_n=20)  # usage 终块,无 ms
        self.assertEqual(_ps._diag_ctx.prompt_eval_ms, 610.0)
        self.assertEqual(_ps._diag_ctx.gen_ms, 45.6)
        self.assertEqual(_ps._diag_ctx.generation_tokens, 20)  # token 数可刷新

    def test_timings_probe_once(self):
        with diag._timings_probe_lock:
            saved = dict(diag._timings_state)
        try:
            diag._timings_state["supported"] = None
            self.assertIsNone(diag.timings_supported())
            self.assertTrue(diag.probe_timings({"prompt_n": 5}))
            self.assertTrue(diag.timings_supported())
            self.assertTrue(diag.probe_timings({"prompt_n": 6}))  # 幂等
            self.assertFalse(diag.probe_timings(None))            # 非 dict 不置位
            self.assertTrue(diag.timings_supported())             # 状态不被非 dict 复位
        finally:
            diag._timings_state.clear()
            diag._timings_state.update(saved)

    def test_timings_probe_off_mode(self):
        saved_src = _ps.PROXY_DIAG_TIMINGS_SOURCE
        try:
            _ps.PROXY_DIAG_TIMINGS_SOURCE = "off"
            self.assertFalse(diag.probe_timings({"prompt_n": 5}))
            self.assertFalse(diag.timings_supported())
        finally:
            _ps.PROXY_DIAG_TIMINGS_SOURCE = saved_src


class TestPersistence(unittest.TestCase):
    """sessions.jsonl 落盘 + 读取 + lifecycle events（R16 / R7）。"""

    def setUp(self):
        self._saved_enabled = _ps.PROXY_DIAG_ENABLED
        _ps.PROXY_DIAG_ENABLED = True
        self._tmp = tempfile.mkdtemp(prefix="diag_test_")
        self._saved_path = _ps._DIAG_SESSIONS_PATH
        self._saved_events = _ps._LIFECYCLE_EVENTS_PATH
        _ps._DIAG_SESSIONS_PATH = os.path.join(self._tmp, "sessions.jsonl")
        _ps._LIFECYCLE_EVENTS_PATH = os.path.join(self._tmp, "lifecycle_events.jsonl")
        _ps._SESSION_REQUEST_COUNT["sess_persist"] = 7
        diag.begin_request("req_p1", "sess_persist", "header")

    def tearDown(self):
        _ps.PROXY_DIAG_ENABLED = self._saved_enabled
        _ps._DIAG_SESSIONS_PATH = self._saved_path
        _ps._LIFECYCLE_EVENTS_PATH = self._saved_events
        _ps._SESSION_REQUEST_COUNT.pop("sess_persist", None)

    def test_finalize_writes_record(self):
        diag.set_route("local", "test-model")
        diag.set_prompt_tokens(sent_n=1000, processed_n=100)
        mc = {"session_id": "sess_persist", "ttft_ms": 12.5, "duration_ms": 300.0,
              "compression_ratio": 0.4, "pipeline": {"truncate": {"strategy": "fifo"}}}
        rec = diag.finalize_request(mc)
        self.assertEqual(rec["session_key"], "sess_persist")
        self.assertEqual(rec["turn"], 7)
        self.assertEqual(rec["hit_ratio"], 0.9)
        self.assertEqual(rec["route_target"], "local")
        self.assertEqual(rec["compression"]["mode"], "fifo")
        # Phase 1 字段名先定死、值为 null（消费方可先行编码）
        self.assertIsNone(rec["epoch_count"])
        self.assertIsNone(rec["is_epoch_turn"])
        # 落盘可读回
        records = diag.read_session_metrics("sess_persist")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["request_id"], "req_p1")

    def test_finalize_skips_without_request_id(self):
        _ps._diag_ctx.request_id = None
        self.assertIsNone(diag.finalize_request({}))

    def test_canonical_mismatch_writes_lifecycle_event(self):
        diag.mark_canonical_mismatch()
        diag.finalize_request({"session_id": "sess_persist"})
        with open(_ps._LIFECYCLE_EVENTS_PATH) as f:
            events = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "canonical_mismatch")
        self.assertEqual(events[0]["session_key"], "sess_persist")

    def test_read_session_metrics_missing_file(self):
        _ps._DIAG_SESSIONS_PATH = os.path.join(self._tmp, "nope.jsonl")
        self.assertEqual(diag.read_session_metrics("sess_persist"), [])

    def test_read_session_metrics_filters_other_sessions(self):
        diag.finalize_request({"session_id": "sess_persist"})
        self.assertEqual(diag.read_session_metrics("other"), [])


class TestDisabledPlane(unittest.TestCase):
    """PROXY_DIAG_ENABLED=false → 零行为（设计 §8：关=零开销零字段）。"""

    def test_disabled_no_accumulation(self):
        saved = _ps.PROXY_DIAG_ENABLED
        try:
            _ps.PROXY_DIAG_ENABLED = False
            diag.begin_request("r", "s", "header")  # no-op
            diag.record_injection("blocker")        # no-op
            self.assertEqual(diag.peek_injections(), [])
        finally:
            _ps.PROXY_DIAG_ENABLED = saved


class TestWarnSuppressed(unittest.TestCase):
    """评审 P2: 诊断层异常可见性——fail-open 但每挂点前 N 次记 WARN。"""

    def setUp(self):
        diag._exception_log_counts.clear()

    def test_first_n_logged_then_silent(self):
        from unittest.mock import patch
        with patch("proxy_logging.log") as mlog:
            diag.warn_suppressed("site_a", ValueError("boom"))
            diag.warn_suppressed("site_a", ValueError("boom2"))
            diag.warn_suppressed("site_a", ValueError("boom3"))
            diag.warn_suppressed("site_a", ValueError("boom4"))  # 超限静默
        self.assertEqual(mlog.call_count, 3)
        for call in mlog.call_args_list:
            args, kwargs = call
            self.assertEqual(kwargs.get("level"), "WARN")
            self.assertIn("site_a", args[0])

    def test_per_site_limit_independent(self):
        from unittest.mock import patch
        with patch("proxy_logging.log") as mlog:
            diag.warn_suppressed("site_a", ValueError("x"))
            diag.warn_suppressed("site_a", ValueError("x"))
            diag.warn_suppressed("site_a", ValueError("x"))
            diag.warn_suppressed("site_b", ValueError("y"))
            diag.warn_suppressed("site_b", ValueError("y"))
        self.assertEqual(mlog.call_count, 5)

    def test_none_exc_noop(self):
        from unittest.mock import patch
        with patch("proxy_logging.log") as mlog:
            diag.warn_suppressed("site_c", None)
        mlog.assert_not_called()


class TestTimingsProbeReset(unittest.TestCase):
    """评审 P2: SIGHUP 重载后 timings 能力探测状态重置（后端可 local↔cloud 切换）。"""

    def test_reset_clears_probe_state(self):
        diag._timings_state["supported"] = True
        diag.reset_timings_probe()
        self.assertIsNone(diag._timings_state["supported"])

    def test_probe_rearms_after_reset(self):
        diag._timings_state["supported"] = None
        self.assertTrue(diag.probe_timings({"prompt_n": 1}))
        self.assertIs(diag.timings_supported(), True)
        diag.reset_timings_probe()
        self.assertIsNone(diag.timings_supported())
        self.assertTrue(diag.probe_timings({"prompt_n": 1}))
        self.assertIs(diag.timings_supported(), True)


class TestPassthroughSseTail(unittest.TestCase):
    """评审 P1: 透传路径（anthropic 协议云端）诊断尾注必须位于 message_stop 之前。

    设计 D1: 解析器收到终止事件后停止读取——流尾追加的尾注会被静默丢弃。
    修复后: 拦截含 message_stop 的 chunk,尾注写在其前;流异常截断时兜底追加。
    """

    def _run(self, chunks):
        import io
        from unittest.mock import patch
        import anthropic_proxy as proxy
        h = proxy.Handler.__new__(proxy.Handler)
        h.wfile = io.BytesIO()
        h.send_response = lambda *a, **k: None
        h.send_header = lambda *a, **k: None
        h._send_common_headers = lambda *a, **k: None
        h.end_headers = lambda: None
        saved_mc = getattr(proxy._metrics_ctx, "mc", None)
        proxy._metrics_ctx.mc = None
        try:
            with patch.object(proxy, "PROXY_DIAG_ENABLED", True), \
                    patch.object(proxy, "PROXY_DIAG_SSE_TAIL", True):
                h._handle_anthropic_stream_passthrough(iter(chunks), {})
        finally:
            proxy._metrics_ctx.mc = saved_mc
        return h.wfile.getvalue().decode("utf-8")

    def test_tail_before_message_stop(self):
        out = self._run([
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta"}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ])
        self.assertEqual(out.count("x-proxy-diag"), 1)
        self.assertLess(out.index("x-proxy-diag"), out.index("event: message_stop"))

    def test_tail_before_message_stop_single_chunk(self):
        # message_stop 与正文在同一 chunk(上游常见)——尾注仍在其前
        out = self._run([
            b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n'
            b'event: message_delta\ndata: {"type":"message_delta"}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ])
        self.assertEqual(out.count("x-proxy-diag"), 1)
        self.assertLess(out.index("x-proxy-diag"), out.index("event: message_stop"))

    def test_fallback_tail_when_stream_truncated(self):
        # 流被截断(无 message_stop)——尾注兜底追加在结尾,诊断仍可达
        out = self._run([
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
        ])
        self.assertEqual(out.count("x-proxy-diag"), 1)
        self.assertTrue(out.rstrip().endswith("}"))

    def test_tail_disabled_no_tail(self):
        import io
        from unittest.mock import patch
        import anthropic_proxy as proxy
        h = proxy.Handler.__new__(proxy.Handler)
        h.wfile = io.BytesIO()
        h.send_response = lambda *a, **k: None
        h.send_header = lambda *a, **k: None
        h._send_common_headers = lambda *a, **k: None
        h.end_headers = lambda: None
        saved_mc = getattr(proxy._metrics_ctx, "mc", None)
        proxy._metrics_ctx.mc = None
        try:
            with patch.object(proxy, "PROXY_DIAG_SSE_TAIL", False):
                h._handle_anthropic_stream_passthrough(iter([
                    b'event: message_stop\ndata: {"type":"message_stop"}\n\n']), {})
        finally:
            proxy._metrics_ctx.mc = saved_mc
        self.assertNotIn("x-proxy-diag", h.wfile.getvalue().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
