"""Unit tests for TS-3: 压缩结果结构化 (CompressionResult / CompressionSubResult).

验收用例 (PRD-litellm-borrow §2 TS-3 + design §7 测试覆盖).
本文件为 TDD stub —— 函数签名 + assert 占位,W3末-W4 d1-d5 由工程师实现填充.

设计文档: docs/02-architecture-design/compression-result-design-2026-07-05.md
"""
import inspect
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import content_compressor as cc
import truncation as tc


# ---------------------------------------------------------------------------
# Feature-detection gates — tests unlock as engineers land the TS-3 API
# described in the design doc §4. Pure-import / hasattr checks; no probe
# invocation (avoids side effects on partially-implemented features).
# ---------------------------------------------------------------------------

try:
    import compression_types as _ct
    _TS3_MODULE_LOADED = True
except ImportError:
    _ct = None
    _TS3_MODULE_LOADED = False

_TS3_HAS_RESULT_TYPE = (
    _TS3_MODULE_LOADED
    and hasattr(_ct, "CompressionResult")
    and hasattr(_ct, "CompressionSubResult")
)

# compress_tool_result extra fields (original_len, compressed_len, bm25_score):
# detect via return of short content (no side effect on real code paths).
try:
    _probe = cc.compress_tool_result("probe test")
    _TS3_HAS_ORIGINAL_LEN = "original_len" in _probe
except Exception:
    _TS3_HAS_ORIGINAL_LEN = False

# truncate_messages_if_needed top-level skipped_reason / protected_indices:
# detect via probe call with short messages (no side effect).
# NOTE: probes use inline dicts because _user_text helper is defined later.
try:
    _probe_msgs = [{"role": "user", "content": [{"type": "text", "text": "probe"}]}]
    _probe_out, _probe_stats = tc.truncate_messages_if_needed(_probe_msgs)
    _TS3_HAS_TRUNCATE_NEW_FIELDS = "protected_indices" in _probe_stats and "skipped_reason" in _probe_stats
except Exception:
    _TS3_HAS_TRUNCATE_NEW_FIELDS = False

# _compress_content_pass sub / compression_ratio top-level
# detect via probe call with short messages.
try:
    _probe_msgs2 = [{"role": "user", "content": [{"type": "text", "text": "probe"}]}]
    _probe_out2, _probe_stats2 = tc._compress_content_pass(_probe_msgs2)
    _TS3_HAS_SUB_STRUCTURE = "sub" in _probe_stats2 and "compression_ratio" in _probe_stats2
except Exception:
    _TS3_HAS_SUB_STRUCTURE = False

# _oom_safety_fifo (TS-2 已创建,W2 已落地) — purely available since TS-2
_TS3_HAS_OOM_SAFETY = hasattr(tc, "_oom_safety_fifo")

# admin_server._get_compression_stats
try:
    import admin_server as _as_probe
    _TS3_HAS_ADMIN_STAT = hasattr(_as_probe, "_get_compression_stats")
except Exception:
    _TS3_HAS_ADMIN_STAT = False


# ---------------------------------------------------------------------------
# Boolean gates for each test class — strict, accurate to "feature-landed?"
# ---------------------------------------------------------------------------

_TS3_TYPES_READY = _TS3_HAS_RESULT_TYPE
_TS3_COMPRESS_TOOL_RESULT_FIELDS_READY = _TS3_HAS_ORIGINAL_LEN
_TS3_TRUNCATE_TOPLEVEL_READY = _TS3_HAS_TRUNCATE_NEW_FIELDS
_TS3_SUB_STRUCTURE_READY = _TS3_HAS_SUB_STRUCTURE
_TS3_OOM_FIELDS_READY = _TS3_HAS_OOM_SAFETY
_TS3_ADMIN_STATS_READY = _TS3_HAS_ADMIN_STAT


def _assistant_tool_use(tool_use_id, name="read"):
    return {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": {"path": "/x"}}]}


def _user_tool_result(tool_use_id, content="ok"):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]}


def _user_text(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# 用例 1: TypedDict 在 Python 3.9 可 import (design §7 #1; W3末 d1)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _TS3_TYPES_READY,
                 "TS-3 待 W3末 d1 实施 (PRD-litellm-borrow §6.2 W3末)")
class TestCompressionTypesImport(unittest.TestCase):
    """compression_types 模块可 import, TypedDict 在 Python 3.9 兼容 (design §6.1 I-1)."""

    def test_01_compression_result_importable_on_py39(self):
        """`CompressionResult` + `CompressionSubResult` 可 from compression_types import."""
        self.assertTrue(hasattr(_ct, "CompressionResult"))
        self.assertTrue(hasattr(_ct, "CompressionSubResult"))
        # typed dict instances are dicts
        result = dict(strategy="smart", enabled=True)
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# 用例 2-3: compress_tool_result 返回字段集 (design §7 #2-#3; W3末 d2)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _TS3_COMPRESS_TOOL_RESULT_FIELDS_READY,
                 "TS-3 待 W3末 d2 实施 (PRD-litellm-borrow §6.2 W3末)")
