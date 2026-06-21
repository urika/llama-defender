"""Unit tests for proxy_state module.

Verifies config invariants, shared state consistency, and config helpers
from the canonical source of truth (not through anthropic_proxy re-exports).
"""
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import proxy_state
import proxy_config


class TestProxyStateImports(unittest.TestCase):
    """Verify key names are importable from proxy_state."""

    def test_backend_vars(self):
        self.assertIsInstance(proxy_state.LLAMA_BASE, str)
        self.assertIsInstance(proxy_state.LLAMA_API_KEY, str)
        self.assertIn(proxy_state.BACKEND_TYPE, ("local", "cloud"))
        self.assertIsInstance(proxy_state.IS_CLOUD, bool)

    def test_concurrency_vars(self):
        self.assertGreater(proxy_state.PROXY_MAX_CONCURRENT, 0)
        self.assertIsNotNone(proxy_state._llama_lock)
        self.assertIsInstance(proxy_state.MODEL_NAME, str)

    def test_shared_state_dicts(self):
        self.assertIsInstance(proxy_state._SESSION_LAST_MESSAGES, dict)
        self.assertIsInstance(proxy_state._DEDUP_CACHE, dict)
        self.assertIsInstance(proxy_state._SESSION_REQUEST_COUNT, dict)
        self.assertIsInstance(proxy_state._LOOP_SESSION_STATE, dict)

    def test_thread_locals(self):
        self.assertIsNotNone(proxy_state._log_ctx)
        self.assertIsNotNone(proxy_state._metrics_ctx)

    def test_reload_spec(self):
        self.assertIsInstance(proxy_state._RELOAD_SPEC, list)
        self.assertGreater(len(proxy_state._RELOAD_SPEC), 40)

    def test_model_aliases(self):
        self.assertIsInstance(proxy_state.MODEL_ALIASES, list)
        self.assertIn("default", proxy_state.MODEL_ALIASES)
        self.assertIn(proxy_state.MODEL_NAME, proxy_state.MODEL_ALIASES)


class TestProxyStateConfigInvariants(unittest.TestCase):
    """Verify config values have reasonable defaults."""

    def test_thresholds_are_monotonic(self):
        self.assertLess(proxy_state.PROXY_CHARS_GROWTH,
                        proxy_state.PROXY_CHARS_EXPANSION)
        self.assertLess(proxy_state.PROXY_CHARS_EXPANSION,
                        proxy_state.PROXY_CHARS_SATURATION)
        self.assertLess(proxy_state.PROXY_CHARS_SATURATION,
                        proxy_state.PROXY_CHARS_OOM_DANGER)

    def test_loop_levels_monotonic(self):
        self.assertLess(proxy_state.PROXY_LOOP_THRESHOLD,
                        proxy_state.PROXY_LOOP_LEVEL2)
        self.assertLess(proxy_state.PROXY_LOOP_LEVEL2,
                        proxy_state.PROXY_LOOP_LEVEL3)

    def test_compression_defaults(self):
        self.assertGreater(proxy_state.PROXY_COMPRESS_THRESHOLD, 0)
        self.assertIn(proxy_state.PROXY_COMPRESS_MODE,
                      ("lossless", "semantic", "aggressive"))

    def test_oom_safe_chars_positive(self):
        self.assertGreater(proxy_state.PROXY_OOM_SAFE_CHARS, 0)
        self.assertEqual(proxy_state.PROXY_PRE_TRUNCATE_CHARS,
                         proxy_state.PROXY_OOM_SAFE_CHARS)

    def test_backend_timeout_positive(self):
        self.assertGreaterEqual(proxy_state.PROXY_BACKEND_TIMEOUT, 60)

    def test_tool_always_keep_not_empty(self):
        self.assertGreater(len(proxy_state.TOOL_ALWAYS_KEEP), 10)
        self.assertIn("Read", proxy_state.TOOL_ALWAYS_KEEP)
        self.assertIn("Write", proxy_state.TOOL_ALWAYS_KEEP)

    def test_frozen_head_non_negative(self):
        self.assertGreaterEqual(proxy_state.PROXY_FROZEN_HEAD, 0)


class TestDefaultResolution(unittest.TestCase):
    """Tests for _default() fallback chain."""

    def test_returns_hardcoded_local_default(self):
        # When resolve_default returns None, fall back to BackendStrategy.
        result = proxy_state._default("PROXY_MAX_CONCURRENT", "4", "1")
        self.assertIn(result, ("1", "4"))

    def test_returns_string(self):
        result = proxy_state._default("PROXY_TOOL_KEEP", "10", "2")
        self.assertIsInstance(result, str)


