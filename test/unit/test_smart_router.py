"""Unit tests for SmartRouter and RouteNotification pipeline stages.

Covers the full 10-level decision matrix (§4.1 of design doc).
"""
import os
import sys
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import proxy_state as _ps
from pipeline import (
    PipelineContext, SmartRouter, RouteNotification,
    _classify_tier,
)


class TestClassifyTier(unittest.TestCase):
    def test_opus(self):
        self.assertEqual(_classify_tier("claude-opus-4-7"), "opus")
        self.assertEqual(_classify_tier("claude-opus-4-5"), "opus")

    def test_haiku(self):
        self.assertEqual(_classify_tier("claude-haiku-4-5"), "haiku")
        self.assertEqual(_classify_tier("claude-3-5-haiku-20241022"), "haiku")

    def test_sonnet(self):
        self.assertEqual(_classify_tier("claude-sonnet-4-6"), "sonnet")
        self.assertEqual(_classify_tier("claude-3-5-sonnet-20241022"), "sonnet")

    def test_unknown_defaults_to_sonnet(self):
        self.assertEqual(_classify_tier(""), "sonnet")
        self.assertEqual(_classify_tier("gpt-4"), "sonnet")
        self.assertEqual(_classify_tier("unknown-model"), "sonnet")


class TestSmartRouterDisabled(unittest.TestCase):
    """Priority 0: routing disabled → always local."""

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", False)
    def test_routing_disabled_short_context(self):
        ctx = PipelineContext(body={"model": "claude-sonnet-4-6"})
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "disabled")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", False)
    def test_routing_disabled_long_context(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            total_chars=200000,
        )
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "disabled")


class TestSmartRouterChars(unittest.TestCase):
    """Priority 7: context size threshold."""

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_below_threshold_stays_local(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_1",
        )
        ctx.stage_config = {"total_chars": 45000, "stage": "expansion"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "under_threshold")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_above_threshold_routes_cloud(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_2",
        )
        ctx.stage_config = {"total_chars": 127843, "stage": "saturation"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertIn("chars_exceed_threshold", ctx._route_reason)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_at_threshold_stays_local(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_3",
        )
        ctx.stage_config = {"total_chars": 90000, "stage": "expansion"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")


class TestSmartRouterSessionConsistency(unittest.TestCase):
    """Priority 2/3: Session-level route state."""

    def setUp(self):
        _ps._SESSION_ROUTE_MAP.clear()
        _ps._SESSION_BELOW_THRESHOLD.clear()

    def tearDown(self):
        _ps._SESSION_ROUTE_MAP.clear()
        _ps._SESSION_BELOW_THRESHOLD.clear()

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_non_sticky_cloud_returns_local_after_threshold_drops(self):
        """SUGGESTION 1: sticky=false allows session to leave cloud after N rounds < threshold %."""
        _ps._SESSION_ROUTE_MAP["sess_non_sticky"] = "cloud"
        _ps._SESSION_BELOW_THRESHOLD.clear()
        # Patch PROXY_ROUTE_STICKY=False, return_rounds=2, return_ratio=0.7
        with patch.object(_ps, "PROXY_ROUTE_STICKY", False), \
             patch.object(_ps, "PROXY_ROUTE_STICKY_RETURN_ROUNDS", 2), \
             patch.object(_ps, "PROXY_ROUTE_STICKY_RETURN_RATIO", 0.7), \
             patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 100000):
            # Round 1: total_chars=10K *   over 70K threshold ratio
            ctx1 = PipelineContext(
                body={"model": "claude-sonnet-4-6"},
                session_id="sess_non_sticky",
            )
            ctx1.stage_config = {"total_chars": 50000, "stage": "init"}
            SmartRouter().process(ctx1)
            self.assertEqual(ctx1._route_target, "cloud")
            self.assertIn("session_already_cloud", ctx1._route_reason)
            self.assertNotIn("sticky_expired", ctx1._route_reason)

            # Round 2: total_chars=50K still below 70K threshold boundary
            ctx2 = PipelineContext(
                body={"model": "claude-sonnet-4-6"},
                session_id="sess_non_sticky",
            )
            ctx2.stage_config = {"total_chars": 50000, "stage": "init"}
            ctx2 = SmartRouter().process(ctx2)
            self.assertEqual(ctx2._route_target, "local")
            self.assertIn("sticky_expired", ctx2._route_reason)
            # After return-to-local, the cloud marker is removed
            self.assertNotIn("sess_non_sticky", _ps._SESSION_ROUTE_MAP)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_STICKY", False)
    @patch.object(_ps, "PROXY_ROUTE_STICKY_RETURN_ROUNDS", 3)
    @patch.object(_ps, "PROXY_ROUTE_STICKY_RETURN_RATIO", 0.7)
    def test_non_sticky_below_count_resets_when_above(self):
        """The below-threshold counter should reset if context spikes back above threshold."""
        _ps._SESSION_ROUTE_MAP["sess_reset"] = "cloud"
        _ps._SESSION_BELOW_THRESHOLD.clear()
        with patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 100000):
            # Round 1: above threshold (e.g. 80K > 70K ratio * 100K)
            ctx1 = PipelineContext(
                body={"model": "claude-sonnet-4-6"},
                session_id="sess_reset",
            )
            ctx1.stage_config = {"total_chars": 80000, "stage": "init"}
            ctx1 = SmartRouter().process(ctx1)
            self.assertEqual(ctx1._route_target, "cloud")
            # Below-count should stay 0 since 80K > 70K boundary
            self.assertEqual(_ps._SESSION_BELOW_THRESHOLD.get("sess_reset", 0), 0)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_session_already_cloud(self):
        _ps._SESSION_ROUTE_MAP["sess_c"] = "cloud"
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_c",
        )
        ctx.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertEqual(ctx._route_reason, "session_already_cloud")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_session_local_forced(self):
        _ps._SESSION_ROUTE_MAP["sess_lf"] = "local_forced"
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_lf",
        )
        ctx.stage_config = {"total_chars": 200000, "stage": "oom_danger"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "session_force_local")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_new_session_starts_local(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_new",
        )
        ctx.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")


