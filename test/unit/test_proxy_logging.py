"""Unit tests for proxy_logging module."""
import json
import os
import sys
import tempfile
import threading
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import proxy_logging as pl
import proxy_state


class TestMaskSensitive(unittest.TestCase):
    def test_mask_long_api_key(self):
        h = {"X-Api-Key": "sk-1234567890abcdef"}
        out = pl._mask_sensitive(h)
        self.assertTrue(out["X-Api-Key"].startswith("sk-12345"))
        self.assertTrue(out["X-Api-Key"].endswith("cdef"))
        self.assertIn("****", out["X-Api-Key"])

    def test_mask_short_api_key(self):
        h = {"Authorization": "short"}
        out = pl._mask_sensitive(h)
        self.assertEqual(out["Authorization"], "shor****")

    def test_other_headers_unchanged(self):
        h = {"Content-Type": "application/json"}
        self.assertEqual(pl._mask_sensitive(h), h)


class TestEnsureJsonlDir(unittest.TestCase):
    def test_creates_dir_with_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = proxy_state._LOG_DIR
            try:
                proxy_state._LOG_DIR = tmp
                pl._ensure_jsonl_dir()
                self.assertTrue(os.path.isdir(tmp))
                mode = os.stat(tmp).st_mode
                self.assertTrue(mode & 0o700)
            finally:
                proxy_state._LOG_DIR = original


class TestLogRequest(unittest.TestCase):
    def test_writes_jsonl_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_path = proxy_state._JSONL_PATH
            try:
                proxy_state._JSONL_PATH = os.path.join(tmp, "requests.jsonl")
                pl.log_request("claude-sonnet-4-6", 100, 50, 200, 123.4, "2024-01-01T00:00:00")
                with open(proxy_state._JSONL_PATH) as f:
                    line = f.readline()
                record = json.loads(line)
                self.assertEqual(record["model"], "claude-sonnet-4-6")
                self.assertEqual(record["status"], 200)
                self.assertEqual(record["input_chars"], 100)
                self.assertEqual(record["output_chars"], 50)
                self.assertEqual(record["duration_ms"], 123.4)
            finally:
                proxy_state._JSONL_PATH = original_path


class TestLogStructured(unittest.TestCase):
    def test_includes_event_and_session(self):
        original = getattr(proxy_state._log_ctx, 'session_id', None)
        proxy_state._log_ctx.session_id = "sess_123"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                original_path = os.environ.get("PROXY_LOG_PATH")
                os.environ["PROXY_LOG_PATH"] = os.path.join(tmp, "proxy.log")
                try:
                    pl.log_structured("TEST", model="x")
                    with open(os.environ["PROXY_LOG_PATH"]) as f:
                        line = f.readline()
                    record = json.loads(line)
                    self.assertEqual(record["event"], "TEST")
                    self.assertEqual(record["session_id"], "sess_123")
                    self.assertEqual(record["model"], "x")
                finally:
                    if original_path is None:
                        os.environ.pop("PROXY_LOG_PATH", None)
                    else:
                        os.environ["PROXY_LOG_PATH"] = original_path
        finally:
            proxy_state._log_ctx.session_id = original


class TestNextJsonlToken(unittest.TestCase):
    def test_increments_counter(self):
        start = proxy_state._jsonl_counter
        t1 = pl._next_jsonl_token()
        t2 = pl._next_jsonl_token()
        self.assertNotEqual(t1, t2)
        self.assertEqual(proxy_state._jsonl_counter, start + 2)


if __name__ == "__main__":
    unittest.main()
