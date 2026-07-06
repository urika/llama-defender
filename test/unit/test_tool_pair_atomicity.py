"""Unit tests for TS-2: Anthropic 工具配对原子单元.

验收用例 (PRD-litellm-borrow §2 TS-2 + design §8 测试覆盖).
本文件为 TDD stub —— 函数签名 + assert 占位,W1-W2 由工程师实现填充.

Pre-declared imports are pinned; any new exports added in truncation.py
must be re-imported explicitly here.
"""
import inspect
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import truncation as tc


def _assistant_tool_use(tool_use_id, name="read", content_blocks=None):
    blocks = content_blocks or [{"type": "tool_use", "id": tool_use_id,
                                 "name": name, "input": {"path": "/x"}}]
    return {"role": "assistant", "content": blocks}


def _user_tool_result(tool_use_id, content="ok"):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]}


def _user_text(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_text(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


_TS2_HAS_FIND_PAIRS = hasattr(tc, "_find_tool_pairs")
_TS2_HAS_PROTECTED_INDICES = hasattr(tc, "_protected_pair_indices")
_TS2_HAS_TRUNCATE_STRATEGY_KW = "strategy" in inspect.signature(
    tc.truncate_messages_if_needed).parameters
_TS2_HAS_OOM_SAFETY = hasattr(tc, "_oom_safety_fifo")


@unittest.skipIf(not _TS2_HAS_FIND_PAIRS,
                 "TS-2 W1 d1-2: _find_tool_pairs 待实现")
class TestFindToolPairs(unittest.TestCase):
    """用例 1-5: _find_tool_pairs 核心识别逻辑 (design §4.1)."""

    def test_01_single_pair_single_tool_use(self):
        """单 tool_use 单 tool_result 配对成功 → [(0, 1)]"""
        msgs = [_assistant_tool_use("t1"), _user_tool_result("t1")]
        self.assertEqual(tc._find_tool_pairs(msgs), [(0, 1)])

    def test_02_same_assistant_multiple_tool_uses(self):
        """同 assistant 多 tool_use 配多 user tool_result (分多条 user).
        全部识别,按 assistant_idx 升序."""
        msgs = [
            _assistant_tool_use("t1", content_blocks=[
                {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "bash", "input": {}},
            ]),
            _user_tool_result("t1"),
            _user_tool_result("t2"),
        ]
        self.assertEqual(tc._find_tool_pairs(msgs), [(0, 1), (0, 2)])

    def test_03_orphan_tool_result_no_sender(self):
        """孤儿 user tool_result (无对应 tool_use) → 不出现在结果中."""
        msgs = [_user_text("hello"),
                _user_tool_result("ghost"),
                _assistant_text("reply")]
        self.assertEqual(tc._find_tool_pairs(msgs), [])

    def test_04_orphan_tool_use_no_result(self):
        """孤儿 assistant tool_use (无对应 tool_result) → 不出现在结果中."""
        msgs = [_assistant_tool_use("t1"),
                _assistant_text("next"),
                _user_text("reply")]
        self.assertEqual(tc._find_tool_pairs(msgs), [])

    def test_05_duplicate_tool_use_id_anomalous_input(self):
        """重复 tool_use_id (协议异常) → 取首配对, 余者标孤儿 (-1, u_idx)."""
        msgs = [
            _assistant_tool_use("dup"),
            _user_tool_result("dup"),
            _assistant_tool_use("dup"),
            _user_tool_result("dup"),
        ]
        pairs = tc._find_tool_pairs(msgs)
        self.assertIn((0, 1), pairs)
        self.assertIn((-1, 3), pairs)


@unittest.skipIf(not _TS2_HAS_PROTECTED_INDICES,
                 "TS-2 W1 d3-4: _protected_pair_indices 待实现")
class TestProtectedPairIndices(unittest.TestCase):
    """用例 6-7: _protected_pair_indices 与 CacheAligner 配合 (design §4.2)."""

    def test_06_cross_segment_pair_both_protected(self):
        """CacheAligner 段内外跨段配对: a 在 protected, u 在 dynamic,两者都入保护集."""
        msgs = [
            _assistant_tool_use("t1"),
            _user_tool_result("t1"),
            _assistant_text("mid"),
            _user_text("end"),
        ]
        protected = tc._protected_pair_indices(msgs, protected_prefix_n=2)
        self.assertIn(0, protected)
        self.assertIn(1, protected)

    def test_07_orphan_inside_protected_delegated_to_wrapper_guard(self):
        """protected 段内孤儿 (老历史孤儿) 不归本设计处理,_find_tool_pairs 不返回."""
        msgs = [_assistant_tool_use("ghost")]
        self.assertEqual(tc._find_tool_pairs(msgs), [])
        protected = tc._protected_pair_indices(msgs, protected_prefix_n=2)
        self.assertIn(0, protected)


@unittest.skipIf(not _TS2_HAS_TRUNCATE_STRATEGY_KW,
                 "TS-2 W1 d3-4: truncate_messages_if_needed(strategy=) 签名待加")
class TestTruncateRespectsPairs(unittest.TestCase):
    """用例 8-10: truncate_messages_if_needed 三路 (smart/rounds/fifo) 整对 drop."""

    def test_08_smart_path_drops_whole_pair(self):
        """smart 路径遇到配对时整对 drop, 无中间态."""
        msgs = [
            _assistant_tool_use("t1"),
            _user_tool_result("t1", content="x" * 5000),
            _assistant_text("y" * 1000),
            _user_text("query"),
        ]
        out, meta = tc.truncate_messages_if_needed(msgs, strategy="smart",
                                                   budget_chars=3000)
        kept_t1_use = any(b.get("type") == "tool_use" and b.get("id") == "t1"
                          for m in out for b in (m.get("content") or [])
                          if isinstance(b, dict))
        kept_t1_result = any(b.get("type") == "tool_result"
                             and b.get("tool_use_id") == "t1"
                             for m in out for b in (m.get("content") or [])
                             if isinstance(b, dict))
        self.assertEqual(kept_t1_use, kept_t1_result,
                         "tool_use 与 tool_result 必须 both kept 或 both dropped")

    def test_09_smart_path_skipped_when_no_other_drop_available(self):
        """smart 路径除被保护对外无可删项 → skipped_reason='invalid_anthropic_tool_sequence'."""
        msgs = [
            _assistant_tool_use("t1"),
            _user_tool_result("t1", content="x" * 8000),
            _assistant_text("z" * 1000),
            _user_text("q"),
        ]
        out, meta = tc.truncate_messages_if_needed(msgs, strategy="smart",
                                                   budget_chars=3000)
        self.assertEqual(meta.get("skipped_reason"),
                         "invalid_anthropic_tool_sequence")

    def test_10_rounds_truncation_protects_first_n_pairs(self):
        """rounds 路径 keep_rounds=2 时保留前 2 对配对完整,不切断."""
        msgs = []
        for i in range(5):
            msgs.append(_assistant_tool_use(f"t{i}"))
            msgs.append(_user_tool_result(f"t{i}", content="x" * 1000))
        out, meta = tc.truncate_messages_if_needed(msgs, keep_rounds=2)
        for pair_idx in (0, 1):
            a_idx = pair_idx * 2
            u_idx = a_idx + 1
            a_in = any(m.get("role") == "assistant"
                       and any(b.get("id") == f"t{pair_idx}"
                               for b in (m.get("content") or [])
                               if isinstance(b, dict))
                       for m in out)
            u_in = any(m.get("role") == "user"
                       and any(b.get("tool_use_id") == f"t{pair_idx}"
                               for b in (m.get("content") or [])
                               if isinstance(b, dict))
                       for m in out)
            self.assertEqual(a_in, u_in, f"pair {pair_idx} split")


@unittest.skipIf(not _TS2_HAS_TRUNCATE_STRATEGY_KW,
                 "TS-2 W2: clear_old_tool_results 配对约束待实现")
class TestClearOldToolResultsRespectsPairs(unittest.TestCase):
    """用例 11: clear_old_tool_results 单边删除被拒 (design §4.4)."""

    def test_11_clear_rejects_unilateral_tool_result_drop(self):
        """清除旧 tool_result 时配对的 tool_use 不可单边删除."""

        msgs = [
            _assistant_tool_use("t1"),
            _user_tool_result("t1", content="old" * 100),
            _assistant_tool_use("t2"),
            _user_tool_result("t2", content="new"),
        ]
        out, meta = tc.clear_old_tool_results(msgs, clear_zone_pct=0.5)
        t1_result_present = any(m.get("role") == "user"
                                and any(b.get("tool_use_id") == "t1"
                                        for b in (m.get("content") or [])
                                        if isinstance(b, dict))
                                for m in out)
        t1_use_present = any(m.get("role") == "assistant"
                             and any(b.get("id") == "t1"
                                     for b in (m.get("content") or [])
                                     if isinstance(b, dict))
                             for m in out)
        self.assertEqual(t1_use_present, t1_result_present,
                         "clear 不允许单边切断配对")


class TestOOMSafetyBreaksProtection(unittest.TestCase):
    """用例 12: OOMSafetyFIFO 紧急路径可打破保护集 (design §6.1 I-3)."""

    @unittest.skipUnless(_TS2_HAS_OOM_SAFETY,
                     "TS-2 W2: _oom_safety_fifo 紧急 helper 待创建")
    def test_12_oom_safety_fifo_breaks_protection(self):
        """OOMSafetyFIFO 紧急 FIFO 触发后允许打破保护集;skipped_reason='oom_emergency'.

        兜底由 _fix_tool_pairings 清理."""
        msgs = [
            _assistant_tool_use("t1"),
            _user_tool_result("t1", content="x" * 10000),
        ]
        out, meta = tc._oom_safety_fifo(msgs, max_chars=1000)
        self.assertEqual(meta.get("skipped_reason"), "oom_emergency")


if __name__ == "__main__":
    unittest.main()