class TestSmartRouterHeaderOverride(unittest.TestCase):
    """Priority 0.6: X-Proxy-Route-To header override."""

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_header_override_local(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            total_chars=200000,
        )
        ctx._route_header_override = "local"
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "header_override")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_header_override_cloud(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            total_chars=5000,
        )
        ctx._route_header_override = "cloud"
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertEqual(ctx._route_reason, "header_override")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_header_override_opus_local(self):
        """P0-1: claude-opus-4-7 with X-Proxy-Route-To: local should route local."""
        ctx = PipelineContext(
            body={"model": "claude-opus-4-7"},
            total_chars=999999,
        )
        ctx._route_header_override = "local"
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "header_override")


class TestSmartRouterPreference(unittest.TestCase):
    """Priority 0.5: Model ID preference adjusts thresholds."""

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_opus_prefers_cloud_now_force(self):
        """opus behavior is 'force_fallback' — routes to cloud, falls back to local on failure."""
        ctx = PipelineContext(
            body={"model": "claude-opus-4-7"},
            session_id="sess_opus_force",
        )
        ctx.stage_config = {"total_chars": 80000, "stage": "expansion"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertIn("model_forced_fallback_cloud", ctx._route_reason)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_haiku_prefers_local_higher_threshold(self):
        """haiku model raises threshold to 120K (90K * 1.33).

        Uses 'expansion' stage (not 'saturation') to avoid Priority 8
        lifecycle-stage auto-trigger.  Only Priority 7 chars applies.
        """
        ctx = PipelineContext(
            body={"model": "claude-haiku-4-5"},
            session_id="sess_haiku",
        )
        ctx.stage_config = {"total_chars": 100000, "stage": "expansion"}
        ctx = SmartRouter().process(ctx)
        # 100000 < 119700 (90000 * 1.33) → local
        self.assertEqual(ctx._route_target, "local")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_opus_force_short_context_cloud(self):
        """opus force routes to cloud even for very short context (no SmartRouter override)."""
        ctx = PipelineContext(
            body={"model": "claude-opus-4-7"},
            session_id="sess_opus_short",
        )
        ctx.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertIn("model_forced_fallback_cloud", ctx._route_reason)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_opus_force_includes_model_name_in_reason(self):
        """force reason tag should include the model name for tracing."""
        ctx = PipelineContext(
            body={"model": "claude-opus-4-7"},
            session_id="sess_opus_reason",
        )
        ctx.stage_config = {"total_chars": 0, "stage": "init"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertIn("model_forced_fallback", ctx._route_reason)
        self.assertIn("claude-opus-4-7", ctx._route_reason)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_sonnet_prefer_still_uses_smart_router(self):
        """sonnet (prefer) still routes via SmartRouter — short context = local."""
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_sonnet_pref",
        )
        ctx.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "under_threshold")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_sonnet_prefer_long_context_routes_to_cloud(self):
        """sonnet (prefer) still routes via SmartRouter — long context = cloud."""
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_sonnet_long",
        )
        ctx.stage_config = {"total_chars": 100000, "stage": "expansion"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertIn("chars_exceed_threshold", ctx._route_reason)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_haiku_prefer_still_uses_bias(self):
        """haiku (prefer) still uses threshold bias — short context = local."""
        ctx = PipelineContext(
            body={"model": "claude-haiku-4-5"},
            session_id="sess_haiku_pref",
        )
        ctx.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "under_threshold")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_unknown_model_uses_sonnet_defaults(self):
        ctx = PipelineContext(
            body={"model": "gpt-4"},
            session_id="sess_unknown",
        )
        ctx.stage_config = {"total_chars": 85000, "stage": "expansion"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")


class TestSmartRouterLifecycle(unittest.TestCase):
    """Priority 8: Lifecycle stage trigger."""

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_saturation_stage_triggers_cloud(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_sat",
        )
        ctx.stage_config = {"total_chars": 95000, "stage": "saturation"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_oom_danger_triggers_cloud(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_oom",
        )
        ctx.stage_config = {"total_chars": 250000, "stage": "oom_danger"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")


class TestSmartRouterCooldown(unittest.TestCase):
    """Cloud cooldown is a local preference, not a hard lock."""

    def setUp(self):
        _ps._cloud_cooldown_start.clear()
        _ps._cloud_fail_count.clear()
        _ps._SESSION_ROUTE_MAP.clear()

    def tearDown(self):
        _ps._cloud_cooldown_start.clear()
        _ps._cloud_fail_count.clear()
        _ps._SESSION_ROUTE_MAP.clear()

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", 1800)
    def test_cooldown_active_prefers_local(self):
        """Below-threshold requests stay local while cooldown is active."""
        import time
        _ps._cloud_cooldown_start["sess_cool"] = time.monotonic()
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_cool",
        )
        ctx.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "cloud_cooldown_active")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", 1800)
    def test_cooldown_active_overridden_by_threshold(self):
        """Safety override: large contexts still route to cloud despite cooldown."""
        import time
        _ps._cloud_cooldown_start["sess_cool"] = time.monotonic()
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_cool",
        )
        ctx.stage_config = {"total_chars": 200000, "stage": "oom_danger"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertIn("cooldown_override", ctx._route_reason)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", 1800)
    def test_cooldown_active_overridden_by_lifecycle_stage(self):
        """Safety override: saturation/oom_danger/pre_trunc stages route to cloud."""
        import time
        _ps._cloud_cooldown_start["sess_cool"] = time.monotonic()
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_cool",
        )
        ctx.stage_config = {"total_chars": 5000, "stage": "oom_danger"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertIn("cooldown_override", ctx._route_reason)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", 1800)
    def test_cooldown_cloud_failures_local_not_hard(self):
        """local_forced from cloud_failures is not a hard lock; threshold can override."""
        import time
        _ps._cloud_cooldown_start["sess_cool"] = time.monotonic()
        _ps._SESSION_ROUTE_MAP["sess_cool"] = "local_forced"
        _ps._SESSION_ROUTE_FORCE_SOURCE["sess_cool"] = "cloud_failures"
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_cool",
        )
        ctx.stage_config = {"total_chars": 200000, "stage": "init"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")
        self.assertIn("cooldown_override", ctx._route_reason)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", 1800)
    def test_cooldown_manual_local_remains_hard(self):
        """User/admin forced local remains a hard lock regardless of cooldown."""
        import time
        _ps._cloud_cooldown_start["sess_cool"] = time.monotonic()
        _ps._SESSION_ROUTE_MAP["sess_cool"] = "local_forced"
        _ps._SESSION_ROUTE_FORCE_SOURCE["sess_cool"] = "user_manual"
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_cool",
        )
        ctx.stage_config = {"total_chars": 200000, "stage": "oom_danger"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "session_force_local")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS", 1800)
    @patch("time.monotonic")
    def test_cooldown_expired_cleans_all_state(self, mock_time):
        """Cooldown expiry triggers full session state clean-up.

        Regression guard: P0#4 — after the cooldown window passes, all
        session state dicts/sets must be cleared so the session can
        route normally again.

        timing: cooldown_start=100, monotonic() returns 2000,
                elapsed = 2000-100 = 1900 > 1800 → expired → cleanup
        """
        mock_time.return_value = 2000.0
        _ps._cloud_cooldown_start["sess_ex"] = 100.0
        _ps._cloud_fail_count["sess_ex"] = 3
        _ps._SESSION_ROUTE_MAP["sess_ex"] = "local_forced"
        _ps._SESSION_ROUTE_FORCE_SOURCE["sess_ex"] = "cloud_failures"
        _ps._ROUTE_NOTIFIED_SESSIONS.add("sess_ex")

        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_ex",
        )
        ctx.stage_config = {"total_chars": 5000, "stage": "init"}
        SmartRouter().process(ctx)

        self.assertNotIn("sess_ex", _ps._cloud_cooldown_start)
        self.assertNotIn("sess_ex", _ps._cloud_fail_count)
        self.assertNotIn("sess_ex", _ps._SESSION_ROUTE_MAP)
        self.assertNotIn("sess_ex", _ps._SESSION_ROUTE_FORCE_SOURCE)
        self.assertNotIn("sess_ex", _ps._ROUTE_NOTIFIED_SESSIONS)


