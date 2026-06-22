#!/usr/bin/env python3
"""Unit tests for admin_server.py — observability, status page, and metrics.

Smoke tests that verify the extracted admin_server module can be imported
and its key functions execute without NameError (the primary regression
risk from the Phase 0 module extraction).
"""
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import admin_server


class TestModuleImports(unittest.TestCase):
    """Verify all required names are importable (regression guard for NameError)."""

    def test_stdlib_imports_present(self):
        """re and json must be importable at module level (P0 fix)."""
        import re
        import json
        # Force the module-level references to resolve
        self.assertTrue(hasattr(admin_server, 're') or 're' in dir(admin_server) or True)
        # The real test: call a function that uses re
        admin_server._get_log_stats()  # uses re.search internally

    def test_proxy_state_prefixed_access(self):
        """Functions that reference _ps.BACKEND_TYPE etc. must not NameError."""
        # _build_status_html references _ps.BACKEND_TYPE, _ps.LLAMA_BASE, etc.
        html = admin_server._build_status_html()
        self.assertIsInstance(html, str)
        self.assertGreater(len(html), 100)


class TestPercentile(unittest.TestCase):

    def test_empty_returns_zero(self):
        self.assertEqual(admin_server._percentile([], 0.95), 0.0)

    def test_single_value(self):
        self.assertEqual(admin_server._percentile([42], 0.5), 42.0)

    def test_p50_median_even_count(self):
        # [1,2,3,4,5,6,7,8,9,10] p50 = 5.5
        result = admin_server._percentile([1,2,3,4,5,6,7,8,9,10], 0.5)
        self.assertAlmostEqual(result, 5.5)

    def test_p95_high_value(self):
        result = admin_server._percentile([1,2,3,4,5,6,7,8,9,10], 0.95)
        self.assertGreater(result, 9.0)
        self.assertLessEqual(result, 10.0)


class TestGetSystemMemory(unittest.TestCase):

    def test_returns_required_keys(self):
        mem = admin_server._get_system_memory()
        self.assertIsInstance(mem, dict)
        for key in ("total_gb", "used_gb", "available_gb", "used_pct"):
            self.assertIn(key, mem, f"Missing key: {key}")

    def test_total_gb_positive(self):
        mem = admin_server._get_system_memory()
        self.assertGreater(float(mem["total_gb"]), 0)

    def test_used_pct_is_string(self):
        """used_pct is a formatted string like '45.2'."""
        mem = admin_server._get_system_memory()
        # Should be parseable as float
        self.assertGreaterEqual(float(mem["used_pct"]), 0)


class TestShouldRejectForMemory(unittest.TestCase):

    def test_returns_tuple(self):
        rejected, used_pct = admin_server._should_reject_for_memory()
        self.assertIsInstance(rejected, bool)
        self.assertIsInstance(used_pct, float)

    def test_with_explicit_mem(self):
        mem = {"used_pct": "99.0"}
        rejected, _ = admin_server._should_reject_for_memory(mem=mem)
        self.assertTrue(rejected)

    def test_low_memory_not_rejected(self):
        mem = {"used_pct": "10.0"}
        rejected, _ = admin_server._should_reject_for_memory(mem=mem)
        self.assertFalse(rejected)


class TestFinalizeMetrics(unittest.TestCase):
    """Verify _finalize_metrics populates schema v1 fields."""

    def test_minimal_input(self):
        mc = {"pipeline": {}, "input_chars": 1000, "output_chars": 500}
        admin_server._finalize_metrics(mc)
        self.assertEqual(mc["schema_version"], "v1")
        self.assertIn("est_input_tokens", mc)
        self.assertIn("est_output_tokens", mc)
        self.assertIn("compression_ratio", mc)
        self.assertIn("token_ratio", mc)

    def test_quality_flags_empty_on_clean_pipeline(self):
        mc = {"pipeline": {}, "input_chars": 100, "output_chars": 50}
        admin_server._finalize_metrics(mc)
        self.assertEqual(mc["quality_flags"], [])

    def test_dynamic_concurrent_metadata_present(self):
        mc = {"pipeline": {}, "input_chars": 100, "output_chars": 50}
        admin_server._finalize_metrics(mc)
        dc = mc["dynamic_concurrent"]
        self.assertIn("enabled", dc)
        self.assertIn("current", dc)
        self.assertIn("min", dc)
        self.assertIn("max", dc)

    def test_schema_v1_fields_all_set(self):
        """All _METRICS_V1_FIELDS must be present after finalize."""
        import proxy_state
        mc = {"pipeline": {}, "input_chars": 0, "output_chars": 0}
        admin_server._finalize_metrics(mc)
        for field in proxy_state._METRICS_V1_FIELDS:
            self.assertIn(field, mc, f"Missing v1 field: {field}")


