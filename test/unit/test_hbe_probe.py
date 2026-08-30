"""Unit tests for hbe_probe — H_BE shadow 探针（只测不动，2026-08-29）。

覆盖：熵计算口径、采样门槛（开关/MIN_CHARS/cadence/in-flight 去重）、
探针成功/无 logprobs/锁超时/后端异常四条路径、落盘记录形态。
全部 mock 后端 HTTP，不打真实服务。
"""
import io
import json
import math
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import hbe_probe
import proxy_state as _ps


def _make_ctx(messages, session_id="sess-hbe", model="test-model"):
    class Ctx:
        pass
    ctx = Ctx()
    ctx.openai_body = {"model": model, "messages": messages}
    ctx.session_id = session_id
    ctx._route_local_model = ""
    return ctx


def _resp_payload(tokens_with_top, content="进度正常。", finish="stop"):
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish,
            "logprobs": {"content": tokens_with_top},
        }],
        "usage": {"prompt_tokens": 1234, "completion_tokens": len(tokens_with_top)},
    }


class _FakeResp(io.BytesIO):
    status = 200

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode("utf-8"))


class TestEntropy(unittest.TestCase):
    """_truncated_entropy_bits：top-k 截断分布熵口径。"""

    def test_deterministic_token(self):
        # 单候选独占（logprob≈0）→ 熵≈0，覆盖≈1
        h, cov = hbe_probe._truncated_entropy_bits([{"token": "a", "logprob": -1e-6}])
        self.assertAlmostEqual(h, 0.0, places=3)
        self.assertAlmostEqual(cov, 1.0, places=3)

    def test_uniform_two(self):
        # 两个等概率候选（各占 0.5）→ 熵=1bit
        p = math.log(0.5)
        h, cov = hbe_probe._truncated_entropy_bits(
            [{"token": "a", "logprob": p}, {"token": "b", "logprob": p}])
        self.assertAlmostEqual(h, 1.0, places=3)
        self.assertAlmostEqual(cov, 1.0, places=3)

    def test_empty_and_invalid(self):
        self.assertEqual(hbe_probe._truncated_entropy_bits([]), (None, 0.0))
        self.assertEqual(
            hbe_probe._truncated_entropy_bits([{"token": "a"}]), (None, 0.0))