class TestSmartRouterOutputMetrics(unittest.TestCase):
    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_output_metrics_local(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_m",
        )
        ctx.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx = SmartRouter().process(ctx)
        metrics = SmartRouter().output_metrics(ctx)
        self.assertEqual(metrics["target"], "local")
        self.assertEqual(metrics["reason"], "under_threshold")
        self.assertIn("agent_tier", metrics)
        self.assertIn("route_bias", metrics)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_output_metrics_cloud(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_m2",
        )
        ctx.stage_config = {"total_chars": 127843, "stage": "saturation"}
        ctx = SmartRouter().process(ctx)
        metrics = SmartRouter().output_metrics(ctx)
        self.assertEqual(metrics["target"], "cloud")
        self.assertIn("chars_exceed_threshold", metrics["reason"])
        self.assertEqual(metrics["agent_tier"], "sonnet")
        self.assertEqual(metrics["route_bias"], "auto")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_output_metrics_opus_tier(self):
        ctx = PipelineContext(
            body={"model": "claude-opus-4-7"},
            session_id="sess_m3",
        )
        ctx.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx = SmartRouter().process(ctx)
        metrics = SmartRouter().output_metrics(ctx)
        self.assertEqual(metrics["agent_tier"], "opus")
        self.assertEqual(metrics["route_bias"], "prefer_cloud")


