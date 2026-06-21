"""Unit tests for text loop detection in anthropic_proxy.

Covers _detect_text_loop, _compute_text_similarity, and edge cases.
Currently only 1 test case exists in test_proxy_fallback.py.
"""
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import anthropic_proxy as proxy
import proxy_state


def _make_msg(text):
    return {"role": "assistant", "content": text}


def _make_msg_list(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


class TestTextLoopEdgeCases(unittest.TestCase):
    """Edge case tests for _detect_text_loop()."""

    def test_empty_messages(self):
        run, is_loop = proxy._detect_text_loop([], threshold=3)
        self.assertFalse(is_loop)
        self.assertEqual(run, 0)

    def test_below_threshold(self):
        msgs = [_make_msg("hello world " + str(i)) for i in range(2)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertFalse(is_loop)

    def test_identical_text_triggers_at_threshold(self):
        text = "A" * 150  # above min_chars=100
        msgs = [_make_msg(text) for _ in range(3)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertTrue(is_loop)
        self.assertGreaterEqual(run, 3)

    def test_completely_different_text_no_loop(self):
        t1 = "A" * 150
        t2 = "B" * 150
        msgs = [_make_msg(t1), _make_msg(t2), _make_msg(t1)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertFalse(is_loop)

    def test_short_messages_break_chain(self):
        text_long = "X" * 150
        text_short = "hi"
        msgs = [_make_msg(text_long), _make_msg(text_short), _make_msg(text_long)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertFalse(is_loop)

    def test_similar_text_at_boundary_triggers(self):
        base = "The quick brown fox jumps over the lazy dog. " * 3
        t1 = base + "A" * 18
        t2 = base + "B" * 18
        msgs = [_make_msg(t1), _make_msg(t2), _make_msg(t1)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertTrue(is_loop)

    def test_list_content_format(self):
        text = "X" * 150
        msgs = [_make_msg_list(text) for _ in range(3)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertTrue(is_loop)

    def test_mixed_content_types(self):
        text = "X" * 150
        msgs = [_make_msg(text), _make_msg_list(text), _make_msg(text)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertTrue(is_loop)

    def test_tool_use_only_messages_break_chain(self):
        text = "X" * 150
        tool_msg = {"role": "assistant", "content": [{"type": "tool_use", "name": "Read"}]}
        msgs = [_make_msg(text), tool_msg, _make_msg(text)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertFalse(is_loop)

    def test_max_run_counts_correctly(self):
        text = "X" * 150
        msgs = [_make_msg(text) for _ in range(5)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertTrue(is_loop)
        self.assertEqual(run, 5)

    def test_max_run_resets_on_break(self):
        text = "X" * 150
        other = "Y" * 150
        msgs = [_make_msg(text), _make_msg(text), _make_msg(other),
                _make_msg(text), _make_msg(text), _make_msg(text)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertTrue(is_loop)
        self.assertEqual(run, 3)

    def test_zero_char_messages_no_loop(self):
        msgs = [{"role": "assistant", "content": ""} for _ in range(3)]
        run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
        self.assertFalse(is_loop)

    def test_disabled_by_env(self):
        text = "X" * 150
        msgs = [_make_msg(text) for _ in range(5)]
        old = proxy.PROXY_TEXT_LOOP_ENABLED
        try:
            proxy.PROXY_TEXT_LOOP_ENABLED = False
            proxy_state.PROXY_TEXT_LOOP_ENABLED = False
            proxy_state.PROXY_TEXT_LOOP_ENABLED = False
            proxy_state.PROXY_TEXT_LOOP_ENABLED = False
            run, is_loop = proxy._detect_text_loop(msgs, threshold=3)
            self.assertFalse(is_loop)
            self.assertEqual(run, 0)
        finally:
            proxy.PROXY_TEXT_LOOP_ENABLED = old
            proxy_state.PROXY_TEXT_LOOP_ENABLED = old
            proxy_state.PROXY_TEXT_LOOP_ENABLED = old
            proxy_state.PROXY_TEXT_LOOP_ENABLED = old


class TestComputeTextSimilarity(unittest.TestCase):
    """Edge cases for _compute_text_similarity()."""

    def test_identical(self):
        self.assertEqual(proxy._compute_text_similarity("hello", "hello"), 1.0)

    def test_completely_different(self):
        sim = proxy._compute_text_similarity("abc", "xyz")
        self.assertLess(sim, 0.1)

    def test_empty_inputs(self):
        self.assertEqual(proxy._compute_text_similarity("", ""), 0.0)
        self.assertEqual(proxy._compute_text_similarity("hello", ""), 0.0)

    def test_single_char(self):
        self.assertEqual(proxy._compute_text_similarity("a", "a"), 1.0)
        self.assertEqual(proxy._compute_text_similarity("a", "b"), 0.0)

    def test_partial_overlap(self):
        sim = proxy._compute_text_similarity("hello", "help")
        # "hello" bigrams: he,el,ll,lo; "help" bigrams: he,el,lp
        # intersection={he,el}=2, union={he,el,ll,lo,lp}=5
        self.assertAlmostEqual(sim, 2/5, places=3)


if __name__ == "__main__":
    unittest.main()