class TestScheduleGates(unittest.TestCase):
    """maybe_schedule 采样门槛：不开关/小 payload/非采样轮不触发。"""

    def setUp(self):
        self._saved = {k: getattr(_ps, k) for k in (
            "PROXY_HBE_ENABLED", "PROXY_HBE_MIN_CHARS", "PROXY_HBE_SAMPLE_EVERY")}
        _ps.PROXY_HBE_ENABLED = True
        _ps.PROXY_HBE_MIN_CHARS = 10
        _ps.PROXY_HBE_SAMPLE_EVERY = 4
        # 探针落盘重定向到临时目录
        self._tmp = tempfile.TemporaryDirectory()
        hbe_probe._HBE_PATH = os.path.join(self._tmp.name, "hbe.jsonl")
        _ps._SESSION_REQUEST_COUNT.clear()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(_ps, k, v)
        hbe_probe._HBE_PATH = None
        _ps._SESSION_REQUEST_COUNT.clear()
        self._tmp.cleanup()
        # Thread 被 mock 的成功调度会持有探针锁而不释放——兜底防后续用例卡死
        while hbe_probe._probe_in_flight.locked():
            try:
                hbe_probe._probe_in_flight.release()
            except Exception:
                break

    def _msgs(self, n=30):
        return [{"role": "user", "content": "x" * n}]

    @mock.patch("hbe_probe.threading.Thread")
    def test_disabled_by_default(self, mock_thread):
        _ps.PROXY_HBE_ENABLED = False
        _ps._SESSION_REQUEST_COUNT["sess-hbe"] = 4
        hbe_probe.maybe_schedule(_make_ctx(self._msgs()), "http://b/v1", "k",
                                 threading.Semaphore(1))
        mock_thread.assert_not_called()

    @mock.patch("hbe_probe.threading.Thread")
    def test_small_payload_skipped(self, mock_thread):
        _ps.PROXY_HBE_MIN_CHARS = 10 ** 9
        _ps._SESSION_REQUEST_COUNT["sess-hbe"] = 4
        hbe_probe.maybe_schedule(_make_ctx(self._msgs()), "http://b/v1", "k",
                                 threading.Semaphore(1))
        mock_thread.assert_not_called()

    @mock.patch("hbe_probe.threading.Thread")
    def test_cadence(self, mock_thread):
        _ps._SESSION_REQUEST_COUNT["sess-hbe"] = 3  # 非 4 的倍数
        hbe_probe.maybe_schedule(_make_ctx(self._msgs()), "http://b/v1", "k",
                                 threading.Semaphore(1))
        mock_thread.assert_not_called()
        _ps._SESSION_REQUEST_COUNT["sess-hbe"] = 4
        hbe_probe.maybe_schedule(_make_ctx(self._msgs()), "http://b/v1", "k",
                                 threading.Semaphore(1))
        mock_thread.assert_called_once()
        # 快照必须含模型与消息（线程参数 snapshot）
        snapshot = mock_thread.call_args.kwargs["args"][0]
        self.assertEqual(snapshot["model"], "test-model")
        self.assertEqual(snapshot["messages"], self._msgs())

    @mock.patch("hbe_probe.threading.Thread")
    def test_in_flight_dedup(self, mock_thread):
        _ps._SESSION_REQUEST_COUNT["sess-hbe"] = 4
        hbe_probe._probe_in_flight.acquire()
        try:
            hbe_probe.maybe_schedule(_make_ctx(self._msgs()), "http://b/v1", "k",
                                     threading.Semaphore(1))
            mock_thread.assert_not_called()
        finally:
            hbe_probe._probe_in_flight.release()


class TestRunProbe(unittest.TestCase):
    """_run_probe 四条路径：ok / no_logprobs / skipped_lock / error。"""

    def setUp(self):
        self._saved = {k: getattr(_ps, k) for k in (
            "PROXY_HBE_TOP_LOGPROBS", "PROXY_HBE_MAX_TOKENS",
            "PROXY_HBE_LOCK_WAIT_S", "PROXY_HBE_TIMEOUT_S")}
        _ps.PROXY_HBE_TOP_LOGPROBS = 20
        _ps.PROXY_HBE_MAX_TOKENS = 48
        _ps.PROXY_HBE_LOCK_WAIT_S = 0.1
        _ps.PROXY_HBE_TIMEOUT_S = 10
        self._tmp = tempfile.TemporaryDirectory()
        hbe_probe._HBE_PATH = os.path.join(self._tmp.name, "hbe.jsonl")
        hbe_probe._probe_in_flight.acquire()  # 模拟 maybe_schedule 已占位

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(_ps, k, v)
        hbe_probe._HBE_PATH = None
        self._tmp.cleanup()
        # _run_probe 正常路径会 release；异常中断的测试兜底释放
        while hbe_probe._probe_in_flight.locked():
            try:
                hbe_probe._probe_in_flight.release()
            except Exception:
                break

    def _run(self, lock=None):
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        hbe_probe._run_probe(payload, "http://backend/v1", "key",
                             lock or threading.Semaphore(1),
                             "sess-hbe", 4, "req-1", 25000, "")
        with open(hbe_probe._HBE_PATH, encoding="utf-8") as f:
            return json.loads(f.readline())

    def test_ok(self):
        p = math.log(0.5)
        toks = [{"token": "a", "logprob": p,
                 "top_logprobs": [{"token": "a", "logprob": p},
                                  {"token": "b", "logprob": p}]}] * 3
        # 第 2 个 token 更不确定 → h_max_token_idx=1
        q = math.log(1 / 3)
        toks[1] = {"token": "x", "logprob": q,
                   "top_logprobs": [{"token": c, "logprob": q} for c in "xyz"]}
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp(_resp_payload(toks))):
            rec = self._run()
        self.assertEqual(rec["result"], "ok")
        self.assertAlmostEqual(rec["h_mean_bits"], (1.0 + math.log2(3) + 1.0) / 3, places=3)
        self.assertEqual(rec["n_tokens"], 3)
        self.assertEqual(rec["turn"], 4)
        self.assertEqual(rec["request_id"], "req-1")
        self.assertEqual(rec["prompt_tokens"], 1234)
        # schema v2 新字段
        self.assertEqual(rec["h_max_token_idx"], 1)
        self.assertFalse(rec["answer_truncated"])  # finish=stop
        self.assertEqual(rec["completion_budget"], 48)  # setUp 注入值
        self.assertFalse(hbe_probe._probe_in_flight.locked())

    def test_answer_truncated_and_preview_cap(self):
        p = math.log(0.5)
        toks = [{"token": "a", "logprob": p,
                 "top_logprobs": [{"token": "a", "logprob": p}]}]
        long_answer = "很" * 3000
        payload = _resp_payload(toks, content=long_answer, finish="length")
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp(payload)):
            rec = self._run()
        self.assertTrue(rec["answer_truncated"])  # finish=length
        self.assertEqual(len(rec["answer_preview"]), 2048)  # 2048 上限

    def test_no_logprobs(self):
        payload = _resp_payload([])
        payload["choices"][0]["logprobs"] = None
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp(payload)):
            rec = self._run()
        self.assertEqual(rec["result"], "no_logprobs")
        self.assertNotIn("h_mean_bits", rec)

    def test_lock_timeout_skips(self):
        busy = threading.Semaphore(1)
        busy.acquire()
        try:
            with mock.patch("urllib.request.urlopen") as mock_url:
                rec = self._run(lock=busy)
            mock_url.assert_not_called()
            self.assertEqual(rec["result"], "skipped_lock")
        finally:
            busy.release()

    def test_backend_error_failopen(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("conn refused")):
            rec = self._run()
        self.assertEqual(rec["result"], "error")
        self.assertIn("URLError", rec["error"])
        self.assertFalse(hbe_probe._probe_in_flight.locked())


