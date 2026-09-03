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

    def test_session_request_fields_persisted(self):
        """A1: session_id/request_id 落盘(此前按会话归因全部断链)。"""
        with tempfile.TemporaryDirectory() as tmp:
            original_path = proxy_state._JSONL_PATH
            try:
                proxy_state._JSONL_PATH = os.path.join(tmp, "requests.jsonl")
                pl.log_request("m", 1, 1, 200, 1.0,
                               session_id="cli_AB12", request_id="req_abc",
                               trace_id="tr_abc")
                with open(proxy_state._JSONL_PATH) as f:
                    record = json.loads(f.readline())
                self.assertEqual(record["session_id"], "cli_AB12")
                self.assertEqual(record["request_id"], "req_abc")
                self.assertEqual(record["trace_id"], "tr_abc")
            finally:
                proxy_state._JSONL_PATH = original_path

    def test_session_request_fields_always_present(self):
        """A1: 两字段恒出现(schema 稳定),未知为空串。"""
        with tempfile.TemporaryDirectory() as tmp:
            original_path = proxy_state._JSONL_PATH
            try:
                proxy_state._JSONL_PATH = os.path.join(tmp, "requests.jsonl")
                pl.log_request("m", 1, 1, 200, 1.0)
                with open(proxy_state._JSONL_PATH) as f:
                    record = json.loads(f.readline())
                self.assertIn("session_id", record)
                self.assertIn("request_id", record)
                self.assertIn("trace_id", record)
                self.assertEqual(record["session_id"], "")
                self.assertEqual(record["request_id"], "")
                self.assertEqual(record["trace_id"], "")
            finally:
                proxy_state._JSONL_PATH = original_path


class TestJsonlRotation(unittest.TestCase):
    """A1: 10MB 轮转 helper(测试注入小阈值;调用方持锁约定)。"""

    def test_rotate_moves_to_dot1(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "requests.jsonl")
            with open(path, "w") as f:
                f.write("x" * 2048)
            lock = threading.Lock()
            with lock:
                pl._maybe_rotate_jsonl(path, lock, rotate_bytes=1024)
            self.assertTrue(os.path.exists(path + ".1"))
            self.assertFalse(os.path.exists(path))  # 原文件被 move 走
            with open(path + ".1") as f:
                self.assertEqual(len(f.read()), 2048)

    def test_no_rotate_below_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "requests.jsonl")
            with open(path, "w") as f:
                f.write("x" * 100)
            lock = threading.Lock()
            with lock:
                pl._maybe_rotate_jsonl(path, lock, rotate_bytes=1024)
            self.assertFalse(os.path.exists(path + ".1"))
            self.assertTrue(os.path.exists(path))


class TestMainLogRotation(unittest.TestCase):
    """A2: 主日志 copytruncate 轮转(50MB×3;测试注入小阈值+高频检查)。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="logrot_")
        self._path = os.path.join(self._tmp, "anthropic_proxy.log")
        self._saved_bytes = pl._LOG_ROTATE_BYTES
        self._saved_keep = pl._LOG_ROTATE_KEEP
        self._saved_interval = pl._LOG_ROTATE_CHECK_INTERVAL
        self._saved_env = os.environ.get("PROXY_LOG_PATH")
        pl._LOG_ROTATE_BYTES = 1024
        pl._LOG_ROTATE_KEEP = 3
        pl._LOG_ROTATE_CHECK_INTERVAL = 1
        os.environ["PROXY_LOG_PATH"] = self._path

    def tearDown(self):
        pl._LOG_ROTATE_BYTES = self._saved_bytes
        pl._LOG_ROTATE_KEEP = self._saved_keep
        pl._LOG_ROTATE_CHECK_INTERVAL = self._saved_interval
        if self._saved_env is None:
            os.environ.pop("PROXY_LOG_PATH", None)
        else:
            os.environ["PROXY_LOG_PATH"] = self._saved_env
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_copytruncate_creates_backups(self):
        for _ in range(6):
            pl.log("x" * 600)  # 每次约 620B;1KB 阈值 → 每两次触发一轮
        self.assertTrue(os.path.exists(self._path + ".1"))
        # 原文件被截断后继续写——只含最后一次的行
        with open(self._path) as f:
            content = f.read()
        self.assertLessEqual(len(content), 1300)
        self.assertIn("x", content)

    def test_backup_chain_max_keep(self):
        for _ in range(30):
            pl.log("x" * 600)
        backups = [f for f in os.listdir(self._tmp) if f.startswith("anthropic_proxy.log.")]
        self.assertLessEqual(len(backups), pl._LOG_ROTATE_KEEP)
        self.assertTrue(any(f.endswith(".1") for f in backups))


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
