#!/usr/bin/env python3
"""Unit tests for lifecycle.py — stage classification and dynamic max_tokens.

Verifies:
- _classify_lifecycle_stage boundary transitions (init → pre_trunc)
- Aggressive continuation branch (session_id with accumulated requests)
- _compute_dynamic_max_tokens ceiling by stage and disabled path
- _normalize_system_messages mid-conversation system handling
- _apply_cache_aligner prefix/dynamic split
"""
import os
import sys
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import lifecycle
import proxy_state as _ps


def _msgs(total_chars):
    """Build a minimal messages list approximating total_chars."""
    # Each char in content contributes roughly 1 char to _estimate_message_chars.
    # Use a single user message with a string of the target length.
    text = "x" * total_chars
    return [{"role": "user", "content": text}]


class TestClassifyLifecycleStageBoundaries(unittest.TestCase):
    """Verify stage transitions at threshold boundaries."""

    def setUp(self):
        # Ensure continuation detection doesn't interfere with boundary tests
        self._orig_cont = _ps.PROXY_SESSION_CONTINUATION_ENABLED

    def tearDown(self):
        _ps.PROXY_SESSION_CONTINUATION_ENABLED = self._orig_cont

    def test_init_stage_below_clear_threshold(self):
        with patch.object(_ps, "PROXY_SESSION_CONTINUATION_ENABLED", False):
            result = lifecycle._classify_lifecycle_stage(_msgs(100))
            self.assertEqual(result["stage"], "init")
            self.assertIsNone(result["truncate_rounds"])

    def test_growth_stage(self):
        with patch.object(_ps, "PROXY_SESSION_CONTINUATION_ENABLED", False):
            # Use threshold between CLEAR_THRESHOLD and CHARS_GROWTH
            growth_chars = _ps.PROXY_CLEAR_THRESHOLD + 100
            if growth_chars >= _ps.PROXY_CHARS_GROWTH:
                self.skipTest("Threshold config makes growth stage unreachable")
            result = lifecycle._classify_lifecycle_stage(_msgs(growth_chars))
            self.assertEqual(result["stage"], "growth")

    def test_pre_trunc_stage_above_oom_danger(self):
        with patch.object(_ps, "PROXY_SESSION_CONTINUATION_ENABLED", False):
            result = lifecycle._classify_lifecycle_stage(_msgs(_ps.PROXY_CHARS_OOM_DANGER + 10000))
            self.assertEqual(result["stage"], "pre_trunc")
            self.assertTrue(result["oom_safety"] if not _ps.IS_CLOUD else True)

    def test_returns_required_keys(self):
        result = lifecycle._classify_lifecycle_stage(_msgs(50))
        required = {"stage", "total_chars", "frozen_head", "clear_zone_pct",
                    "thinking_keep", "truncate_rounds", "oom_safety",
                    "is_continuation", "request_count"}
        self.assertEqual(set(result.keys()), required)

    def test_total_chars_reflects_input(self):
        result = lifecycle._classify_lifecycle_stage(_msgs(500))
        self.assertGreater(result["total_chars"], 0)


class TestClassifyContinuation(unittest.TestCase):
    """Verify the aggressive continuation branch."""

    def test_continuation_increments_request_count(self):
        """Two calls with the same session_id should increment request_count."""
        session_id = "test_cont_1"
        # Clean up any prior state
        with _ps._state_lock:
            _ps._SESSION_REQUEST_COUNT.pop(session_id, None)
        try:
            r1 = lifecycle._classify_lifecycle_stage(_msgs(100), session_id=session_id)
            r2 = lifecycle._classify_lifecycle_stage(_msgs(100), session_id=session_id)
            self.assertEqual(r1["request_count"], 0)
            self.assertEqual(r2["request_count"], 1)
        finally:
            with _ps._state_lock:
                _ps._SESSION_REQUEST_COUNT.pop(session_id, None)

    def test_no_session_id_no_continuation(self):
        result = lifecycle._classify_lifecycle_stage(_msgs(100), session_id=None)
        self.assertFalse(result["is_continuation"])
        self.assertEqual(result["request_count"], 0)


