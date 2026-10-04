#!/usr/bin/env python3
"""test_micro_turn_narration.py — L-62: 旁白文本不得掐灭 ctx_recall 微轮自答。

出典(EXP-12 轮1, 2026-09-28, run 081154): 38/38 次 ctx_recall 调用都伴随
模型旁白文本(182–1828 chars, 例 "I'm stuck in a loop. Let me break out and
take a completely different approach. I'll use `ctx_recall` …")。旧行为在首个
工具 delta 处把 rescue 缓冲文本补发(`_flush_pending_text`)⇒
`text_block_started=True` ⇒ 微轮门槛 `not text_block_started` 永不过 ⇒
调用漏给客户端(工具面不含 ctx_recall)⇒ 38/38 回 No such tool available、
死环烧 38 轮。同批另两会话因调用落在无文本响应而正常开火(17/6 次)——
即"机制可用性被模型嘴型决定"，非客户端差异。

本组钉死四条不变量:
  1) 旁白持有 → 客户端不可见, 随 follow-up 的 assistant 消息交给模型;
  2) 重派不可用/预算耗尽/开关关闭 → 旁白照常补发(fail-open, 块序 text→tools);
  3) 非 ctx_recall 工具与关闭态 → 旧行为逐位不变;
  4) 降级("旁白已发射 ⇒ 回落路径 A")必须留日志(此前完全静默)。
"""
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import anthropic_proxy as proxy  # noqa: E402


class _FakeWfile:
    def __init__(self):
        self.chunks = []

    def write(self, b):
        self.chunks.append(b)

    def flush(self):
        pass


class _FakeHandler:
    """只承载流式中继所需的状态面(与 test_proxy_fallback 的 fake 同形)。"""

    _sse_heartbeat_wanted = False

    def __init__(self, micro_dispatch=None):
        self._openai_mode = False
        self._request_id = "req_l62"
        self._route_response_headers = None
        self._queue_response_headers = None
        self._diag_response_headers = {}
        self._last_jsonl_token = "l62"
        self._micro_recall_dispatch = micro_dispatch
        self._micro_continuation = False
        self.wfile = _FakeWfile()

    def send_response(self, code):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    def _send_diag_headers(self):
        pass

    def _send_sse_stream_headers(self):
        self._sse_head_sent = True

    def _heartbeat_lines(self, resp):
        return proxy.Handler._heartbeat_lines(self, resp)

    # 读回客户端实际收到的 SSE 文本
    def client_text(self):
        return b"".join(self.wfile.chunks).decode("utf-8", "replace")


def _chunk(delta=None, finish_reason=None):
    obj = {"id": "chatcmpl-l62", "object": "chat.completion.chunk"}
    choice = {"index": 0, "delta": delta or {}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    obj["choices"] = [choice]
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def _resp(*lines):
    return io.BytesIO(b"".join(lines))


NARRATION = "I'm stuck in a loop. Let me break out and take a completely different approach."


def _tool_delta(name, args='{"query": "delegate_to"}', idx=0, tc_id="call_a"):
    return _chunk({"tool_calls": [{"index": idx, "id": tc_id, "type": "function",
                                   "function": {"name": name, "arguments": args}}]})


def _text_delta(t):
    return _chunk({"content": t})


DONE = b"data: [DONE]\n\n"


def _follow_up(content=""):
    return [{"role": "assistant", "content": content,
             "tool_calls": [{"id": "call_a", "type": "function",
                             "function": {"name": "ctx_recall",
                                          "arguments": '{"query": "delegate_to"}'}}]},
            {"role": "tool", "tool_call_id": "call_a", "content": "ctx_recall: 命中 1 条"}]


class _RelayCase(unittest.TestCase):
    """公共夹具: rescue 开、微轮开、旁白持有开(生产默认)。"""

    def _run(self, handler, resp, *, hold=True, micro=True, rescued=True,
             dispatch=None, log_sink=None):
        if dispatch is not None:
            handler._micro_recall_dispatch = dispatch
        patches = [
            patch.object(proxy, "log", side_effect=(log_sink.append if log_sink is not None else lambda *a, **k: None)),
            patch.object(proxy, "PROXY_DIAG_ENABLED", False),
            patch.object(proxy._log_ctx, "session_id", "sid-l62", create=True),
            patch.object(proxy._ps, "PROXY_RESCUE_ENABLED", rescued, create=True),
            patch.object(proxy._ps, "PROXY_PD_ENABLED", True),
            patch.object(proxy._ps, "PROXY_PD_MICRO_TURN_ENABLED", micro),
            patch.object(proxy._ps, "PROXY_PD_MICRO_TURN_HOLD_TEXT", hold),
        ]
        body = {"model": "claude-sonnet-4-6", "max_tokens": 4096}
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        proxy.Handler._handle_streaming_response(handler, resp, body)


class TestNarrationHeld(_RelayCase):
    def test_narration_held_and_self_answered_client_transparent(self):
        """旁白 + ctx_recall: 客户端既不见旁白也不见调用; 旁白随 follow-up 给模型。"""
        dispatched = []
        h = _FakeHandler(lambda msgs: dispatched.append(msgs) or True)
        resp = _resp(_text_delta(NARRATION), _tool_delta("ctx_recall"),
                     _chunk({}, finish_reason="tool_calls"), DONE)
        with patch("ctx_recall.split_follow_up_messages",
                   return_value=(_follow_up(), True)):
            self._run(h, resp)
        self.assertEqual(len(dispatched), 1, "微轮应重派一次")
        self.assertEqual(dispatched[0][0]["content"], NARRATION,
                         "旁白应随 follow-up 的 assistant 消息交给模型")
        client = h.client_text()
        self.assertNotIn(NARRATION, client, "旁白不得发给客户端")
        self.assertNotIn("text_delta", client, "不得开文本块")
        self.assertNotIn("ctx_recall", client, "幽灵调用不得漏给客户端")

    def test_text_free_call_keeps_follow_up_content_untouched(self):
        """无旁白调用(旧路径): follow-up content 不被改写。"""
        dispatched = []
        h = _FakeHandler(lambda msgs: dispatched.append(msgs) or True)
        resp = _resp(_tool_delta("ctx_recall"), _chunk({}, finish_reason="tool_calls"), DONE)
        with patch("ctx_recall.split_follow_up_messages",
                   return_value=(_follow_up(""), True)):
            self._run(h, resp)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0][0]["content"], "")

    def test_mixed_batch_second_tool_flushes_narration(self):
        """混合批(cr 先到、其它工具后到): 旁白照旧发射, 且留下降级日志。"""
        logs = []
        h = _FakeHandler(lambda msgs: False)
        resp = _resp(_text_delta(NARRATION), _tool_delta("ctx_recall", idx=0),
                     _tool_delta("Read", args='{"file_path": "x"}', idx=1, tc_id="call_b"),
                     _chunk({}, finish_reason="tool_calls"), DONE)
        self._run(h, resp, dispatch=lambda msgs: False, log_sink=logs)
        client = h.client_text()
        self.assertIn(NARRATION, client, "非 cr 工具到达时应补发旁白")
        self.assertIn("Read", client)
        self.assertTrue(any("MICRO_TURN] skipped" in ln for ln in logs),
                        "降级必须可见: %r" % logs)