class TestParseConfEnv(unittest.TestCase):
    """Tests for _parse_conf_env in proxy_state."""

    def test_basic_key_value(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write('LLAMA_HOST="127.0.0.1"\nLLAMA_PORT="8081"\n')
            f.flush()
            result = proxy_state._parse_conf_env(f.name)
        os.unlink(f.name)
        self.assertEqual(result["LLAMA_HOST"], "127.0.0.1")
        self.assertEqual(result["LLAMA_PORT"], "8081")

    def test_comments_ignored(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write('# This is a comment\nKEY="value"\n')
            f.flush()
            result = proxy_state._parse_conf_env(f.name)
        os.unlink(f.name)
        self.assertEqual(result["KEY"], "value")

    def test_inline_comment_stripped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write('KEY="value" # inline comment\n')
            f.flush()
            result = proxy_state._parse_conf_env(f.name)
        os.unlink(f.name)
        self.assertEqual(result["KEY"], "value")

    def test_single_quotes(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write("KEY='value with spaces'\n")
            f.flush()
            result = proxy_state._parse_conf_env(f.name)
        os.unlink(f.name)
        self.assertEqual(result["KEY"], "value with spaces")

    def test_value_with_equals(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write('EXTRA="--foo=bar --baz=qux"\n')
            f.flush()
            result = proxy_state._parse_conf_env(f.name)
        os.unlink(f.name)
        self.assertEqual(result["EXTRA"], "--foo=bar --baz=qux")

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write("")
            f.flush()
            result = proxy_state._parse_conf_env(f.name)
        os.unlink(f.name)
        self.assertEqual(result, {})

    def test_missing_file(self):
        result = proxy_state._parse_conf_env("/nonexistent/path.conf")
        self.assertEqual(result, {})

    def test_line_without_equals_ignored(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write("not a key value pair\nKEY=val\n")
            f.flush()
            result = proxy_state._parse_conf_env(f.name)
        os.unlink(f.name)
        self.assertEqual(result, {"KEY": "val"})

    def test_whitespace_value(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write('KEY="   "\n')
            f.flush()
            result = proxy_state._parse_conf_env(f.name)
        os.unlink(f.name)
        self.assertEqual(result["KEY"], "   ")


class TestCastConfigValue(unittest.TestCase):
    """Tests for _cast_config_value."""

    def test_cast_int(self):
        self.assertEqual(proxy_state._cast_config_value("42", "int"), 42)

    def test_cast_float(self):
        self.assertEqual(proxy_state._cast_config_value("3.14", "float"), 3.14)

    def test_cast_bool_true(self):
        for v in ("true", "True", "1", "yes"):
            self.assertTrue(proxy_state._cast_config_value(v, "bool"))

    def test_cast_bool_false(self):
        for v in ("false", "False", "0", "no", ""):
            self.assertFalse(proxy_state._cast_config_value(v, "bool"))

    def test_cast_unknown_returns_value(self):
        self.assertEqual(proxy_state._cast_config_value("abc", "str"), "abc")


class TestProxyConfigImportsFromProxyState(unittest.TestCase):
    """Verify proxy_config shares state with proxy_state (no duplication)."""

    def test_state_lock_is_same_object(self):
        self.assertIs(proxy_config._state_lock, proxy_state._state_lock)

    def test_session_request_count_is_same(self):
        self.assertIs(proxy_config._SESSION_REQUEST_COUNT,
                      proxy_state._SESSION_REQUEST_COUNT)

    def test_session_last_messages_is_same(self):
        self.assertIs(proxy_config._SESSION_LAST_MESSAGES,
                      proxy_state._SESSION_LAST_MESSAGES)

    def test_dedup_cache_is_same(self):
        self.assertIs(proxy_config._DEDUP_CACHE, proxy_state._DEDUP_CACHE)

    def test_latency_window_is_same(self):
        self.assertIs(proxy_config._LATENCY_WINDOW, proxy_state._LATENCY_WINDOW)

    def test_error_window_is_same(self):
        self.assertIs(proxy_config._ERROR_WINDOW, proxy_state._ERROR_WINDOW)


class TestReloadSpecCoverage(unittest.TestCase):
    """Verify _RELOAD_SPEC covers key config variables."""

    def test_reload_spec_covers_tool_clearing(self):
        names = {entry[1] for entry in proxy_state._RELOAD_SPEC}
        self.assertIn("PROXY_CLEAR_ENABLED", names)
        self.assertIn("PROXY_TOOL_KEEP", names)

    def test_reload_spec_covers_ctx_truncation(self):
        names = {entry[1] for entry in proxy_state._RELOAD_SPEC}
        self.assertIn("PROXY_CTX_LIMIT_ENABLED", names)
        self.assertIn("PROXY_CTX_TRUNCATE_STRATEGY", names)

    def test_reload_spec_covers_output_control(self):
        names = {entry[1] for entry in proxy_state._RELOAD_SPEC}
        self.assertIn("PROXY_MAX_TOKENS_OVERRIDE", names)
        self.assertIn("PROXY_BACKEND_TIMEOUT", names)

    def test_reload_spec_covers_loop_detection(self):
        names = {entry[1] for entry in proxy_state._RELOAD_SPEC}
        self.assertIn("PROXY_TEXT_LOOP_ENABLED", names)
        self.assertIn("PROXY_TEXT_LOOP_THRESHOLD", names)

    def test_reload_spec_covers_blocker(self):
        names = {entry[1] for entry in proxy_state._RELOAD_SPEC}
        self.assertIn("PROXY_BLOCKER_ENABLED", names)
        self.assertIn("PROXY_BLOCKER_THRESHOLD", names)


class TestProxyStateAll(unittest.TestCase):
    """Verify __all__ covers all critical public names."""

    def test_all_covers_key_config(self):
        self.assertIn("PROXY_CLEAR_ENABLED", proxy_state.__all__)
        self.assertIn("PROXY_BACKEND_TIMEOUT", proxy_state.__all__)
        self.assertIn("IS_CLOUD", proxy_state.__all__)
        self.assertIn("MODEL_NAME", proxy_state.__all__)

    def test_all_covers_shared_state(self):
        self.assertIn("_SESSION_LAST_MESSAGES", proxy_state.__all__)
        self.assertIn("_DEDUP_CACHE", proxy_state.__all__)
        self.assertIn("_state_lock", proxy_state.__all__)

    def test_all_covers_helpers(self):
        self.assertIn("_parse_conf_env", proxy_state.__all__)
        self.assertIn("_cast_config_value", proxy_state.__all__)


class TestReloadSpecDefaultsConsistency(unittest.TestCase):
    """Verify _RELOAD_SPEC defaults match CONFIG_REGISTRY canonical defaults.

    This prevents drift between the two sources of truth. If someone updates
    CONFIG_REGISTRY but forgets _RELOAD_SPEC (or vice versa), this test fails.
    """

    @classmethod
    def setUpClass(cls):
        cls.registry = proxy_config.CONFIG_REGISTRY

    def _resolve_registry_default(self, env_key, mode):
        """Get the canonical default string from CONFIG_REGISTRY for a mode."""
        entry = self.registry.get(env_key)
        if not entry:
            return None
        defaults = entry.get("defaults", {})
        return defaults.get(mode, defaults.get("all"))

    def _cast_to_canonical_str(self, value, cast):
        """Normalize a value to its string representation for comparison."""
        if cast == "bool":
            return "true" if proxy_state._cast_config_value(value, cast) else "false"
        if cast == "int":
            return str(int(value))
        if cast == "float":
            return str(float(value))
        return str(value)

    def test_reload_spec_matches_config_registry_cloud(self):
        """Each RELOAD_SPEC cloud_default must match CONFIG_REGISTRY cloud default."""
        mismatches = []
        for env_key, py_name, cast, cloud_def, local_def in proxy_state._RELOAD_SPEC:
            registry_default = self._resolve_registry_default(env_key, "cloud")
            if registry_default is None:
                mismatches.append(f"{env_key}: missing from CONFIG_REGISTRY")
                continue
            spec_val = self._cast_to_canonical_str(cloud_def, cast)
            reg_val = self._cast_to_canonical_str(registry_default, cast)
            if spec_val != reg_val:
                mismatches.append(
                    f"{env_key}: RELOAD_SPEC={spec_val} vs CONFIG_REGISTRY={reg_val}"
                )
        if mismatches:
            self.fail("Cloud defaults drift detected:\n  " + "\n  ".join(mismatches))

    def test_reload_spec_matches_config_registry_local(self):
        """Each RELOAD_SPEC local_default must match CONFIG_REGISTRY local default."""
        mismatches = []
        for env_key, py_name, cast, cloud_def, local_def in proxy_state._RELOAD_SPEC:
            registry_default = self._resolve_registry_default(env_key, "local")
            if registry_default is None:
                mismatches.append(f"{env_key}: missing from CONFIG_REGISTRY")
                continue
            spec_val = self._cast_to_canonical_str(local_def, cast)
            reg_val = self._cast_to_canonical_str(registry_default, cast)
            if spec_val != reg_val:
                mismatches.append(
                    f"{env_key}: RELOAD_SPEC={spec_val} vs CONFIG_REGISTRY={reg_val}"
                )
        if mismatches:
            self.fail("Local defaults drift detected:\n  " + "\n  ".join(mismatches))

    def test_reload_spec_py_names_exist_in_module(self):
        """Every py_name in RELOAD_SPEC must be an attribute of proxy_state."""
        missing = []
        for env_key, py_name, _cast, _cd, _ld in proxy_state._RELOAD_SPEC:
            if not hasattr(proxy_state, py_name):
                missing.append(f"{env_key} → {py_name}: not found on proxy_state")
        if missing:
            self.fail("RELOAD_SPEC refers to missing module attributes:\n  " +
                      "\n  ".join(missing))

    def test_reload_spec_covers_compression_vars(self):
        """Compression-related vars must be in RELOAD_SPEC for hot-reload support."""
        names = {entry[1] for entry in proxy_state._RELOAD_SPEC}
        self.assertIn("PROXY_COMPRESS_ENABLED", names)
        self.assertIn("PROXY_COMPRESS_AUDIT", names)


if __name__ == "__main__":
    unittest.main()
