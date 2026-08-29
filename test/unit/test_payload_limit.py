#!/usr/bin/env python3
"""Unit tests for per-backend payload size limits (P0).

Verifies that:
  - Oversized local-bound requests are rejected with 413 before reaching local backend.
  - Cloud-bound requests can use a larger payload limit.
  - Boundary conditions around both limits are handled correctly.

Run directly:
    python3 test/unit/test_payload_limit.py
Or via the unified runner:
    bash test/run_tests.sh --unit
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import anthropic_proxy as proxy
import proxy_state


def _build_body(content_chars):
    """Return a valid JSON body for /v1/messages with given user content length."""
    return json.dumps({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "x" * content_chars}],
        "max_tokens": 10,
    }).encode("utf-8")


class TestPayloadSizeLimit(unittest.TestCase):
    """P0: Request body size hard limit returns 413 before backend dispatch."""

    LOCAL_LIMIT = 1000
    CLOUD_LIMIT = 5000

    def setUp(self):
        # 全 handler 流程会经 diagnostics 落盘——重定向到 tmp 防污染生产
        # sessions.jsonl(Phase 2 效度数据集纯净性,2026-08-29 实测每跑 +4 条)
        self._tmp = tempfile.mkdtemp(prefix="pl_diag_")
        self._saved = (proxy_state._DIAG_DIR, proxy_state._DIAG_SESSIONS_PATH,
                       proxy_state._LIFECYCLE_EVENTS_PATH)
        proxy_state._DIAG_DIR = self._tmp
        proxy_state._DIAG_SESSIONS_PATH = os.path.join(self._tmp, "sessions.jsonl")
        proxy_state._LIFECYCLE_EVENTS_PATH = os.path.join(self._tmp, "lifecycle.jsonl")

    def tearDown(self):
        (proxy_state._DIAG_DIR, proxy_state._DIAG_SESSIONS_PATH,
         proxy_state._LIFECYCLE_EVENTS_PATH) = self._saved

    def _make_handler(self, body_bytes, path="/v1/messages", headers=None):
        """Create a Handler instance without invoking the HTTP server constructor."""
        h = proxy.Handler.__new__(proxy.Handler)
        h.path = path
        h.headers = {"Content-Length": str(len(body_bytes))}
        if headers:
            h.headers.update(headers)
        h.rfile = io.BytesIO(body_bytes)
        h._request_id = "req_test"
        h._responses = []
        h._openai_mode = False
        h._openai_model = None

        def fake_respond_json(data, status=200, extra_headers=None):
            h._responses.append({
                "data": data,
                "status": status,
                "extra_headers": extra_headers,
            })

        h._respond_json = fake_respond_json
        return h

    @patch.object(proxy, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy_state, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy_state, "PROXY_MAX_REQUEST_BYTES", LOCAL_LIMIT)
    @patch.object(proxy_state, "PROXY_ROUTE_ENABLED", False)
    def test_oversized_local_request_returns_413(self):
        """Local-routed openai_body > PROXY_MAX_REQUEST_BYTES -> 413 payload_too_large."""
        # 852 chars makes the serialized openai_body (with long local model name)
        # exceed 1000 bytes while keeping the anthropic body small enough to parse.
        body = _build_body(852)
        h = self._make_handler(body)
        proxy.Handler.do_POST(h)
        self.assertEqual(len(h._responses), 1, "should have exactly one response")
        resp = h._responses[0]
        self.assertEqual(resp["status"], 413)
        err = resp["data"]["error"]
        self.assertEqual(err["type"], "payload_too_large")
        self.assertEqual(err["max_bytes"], self.LOCAL_LIMIT)
        self.assertEqual(err["route_target"], "local")

    @patch.object(proxy, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy_state, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy_state, "PROXY_MAX_REQUEST_BYTES", LOCAL_LIMIT)
    @patch.object(proxy_state, "PROXY_CLOUD_MAX_REQUEST_BYTES", CLOUD_LIMIT)
    @patch.object(proxy_state, "PROXY_ROUTE_ENABLED", True)
    @patch.object(proxy_state, "PROXY_ROUTE_THRESHOLD_CHARS", 1)
    @patch.object(proxy_state, "PROXY_CLOUD_BASE_URL", "https://cloud.example.com/v1")
    @patch.object(proxy_state, "PROXY_CLOUD_API_KEY", "sk-cloud")
    @patch.object(proxy_state, "PROXY_ROUTE_FALLBACK_ENABLED", False)
    def test_oversized_cloud_request_uses_cloud_limit(self):
        """Cloud-routed request between local and cloud limits is not rejected.

        Fallback disabled so an unreachable cloud endpoint (URLError in the
        test sandbox) returns 503 instead of falling back to the local backend,
        whose smaller limit would 413 and mask the assertion target.
        """
        # Body size between LOCAL_LIMIT and CLOUD_LIMIT.
        body = _build_body(self.LOCAL_LIMIT + 500)
        h = self._make_handler(body)
        proxy.Handler.do_POST(h)
        rejected = [r for r in h._responses if r["status"] == 413]
        self.assertEqual(rejected, [], "cloud-routed request under cloud limit must not be 413-rejected")

    @patch.object(proxy, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy_state, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy_state, "PROXY_MAX_REQUEST_BYTES", LOCAL_LIMIT)
    @patch.object(proxy_state, "PROXY_CLOUD_MAX_REQUEST_BYTES", LOCAL_LIMIT + 1000)
    @patch.object(proxy_state, "PROXY_ROUTE_ENABLED", True)
    @patch.object(proxy_state, "PROXY_ROUTE_THRESHOLD_CHARS", 1)
    @patch.object(proxy_state, "PROXY_CLOUD_BASE_URL", "https://cloud.example.com/v1")
    @patch.object(proxy_state, "PROXY_CLOUD_API_KEY", "sk-cloud")
    def test_cloud_request_over_cloud_limit_returns_413(self):
        """Cloud-routed request exceeding PROXY_CLOUD_MAX_REQUEST_BYTES -> 413."""
        body = _build_body(self.LOCAL_LIMIT + 1500)
        h = self._make_handler(body)
        proxy.Handler.do_POST(h)
        self.assertEqual(len(h._responses), 1, "should have exactly one response")
        resp = h._responses[0]
        self.assertEqual(resp["status"], 413)
        err = resp["data"]["error"]
        self.assertEqual(err["type"], "payload_too_large")
        self.assertEqual(err["route_target"], "cloud")

    @patch.object(proxy, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy_state, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy_state, "PROXY_MAX_REQUEST_BYTES", LOCAL_LIMIT)
    def test_under_limit_not_rejected(self):
        """Content-Length < PROXY_MAX_REQUEST_BYTES -> not 413-rejected."""
        body = _build_body(100)
        h = self._make_handler(body)
        proxy.Handler.do_POST(h)
        rejected = [r for r in h._responses if r["status"] == 413]
        self.assertEqual(rejected, [], "under-limit request must not be 413-rejected")

    @patch.object(proxy, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy_state, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy_state, "PROXY_MAX_REQUEST_BYTES", LOCAL_LIMIT)
    def test_zero_content_length_not_rejected(self):
        """Content-Length=0 -> not 413-rejected."""
        h = self._make_handler(b"")
        proxy.Handler.do_POST(h)
        rejected = [r for r in h._responses if r["status"] == 413]
        self.assertEqual(rejected, [], "zero-length request must not be 413-rejected")


if __name__ == "__main__":
    unittest.main()
