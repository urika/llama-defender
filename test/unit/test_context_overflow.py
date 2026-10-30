"""会话级上下文溢出终态化（DEF-329）——分类器看穿包装 + 溢出闩短路。

背景（2026-10-29 22:00 实测）：ctx engine 硬上限溢出（ContextOverflowError）被
pipeline 包成 RuntimeError ⇒ 错误分类器只看最外层类型 ⇒ 落兜底 500 unknown_error
⇒ 客户端按 5xx 无限重试（**1634 次**，每次重传 699K chars，顺带把 swap 顶到 96%）。
§4.9 契约本应回 413 且不可重试（会话该结束或提高 ctx 预算）。

两处修：
① `loop_detection._classify_exception` 沿 `__cause__` 链找最特异类型
   （ContextOverflowError → 413/context_window_exceeded/不可重试）。
② `anthropic_proxy` 会话级溢出闩：同会话 + 请求体未变小 ⇒ 直接 413 短路
   （不进管线/引擎队列）；请求变小 ⇒ 放行（客户端裁剪历史后可续）。
"""
import os
import sys
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import anthropic_proxy as proxy
import loop_detection


class TestClassifyThroughWrapper(unittest.TestCase):
    """分类器必须看穿 pipeline 的 RuntimeError 包装（raise ... from e）。"""

    @staticmethod
    def _wrap(exc):
        try:
            try:
                raise exc
            except Exception as e:
                raise RuntimeError(
                    f"Pipeline stage 'context_engine' failed: "
                    f"{type(e).__name__}: {e}") from e
        except Exception as w:
            return w

    def test_wrapped_context_overflow_is_413_terminal(self):
        e = self._wrap(RuntimeError(
            "canonical history exceeds epoch budget even after collapse "
            "(K=4 + L3 halved); session should end or ctx budget raised"))
        # 造一个同名异常类（避免依赖 context_engine 内部结构）
        e.__cause__ = type("ContextOverflowError", (Exception,), {})(
            "canonical history exceeds epoch budget even after collapse")
        status, etype, retryable = loop_detection._classify_exception(e)
        self.assertEqual(status, 413)
        self.assertEqual(etype, "context_window_exceeded")
        self.assertFalse(retryable)

    def test_direct_context_overflow_still_413(self):
        exc = type("ContextOverflowError", (Exception,), {})("boom")
        self.assertEqual(loop_detection._classify_exception(exc),
                         (413, "context_window_exceeded", False))

    def test_wrapped_timeout_still_504_retryable(self):
        e = self._wrap(TimeoutError("read timed out"))
        status, etype, retryable = loop_detection._classify_exception(e)
        self.assertEqual(status, 504)
        self.assertEqual(etype, "timeout_error")
        self.assertTrue(retryable)

    def test_unrelated_error_unchanged(self):
        status, etype, retryable = loop_detection._classify_exception(ValueError("bad"))
        self.assertEqual(status, 500)
        self.assertEqual(etype, "internal_error")
        self.assertFalse(retryable)

    def test_exception_chain_no_cycle(self):
        """cause 链含环时不死循环。"""
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        chain = loop_detection._exception_chain(a)
        self.assertLessEqual(len(chain), 6)


class TestOverflowLatch(unittest.TestCase):
    """会话级溢出闩：同量级短路 413；请求变小放行；TTL 过期放行。"""

    def setUp(self):
        proxy._SESSION_OVERFLOW.clear()

    def tearDown(self):
        proxy._SESSION_OVERFLOW.clear()

    def test_same_size_short_circuits_413(self):
        proxy._overflow_latch_set("s1", 699_176)
        body = proxy._overflow_latch_check("s1", 699_176, "req_1")
        self.assertIsNotNone(body)
        err = body["error"]
        self.assertEqual(err["type"], "context_window_exceeded")
        self.assertFalse(err["retryable"])
        self.assertIn("699,176", err["message"])

    def test_grown_request_also_short_circuits(self):
        proxy._overflow_latch_set("s1", 699_176)
        self.assertIsNotNone(proxy._overflow_latch_check("s1", 800_000))

    def test_shrunk_request_allowed(self):
        """客户端裁剪历史后请求变小 ⇒ 放行（不做永久封禁）。"""
        proxy._overflow_latch_set("s1", 699_176)
        self.assertIsNone(proxy._overflow_latch_check("s1", 500_000))

    def test_other_session_unaffected(self):
        proxy._overflow_latch_set("s1", 699_176)
        self.assertIsNone(proxy._overflow_latch_check("s2", 699_176))

    def test_ttl_expiry_allows_again(self):
        proxy._overflow_latch_set("s1", 699_176)
        proxy._SESSION_OVERFLOW["s1"]["ts"] = time.time() - (proxy._SESSION_OVERFLOW_TTL + 5)
        self.assertIsNone(proxy._overflow_latch_check("s1", 699_176))

    def test_latch_set_ignores_empty_inputs(self):
        proxy._overflow_latch_set("", 100)
        proxy._overflow_latch_set("s1", 0)
        self.assertEqual(proxy._SESSION_OVERFLOW, {})

    def test_latch_max_sessions_evicts(self):
        for i in range(proxy._SESSION_OVERFLOW_MAX + 10):
            proxy._overflow_latch_set(f"s{i}", 1000)
        self.assertLessEqual(len(proxy._SESSION_OVERFLOW), proxy._SESSION_OVERFLOW_MAX)


if __name__ == "__main__":
    unittest.main()
