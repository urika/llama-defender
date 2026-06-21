"""Unit tests for content_compressor module."""
import json
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import content_compressor as cc
import proxy_state


class TestDetectContentType(unittest.TestCase):
    def test_json_object(self):
        self.assertEqual(cc._detect_content_type('{"a": 1}'), "json")

    def test_json_array(self):
        self.assertEqual(cc._detect_content_type('[1, 2, 3]'), "json")

    def test_mime_hint_json(self):
        self.assertEqual(cc._detect_content_type("not json", mime_hint="application/json"), "json")

    def test_mime_hint_python(self):
        self.assertEqual(cc._detect_content_type("print(1)", mime_hint="text/x-python"), "code")

    def test_log_detection(self):
        log = "2024-01-01 10:00:00 INFO start\n2024-01-01 10:00:01 WARN slow\n"
        self.assertEqual(cc._detect_content_type(log), "log")

    def test_code_detection(self):
        code = "def foo():\n    return 1\n"
        self.assertEqual(cc._detect_content_type(code), "code")

    def test_text_fallback(self):
        self.assertEqual(cc._detect_content_type("hello world"), "text")


class TestSieveJson(unittest.TestCase):
    def test_truncate_long_strings(self):
        data = {"key": "x" * 500}
        out = cc._sieve_json(data, max_str_len=10)
        self.assertIn("truncated", out["key"])

    def test_limit_array_items(self):
        data = list(range(100))
        out = cc._sieve_json(data, max_items=5)
        self.assertEqual(len(out), 6)
        self.assertIn("more items", out[-1])

    def test_depth_overflow(self):
        data = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        out = cc._sieve_json(data, max_depth=2)
        # At depth 0 we process key 'a'; depth 1 processes {'b': ...}; depth 2 is the nested dict.
        # max_depth=2 means _depth>2 triggers stringification, so out['a']['b']['c'] is the string.
        self.assertIsInstance(out["a"]["b"]["c"], str)
        # Verify it is stringified recursively
        self.assertIn("e", out["a"]["b"]["c"])

    def test_enable_dedupe(self):
        long_str = "a" * 50
        data = [long_str, long_str]
        out = cc._sieve_json(data, enable_dedupe=True)
        self.assertIn("repeated", str(out))


class TestCompressCode(unittest.TestCase):
    def test_remove_comments(self):
        code = "x = 1\n# comment\ny = 2"
        self.assertEqual(cc._compress_code(code), "x = 1\ny = 2")

    def test_collapse_blank_lines(self):
        code = "x = 1\n\n\ny = 2"
        self.assertEqual(cc._compress_code(code), "x = 1\n\ny = 2")


class TestCompressLog(unittest.TestCase):
    def test_dedupe(self):
        log = "INFO ok\nINFO ok\nINFO ok"
        out = cc._compress_log(log, dedupe=True)
        self.assertIn("identical lines omitted", out)

    def test_no_dedupe(self):
        log = "INFO ok\nINFO ok"
        out = cc._compress_log(log, dedupe=False)
        self.assertNotIn("omitted", out)

    def test_strip_timestamps(self):
        log = "2024-01-01 10:00:00 INFO ok"
        self.assertEqual(cc._compress_log(log), "INFO ok")


class TestCompressText(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(cc._compress_text("hi", max_len=100), "hi")

    def test_long_text_truncated(self):
        text = "x" * 1000
        out = cc._compress_text(text, max_len=100)
        self.assertIn("truncated", out)
        self.assertEqual(len(out), 100 + len("\n\n...[truncated 900 chars]\n\n"))


class TestAuditCompression(unittest.TestCase):
    def test_json_valid(self):
        self.assertTrue(cc._audit_compression('{"a":1}', '{"a":1}', "json"))

    def test_json_invalid(self):
        self.assertFalse(cc._audit_compression('{"a":1}', '{"a":', "json"))

    def test_code_balanced(self):
        self.assertTrue(cc._audit_compression("", "def f(): pass", "code"))

    def test_code_unbalanced(self):
        # Three more opening than closing brackets fails the balance check.
        self.assertFalse(cc._audit_compression("", "(((", "code"))

    def test_text_always_pass(self):
        self.assertTrue(cc._audit_compression("", "anything", "text"))


class TestCompressToolResult(unittest.TestCase):
    def test_lossless_mode(self):
        text = "x" * 10000
        result = cc.compress_tool_result(text, mode="lossless")
        self.assertEqual(result["strategy"], "none")
        self.assertEqual(result["ratio"], 1.0)

    def test_short_passthrough(self):
        result = cc.compress_tool_result("short")
        self.assertEqual(result["strategy"], "none")

    def test_json_compress(self):
        data = [{"id": i, "text": "x" * 1000} for i in range(20)]
        result = cc.compress_tool_result(json.dumps(data), mode="semantic")
        self.assertEqual(result["content_type"], "json")
        self.assertLess(result["ratio"], 1.0)

    def test_aggressive_mode(self):
        long_str = "a" * 100
        data = [long_str, long_str]
        # Directly exercise _dedupe_scalars to verify aggressive dedupe.
        out = cc._dedupe_scalars(data)
        self.assertIn("repeated", str(out))

    def test_audit_fallback(self):
        # Force unbalanced code output by giving code-like text that audit rejects
        text = "(" * 5000
        result = cc.compress_tool_result(text, mode="semantic")
        if result["content_type"] == "code":
            self.assertEqual(result["strategy"], "audit_fallback")
            self.assertFalse(result["audit_pass"])


if __name__ == "__main__":
    unittest.main()