class TestRouteNotification(unittest.TestCase):
    """Stage 2.6: Route switch logging."""

    def test_noop_when_target_is_local(self):
        ctx = PipelineContext(body={"model": "x"})
        ctx._route_target = "local"
        result = RouteNotification().process(ctx)
        self.assertEqual(result._route_target, "local")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_logs_cloud_switch(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_notify",
        )
        ctx._route_target = "cloud"
        ctx._route_reason = "chars_exceed_threshold"
        ctx._route_cloud_model = "deepseek-v4-flash"
        result = RouteNotification().process(ctx)
        self.assertEqual(result._route_target, "cloud")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_notifies_only_once_per_session(self):
        notified_key = "_route_notified_sess_once"
        setattr(_ps, notified_key, True)
        ctx = PipelineContext(
            body={"model": "x"},
            session_id="sess_once",
        )
        ctx._route_target = "cloud"
        result = RouteNotification().process(ctx)
        self.assertEqual(result._route_target, "cloud")
        delattr(_ps, notified_key)


class TestPipelineContextRouteFields(unittest.TestCase):
    """Verify route fields on PipelineContext."""

    def test_default_values(self):
        ctx = PipelineContext()
        self.assertEqual(ctx._route_target, "local")
        self.assertEqual(ctx._route_reason, "")
        self.assertEqual(ctx._route_header_override, "")
        self.assertEqual(ctx._route_actual_cost, 0.0)
        self.assertEqual(ctx._route_cloud_model, "")
        self.assertEqual(ctx._emergency_fallback, False)
        self.assertEqual(ctx._agent_model_tier, "sonnet")

    def test_getattr_safety(self):
        ctx = PipelineContext()
        self.assertEqual(getattr(ctx, '_route_target', 'local'), 'local')
        self.assertEqual(getattr(ctx, '_route_cloud_model', ''), '')


class TestRouteNotificationFull(unittest.TestCase):
    """Phase 2: Full message injection."""

    def setUp(self):
        self._original_notified = set(_ps._ROUTE_NOTIFIED_SESSIONS)

    def tearDown(self):
        _ps._ROUTE_NOTIFIED_SESSIONS.clear()
        _ps._ROUTE_NOTIFIED_SESSIONS.update(self._original_notified)
        _ps._SESSION_ROUTE_MAP.clear()

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_injects_first_route_notice(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_full_notify",
        )
        ctx._route_target = "cloud"
        ctx._route_reason = "chars_exceed_threshold"
        ctx._route_cloud_model = "deepseek-v4-flash"
        ctx.stage_config = {"total_chars": 127843, "stage": "saturation"}
        msg_count_before = len(ctx.messages)
        result = RouteNotification().process(ctx)
        self.assertEqual(len(result.messages), msg_count_before + 1)
        notice = result.messages[-1]["content"][0]["text"]
        self.assertIn("Switched to cloud model", notice)
        self.assertIn("chars exceeds local", notice)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_injects_emergency_notice(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_emerg",
        )
        ctx._route_target = "cloud"
        ctx._route_reason = "cloud_fallback"
        ctx._route_cloud_model = "deepseek-v4-flash"
        ctx._emergency_fallback = True
        ctx.stage_config = {"total_chars": 250000, "stage": "oom_danger"}
        msg_count_before = len(ctx.messages)
        result = RouteNotification().process(ctx)
        self.assertEqual(len(result.messages), msg_count_before + 1)
        notice = result.messages[-1]["content"][0]["text"]
        self.assertIn("emergency fallback", notice)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_does_not_inject_twice(self):
        ctx = PipelineContext(
            body={"model": "x"},
            session_id="sess_twice",
        )
        ctx._route_target = "cloud"
        _ps._ROUTE_NOTIFIED_SESSIONS.add("sess_twice")
        msg_count = len(ctx.messages)
        result = RouteNotification().process(ctx)
        self.assertEqual(len(result.messages), msg_count)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_CLOUD_PRICE_INPUT", 0.5)
    @patch.object(_ps, "PROXY_CLOUD_PRICE_OUTPUT", 1.5)
    @patch.object(_ps, "PROXY_CTX_TOKEN_RATIO", 2.0)
    def test_first_route_notice_includes_dynamic_cost(self):
        """RouteNotification message uses config-driven cost, not hardcoded values.
        
        Regression guard: P2#11 — cost info must be calculated from PROXY_CLOUD_PRICE_*
        config, not hardcoded as "~¥0.01-0.04".
        """
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6", "max_tokens": 4096},
            session_id="sess_cost",
        )
        ctx._route_target = "cloud"
        ctx._route_reason = "chars_exceed_threshold"
        ctx._route_cloud_model = "deepseek-v4-flash"
        ctx.stage_config = {"total_chars": 100000, "stage": "saturation"}
        result = RouteNotification().process(ctx)
        notice = result.messages[-1]["content"][0]["text"]
        # Must contain dynamic cost info from catalog price, not hardcoded
        self.assertIn("¥", notice)
        self.assertIn("¥3.00/M", notice)  # catalog deepseek-v4-flash input (2026-08-17 分时高峰价)
        self.assertIn("¥9.00/M", notice)  # catalog deepseek-v4-flash output
        # Should NOT contain the old hardcoded string
        self.assertNotIn("¥0.01-0.04", notice)