class TestComputeDynamicMaxTokens(unittest.TestCase):
    """Verify dynamic max_tokens ceiling logic."""

    def test_disabled_returns_original(self):
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", False):
            adjusted, reason = lifecycle._compute_dynamic_max_tokens(8192, {"stage": "init"})
            self.assertEqual(adjusted, 8192)
            self.assertEqual(reason, "dynamic_disabled")

    def test_init_stage_capped_at_init_ceiling(self):
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True):
            adjusted, reason = lifecycle._compute_dynamic_max_tokens(
                8192, {"stage": "init"}, mem={"available_gb": 40, "total_gb": 48}
            )
            self.assertLessEqual(adjusted, _ps.PROXY_DYNAMIC_MAX_TOKENS_INIT)
            self.assertIn("stage=init", reason)

    def test_saturation_stage_capped_lower(self):
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True):
            adjusted, _ = lifecycle._compute_dynamic_max_tokens(
                8192, {"stage": "saturation"}, mem={"available_gb": 40, "total_gb": 48}
            )
            self.assertLessEqual(adjusted, _ps.PROXY_DYNAMIC_MAX_TOKENS_SATURATION)

    def test_low_memory_triggers_discount(self):
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True):
            adjusted, reason = lifecycle._compute_dynamic_max_tokens(
                8192, {"stage": "init"},
                mem={"available_gb": 5, "total_gb": 48}  # < 20% available
            )
            self.assertIn("low_memory", reason)

    def test_result_at_least_one(self):
        """Adjusted value is clamped to >= 1."""
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True):
            adjusted, _ = lifecycle._compute_dynamic_max_tokens(
                0, {"stage": "pre_trunc"}, mem={"available_gb": 0.1, "total_gb": 48}
            )
            self.assertGreaterEqual(adjusted, 1)

    def test_oom_danger_stage_capped_at_oom_ceiling(self):
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True):
            adjusted, reason = lifecycle._compute_dynamic_max_tokens(
                8192, {"stage": "oom_danger"}, mem={"available_gb": 40, "total_gb": 48}
            )
            self.assertLessEqual(adjusted, _ps.PROXY_DYNAMIC_MAX_TOKENS_OOM)
            self.assertIn("stage=oom_danger", reason)

    def test_pre_trunc_stage_capped_at_oom_ceiling(self):
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True):
            adjusted, reason = lifecycle._compute_dynamic_max_tokens(
                8192, {"stage": "pre_trunc"}, mem={"available_gb": 40, "total_gb": 48}
            )
            self.assertLessEqual(adjusted, _ps.PROXY_DYNAMIC_MAX_TOKENS_OOM)
            self.assertIn("stage=pre_trunc", reason)

    def test_oom_stage_distinct_from_saturation(self):
        """OOM/PRETRUNC ceiling is lower than saturation; the tiers no longer share a cap."""
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 8192), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_OOM", 2048):
            sat_adj, _ = lifecycle._compute_dynamic_max_tokens(
                8192, {"stage": "saturation"}, mem={"available_gb": 40, "total_gb": 48}
            )
            oom_adj, _ = lifecycle._compute_dynamic_max_tokens(
                8192, {"stage": "oom_danger"}, mem={"available_gb": 40, "total_gb": 48}
            )
            pre_adj, _ = lifecycle._compute_dynamic_max_tokens(
                8192, {"stage": "pre_trunc"}, mem={"available_gb": 40, "total_gb": 48}
            )
            self.assertEqual(sat_adj, 8192)
            self.assertEqual(oom_adj, 2048)
            self.assertEqual(pre_adj, 2048)

    # --- L-73(2026-10-07): 截断事故修复——工具轮次的下限兜底 ---

    def test_tool_turn_gets_floor(self):
        """带工具的轮次不得低于 FLOOR: 参数载荷被切断会静默落盘残缺内容。"""
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 8192), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_FLOOR", 32768):
            adjusted, reason = lifecycle._compute_dynamic_max_tokens(
                32000, {"stage": "saturation"},
                mem={"available_gb": 40, "total_gb": 48}, has_tools=True
            )
            self.assertEqual(adjusted, 32000)   # min(FLOOR, orig)
            self.assertIn("floor=32768", reason)

    def test_text_turn_keeps_stage_cap(self):
        """纯文本轮次保留降压语义(截断文本可见可重试), 兜底不介入。"""
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 8192), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_FLOOR", 32768):
            adjusted, reason = lifecycle._compute_dynamic_max_tokens(
                32000, {"stage": "saturation"},
                mem={"available_gb": 40, "total_gb": 48}, has_tools=False
            )
            self.assertEqual(adjusted, 8192)
            self.assertNotIn("floor", reason)

    def test_long_payload_turn_no_reduction(self):
        """含长载荷工具(Write/Edit/Bash...)的轮次完全不降压。"""
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 8192), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_WRITE_NO_REDUCE", True):
            adjusted, reason = lifecycle._compute_dynamic_max_tokens(
                32000, {"stage": "saturation"},
                mem={"available_gb": 5, "total_gb": 48},   # 低内存也不降
                has_long_payload_tools=True, has_tools=True
            )
            self.assertEqual(adjusted, 32000)
            self.assertIn("long_payload_no_reduce", reason)

    def test_bash_counts_as_long_payload(self):
        """Bash 入列: heredoc 写文件/长命令截断不可逆, 同样不降压。"""
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 8192):
            adjusted, reason = lifecycle._compute_dynamic_max_tokens(
                32000, {"stage": "saturation"},
                mem={"available_gb": 40, "total_gb": 48},
                has_long_payload_tools=True, has_tools=True
            )
            self.assertEqual(adjusted, 32000)
            self.assertIn("long_payload_no_reduce", reason)

    def test_long_payload_no_reduce_can_be_disabled(self):
        """关闭豁免后回到 FLOOR 兜底(仍不跌破下限)。"""
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 8192), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_FLOOR", 32768), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_WRITE_NO_REDUCE", False):
            adjusted, reason = lifecycle._compute_dynamic_max_tokens(
                32000, {"stage": "saturation"},
                mem={"available_gb": 40, "total_gb": 48},
                has_long_payload_tools=True, has_tools=True
            )
            self.assertEqual(adjusted, 32000)
            self.assertIn("floor=32768", reason)

    def test_floor_zero_disables_guard(self):
        """FLOOR=0 关闭兜底 → 恢复旧的激进降压行为。"""
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 8192), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_FLOOR", 0):
            adjusted, _ = lifecycle._compute_dynamic_max_tokens(
                32000, {"stage": "saturation"},
                mem={"available_gb": 40, "total_gb": 48}, has_tools=True
            )
            self.assertEqual(adjusted, 8192)

    def test_floor_never_exceeds_client_request(self):
        """兜底不得把上限抬到客户端请求值之上。"""
        with patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_ENABLED", True), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_SATURATION", 2048), \
             patch.object(_ps, "PROXY_DYNAMIC_MAX_TOKENS_FLOOR", 32768):
            adjusted, _ = lifecycle._compute_dynamic_max_tokens(
                4096, {"stage": "saturation"},
                mem={"available_gb": 40, "total_gb": 48}, has_tools=True
            )
            self.assertEqual(adjusted, 4096)


