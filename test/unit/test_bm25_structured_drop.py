"""TS-4 (2026-08-18 压缩日志分析落地) 单元测试:

1. BM25 drop 分支类型感知结构化压缩 (json/code 保结构,不再是盲目 30% 截断)
2. drop 分支封顶比例 PROXY_BM25_DROP_TARGET_RATIO
3. fifo 截断 stats 补 chars_before/chars_after/compression_ratio
4. PROXY_BM25_DROP_THRESHOLD 默认值 0.5 -> 0.1
"""
import json
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import content_compressor as cc
import proxy_state
import truncation as tr


def _long_json(n_items=200):
    return json.dumps({"items": [{"id": i, "name": f"row_{i}", "payload": "x" * 40} for i in range(n_items)]})


def _long_code(n_funcs=120):
    parts = []
    for i in range(n_funcs):
        parts.append(f"def function_{i}(arg_{i}):\n    # comment for function {i}\n    return arg_{i} * {i}\n")
    return "\n".join(parts)


class TestBM25StructuredDrop(unittest.TestCase):
    """BM25 drop 分支 (score < drop_threshold) 的结构化感知压缩."""

    def _drop(self, content, **kw):
        return cc.compress_tool_result(
            content,
            bm25_score=0.0,
            bm25_drop_threshold=0.1,
            bm25_keep_threshold=3.5,
            **kw
        )

    def test_01_json_drop_keeps_valid_json(self):
        """JSON 内容在 drop 分支应保结构 (可解析),而非头尾截断."""
        result = self._drop(_long_json())
        self.assertTrue(result["strategy"].startswith("bm25_"))
        self.assertIn("json", result["content_type"])
        parsed = json.loads(result["compressed"])
        self.assertIn("items", parsed)
        self.assertLess(result["ratio"], 1.0)

    def test_02_code_drop_preserves_defs(self):
        """代码内容在 drop 分支应保留函数定义 (code_compress),非盲目截断."""
        code = _long_code(60)
        result = self._drop(code, mime_hint="py")
        self.assertEqual(result["content_type"], "code")
        self.assertIn("def function_0", result["compressed"])
        self.assertLess(result["ratio"], 1.0)

    def test_03_cap_applies_when_structured_insufficient(self):
        """结构化压缩后仍高于目标比例 → 叠加截断封顶 (+cap)."""
        # 无注释的稠密代码: code_compress 几乎不缩减 → 触发封顶
        dense_code = "\n".join(
            f"def function_{i}(arg_{i}):\n    return arg_{i} * {i} + len(str(arg_{i}))" for i in range(200)
        )
        self.assertGreater(len(dense_code), proxy_state.PROXY_COMPRESS_THRESHOLD)
        result = self._drop(dense_code, mime_hint="py")
        self.assertTrue(result["strategy"].endswith("+cap"), result["strategy"])
        target = proxy_state.PROXY_BM25_DROP_TARGET_RATIO
        # _aggressive_truncate 的 target=max(int(len*ratio),200) 有少量边界误差
        self.assertLessEqual(result["ratio"], target + 0.05, result["ratio"])

    def test_04_short_content_in_drop_branch_not_capped(self):
        """低于 threshold 的内容即使低分也不触发封顶 (小载荷不值得截断)."""
        short_text = "plain narrative text " * 50  # ~1000 chars < 4096
        result = self._drop(short_text)
        self.assertEqual(result["ratio"], 1.0)

    def test_05_text_drop_still_compresses(self):
        """长 text 内容低分时仍被显著压缩 (兼容旧语义)."""
        result = self._drop("irrelevant_log_padding_" * 300)
        self.assertLess(result["ratio"], 0.5)

    def test_06_keep_branch_unchanged(self):
        """高分支路回归: >= keep_threshold 原文保留."""
        result = cc.compress_tool_result(
            _long_code(60), bm25_score=4.0,
            bm25_drop_threshold=0.1, bm25_keep_threshold=3.5)
        self.assertEqual(result["strategy"], "none")
        self.assertEqual(result["ratio"], 1.0)

    def test_07_main_path_unchanged_without_score(self):
        """bm25_score=None 主路径回归: 阈值+类型路由行为不变."""
        result = cc.compress_tool_result(_long_json())
        self.assertEqual(result["strategy"], "json_sieve")
        json.loads(result["compressed"])


class TestBM25ConfigDefaults(unittest.TestCase):
    """TS-4 配置默认值."""

    def test_01_drop_threshold_default_lowered(self):
        self.assertEqual(proxy_state.PROXY_BM25_DROP_THRESHOLD, 0.1)

    def test_02_drop_target_ratio_registered(self):
        self.assertGreater(proxy_state.PROXY_BM25_DROP_TARGET_RATIO, 0.0)
        self.assertLess(proxy_state.PROXY_BM25_DROP_TARGET_RATIO, 1.0)
        self.assertIn("PROXY_BM25_DROP_TARGET_RATIO", proxy_state.__all__)
        # 热重载规范中已注册
        names = [spec[0] for spec in proxy_state._RELOAD_SPEC]
        self.assertIn("PROXY_BM25_DROP_TARGET_RATIO", names)

    def test_03_registry_defaults_synced(self):
        import proxy_config
        reg = proxy_config.CONFIG_REGISTRY["PROXY_BM25_DROP_THRESHOLD"]
        self.assertEqual(reg["defaults"]["all"], "0.1")
        reg2 = proxy_config.CONFIG_REGISTRY["PROXY_BM25_DROP_TARGET_RATIO"]
        self.assertEqual(reg2["defaults"]["all"], "0.45")


class TestFifoTruncationMetrics(unittest.TestCase):
    """fifo 截断 stats 的 compression_ratio / chars_before / chars_after."""

    def _make_messages(self, n=60):
        msgs = []
        for i in range(n):
            msgs.append({"role": "user", "content": [{"type": "text", "text": f"question {i} " + "y" * 200}]})
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"answer {i} " + "z" * 200}]})
        return msgs

    def setUp(self):
        self._old_strategy = tr._ps.PROXY_CTX_TRUNCATE_STRATEGY
        self._old_limit_enabled = tr._ps.PROXY_CTX_LIMIT_ENABLED
        self._old_keep = tr._ps.PROXY_CTX_KEEP_MESSAGES
        tr._ps.PROXY_CTX_TRUNCATE_STRATEGY = "fifo"
        tr._ps.PROXY_CTX_LIMIT_ENABLED = True
        tr._ps.PROXY_CTX_KEEP_MESSAGES = 10

    def tearDown(self):
        tr._ps.PROXY_CTX_TRUNCATE_STRATEGY = self._old_strategy
        tr._ps.PROXY_CTX_LIMIT_ENABLED = self._old_limit_enabled
        tr._ps.PROXY_CTX_KEEP_MESSAGES = self._old_keep

    def test_01_fifo_stats_have_real_ratio(self):
        msgs = self._make_messages()
        _, stats = tr.truncate_messages_if_needed(msgs)
        self.assertTrue(stats.get("truncated"))
        self.assertIn("chars_before", stats)
        self.assertIn("chars_after", stats)
        self.assertGreater(stats["chars_before"], stats["chars_after"])
        self.assertLess(stats["compression_ratio"], 1.0)
        # ratio 与 chars 一致
        self.assertAlmostEqual(
            stats["compression_ratio"],
            round(stats["chars_after"] / stats["chars_before"], 4),
            places=3,
        )


if __name__ == "__main__":
    unittest.main()
