"""Unit tests for truncation edge cases: _compute_adaptive_rounds and _extract_middle_summary_rules."""
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import truncation as tr


class TestComputeAdaptiveRounds(unittest.TestCase):
    """Tests for _compute_adaptive_rounds() — dynamic round budget adjustment."""

    def test_base_rounds_no_errors(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        result = tr._compute_adaptive_rounds(messages, 10)
        self.assertEqual(result, 10)

    def test_error_in_tool_result_adds_extra(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Error: something failed"}]},
        ]
        result = tr._compute_adaptive_rounds(messages, 10)
        self.assertEqual(result, 11)

    def test_multiple_errors_capped_at_double(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Error: a"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "Exception: b"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "content": "failed: c"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t4", "content": "traceback: d"}]},
        ]
        result = tr._compute_adaptive_rounds(messages, 10)
        self.assertEqual(result, 14)

    def test_write_and_edit_count_trigger(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/a"}},
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/b"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/tmp/c"}},
            ]},
        ]
        result = tr._compute_adaptive_rounds(messages, 10)
        self.assertEqual(result, 11)

    def test_notebook_edit_counts_as_write(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "NotebookEdit", "input": {"file_path": "/tmp/a"}},
                {"type": "tool_use", "name": "NotebookEdit", "input": {"file_path": "/tmp/b"}},
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/c"}},
            ]},
        ]
        result = tr._compute_adaptive_rounds(messages, 10)
        self.assertEqual(result, 11)

    def test_string_content_user_error(self):
        messages = [
            {"role": "user", "content": "got an Exception here"},
        ]
        result = tr._compute_adaptive_rounds(messages, 10)
        self.assertEqual(result, 11)

    def test_empty_messages(self):
        result = tr._compute_adaptive_rounds([], 10)
        self.assertEqual(result, 10)

    def test_low_base_rounds_still_capped(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Error"}]},
        ]
        result = tr._compute_adaptive_rounds(messages, 3)
        self.assertEqual(result, 4)


class TestExtractMiddleSummaryRules(unittest.TestCase):
    """Tests for _extract_middle_summary_rules() — rule-based context summarization."""

    def test_empty_messages_returns_none(self):
        result = tr._extract_middle_summary_rules([])
        self.assertIsNone(result)

    def test_no_relevant_content_returns_none(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "world"}]},
        ]
        result = tr._extract_middle_summary_rules(messages)
        self.assertIsNone(result)

    def test_error_in_tool_result_captured(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Error: something broke"}]},
        ]
        result = tr._extract_middle_summary_rules(messages)
        self.assertIsNotNone(result)
        self.assertIn("Error: something broke", result)
        self.assertIn("<errors_solutions>", result)

    def test_successful_result_captured(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "successfully created file"}]},
        ]
        result = tr._extract_middle_summary_rules(messages)
        self.assertIsNotNone(result)
        self.assertIn("[resolved]", result)

    def test_file_states_tracked(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/test.py"}},
            ]},
        ]
        result = tr._extract_middle_summary_rules(messages)
        self.assertIsNotNone(result)
        self.assertIn("/tmp/test.py", result)
        self.assertIn("<file_states>", result)

    def test_code_changes_tracked(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/tmp/test.py"}},
            ]},
        ]
        result = tr._extract_middle_summary_rules(messages)
        self.assertIsNotNone(result)
        self.assertIn("Edit(/tmp/test.py)", result)
        self.assertIn("<code_changes>", result)

    def test_decisions_tracked(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "DECISION: use Python 3.9"},
            ]},
        ]
        result = tr._extract_middle_summary_rules(messages)
        self.assertIsNotNone(result)
        self.assertIn("DECISION: use Python 3.9", result)
        self.assertIn("<decisions>", result)

    def test_multiple_sections_combined(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Error: fail"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/a.py"}},
            ]},
        ]
        result = tr._extract_middle_summary_rules(messages)
        self.assertIsNotNone(result)
        self.assertIn("<errors_solutions>", result)
        self.assertIn("<code_changes>", result)
        self.assertIn("<file_states>", result)

    def test_header_includes_message_count(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Error: x"}]},
        ]
        result = tr._extract_middle_summary_rules(messages)
        self.assertIn("[Compressed context from 1 earlier messages", result)

    def test_tool_result_list_content(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"text": "error: failed"}]}]},
        ]
        result = tr._extract_middle_summary_rules(messages)
        self.assertIsNone(result)

    def test_path_vs_file_path(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {"path": "/tmp/doc.md"}},
            ]},
        ]
        result = tr._extract_middle_summary_rules(messages)
        self.assertIsNotNone(result)
        self.assertIn("/tmp/doc.md", result)


if __name__ == "__main__":
    unittest.main()
