"""Unit tests for session-level analyzer in admin_server.py."""
import os
import sys
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import admin_server
import proxy_state as _ps


class TestAnalyzeSession(unittest.TestCase):
    """Tests for _analyze_session() timeline and aggregate computation."""

    def _sample_rows(self):
        return [
            {
                "ts": "2026-06-22T10:00:00",
                "session_id": "sess_a",
                "input_chars": 10000,
                "input_msgs": 10,
                "input_tools": 3,
                "output_chars": 200,
                "duration_ms": 1200,
                "status": 200,
                "est_input_tokens": 3000,
                "est_output_tokens": 60,
                "pipeline": {
                    "backend_dispatcher": {
                        "route_target": "local",
                        "route_reason": "under_threshold",
                    },
                    "smart_router": {"stage": "init"},
                    "loop_detect": {"max_run": 1},
                    "blocker_detect": {"triggered": False},
                    "truncate": {"triggered": False},
                },
            },
            {
                "ts": "2026-06-22T10:00:10",
                "session_id": "sess_a",
                "input_chars": 120000,
                "input_msgs": 25,
                "input_tools": 8,
                "output_chars": 500,
                "duration_ms": 8500,
                "status": 200,
                "est_input_tokens": 40000,
                "est_output_tokens": 150,
                "pipeline": {
                    "backend_dispatcher": {
                        "route_target": "cloud",
                        "route_reason": "chars_exceed_threshold",
                        "dispatch_latency_ms": 320,
                    },
                    "smart_router": {"stage": "saturation"},
                    "loop_detect": {"max_run": 2},
                    "blocker_detect": {"triggered": False},
                    "truncate": {"triggered": True},
                },
            },
            {
                "ts": "2026-06-22T10:00:25",
                "session_id": "sess_a",
                "input_chars": 130000,
                "input_msgs": 27,
                "input_tools": 8,
                "output_chars": 0,
                "duration_ms": 4200,
                "status": 503,
                "pipeline": {
                    "backend_dispatcher": {
                        "route_target": "local_forced",
                        "route_reason": "cloud_fallback",
                        "route_fallback": True,
                        "emergency_fallback": True,
                    },
                    "smart_router": {"stage": "saturation"},
                    "loop_detect": {"max_run": 1},
                    "blocker_detect": {"triggered": True},
                    "truncate": {"triggered": True},
                },
            },
        ]

    @patch.object(admin_server, "_load_session_metrics", lambda sid, max_lines=200000: [])
    def test_empty_session(self):
        data = admin_server._analyze_session("sess_empty")
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["timeline"], [])

    @patch.object(admin_server, "_load_session_metrics", lambda sid, max_lines=200000: TestAnalyzeSession()._sample_rows())
    def test_aggregates(self):
        data = admin_server._analyze_session("sess_a")
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["local_count"], 2)  # local + local_forced
        self.assertEqual(data["cloud_count"], 1)
        self.assertEqual(data["unknown_count"], 0)
        self.assertEqual(data["error_count"], 1)
        self.assertEqual(data["max_input_chars"], 130000)
        self.assertEqual(data["total_output_chars"], 700)
        self.assertEqual(len(data["switches"]), 2)
        self.assertAlmostEqual(data["duration_seconds"], 25.0, delta=0.1)

    @patch.object(admin_server, "_load_session_metrics", lambda sid, max_lines=200000: TestAnalyzeSession()._sample_rows())
    def test_timeline_flags(self):
        data = admin_server._analyze_session("sess_a")
        timeline = data["timeline"]
        self.assertEqual(timeline[0]["target"], "local")
        self.assertEqual(timeline[1]["target"], "cloud")
        self.assertTrue(timeline[2]["fallback"])
        self.assertTrue(timeline[2]["emergency"])
        self.assertTrue(timeline[2]["blocker"])

    @patch.object(admin_server, "_load_session_metrics", lambda sid, max_lines=200000: TestAnalyzeSession()._sample_rows())
    def test_cloud_cost_estimated(self):
        data = admin_server._analyze_session("sess_a")
        # Only the successful cloud request (second row) contributes cost
        expected = (40000 * _ps.PROXY_CLOUD_PRICE_INPUT + 150 * _ps.PROXY_CLOUD_PRICE_OUTPUT) / 1_000_000
        self.assertAlmostEqual(data["cloud_cost"], expected, places=6)


class TestBuildSessionHtml(unittest.TestCase):
    """Tests for _build_session_html() rendering."""

    @patch.object(admin_server, "_load_session_metrics", lambda sid, max_lines=200000: [])
    def test_empty_session_html(self):
        html = admin_server._build_session_html("sess_empty")
        self.assertIn("未找到会话数据", html)
        self.assertIn("sess_empty", html)

    @patch.object(admin_server, "_load_session_metrics", lambda sid, max_lines=200000: TestAnalyzeSession()._sample_rows())
    def test_html_contains_timeline(self):
        html = admin_server._build_session_html("sess_a")
        self.assertIn("Session Analysis", html)
        self.assertIn("sess_a", html)
        self.assertIn("请求时间线", html)
        self.assertIn("cloud", html)
        self.assertIn("local", html)


if __name__ == "__main__":
    unittest.main()
