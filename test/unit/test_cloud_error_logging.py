"""Tests for cloud error logging helper."""
import json
import os
import tempfile
import unittest

from pipeline import PipelineContext, _log_cloud_error


class TestLogCloudError(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["PROXY_CLOUD_ERROR_LOG_DIR"] = self.tmpdir.name

    def tearDown(self):
        del os.environ["PROXY_CLOUD_ERROR_LOG_DIR"]
        self.tmpdir.cleanup()

    def test_writes_rotated_jsonl_with_request_and_response(self):
        ctx = PipelineContext(
            body={},
            request_id="req_123",
        )
        ctx._session_id = "sess_123"
        ctx._route_target = "cloud"
        ctx._route_reason = "chars_exceed_threshold"
        ctx.openai_body = {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "api_key": "should-be-masked",
        }

        _log_cloud_error(ctx, 400, '{"error":"missing tool_call_id"}')

        log_files = [f for f in os.listdir(self.tmpdir.name) if f.startswith("cloud_errors_")]
        self.assertEqual(len(log_files), 1)

        with open(os.path.join(self.tmpdir.name, log_files[0]), "r", encoding="utf-8") as f:
            record = json.loads(f.readline())

        self.assertEqual(record["status"], 400)
        self.assertEqual(record["request_id"], "req_123")
        self.assertEqual(record["session_id"], "sess_123")
        self.assertEqual(record["route_target"], "cloud")
        self.assertEqual(record["response_body"], '{"error":"missing tool_call_id"}')
        self.assertEqual(record["model"], "deepseek-v4-flash")
        self.assertEqual(record["request_body"]["api_key"], "***")
        self.assertIn("messages", record["request_body"])

    def test_truncates_long_response_body(self):
        ctx = PipelineContext(body={}, request_id="req_2")
        ctx.openai_body = {"model": "x"}

        long_body = "x" * 5000
        _log_cloud_error(ctx, 500, long_body)

        log_files = [f for f in os.listdir(self.tmpdir.name) if f.startswith("cloud_errors_")]
        with open(os.path.join(self.tmpdir.name, log_files[0]), "r", encoding="utf-8") as f:
            record = json.loads(f.readline())

        self.assertEqual(len(record["response_body"]), 2000)


if __name__ == "__main__":
    unittest.main()