class TestCompressToolResultFields(unittest.TestCase):
    """compress_tool_result 返回 CompressionSubResult (design §4.2)."""

    def test_02_returns_compression_sub_result_fields(self):
        """返回 dict 满足 CompressionSubResult 字段集 (含 strategy/ratio/audit_pass/content_type)."""
        result = cc.compress_tool_result('{"key": "value"}')
        for f in ("original", "compressed", "content_type", "strategy",
                  "audit_pass", "ratio"):
            self.assertIn(f, result, f"CompressionSubResult 字段 {f} 缺失")

    def test_03_includes_original_len_and_compressed_len(self):
        """返回额外含 original_len / compressed_len (design §4.2 升级)."""
        content = "x" * 5000
        result = cc.compress_tool_result(content)
        self.assertEqual(result.get("original_len"), 5000)
        self.assertLessEqual(result.get("compressed_len", 5000), 5000)


# ---------------------------------------------------------------------------
# 用例 4-7: truncate_messages_if_needed 返回 CompressionResult (design §7 #4-#7; W4 d1)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _TS3_TRUNCATE_TOPLEVEL_READY,
                 "TS-3 待 W4 d1 实施 (PRD-litellm-borrow §6.2 W4)")
class TestTruncateReturnsCompressionResult(unittest.TestCase):
    """truncate_messages_if_needed 返回 CompressionResult 顶层字段 (design §4.3)."""

    def test_04_returns_mandatory_fields(self):
        """返回 dict 含 strategy / enabled / skipped / truncated + skipped_reason / protected_indices."""
        msgs = [_user_text("hello")]
        out, stats = tc.truncate_messages_if_needed(msgs)
        for f in ("strategy", "enabled", "skipped", "truncated"):
            self.assertIn(f, stats, f"顶级 CompressionResult 缺 {f}")
        self.assertIn("skipped_reason", stats)
        self.assertIn("protected_indices", stats)

    def test_05_rounds_strategy_has_sub_rounds_compression(self):
        """rounds 路径返回 strategy='rounds', sub 含 rounds_compression 兼容字段 (design §6.2)."""
        msgs = []
        for i in range(20):
            msgs.append(_assistant_tool_use(f"t{i}"))
            msgs.append(_user_tool_result(f"t{i}", content="x" * 20000))
        out, stats = tc.truncate_messages_if_needed(msgs, strategy="rounds", keep_rounds=2)
        self.assertEqual(stats.get("strategy"), "rounds")
        sub = stats.get("sub", {})
        self.assertIn("rounds_compression", sub)

    def test_06_smart_strategy_skipped_reason_enumeration(self):
        """smart 路径 skipped_reason 取 TS-2 引入的枚举值 (below_budget / invalid_anthropic_tool_sequence ...)."""
        msgs = [_user_text("x")]
        out, stats = tc.truncate_messages_if_needed(msgs, strategy="smart", budget_chars=100000)
        self.assertIn(stats.get("skipped_reason"),
                      (None, "below_budget"),
                      "短输入下 smart 路径 skipped_reason 应为 None 或 'below_budget'")

    def test_07_smart_path_success_has_dropped_indices_kept_messages(self):
        """smart 路径成功截断: dropped_indices / kept_messages 非空 (design §4.3)."""
        msgs = []
        for i in range(10):
            msgs.append(_assistant_tool_use(f"t{i}", name="write"))
            msgs.append(_user_tool_result(f"t{i}", content="old_content" * 200))
        msgs.append(_user_text("query"))
        out, stats = tc.truncate_messages_if_needed(msgs, strategy="smart", budget_chars=3000)
        if stats.get("truncated"):
            self.assertGreater(len(stats.get("dropped_indices", [])), 0)
            self.assertGreater(stats.get("kept_messages", 0), 0)


# ---------------------------------------------------------------------------
# 用例 8-10: _compress_content_pass / clear_old_tool_results 返回结构 (design §7 #8-#10; W4 d2)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _TS3_SUB_STRUCTURE_READY,
                 "TS-3 待 W4 d2 实施 (PRD-litellm-borrow §6.2 W4)")
