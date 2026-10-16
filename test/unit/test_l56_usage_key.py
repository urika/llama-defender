#!/usr/bin/env python3
"""L-56 回归: 流式 usage 回填的引擎会话键归因(DEF-309 口径对齐)。

背景(swe-eval 台账 L-56; 补丁档 results/patches/ctx-usage-session-key-20260928,
根因件 OBSERVATION-20260928-0612-ctx-trigger-scale-backfill): 流式 usage 回填
曾写 `_log_ctx.session_id`(8 字符日志显示 id), 而 ctx 引擎 stage 读
`ctx.session_id` = `_log_ctx.session_key`(全量 header sid, pipeline
SessionExtractStage 同源取键) ⇒ 同一真实会话在引擎成两个对象, 长键对象的
last_sent_tokens 恒 0, `_trigger_scale()` 退化为只剩 est×1.6(EXP-12 轮1 实测
有效触发 ≈83K 真实 token vs 名义 S=70K)。

修复(82b0e57, 2026-09-29): 取键与 pipeline 同写法——session_key 优先、
session_id 回落。本测试构造假 usage 响应驱动真实流式路径
(`Handler._handle_streaming_response`), 以 stub context_engine.ENGINE 捕获
回填键归因, 防后续演进回退到 8 字符键。

运行: cd llama.cpp && python3 -m pytest test/unit/test_l56_usage_key.py -q
"""

import io
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import anthropic_proxy  # noqa: E402
import proxy_state as _ps  # noqa: E402

# L-56 实测同款键对: 全量 header sid vs 其 8 字符日志显示 id
_FULL_KEY = "sweaecef26a-instance_ansible__ansible-0ea40e09d1b35b"
_SHORT_KEY = _FULL_KEY[:8]


class _RecordingSession:
    """记录自身键与 record_usage 入参; 返回非 None hit 使日志路径全开。"""

    def __init__(self, engine, key):
        self._engine = engine
        self._key = key

    def record_usage(self, prompt_tokens, cached_tokens):
        self._engine.records.append((self._key, prompt_tokens, cached_tokens))
        return 0.5


class _RecordingEngine:
    """替代 context_engine.ENGINE: 记录 get_or_create 的键(归因断言面)。"""

    def __init__(self):
        self.created = []
        self.records = []

    def get_or_create(self, key):
        self.created.append(key)
        return _RecordingSession(self, key)


class TestL56UsageKeyAttribution(unittest.TestCase):
    """流式 usage 回填必须落全量 session_key(与 pipeline 读侧同源)。"""

    def setUp(self):
        # 旗标存/取: 引擎开; 诊断/档案关——聚焦回填键归因, 隔离外部状态
        self._saved = (
            anthropic_proxy.PROXY_DIAG_ENABLED,
            anthropic_proxy.PROXY_REQUEST_ARCHIVE_ENABLED,
            _ps.PROXY_CTX_ENGINE_ENABLED,
        )
        anthropic_proxy.PROXY_DIAG_ENABLED = False
        anthropic_proxy.PROXY_REQUEST_ARCHIVE_ENABLED = False
        _ps.PROXY_CTX_ENGINE_ENABLED = True

        # stub context_engine(方法内 import 走 sys.modules)
        self._engine = _RecordingEngine()
        self._stub = types.ModuleType("context_engine")
        self._stub.ENGINE = self._engine
        self._saved_mod = sys.modules.get("context_engine")
        sys.modules["context_engine"] = self._stub

        # L-56 场景: 两把键并存(do_GET/do_POST 的真实置值形态)
        _ps._log_ctx.session_key = _FULL_KEY
        _ps._log_ctx.session_id = _SHORT_KEY

    def tearDown(self):
        (anthropic_proxy.PROXY_DIAG_ENABLED,
         anthropic_proxy.PROXY_REQUEST_ARCHIVE_ENABLED,
         _ps.PROXY_CTX_ENGINE_ENABLED) = self._saved
        if self._saved_mod is not None:
            sys.modules["context_engine"] = self._saved_mod
        else:
            sys.modules.pop("context_engine", None)
        _ps._log_ctx.session_key = None
        _ps._log_ctx.session_id = None

    def _run_stream(self):
        """最小驱动: 微轮续流形态(跳过头段/message_start), 纯 usage 流。

        wfile=BytesIO 收尾事件; resp 为普通行迭代(无 fp → 看门狗裸迭代
        降级, 与 _timed_stream_lines 降级语义一致)。
        """
        h = object.__new__(anthropic_proxy.Handler)
        h.wfile = io.BytesIO()
        h._micro_continuation = True
        h._sse_heartbeat_wanted = False
        h._last_jsonl_token = "l56-test"
        resp = [
            b'data: {"choices":[],"usage":{"prompt_tokens":36614,'
            b'"completion_tokens":5,'
            b'"prompt_tokens_details":{"cached_tokens":36384}}}\n',
            b"data: [DONE]\n",
        ]
        h._handle_streaming_response(resp, {"model": "test-model",
                                            "max_tokens": 64})
        return h

    def test_full_session_key_used_for_usage_backfill(self):
        """主断言: 回填落全量 session_key; 8 字符显示 id 不再成为引擎键。"""
        self._run_stream()
        self.assertEqual(self._engine.created, [_FULL_KEY])
        self.assertEqual(self._engine.records,
                         [(_FULL_KEY, 36614, 36384)])
        self.assertNotIn(_SHORT_KEY, self._engine.created)

    def test_fallback_to_session_id_when_key_absent(self):
        """回落语义: session_key 缺席(旧代理路径/测试)时回落 session_id。"""
        _ps._log_ctx.session_key = None
        self._run_stream()
        self.assertEqual(self._engine.created, [_SHORT_KEY])

    def test_no_key_no_backfill(self):
        """两键皆缺: 回填跳过, 不建幽灵引擎会话、不崩。"""
        _ps._log_ctx.session_key = None
        _ps._log_ctx.session_id = None
        self._run_stream()
        self.assertEqual(self._engine.created, [])
        self.assertEqual(self._engine.records, [])


if __name__ == "__main__":
    unittest.main()
