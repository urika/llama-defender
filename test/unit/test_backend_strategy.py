#!/usr/bin/env python3
"""Unit tests for backend_strategy.py — LocalStrategy / CloudStrategy.

Verifies the strategy pattern's correctness:
- Factory routing (create(is_cloud) returns the right class)
- DEFAULTS dict completeness and key presence
- Strategy flag differentiation (oom_safety_enabled, prefix_cache_enabled)
- Default value consistency between LocalStrategy and CloudStrategy
"""
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend_strategy import BackendStrategy, LocalStrategy, CloudStrategy


class TestStrategyFactory(unittest.TestCase):
    """Verify create() routes IS_CLOUD to the right strategy."""

    def test_create_false_returns_local(self):
        self.assertIs(BackendStrategy.create(False), LocalStrategy)

    def test_create_true_returns_cloud(self):
        self.assertIs(BackendStrategy.create(True), CloudStrategy)

    def test_create_returns_class_not_instance(self):
        """create() returns a class (not instance) — strategies are stateless."""
        result = BackendStrategy.create(False)
        # Should be a class object
        self.assertTrue(isinstance(result, type))


class TestStrategyFlags(unittest.TestCase):
    """Verify the two behavioral flags differentiate local vs cloud."""

    def test_local_oom_safety_enabled(self):
        self.assertTrue(LocalStrategy.oom_safety_enabled)

    def test_local_prefix_cache_enabled(self):
        self.assertTrue(LocalStrategy.prefix_cache_enabled)

    def test_cloud_oom_safety_disabled(self):
        self.assertFalse(CloudStrategy.oom_safety_enabled)

    def test_cloud_prefix_cache_disabled(self):
        self.assertFalse(CloudStrategy.prefix_cache_enabled)

    def test_base_defaults_empty(self):
        """BackendStrategy base DEFAULTS is empty — subclasses override."""
        self.assertEqual(BackendStrategy.DEFAULTS, {})


class TestStrategyDefaultsStructure(unittest.TestCase):
    """Verify DEFAULTS dicts have correct structure and key coverage."""

    REQUIRED_KEYS = {
        # Concurrency
        "PROXY_MAX_CONCURRENT", "MODEL_NAME",
        # Clearing
        "PROXY_CLEAR_ENABLED", "PROXY_CLEAR_THRESHOLD", "PROXY_TOOL_KEEP",
        "PROXY_FROZEN_HEAD", "PROXY_CACHE_ALIGN_ENABLED",
        # Compression
        "PROXY_COMPRESS_ENABLED", "PROXY_COMPRESS_THRESHOLD",
        # Context truncation
        "PROXY_CTX_LIMIT_ENABLED", "PROXY_CTX_CHARS_LIMIT",
        # Lifecycle thresholds
        "PROXY_CHARS_GROWTH", "PROXY_CHARS_EXPANSION", "PROXY_CHARS_OOM_DANGER",
        # Memory
        "PROXY_MEMORY_REJECT_THRESHOLD",
        # Dynamic tokens
        "PROXY_DYNAMIC_MAX_TOKENS_ENABLED",
        # Dynamic concurrency
        "PROXY_DYNAMIC_CONCURRENT_ENABLED", "PROXY_DYNAMIC_CONCURRENT_MAX",
        # Blocker / tool filter
        "PROXY_BLOCKER_ENABLED", "PROXY_TOOL_FILTER_ENABLED",
        # Loop
        "PROXY_LOOP_THRESHOLD",
    }

    def test_local_has_all_required_keys(self):
        missing = self.REQUIRED_KEYS - set(LocalStrategy.DEFAULTS)
        self.assertFalse(missing, f"LocalStrategy missing keys: {missing}")

    def test_cloud_has_all_required_keys(self):
        missing = self.REQUIRED_KEYS - set(CloudStrategy.DEFAULTS)
        self.assertFalse(missing, f"CloudStrategy missing keys: {missing}")

    def test_local_and_cloud_keys_match(self):
        """Both strategies should define the same set of keys."""
        local_keys = set(LocalStrategy.DEFAULTS)
        cloud_keys = set(CloudStrategy.DEFAULTS)
        self.assertEqual(local_keys, cloud_keys,
                         f"Key mismatch: local_only={local_keys - cloud_keys}, "
                         f"cloud_only={cloud_keys - local_keys}")

    def test_all_defaults_are_strings(self):
        """DEFAULTS values must be strings (cast happens downstream)."""
        for key, val in LocalStrategy.DEFAULTS.items():
            self.assertIsInstance(val, str, f"LocalStrategy[{key}]={val!r} is not str")
        for key, val in CloudStrategy.DEFAULTS.items():
            self.assertIsInstance(val, str, f"CloudStrategy[{key}]={val!r} is not str")


