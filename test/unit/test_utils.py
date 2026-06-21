"""Unit tests for utility functions in anthropic_proxy.

Covers _percentile, _message_stable_hash, _cast_config_value,
_next_jsonl_token, _ensure_jsonl_dir, _mask_sensitive.
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import anthropic_proxy as proxy


class TestPercentile(unittest.TestCase):
    """Tests for _percentile() — pure statistical function."""

    def test_empty_returns_zero(self):
        self.assertEqual(proxy._percentile([], 0.5), 0.0)
        self.assertEqual(proxy._percentile([], 0.99), 0.0)

    def test_single_value(self):
        self.assertEqual(proxy._percentile([42], 0.5), 42.0)
        self.assertEqual(proxy._percentile([42], 0.0), 42.0)
        self.assertEqual(proxy._percentile([42], 1.0), 42.0)

    def test_p50_median(self):
        self.assertEqual(proxy._percentile([1, 2, 3, 4, 5], 0.5), 3.0)
        self.assertEqual(proxy._percentile([1, 2, 3, 4], 0.5), 2.5)

    def test_p90_linear_interpolation(self):
        vals = list(range(1, 101))  # 1..100
        result = proxy._percentile(vals, 0.90)
        self.assertAlmostEqual(result, 90.1, places=1)

    def test_p99_boundary(self):
        vals = list(range(1, 101))
        result = proxy._percentile(vals, 0.99)
        self.assertAlmostEqual(result, 99.01, places=1)

    def test_unsorted_input(self):
        unsorted = [5, 1, 4, 2, 3]
        self.assertEqual(proxy._percentile(unsorted, 0.5), 3.0)

    def test_latency_window_format(self):
        latencies = [100, 200, 300, 500, 1000, 2000, 5000, 30000]
        p50 = proxy._percentile(latencies, 0.50)
        p95 = proxy._percentile(latencies, 0.95)
        self.assertGreater(p95, p50)
        self.assertLess(p95, max(latencies) + 1)


class TestMessageStableHash(unittest.TestCase):
    """Tests for _message_stable_hash() — deterministic hash for prefix comparison."""

    def test_same_content_same_hash(self):
        msg1 = {"role": "user", "content": "hello"}
        msg2 = {"role": "user", "content": "hello"}
        self.assertEqual(proxy._message_stable_hash(msg1), proxy._message_stable_hash(msg2))

    def test_different_content_different_hash(self):
        msg1 = {"role": "user", "content": "hello"}
        msg2 = {"role": "user", "content": "world"}
        self.assertNotEqual(proxy._message_stable_hash(msg1), proxy._message_stable_hash(msg2))

    def test_key_order_independent(self):
        msg1 = {"role": "user", "content": "hello"}
        msg2 = {"content": "hello", "role": "user"}
        self.assertEqual(proxy._message_stable_hash(msg1), proxy._message_stable_hash(msg2))

    def test_nested_structure(self):
        msg = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
        h = proxy._message_stable_hash(msg)
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)

    def test_unhashable_fallback(self):
        msg = {"role": "user", "tags": {1, 2, 3}}
        h = proxy._message_stable_hash(msg)
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)


class TestCastConfigValue(unittest.TestCase):
    """Tests for _cast_config_value() — type coercion for config values."""

    def test_int_cast(self):
        self.assertEqual(proxy._cast_config_value("42", "int"), 42)
        self.assertEqual(proxy._cast_config_value("-1", "int"), -1)
        self.assertEqual(proxy._cast_config_value("0", "int"), 0)

    def test_float_cast(self):
        self.assertEqual(proxy._cast_config_value("3.14", "float"), 3.14)
        self.assertEqual(proxy._cast_config_value("0.0", "float"), 0.0)

    def test_bool_cast_true(self):
        for v in ("true", "True", "1", "yes", "YES"):
            self.assertTrue(proxy._cast_config_value(v, "bool"), f"'{v}' should be True")

    def test_bool_cast_false(self):
        for v in ("false", "False", "0", "no", "NO", ""):
            self.assertFalse(proxy._cast_config_value(v, "bool"), f"'{v}' should be False")

    def test_str_cast_passthrough(self):
        self.assertEqual(proxy._cast_config_value("hello", "str"), "hello")
        self.assertEqual(proxy._cast_config_value("", "str"), "")

    def test_unknown_cast_passthrough(self):
        self.assertEqual(proxy._cast_config_value("xyz", "list"), "xyz")


class TestNextJsonlToken(unittest.TestCase):
    """Tests for _next_jsonl_token() — unique token generation."""

    def test_format(self):
        token = proxy._next_jsonl_token()
        self.assertTrue(token.startswith("req_"))
        parts = token.split("_")
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[1].isdigit())

    def test_monotonically_increasing(self):
        t1 = proxy._next_jsonl_token()
        t2 = proxy._next_jsonl_token()
        c1 = int(t1.split("_")[1])
        c2 = int(t2.split("_")[1])
        self.assertGreater(c2, c1)

    def test_uniqueness(self):
        tokens = {proxy._next_jsonl_token() for _ in range(100)}
        self.assertEqual(len(tokens), 100)


class TestEnsureJsonlDir(unittest.TestCase):
    """Tests for _ensure_jsonl_dir() — directory creation."""

    def test_creates_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = os.path.join(tmpdir, "nonexistent", "logs")
            old_log_dir = proxy._LOG_DIR
            try:
                proxy._LOG_DIR = log_dir
                proxy._ensure_jsonl_dir()
                self.assertTrue(os.path.isdir(log_dir))
            finally:
                proxy._LOG_DIR = old_log_dir


class TestMaskSensitive(unittest.TestCase):
    """Tests for _mask_sensitive() — header redaction."""

    def test_authorization_long(self):
        h = {"Authorization": "Bearer sk-1234567890abcdef"}
        masked = proxy._mask_sensitive(h)
        self.assertNotIn("1234567890abcdef", masked["Authorization"])
        self.assertTrue(masked["Authorization"].count("*") >= 4)

    def test_authorization_short(self):
        h = {"Authorization": "sk-12"}
        masked = proxy._mask_sensitive(h)
        self.assertIn("****", masked["Authorization"])

    def test_x_api_key(self):
        h = {"x-api-key": "my-secret-key-value"}
        masked = proxy._mask_sensitive(h)
        self.assertIn("****", masked["x-api-key"])

    def test_normal_headers_unmasked(self):
        h = {"Content-Type": "application/json", "Accept": "*/*"}
        masked = proxy._mask_sensitive(h)
        self.assertEqual(masked["Content-Type"], "application/json")
        self.assertEqual(masked["Accept"], "*/*")

    def test_non_dict_passthrough(self):
        self.assertEqual(proxy._mask_sensitive(None), None)
        self.assertEqual(proxy._mask_sensitive("string"), "string")

    def test_case_insensitive_key_match(self):
        h = {"AUTHORIZATION": "Bearer token123456789012"}
        masked = proxy._mask_sensitive(h)
        self.assertIn("****", masked["AUTHORIZATION"])


if __name__ == "__main__":
    unittest.main()
