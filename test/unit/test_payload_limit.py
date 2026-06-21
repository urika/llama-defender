#!/usr/bin/env python3
"""Unit tests for PROXY_MAX_REQUEST_BYTES 413 payload limit (P0).

Verifies that the proxy rejects oversized request bodies with HTTP 413
before any pipeline processing, preventing Metal OOM from large payloads.

Run directly:
    python3 test/unit/test_payload_limit.py
Or via the unified runner:
    bash test/run_tests.sh --unit
"""
import io
import os
import sys
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import anthropic_proxy as proxy


class TestPayloadSizeLimit(unittest.TestCase):
    """P0: Request body size hard limit returns 413 before pipeline processing."""

    def _make_handler(self, content_length, path="/v1/messages"):
        """Create a Handler instance without invoking the HTTP server constructor."""
        h = proxy.Handler.__new__(proxy.Handler)
        h.path = path
        h.headers = {"Content-Length": str(content_length)}
        h.rfile = io.BytesIO(b"x" * content_length)
        h._request_id = "req_test"
        h._responses = []

        def fake_respond_json(data, status=200, extra_headers=None):
            h._responses.append({
                "data": data,
                "status": status,
                "extra_headers": extra_headers,
            })

        h._respond_json = fake_respond_json
        return h

    @patch.object(proxy, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy, "PROXY_MAX_REQUEST_BYTES", 1000)
    def test_oversized_request_returns_413(self):
        """Content-Length > PROXY_MAX_REQUEST_BYTES → 413 payload_too_large."""
        h = self._make_handler(content_length=2000)
        proxy.Handler.do_POST(h)
        self.assertEqual(len(h._responses), 1, "should have exactly one response")
        resp = h._responses[0]
        self.assertEqual(resp["status"], 413)
        err = resp["data"]["error"]
        self.assertEqual(err["type"], "payload_too_large")
        self.assertEqual(err["received_bytes"], 2000)
        self.assertEqual(err["max_bytes"], 1000)

    @patch.object(proxy, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy, "PROXY_MAX_REQUEST_BYTES", 1000)
    def test_exact_limit_not_rejected(self):
        """Content-Length == PROXY_MAX_REQUEST_BYTES → not 413-rejected (boundary)."""
        h = self._make_handler(content_length=1000)
        proxy.Handler.do_POST(h)
        rejected = [r for r in h._responses if r["status"] == 413]
        self.assertEqual(rejected, [], "exact-limit request must not be 413-rejected")

    @patch.object(proxy, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy, "PROXY_MAX_REQUEST_BYTES", 1000)
    def test_under_limit_not_rejected(self):
        """Content-Length < PROXY_MAX_REQUEST_BYTES → not 413-rejected."""
        h = self._make_handler(content_length=500)
        proxy.Handler.do_POST(h)
        rejected = [r for r in h._responses if r["status"] == 413]
        self.assertEqual(rejected, [], "under-limit request must not be 413-rejected")

    @patch.object(proxy, "PROXY_METRICS_ENABLED", False)
    @patch.object(proxy, "PROXY_MAX_REQUEST_BYTES", 1000)
    def test_zero_content_length_not_rejected(self):
        """Content-Length=0 → not 413-rejected."""
        h = self._make_handler(content_length=0)
        proxy.Handler.do_POST(h)
        rejected = [r for r in h._responses if r["status"] == 413]
        self.assertEqual(rejected, [], "zero-length request must not be 413-rejected")


if __name__ == "__main__":
    unittest.main()