class TestFailOpen(_RelayCase):
    def test_budget_exhausted_flushes_and_emits_call(self):
        """重派闭包返回 False(预算耗尽): 旁白补发 + 调用照常发射, 块序 text→tools。"""
        logs = []
        h = _FakeHandler(None)
        resp = _resp(_text_delta(NARRATION), _tool_delta("ctx_recall"),
                     _chunk({}, finish_reason="tool_calls"), DONE)
        self._run(h, resp, dispatch=lambda msgs: False, log_sink=logs)
        client = h.client_text()
        self.assertIn(NARRATION, client, "fail-open: 旁白必须补发(不得丢失)")
        self.assertIn("ctx_recall", client, "fail-open: 调用照常发射(回落路径 A)")
        self.assertLess(client.index("text_delta"), client.index("ctx_recall"),
                        "块序必须 text(index0) → tools(index1+)")
        self.assertTrue(any("MICRO_TURN] skipped" in ln for ln in logs))

    def test_dispatch_hook_missing_flushes_narration(self):
        """无重派钩子(非流式/异常路径): 旁白与调用都照常发射。"""
        h = _FakeHandler(None)
        resp = _resp(_text_delta(NARRATION), _tool_delta("ctx_recall"),
                     _chunk({}, finish_reason="tool_calls"), DONE)
        self._run(h, resp)
        client = h.client_text()
        self.assertIn(NARRATION, client)
        self.assertIn("ctx_recall", client)


class TestLegacyBehaviorUnchanged(_RelayCase):
    def test_hold_flag_off_matches_legacy(self):
        """开关关闭: 旁白在首个工具 delta 处即发射, 不重派(旧行为)。"""
        called = []
        h = _FakeHandler(lambda msgs: called.append(msgs) or True)
        resp = _resp(_text_delta(NARRATION), _tool_delta("ctx_recall"),
                     _chunk({}, finish_reason="tool_calls"), DONE)
        with patch("ctx_recall.split_follow_up_messages",
                   return_value=(_follow_up(), True)):
            self._run(h, resp, hold=False)
        self.assertEqual(called, [], "关闭态不得重派")
        client = h.client_text()
        self.assertIn(NARRATION, client)
        self.assertIn("ctx_recall", client)

    def test_micro_turn_flag_off_matches_legacy(self):
        called = []
        h = _FakeHandler(lambda msgs: called.append(msgs) or True)
        resp = _resp(_text_delta(NARRATION), _tool_delta("ctx_recall"),
                     _chunk({}, finish_reason="tool_calls"), DONE)
        self._run(h, resp, micro=False)
        self.assertEqual(called, [])
        client = h.client_text()
        self.assertIn(NARRATION, client)
        self.assertIn("ctx_recall", client)

    def test_non_recall_tool_unaffected(self):
        """普通工具(Read): 文本在首个 delta 处补发, 不进入持有。"""
        called = []
        h = _FakeHandler(lambda msgs: called.append(msgs) or True)
        resp = _resp(_text_delta(NARRATION),
                     _tool_delta("Read", args='{"file_path": "x"}'),
                     _chunk({}, finish_reason="tool_calls"), DONE)
        self._run(h, resp)
        self.assertEqual(called, [])
        client = h.client_text()
        self.assertIn(NARRATION, client)
        self.assertIn("Read", client)


if __name__ == "__main__":
    unittest.main()