class TestNormalizeSystemMessages(unittest.TestCase):

    def test_first_system_kept(self):
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hi"},
        ]
        result = lifecycle._normalize_system_messages(msgs)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[0]["content"], "You are helpful")

    def test_second_system_converted_to_user(self):
        msgs = [
            {"role": "system", "content": "First"},
            {"role": "system", "content": "Second"},
            {"role": "user", "content": "Hi"},
        ]
        result = lifecycle._normalize_system_messages(msgs)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[1]["role"], "user")
        self.assertIn("[System update]", str(result[1]["content"]))

    def test_empty_messages_passthrough(self):
        self.assertEqual(lifecycle._normalize_system_messages([]), [])

    def test_no_system_messages_unchanged(self):
        msgs = [{"role": "user", "content": "Hi"}]
        self.assertEqual(lifecycle._normalize_system_messages(msgs), msgs)


class TestApplyCacheAligner(unittest.TestCase):

    def test_disabled_returns_empty_prefix(self):
        with patch.object(_ps, "PROXY_CACHE_ALIGN_ENABLED", False):
            prefix, dynamic = lifecycle._apply_cache_aligner([{"role": "user", "content": "x"}])
            self.assertEqual(prefix, [])

    def test_enabled_splits_at_head(self):
        with patch.object(_ps, "PROXY_CACHE_ALIGN_ENABLED", True), \
             patch.object(_ps, "PROXY_CACHE_ALIGN_HEAD", 2):
            msgs = [{"role": "user", "content": str(i)} for i in range(5)]
            prefix, dynamic = lifecycle._apply_cache_aligner(msgs)
            self.assertEqual(len(prefix), 2)
            self.assertEqual(len(dynamic), 3)

    def test_head_exceeds_length_returns_all_dynamic(self):
        with patch.object(_ps, "PROXY_CACHE_ALIGN_ENABLED", True), \
             patch.object(_ps, "PROXY_CACHE_ALIGN_HEAD", 10):
            msgs = [{"role": "user", "content": "x"}]
            prefix, dynamic = lifecycle._apply_cache_aligner(msgs)
            self.assertEqual(len(prefix), 1)
            self.assertEqual(len(dynamic), 0)


if __name__ == "__main__":
    unittest.main()