class TestConfigWiring(unittest.TestCase):
    """配置接线：registry 默认值 / proxy_state 物化 / _RELOAD_SPEC 条目齐全。"""

    def test_registry_defaults(self):
        import proxy_config
        for key in ("PROXY_HBE_ENABLED", "PROXY_HBE_MIN_CHARS",
                    "PROXY_HBE_SAMPLE_EVERY", "PROXY_HBE_TOP_LOGPROBS",
                    "PROXY_HBE_MAX_TOKENS", "PROXY_HBE_LOCK_WAIT_S",
                    "PROXY_HBE_TIMEOUT_S"):
            self.assertIn(key, proxy_config.CONFIG_REGISTRY)
        self.assertEqual(
            proxy_config.CONFIG_REGISTRY["PROXY_HBE_ENABLED"]["defaults"]["all"],
            "false")

    def test_reload_spec(self):
        spec_keys = {e[0] for e in _ps._RELOAD_SPEC}
        for key in ("PROXY_HBE_ENABLED", "PROXY_HBE_MIN_CHARS",
                    "PROXY_HBE_SAMPLE_EVERY", "PROXY_HBE_TOP_LOGPROBS",
                    "PROXY_HBE_MAX_TOKENS", "PROXY_HBE_LOCK_WAIT_S",
                    "PROXY_HBE_TIMEOUT_S"):
            self.assertIn(key, spec_keys)

    def test_validate_no_unregistered(self):
        import proxy_config
        env = {k: "1" for k in (
            "PROXY_HBE_ENABLED", "PROXY_HBE_MIN_CHARS", "PROXY_HBE_SAMPLE_EVERY",
            "PROXY_HBE_TOP_LOGPROBS", "PROXY_HBE_MAX_TOKENS",
            "PROXY_HBE_LOCK_WAIT_S", "PROXY_HBE_TIMEOUT_S")}
        self.assertEqual(proxy_config.list_unregistered_env_vars(env), [])


if __name__ == "__main__":
    unittest.main()