class TestSmartRouterDailyBudget(unittest.TestCase):
    """Phase 2: Daily budget enforcement."""

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_DAILY_BUDGET", 5.0)
    def test_budget_exceeded_forces_local(self):
        import time
        today = time.strftime("%Y-%m-%d")
        with _ps._state_lock:
            _ps._route_daily_date = today
            _ps._route_daily_cost = 6.0
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_budget",
        )
        ctx.stage_config = {"total_chars": 200000, "stage": "oom_danger"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertIn("daily_budget_exceeded", ctx._route_reason)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_DAILY_BUDGET", 5.0)
    def test_budget_not_exceeded_allows_cloud(self):
        import time
        today = time.strftime("%Y-%m-%d")
        with _ps._state_lock:
            _ps._route_daily_date = today
            _ps._route_daily_cost = 2.0
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_under_budget",
        )
        ctx.stage_config = {"total_chars": 200000, "stage": "oom_danger"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_DAILY_BUDGET", 0)
    def test_budget_disabled_allows_cloud(self):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_no_budget",
        )
        ctx.stage_config = {"total_chars": 200000, "stage": "oom_danger"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_DAILY_BUDGET", 5.0)
    @patch.object(_ps, "PROXY_ROUTE_DAILY_BUDGET_HARD_STOP", True)
    def test_budget_exceeded_with_hard_stop(self):
        import time
        today = time.strftime("%Y-%m-%d")
        with _ps._state_lock:
            _ps._route_daily_date = today
            _ps._route_daily_cost = 6.0
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_hard_stop",
        )
        ctx.stage_config = {"total_chars": 200000, "stage": "oom_danger"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "local")
        self.assertIn("daily_budget_exceeded", ctx._route_reason)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_DAILY_BUDGET", 5.0)
    @patch.object(_ps, "PROXY_ROUTE_DAILY_BUDGET_HARD_STOP", False)
    def test_budget_exceeded_without_hard_stop_allows_cloud(self):
        import time
        today = time.strftime("%Y-%m-%d")
        with _ps._state_lock:
            _ps._route_daily_date = today
            _ps._route_daily_cost = 6.0
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_no_hard_stop",
        )
        ctx.stage_config = {"total_chars": 200000, "stage": "oom_danger"}
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")

    def test_budget_alert_levels(self):
        self.assertEqual(_ps._get_budget_alert_level(0.0), "")
        self.assertEqual(_ps._get_budget_alert_level(49.9), "")
        self.assertEqual(_ps._get_budget_alert_level(50.0), "warning")
        self.assertEqual(_ps._get_budget_alert_level(79.9), "warning")
        self.assertEqual(_ps._get_budget_alert_level(80.0), "danger")
        self.assertEqual(_ps._get_budget_alert_level(99.9), "danger")
        self.assertEqual(_ps._get_budget_alert_level(100.0), "critical")

    def test_budget_alert_tiers_parsing(self):
        with patch.object(_ps, "PROXY_ROUTE_BUDGET_ALERT_TIERS", "25,75,90"):
            self.assertEqual(_ps._parse_budget_alert_tiers(), (25, 75, 90))
            self.assertEqual(_ps._get_budget_alert_level(24.0), "")
            self.assertEqual(_ps._get_budget_alert_level(25.0), "warning")
            self.assertEqual(_ps._get_budget_alert_level(75.0), "danger")
            self.assertEqual(_ps._get_budget_alert_level(90.0), "critical")


