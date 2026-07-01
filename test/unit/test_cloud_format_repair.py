"""Tests for BackendDispatcher cloud 400 format repair + retry."""
import io
import json
import threading
import unittest
import urllib.error
from unittest import mock

from pipeline import BackendDispatcher, PipelineContext


class _MockHandler:
    """Minimal handler stand-in."""
    def __init__(self):
        self.responses = []

    def _respond_json(self, data, status):
        self.responses.append((status, data))

    def _handle_streaming_response(self, resp, body):
        self.responses.append(("stream", resp.read()))

    def _handle_non_streaming_response(self, resp, body):
        self.responses.append(("non_stream", resp.read()))


class _FakeHTTPError(urllib.error.HTTPError):
    """HTTPError whose read() returns a fixed body."""
    def __init__(self, code, body):
        self.code = code
        self._body = body.encode("utf-8")
        super().__init__("http://example.com", code, "", {}, None)

    def read(self):
        return self._body


class _FakeResponse:
    def __init__(self, body):
        self.status = 200
        self._body = body.encode("utf-8")

    def read(self, amt=-1):
        return self._body


class TestCloudFormatRepair(unittest.TestCase):
    def setUp(self):
        self.handler = _MockHandler()
        self.dispatcher = BackendDispatcher(
            llama_lock=threading.Semaphore(1),
            cloud_lock=threading.Semaphore(1),
            handler=self.handler,
        )
        self.ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6", "messages": [], "stream": False},
            request_id="req_1",
            client_type="opencode",
        )
        self.ctx._route_target = "cloud"
        self.ctx._route_reason = "chars_exceed_threshold"
        self.ctx._route_cloud_model = "deepseek-v4-flash"
        self.ctx.openai_body = {
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "assistant", "content": "[Calling tool...]"},
                {"role": "tool", "tool_call_id": "call_abc123abc123abc123abc123", "content": "result"},
            ],
            "stream": False,
        }

    @mock.patch("pipeline._ps.PROXY_ROUTE_FALLBACK_ENABLED", True)
    @mock.patch("pipeline._ps.PROXY_CLOUD_API_KEY", "test-cloud-key")
    @mock.patch("pipeline.urllib.request.urlopen")
    def test_retries_cloud_after_message_repair(self, mock_urlopen, *_):
        # First call fails with a repairable 400; second call succeeds.
        mock_urlopen.side_effect = [
            _FakeHTTPError(400, '{"error":{"message":"missing tool_call_id"}}'),
            _FakeResponse('{"id":"x","choices":[{"message":{"content":"ok"}}],"usage":{}}'),
        ]

        self.dispatcher.process(self.ctx)

        self.assertEqual(self.dispatcher._backend_status, 200)
        self.assertFalse(self.dispatcher._route_fallback)
        # Second urlopen call should use repaired messages.
        second_call_body = json.loads(mock_urlopen.call_args_list[1][0][0].data)
        self.assertEqual(second_call_body["messages"][1]["role"], "user")
        self.assertIn("call_abc123abc123abc123abc123", second_call_body["messages"][1]["content"])

    @mock.patch("pipeline._ps.PROXY_ROUTE_FALLBACK_ENABLED", True)
    @mock.patch("pipeline._ps.PROXY_CLOUD_API_KEY", "test-cloud-key")
    @mock.patch("pipeline.urllib.request.urlopen")
    def test_falls_back_when_repair_does_not_help(self, mock_urlopen, *_):
        # First call fails repairable 400; retry also fails.
        mock_urlopen.side_effect = [
            _FakeHTTPError(400, '{"error":{"message":"missing tool_call_id"}}'),
            _FakeHTTPError(400, '{"error":{"message":"still bad"}}'),
            _FakeResponse('{"id":"x","choices":[{"message":{"content":"local ok"}}],"usage":{}}'),
        ]

        self.dispatcher.process(self.ctx)

        self.assertTrue(self.dispatcher._route_fallback)
        self.assertEqual(self.ctx._route_target, "local_forced")

    @mock.patch("pipeline._ps.PROXY_ROUTE_FALLBACK_ENABLED", True)
    @mock.patch("pipeline._ps.PROXY_CLOUD_API_KEY", "test-cloud-key")
    @mock.patch("pipeline.urllib.request.urlopen")
    def test_non_400_does_not_trigger_repair(self, mock_urlopen, *_):
        mock_urlopen.side_effect = [
            _FakeHTTPError(500, '{"error":"server error"}'),
            _FakeResponse('{"id":"x","choices":[{"message":{"content":"local ok"}}],"usage":{}}'),
        ]

        self.dispatcher.process(self.ctx)

        self.assertTrue(self.dispatcher._route_fallback)
        # Only two urlopen calls: original cloud 500, then local 200.
        self.assertEqual(mock_urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