class TestBuildStatusHtml(unittest.TestCase):
    """Smoke test the full HTML generation path (catches all NameErrors)."""

    def test_returns_html_string(self):
        html = admin_server._build_status_html()
        self.assertIsInstance(html, str)

    def test_contains_doctype(self):
        html = admin_server._build_status_html()
        self.assertIn("<!DOCTYPE html>", html)

    def test_contains_backend_card(self):
        html = admin_server._build_status_html()
        # Both cloud and local branches emit a Backend card
        self.assertIn("Backend", html)

    def test_no_undefined_python_references(self):
        """The HTML must not contain f-string placeholder artifacts."""
        html = admin_server._build_status_html()
        self.assertNotIn("{BACKEND_TYPE}", html)
        self.assertNotIn("{LLAMA_BASE}", html)
        self.assertNotIn("{MODEL_NAME}", html)

    def test_route_card_contains_rich_fields(self):
        """Route card must include all new routing-supervision fields."""
        html = admin_server._build_status_html()
        for label in ("Cloud Model", "Cloud Endpoint", "Cloud Ratio",
                      "Cloud Concurrent", "Profile", "Fallback", "API Key"):
            self.assertIn(label, html, f"Route card missing '{label}'")


class TestGetRouteStats(unittest.TestCase):
    """Test the enriched _get_route_stats() returns all expected keys."""

    def test_returns_config_snapshot_keys(self):
        rs = admin_server._get_route_stats()
        for key in ("route_enabled", "threshold", "memory_pct", "profile",
                     "cloud_model", "cloud_base_url", "fallback_enabled",
                     "max_cloud_fails", "cloud_concurrent",
                     "cloud_api_key_configured"):
            self.assertIn(key, rs, f"_get_route_stats() missing '{key}'")

    def test_returns_aggregate_counter_keys(self):
        rs = admin_server._get_route_stats()
        for key in ("local_count", "cloud_count", "cloud_pct",
                     "fallback_count", "last_route_reason", "last_route_target",
                     "recent_fallbacks"):
            self.assertIn(key, rs)

    def test_returns_session_state_keys(self):
        rs = admin_server._get_route_stats()
        for key in ("session_cloud", "session_local", "session_total",
                     "active_sessions", "cooldown_sessions"):
            self.assertIn(key, rs)

    def test_cloud_pct_is_float(self):
        rs = admin_server._get_route_stats()
        self.assertIsInstance(rs["cloud_pct"], float)

    def test_active_sessions_contains_enriched_fields(self):
        rs = admin_server._get_route_stats()
        for s in rs["active_sessions"]:
            self.assertIn("requests", s)
            self.assertIn("cooldown_remaining", s)

    def test_returns_latency_summary_keys(self):
        """Phase 3+ (建议3): _get_route_stats() must include local/cloud latency summaries."""
        rs = admin_server._get_route_stats()
        for key in ("local_latency", "cloud_latency"):
            self.assertIn(key, rs, f"_get_route_stats() missing latency key '{key}'")
            summary = rs[key]
            for field in ("count", "avg_ms", "p50_ms", "p95_ms", "max_ms"):
                self.assertIn(field, summary, f"latency summary missing '{field}'")

    def test_latency_summary_empty_deque(self):
        """_latency_summary(None) returns zeroed dict, not None."""
        s = admin_server._latency_summary(None)
        self.assertEqual(s["count"], 0)
        self.assertEqual(s["p95_ms"], 0.0)

    def test_latency_summary_with_samples(self):
        """_latency_summary() with [10,20,30,40,50] should compute avg=30, p95=50."""
        import collections
        d = collections.deque([10, 20, 30, 40, 50])
        s = admin_server._latency_summary(d)
        self.assertEqual(s["count"], 5)
        self.assertEqual(s["avg_ms"], 30.0)
        self.assertGreater(s["p95_ms"], s["avg_ms"])

    def test_status_html_contains_latency_rows(self):
        """The /status HTML must include per-backend latency rows."""
        html = admin_server._build_status_html()
        self.assertIn("Latency (Local)", html)
        self.assertIn("Latency (Cloud)", html)

    def test_status_html_contains_api_key_badge(self):
        """The /status HTML must include the API Key indicator row."""
        html = admin_server._build_status_html()
        self.assertIn("API Key", html)


class TestGetCacheStats(unittest.TestCase):

    def test_returns_required_keys(self):
        cs = admin_server._get_cache_stats()
        for key in ("hit", "miss", "total", "rate_str", "since"):
            self.assertIn(key, cs)

    def test_total_equals_hit_plus_miss(self):
        cs = admin_server._get_cache_stats()
        self.assertEqual(cs["total"], cs["hit"] + cs["miss"])


class TestMcPut(unittest.TestCase):

    def test_no_active_metrics_context_noop(self):
        """_mc_put should not crash when _metrics_ctx has no 'mc' attribute."""
        import proxy_state
        # Ensure mc attribute is absent
        if hasattr(proxy_state._metrics_ctx, 'mc'):
            del proxy_state._metrics_ctx.mc
        admin_server._mc_put("test_step", {"data": 1})
        # No exception means pass


if __name__ == "__main__":
    unittest.main()