class TestSessionLifecycle(unittest.TestCase):
    """Full session lifecycle: new → cloud → stay cloud → new session → local."""

    def setUp(self):
        _ps._SESSION_ROUTE_MAP.clear()
        _ps._cloud_cooldown_start.clear()

    def tearDown(self):
        _ps._SESSION_ROUTE_MAP.clear()
        _ps._cloud_cooldown_start.clear()

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_session_lifecycle_new_to_cloud_to_new(self):
        # Request 1: short context → local
        ctx1 = PipelineContext(body={"model": "claude-sonnet-4-6"}, session_id="sess_A")
        ctx1.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx1 = SmartRouter().process(ctx1)
        self.assertEqual(ctx1._route_target, "local")

        # Request 2: context grows → cloud
        ctx2 = PipelineContext(body={"model": "claude-sonnet-4-6"}, session_id="sess_A")
        ctx2.stage_config = {"total_chars": 127000, "stage": "saturation"}
        ctx2 = SmartRouter().process(ctx2)
        self.assertEqual(ctx2._route_target, "cloud")

        # Request 3: same session, still long → stays cloud
        ctx3 = PipelineContext(body={"model": "claude-sonnet-4-6"}, session_id="sess_A")
        ctx3.stage_config = {"total_chars": 200000, "stage": "oom_danger"}
        ctx3 = SmartRouter().process(ctx3)
        self.assertEqual(ctx3._route_target, "cloud")

        # Request 4: new session → back to local
        ctx4 = PipelineContext(body={"model": "claude-sonnet-4-6"}, session_id="sess_B")
        ctx4.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx4 = SmartRouter().process(ctx4)
        self.assertEqual(ctx4._route_target, "local")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "PROXY_ROUTE_THRESHOLD_CHARS", 90000)
    def test_header_override_single_request_not_sticky(self):
        ctx1 = PipelineContext(body={"model": "claude-sonnet-4-6"}, session_id="sess_hdr")
        ctx1.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx1._route_header_override = "cloud"
        ctx1 = SmartRouter().process(ctx1)
        self.assertEqual(ctx1._route_target, "cloud")

        ctx2 = PipelineContext(body={"model": "claude-sonnet-4-6"}, session_id="sess_hdr")
        ctx2.stage_config = {"total_chars": 5000, "stage": "init"}
        ctx2 = SmartRouter().process(ctx2)
        self.assertEqual(ctx2._route_target, "local")

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_empty_session_id_still_works(self):
        ctx = PipelineContext(body={"model": "claude-sonnet-4-6"}, session_id="")
        ctx.stage_config = {"total_chars": 200000, "stage": "oom_danger"}
        ctx._route_header_override = "cloud"
        ctx = SmartRouter().process(ctx)
        self.assertEqual(ctx._route_target, "cloud")


class TestFormatConverterRouting(unittest.TestCase):
    """FormatConverter selects correct model based on route target."""

    def test_local_path_uses_model_name(self):
        from pipeline import FormatConverter
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6", "max_tokens": 4096, "messages": []},
            is_stream=False,
        )
        ctx._route_target = "local"
        ctx.messages = []
        ctx = FormatConverter().process(ctx)
        self.assertEqual(ctx.openai_body["model"], _ps.MODEL_NAME)

    @patch.object(_ps, "PROXY_CLOUD_MODEL", "deepseek-v4-flash")
    def test_cloud_path_uses_cloud_model(self):
        from pipeline import FormatConverter
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6", "max_tokens": 4096, "messages": []},
            is_stream=False,
        )
        ctx._route_target = "cloud"
        ctx._route_cloud_model = "deepseek-v4-flash"
        ctx.messages = []
        ctx = FormatConverter().process(ctx)
        self.assertEqual(ctx.openai_body["model"], "deepseek-v4-flash")