class TestStrategyDefaultsDifferentiation(unittest.TestCase):
    """Verify local vs cloud defaults differ where they should."""

    # Keys that MUST differ between local and cloud
    MUST_DIFFER = {
        "PROXY_MAX_CONCURRENT",  # 1 vs 4
        "MODEL_NAME",            # mlx vs deepseek
        "PROXY_CLEAR_ENABLED",   # true vs false
        "PROXY_CTX_LIMIT_ENABLED",
        "PROXY_DYNAMIC_MAX_TOKENS_ENABLED",
        "PROXY_DYNAMIC_CONCURRENT_ENABLED",
        "PROXY_BLOCKER_ENABLED",
        "PROXY_TOOL_FILTER_ENABLED",
        "PROXY_CHARS_GROWTH",
        "PROXY_CHARS_EXPANSION",
        "PROXY_CHARS_OOM_DANGER",
        "PROXY_MEMORY_REJECT_THRESHOLD",
        "PROXY_FROZEN_HEAD",
        "PROXY_CACHE_ALIGN_ENABLED",
        "PROXY_COMPRESS_ENABLED",
    }

    def test_differentiated_keys_actually_differ(self):
        for key in self.MUST_DIFFER:
            local_val = LocalStrategy.DEFAULTS.get(key)
            cloud_val = CloudStrategy.DEFAULTS.get(key)
            self.assertNotEqual(local_val, cloud_val,
                                f"{key}: local={local_val!r} == cloud={cloud_val!r}")

    def test_shared_keys_actually_match(self):
        """Keys that should be identical (e.g. PROXY_LOOP_THRESHOLD) do match."""
        shared_keys = {
            "PROXY_LOOP_THRESHOLD", "PROXY_LOOP_THRESHOLD",
            "PROXY_TEXT_LOOP_ENABLED", "PROXY_TEXT_LOOP_THRESHOLD",
            "PROXY_DEDUP_WINDOW", "PROXY_BLOCKER_THRESHOLD",
            "PROXY_BACKEND_TIMEOUT", "PROXY_RETRY_AFTER_SECONDS",
            "PROXY_OOM_SAFE_CHARS", "PROXY_OOM_SAFE_TOKENS",
            "PROXY_COMPRESS_THRESHOLD", "PROXY_COMPRESS_MODE",
        }
        for key in shared_keys:
            local_val = LocalStrategy.DEFAULTS.get(key)
            cloud_val = CloudStrategy.DEFAULTS.get(key)
            self.assertEqual(local_val, cloud_val,
                             f"{key}: local={local_val!r} != cloud={cloud_val!r}")


class TestGetDefault(unittest.TestCase):
    """Verify get_default() lookup with fallback."""

    def test_existing_key(self):
        self.assertEqual(LocalStrategy.get_default("PROXY_MAX_CONCURRENT"), "1")
        self.assertEqual(CloudStrategy.get_default("PROXY_MAX_CONCURRENT"), "4")

    def test_missing_key_returns_fallback(self):
        self.assertEqual(LocalStrategy.get_default("NONEXISTENT_KEY", "fallback"), "fallback")

    def test_missing_key_returns_none_default(self):
        self.assertIsNone(LocalStrategy.get_default("NONEXISTENT_KEY"))


if __name__ == "__main__":
    unittest.main()