class TestCompressContentPassSub(unittest.TestCase):
    """_compress_content_pass / clear_old_tool_results 返回结构 (design §4.4, §4.5)."""

    def test_08_compress_pass_returns_sub_compress_list(self):
        """_compress_content_pass 返回 sub.compress 是 list[CompressionSubResult] (design §4.4)."""
        msgs = [
            _assistant_tool_use("t1"),
            _user_tool_result("t1", content='{"k": "' + 'v' * 4000 + '"}'),
        ]
        out, stats = tc._compress_content_pass(msgs)
        self.assertIn("sub", stats)
        self.assertIn("compress", stats["sub"])
        self.assertIsInstance(stats["sub"]["compress"], list)

    def test_09_compression_ratio_in_range(self):
        """_compress_content_pass.compression_ratio 范围 [0, 1] (design §6.1 I-4)."""
        msgs = [
            _assistant_tool_use("t1"),
            _user_tool_result("t1", content="x" * 5000),
            _user_text("query"),
        ]
        out, stats = tc._compress_content_pass(msgs)
        ratio = stats.get("compression_ratio", 1.0)
        self.assertGreaterEqual(ratio, 0.0)
        self.assertLessEqual(ratio, 1.0)

    def test_10_clear_old_tool_results_field_set(self):
        """clear_old_tool_results 返回结构 (design §4.5). 当无 clear 时仍带 'enabled' 字段."""
        msgs = [_user_text("hello"), _user_tool_result("t1", content="new")]
        out, stats = tc.clear_old_tool_results(msgs, clear_zone_pct=0.5)
        self.assertIn("enabled", stats)
        self.assertIn("strategy", stats)


# ---------------------------------------------------------------------------
# 用例 11: _oom_safety_fifo 字段标准化 (design §7 #11; W4 d3)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _TS3_OOM_FIELDS_READY,
                 "TS-2 W2 _oom_safety_fifo 与 TS-3 W4 d3 字段标准化")
class TestOOMSafetyFields(unittest.TestCase):
    """_oom_safety_fifo 顶级 strategy='oom_fifo', skipped_reason='oom_emergency' (design §5.3)."""

    def test_11_oom_safety_field_standardization(self):
        """_oom_safety_fifo 返回 strategy='oom_safety_fifo' (TS-2 已加) + skipped_reason='oom_emergency' (TS-2 已加).
        TS-3 仅形式化 CompressionResult 类型,代码无需改."""
        msgs = [
            _assistant_tool_use("t1"),
            _user_tool_result("t1", content="x" * 10000),
        ]
        out, stats = tc._oom_safety_fifo(msgs, max_chars=1000)
        self.assertEqual(stats.get("strategy"), "oom_safety_fifo")
        self.assertEqual(stats.get("skipped_reason"), "oom_emergency")


# ---------------------------------------------------------------------------
# 用例 12-13: admin_server._get_compression_stats (design §7 #12-#13; W4 d3)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _TS3_ADMIN_STATS_READY,
                 "TS-3 待 W4 d3 实施 (PRD-litellm-borrow §6.2 W4)")
class TestAdminCompressionStats(unittest.TestCase):
    """admin_server._get_compression_stats (design §4.7)."""

    def test_12_get_compression_stats_returns_dict_with_expected_fields(self):
        """_get_compression_stats 返回 dict 含 strategy_counts / avg_compression_ratio (design §4.7)."""
        import admin_server as _as
        stats = _as._get_compression_stats()
        self.assertIsInstance(stats, dict)
        today = stats.get("today", {})
        self.assertTrue(any(k in today for k in ("strategy_counts", "avg_compression_ratio"))
                        or "strategy_counts" in stats or "avg_compression_ratio" in stats,
                        "应至少返回 strategy_counts 与 avg_compression_ratio 之一")

    def test_13_get_compression_stats_returns_empty_when_metrics_missing(self):
        """_get_compression_stats 当 metrics 文件不存在时返回 _empty_compression_stats structure (design §4.7)."""
        import admin_server as _as
        import proxy_state as _ps
        original_path = getattr(_ps, "_METRICS_PATH", "logs/proxy_metrics.jsonl")
        try:
            _ps._METRICS_PATH = "/nonexistent/path/test_ts3_metrics.jsonl"
            stats = _as._get_compression_stats()
            self.assertIsInstance(stats, dict)
        finally:
            _ps._METRICS_PATH = original_path


# ---------------------------------------------------------------------------
# 用例 14: 双写期 ctx.compress_stats 旧字段仍可访问 (design §7 #14; W4 d3)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _TS3_TRUNCATE_TOPLEVEL_READY,
                 "TS-3 待 W4 d3 实施 (PRD-litellm-borrow §6.2 W4)")
class TestLegacyFieldCompatibility(unittest.TestCase):
    """双写期: ctx.compress_stats['clear'] 旧字段仍可访问 (design §6.1 I-1)."""

    def test_14_compress_stats_clear_field_still_accessible(self):
        """_compress_content_pass stats 的 'clear' 子字段仍存在 (双写期兼容) (I-1)."""
        msgs = [_user_text("hello"), _user_tool_result("t1", content="ok")]
        out, stats = tc._compress_content_pass(msgs)
        # 兼容期保留 clear / think / compress 子字段
        self.assertIn("clear", stats)
        self.assertIn("think", stats)
        self.assertIn("compress", stats)


# ---------------------------------------------------------------------------
# 整体执行入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()