class TestSessionTierHelpers(unittest.TestCase):
    """Phase 4 (建议4): dynamic loop threshold by session tier."""

    def setUp(self):
        _ps._SESSION_REQUEST_COUNT.clear()

    def tearDown(self):
        _ps._SESSION_REQUEST_COUNT.clear()

    # --- _effective_session_tier ---

    def test_session_tier_short_below_bound(self):
        _ps._SESSION_REQUEST_COUNT["sess"] = 5
        self.assertEqual(_ps._SESSION_REQUEST_COUNT["sess"], 5)
        from loop_detection import _effective_session_tier
        self.assertEqual(_effective_session_tier("sess"), "short")

    def test_session_tier_short_at_bound(self):
        _ps._SESSION_REQUEST_COUNT["sess"] = _ps.PROXY_LOOP_SESSION_SHORT_BOUND
        from loop_detection import _effective_session_tier
        self.assertEqual(_effective_session_tier("sess"), "short")

    def test_session_tier_long_above_short_bound(self):
        _ps._SESSION_REQUEST_COUNT["sess"] = _ps.PROXY_LOOP_SESSION_SHORT_BOUND + 1
        from loop_detection import _effective_session_tier
        self.assertEqual(_effective_session_tier("sess"), "long")

    def test_session_tier_long_at_long_bound(self):
        _ps._SESSION_REQUEST_COUNT["sess"] = _ps.PROXY_LOOP_SESSION_LONG_BOUND
        from loop_detection import _effective_session_tier
        self.assertEqual(_effective_session_tier("sess"), "long")

    def test_session_tier_very_long_above_long_bound(self):
        _ps._SESSION_REQUEST_COUNT["sess"] = _ps.PROXY_LOOP_SESSION_LONG_BOUND + 1
        from loop_detection import _effective_session_tier
        self.assertEqual(_effective_session_tier("sess"), "very_long")

    def test_session_tier_unknown_session_short(self):
        """session_id not in _SESSION_REQUEST_COUNT → fallback to short."""
        from loop_detection import _effective_session_tier
        self.assertEqual(_effective_session_tier("nonexistent"), "short")

    def test_session_tier_empty_id_short(self):
        from loop_detection import _effective_session_tier
        self.assertEqual(_effective_session_tier(""), "short")

    # --- _effective_loop_threshold ---

    def test_loop_threshold_short_uses_default(self):
        _ps._SESSION_REQUEST_COUNT["s"] = 1
        from loop_detection import _effective_loop_threshold
        self.assertEqual(_effective_loop_threshold("s"), _ps.PROXY_LOOP_THRESHOLD)

    def test_loop_threshold_long_uses_long(self):
        _ps._SESSION_REQUEST_COUNT["s"] = _ps.PROXY_LOOP_SESSION_SHORT_BOUND + 1
        from loop_detection import _effective_loop_threshold
        self.assertEqual(_effective_loop_threshold("s"), _ps.PROXY_LOOP_THRESHOLD_LONG)

    def test_loop_threshold_very_long_uses_very_long(self):
        _ps._SESSION_REQUEST_COUNT["s"] = _ps.PROXY_LOOP_SESSION_LONG_BOUND + 1
        from loop_detection import _effective_loop_threshold
        self.assertEqual(_effective_loop_threshold("s"), _ps.PROXY_LOOP_THRESHOLD_VERY_LONG)

    # --- _effective_text_loop_threshold ---

    def test_text_loop_threshold_short_uses_default(self):
        _ps._SESSION_REQUEST_COUNT["s"] = 1
        from loop_detection import _effective_text_loop_threshold
        self.assertEqual(_effective_text_loop_threshold("s"), _ps.PROXY_TEXT_LOOP_THRESHOLD)

    def test_text_loop_threshold_long(self):
        _ps._SESSION_REQUEST_COUNT["s"] = _ps.PROXY_LOOP_SESSION_SHORT_BOUND + 1
        from loop_detection import _effective_text_loop_threshold
        self.assertEqual(_effective_text_loop_threshold("s"), _ps.PROXY_TEXT_LOOP_THRESHOLD_LONG)

    # --- _effective_blocker_threshold ---

    def test_blocker_threshold_short_uses_default(self):
        _ps._SESSION_REQUEST_COUNT["s"] = 1
        from loop_detection import _effective_blocker_threshold
        self.assertEqual(_effective_blocker_threshold("s"), _ps.PROXY_BLOCKER_THRESHOLD)

    def test_blocker_threshold_long(self):
        _ps._SESSION_REQUEST_COUNT["s"] = _ps.PROXY_LOOP_SESSION_SHORT_BOUND + 1
        from loop_detection import _effective_blocker_threshold
        self.assertEqual(_effective_blocker_threshold("s"), _ps.PROXY_BLOCKER_THRESHOLD_LONG)


class TestCharBucket(unittest.TestCase):
    """Context size character bucket (used in metrics).

    Boundaries (strict less-than):
      0–10K → xs, 10K–50K → sm, 50K–150K → md,
      150K–400K → lg, 400K–1M → xl, >1M → xxl
    """

    def test_bucket_xs(self):
        from pipeline import _char_bucket
        self.assertEqual(_char_bucket(0), "xs")
        self.assertEqual(_char_bucket(500), "xs")
        self.assertEqual(_char_bucket(9999), "xs")

    def test_bucket_sm(self):
        from pipeline import _char_bucket
        self.assertEqual(_char_bucket(10000), "sm")
        self.assertEqual(_char_bucket(45000), "sm")

    def test_bucket_md(self):
        from pipeline import _char_bucket
        self.assertEqual(_char_bucket(50000), "md")
        self.assertEqual(_char_bucket(100000), "md")
        self.assertEqual(_char_bucket(149999), "md")

    def test_bucket_lg(self):
        from pipeline import _char_bucket
        self.assertEqual(_char_bucket(150000), "lg")
        self.assertEqual(_char_bucket(250000), "lg")

    def test_bucket_xl(self):
        from pipeline import _char_bucket
        self.assertEqual(_char_bucket(400000), "xl")
        self.assertEqual(_char_bucket(500000), "xl")

    def test_bucket_xxl(self):
        from pipeline import _char_bucket
        self.assertEqual(_char_bucket(1_000_000), "xxl")
        self.assertEqual(_char_bucket(1_500_000), "xxl")

    def test_bucket_unknown(self):
        from pipeline import _char_bucket
        self.assertEqual(_char_bucket("not_a_number"), "unknown")
        self.assertEqual(_char_bucket(""), "xs")  # "" or 0 → 0 → xs


class TestV1ModelsRouting(unittest.TestCase):
    """/v1/models returns stable aliases, never exposes MODEL_NAME."""

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", False)
    @patch.object(_ps, "IS_CLOUD", False)
    def test_routing_disabled_includes_opus(self):
        """opus is always included (M-5: prevent 404 on existing sessions)."""
        _ps.invalidate_model_aliases_cache()
        aliases = _ps.get_model_aliases()
        self.assertIn("claude-sonnet-4-6", aliases)
        self.assertIn("claude-haiku-4-5", aliases)
        self.assertIn("claude-opus-4-7", aliases)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "IS_CLOUD", False)
    @patch.object(_ps, "PROXY_CLOUD_API_KEY", "sk-real-key")
    def test_routing_enabled_includes_opus(self):
        """opus always appears (M-5). With cloud key, routes via cloud; without, falls back to local."""
        _ps.invalidate_model_aliases_cache()
        aliases = _ps.get_model_aliases()
        self.assertIn("claude-opus-4-7", aliases)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    @patch.object(_ps, "IS_CLOUD", True)
    def test_cloud_mode_includes_opus(self):
        _ps.invalidate_model_aliases_cache()
        aliases = _ps.get_model_aliases()
        self.assertIn("claude-opus-4-7", aliases)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_never_exposes_model_name(self):
        _ps.invalidate_model_aliases_cache()
        aliases = _ps.get_model_aliases()
        self.assertNotIn(_ps.MODEL_NAME, aliases)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_includes_legacy_compat_aliases(self):
        _ps.invalidate_model_aliases_cache()
        aliases = _ps.get_model_aliases()
        self.assertIn("claude-3-5-sonnet-20241022", aliases)
        self.assertIn("default", aliases)


class TestIsSensitiveRequest(unittest.TestCase):
    """Phase 2: Sensitive path detection."""

    @patch.object(_ps, "PROXY_ROUTE_SENSITIVE_PATTERNS", "")
    def test_empty_patterns_returns_false(self):
        from pipeline import _is_sensitive_request
        ctx = PipelineContext(messages=[])
        self.assertFalse(_is_sensitive_request(ctx))

    @patch.object(_ps, "PROXY_ROUTE_SENSITIVE_PATTERNS", ".env,.secret")
    def test_detects_sensitive_path(self):
        from pipeline import _is_sensitive_request
        ctx = PipelineContext(messages=[{
            "role": "user",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/app/.env"}}
            ],
        }])
        self.assertTrue(_is_sensitive_request(ctx))


@patch.object(_ps, "PROXY_ROUTE_ENABLED", False)
@patch.object(_ps, "IS_CLOUD", False)
class TestGetModelAliasesThreadSafety(unittest.TestCase):
    """P2#9: get_model_aliases must not crash under concurrent access."""

    @classmethod
    def setUpClass(cls):
        cls._invoke_count = 0

    def setUp(self):
        _ps.invalidate_model_aliases_cache()

    def test_concurrent_calls_return_same_list(self):
        """10 threads calling get_model_aliases should all get the same result."""
        results = []

        def worker():
            _ps.invalidate_model_aliases_cache()  # simulate first-call race
            aliases = _ps.get_model_aliases()
            results.append(aliases)

        import threading
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All results must be identical and well-formed
        first = results[0] if results else []
        for r in results[1:]:
            self.assertEqual(first, r)
        self.assertIn("claude-sonnet-4-6", first)
        self.assertNotIn(_ps.MODEL_NAME, first)


if __name__ == "__main__":
    unittest.main()


class TestRouteNotificationWording(unittest.TestCase):
    """Phase D+ 文案修正: reason-aware wording + catalog/subscription pricing."""

    def setUp(self):
        self._original_notified = set(_ps._ROUTE_NOTIFIED_SESSIONS)

    def tearDown(self):
        _ps._ROUTE_NOTIFIED_SESSIONS.clear()
        _ps._ROUTE_NOTIFIED_SESSIONS.update(self._original_notified)

    def _notice(self, reason, model, total_chars=2000):
        ctx = PipelineContext(
            body={"model": "claude-sonnet-4-6"},
            session_id="sess_wording",
        )
        ctx._route_target = "cloud"
        ctx._route_reason = reason
        ctx._route_cloud_model = model
        ctx.stage_config = {"total_chars": total_chars, "stage": "growth"}
        result = RouteNotification().process(ctx)
        return result.messages[-1]["content"][0]["text"]

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_threshold_reason_keeps_exceeds_wording(self):
        notice = self._notice("chars_exceed_threshold", "deepseek-v4-flash",
                              total_chars=127843)
        self.assertIn("chars exceeds local", notice)
        self.assertIn("Estimated cost", notice)          # pay-per-use: estimate
        self.assertIn("deepseek-v4-flash", notice)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_header_override_reason_not_exceeds(self):
        """Override-triggered switch must not claim 'context exceeds limit'."""
        notice = self._notice("header_override", "glm-5.3", total_chars=22)
        self.assertIn("route override", notice)
        self.assertNotIn("exceeds local", notice)

    @patch.object(_ps, "PROXY_ROUTE_ENABLED", True)
    def test_subscription_model_no_per_token_charge(self):
        """Subscription models (catalog price 0/0) — no per-token estimate."""
        notice = self._notice("model_forced_fallback_cloud(claude-opus-4-7->glm-5.3)",
                              "glm-5.3", total_chars=5000)
        self.assertIn("no per-token charge", notice)
        self.assertNotIn("input ¥", notice)
        self.assertIn("model preference", notice)
