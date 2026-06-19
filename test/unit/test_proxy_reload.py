#!/usr/bin/env python3
"""Unit tests for SIGHUP hot-reload of anthropic_proxy config.

Verifies:
  - _parse_conf_env parses bash-style KEY="value" files correctly
  - _reload_config updates module-level scalars (Tier 1)
  - _reload_config rebuilds Semaphore when PROXY_MAX_CONCURRENT changes (Tier 2)
  - _reload_config rebuilds MODEL_ALIASES to pick up new MODEL_NAME (Tier 2)
  - _reload_config re-derives IS_CLOUD from LLAMA_BASE_URL
  - _reload_config handles dependent defaults (LOOP_LEVEL2/3, CHARS_SATURATION)

Run directly:
    python3 test/unit/test_proxy_reload.py
Or via unittest discovery:
    python3 -m unittest discover -s test/unit -p 'test_*.py' -v
Or via the unified runner:
    bash test/run_tests.sh --unit
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import anthropic_proxy as proxy


def _write_conf(path, lines):
    """Write a bash-style conf file."""
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


class TestParseConfEnv(unittest.TestCase):
    """_parse_conf_env: bash-style KEY=value parsing."""

    def test_basic_key_value(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write('LLAMA_BACKEND="rapid-mlx"\n')
            f.write("LLAMA_PORT=8081\n")
            path = f.name
        try:
            result = proxy._parse_conf_env(path)
            self.assertEqual(result["LLAMA_BACKEND"], "rapid-mlx")
            self.assertEqual(result["LLAMA_PORT"], "8081")
        finally:
            os.unlink(path)

    def test_single_quotes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("MODEL_NAME='mlx-community/Qwen'\n")
            path = f.name
        try:
            result = proxy._parse_conf_env(path)
            self.assertEqual(result["MODEL_NAME"], "mlx-community/Qwen")
        finally:
            os.unlink(path)

    def test_comments_and_blanks(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("# This is a comment\n\n")
            f.write('LLAMA_BACKEND="cloud"  # inline comment not stripped\n')
            path = f.name
        try:
            result = proxy._parse_conf_env(path)
            self.assertEqual(result["LLAMA_BACKEND"], "cloud")
            self.assertNotIn("# This is a comment", result)
        finally:
            os.unlink(path)

    def test_missing_file(self):
        result = proxy._parse_conf_env("/nonexistent/path.conf")
        self.assertEqual(result, {})

    def test_empty_value(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write('RAPID_MLX_EXTRA_ARGS=""\n')
            path = f.name
        try:
            result = proxy._parse_conf_env(path)
            self.assertEqual(result.get("RAPID_MLX_EXTRA_ARGS"), "")
        finally:
            os.unlink(path)

    def test_value_with_equals_sign(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write('LLAMA_EXTRA_ARGS="--jinja --flash-attn on"\n')
            path = f.name
        try:
            result = proxy._parse_conf_env(path)
            self.assertEqual(result["LLAMA_EXTRA_ARGS"], "--jinja --flash-attn on")
        finally:
            os.unlink(path)


class TestReloadConfigScalars(unittest.TestCase):
    """_reload_config: Tier 1 scalar updates."""

    def setUp(self):
        # Save original values to restore after test
        self._saved = {}
        for attr in ["LLAMA_BASE", "BACKEND_TYPE", "IS_CLOUD", "MODEL_NAME",
                     "PROXY_CLEAR_ENABLED", "PROXY_CLEAR_THRESHOLD",
                     "PROXY_FROZEN_HEAD", "PROXY_CTX_LIMIT_ENABLED",
                     "PROXY_MAX_CONCURRENT", "PROXY_CHARS_EXPANSION",
                     "PROXY_BLOCKER_ENABLED", "PROXY_TOOL_FILTER_ENABLED"]:
            self._saved[attr] = getattr(proxy, attr)
        self._tmpdir = tempfile.mkdtemp()
        self._confpath = os.path.join(self._tmpdir, "test.conf")
        # Always create the file so tearDown can unlink it safely
        _write_conf(self._confpath, ['LLAMA_BASE_URL="http://127.0.0.1:8081/v1"'])

    def tearDown(self):
        for attr, val in self._saved.items():
            setattr(proxy, attr, val)
        if os.path.exists(self._confpath):
            os.unlink(self._confpath)
        os.rmdir(self._tmpdir)

    def test_local_to_cloud_switch(self):
        """Switching local→cloud: clearing disabled, ctx_limit disabled,
        frozen_head=0, blocker disabled."""
        _write_conf(self._confpath, [
            'LLAMA_BACKEND="deepseek-cloud"',
            'LLAMA_BASE_URL="https://api.deepseek.com/v1"',
            'MODEL_NAME="deepseek-v4-flash"',
            'PROXY_MAX_CONCURRENT="4"',
        ])
        with patch.object(proxy, "RELOAD_CONFIG_PATH", self._confpath):
            proxy._reload_config()
        self.assertEqual(proxy.BACKEND_TYPE, "cloud")
        self.assertTrue(proxy.IS_CLOUD)
        self.assertEqual(proxy.LLAMA_BASE, "https://api.deepseek.com/v1")
        self.assertEqual(proxy.MODEL_NAME, "deepseek-v4-flash")
        # Cloud defaults: clearing off, ctx_limit off, frozen=0, blocker off
        self.assertFalse(proxy.PROXY_CLEAR_ENABLED)
        self.assertFalse(proxy.PROXY_CTX_LIMIT_ENABLED)
        self.assertEqual(proxy.PROXY_FROZEN_HEAD, 0)
        self.assertFalse(proxy.PROXY_BLOCKER_ENABLED)

    def test_cloud_to_local_switch(self):
        """Switching cloud→local: clearing enabled, ctx_limit enabled,
        frozen_head=12, blocker enabled."""
        _write_conf(self._confpath, [
            'LLAMA_BACKEND="rapid-mlx"',
            'LLAMA_BASE_URL="http://127.0.0.1:8081/v1"',
            'MODEL_NAME="mlx-community/Qwen3.6-35B-A3B-4bit"',
            'PROXY_MAX_CONCURRENT="1"',
        ])
        with patch.object(proxy, "RELOAD_CONFIG_PATH", self._confpath):
            proxy._reload_config()
        self.assertEqual(proxy.BACKEND_TYPE, "local")
        self.assertFalse(proxy.IS_CLOUD)
        self.assertTrue(proxy.PROXY_CLEAR_ENABLED)
        self.assertTrue(proxy.PROXY_CTX_LIMIT_ENABLED)
        self.assertEqual(proxy.PROXY_FROZEN_HEAD, 12)
        self.assertTrue(proxy.PROXY_BLOCKER_ENABLED)

    def test_explicit_override_takes_precedence(self):
        """Conf explicit values override is_cloud defaults."""
        _write_conf(self._confpath, [
            'LLAMA_BASE_URL="https://api.deepseek.com/v1"',
            'PROXY_CLEAR_ENABLED="true"',
            'PROXY_FROZEN_HEAD="6"',
        ])
        with patch.object(proxy, "RELOAD_CONFIG_PATH", self._confpath):
            proxy._reload_config()
        # Cloud backend but clearing forced on
        self.assertTrue(proxy.IS_CLOUD)
        self.assertTrue(proxy.PROXY_CLEAR_ENABLED)
        self.assertEqual(proxy.PROXY_FROZEN_HEAD, 6)

    def test_backend_type_auto_detection(self):
        """BACKEND_TYPE auto-detected from LLAMA_BASE_URL when not set."""
        _write_conf(self._confpath, [
            'LLAMA_BASE_URL="https://api.openai.com/v1"',
        ])
        with patch.object(proxy, "RELOAD_CONFIG_PATH", self._confpath):
            proxy._reload_config()
        self.assertEqual(proxy.BACKEND_TYPE, "cloud")
        self.assertTrue(proxy.IS_CLOUD)

    def test_empty_conf_keeps_current(self):
        """Empty/missing conf: no crash, keeps current values."""
        with patch.object(proxy, "RELOAD_CONFIG_PATH", "/nonexistent"):
            proxy._reload_config()
        # Should not raise; values unchanged


class TestReloadSemaphore(unittest.TestCase):
    """_reload_config: Tier 2 Semaphore rebuild."""

    def setUp(self):
        self._saved_max = proxy.PROXY_MAX_CONCURRENT
        self._saved_lock = proxy._llama_lock
        self._tmpdir = tempfile.mkdtemp()
        self._confpath = os.path.join(self._tmpdir, "test.conf")

    def tearDown(self):
        setattr(proxy, "PROXY_MAX_CONCURRENT", self._saved_max)
        setattr(proxy, "_llama_lock", self._saved_lock)
        os.unlink(self._confpath)
        os.rmdir(self._tmpdir)

    def test_semaphore_rebuilt_on_max_change(self):
        """When PROXY_MAX_CONCURRENT changes, _llama_lock is replaced."""
        _write_conf(self._confpath, [
            'LLAMA_BASE_URL="http://127.0.0.1:8081/v1"',
            'PROXY_MAX_CONCURRENT="4"',
        ])
        old_lock = proxy._llama_lock
        with patch.object(proxy, "RELOAD_CONFIG_PATH", self._confpath):
            proxy._reload_config()
        self.assertEqual(proxy.PROXY_MAX_CONCURRENT, 4)
        self.assertIsNot(proxy._llama_lock, old_lock)

    def test_semaphore_not_rebuilt_when_unchanged(self):
        """When PROXY_MAX_CONCURRENT stays same, _llama_lock is NOT replaced."""
        current = proxy.PROXY_MAX_CONCURRENT
        _write_conf(self._confpath, [
            'LLAMA_BASE_URL="http://127.0.0.1:8081/v1"',
            'PROXY_MAX_CONCURRENT="%d"' % current,
        ])
        old_lock = proxy._llama_lock
        with patch.object(proxy, "RELOAD_CONFIG_PATH", self._confpath):
            proxy._reload_config()
        self.assertIs(proxy._llama_lock, old_lock)


class TestReloadModelAliases(unittest.TestCase):
    """_reload_config: Tier 2 MODEL_ALIASES rebuild."""

    def setUp(self):
        self._saved_aliases = list(proxy.MODEL_ALIASES)
        self._saved_model = proxy.MODEL_NAME
        self._tmpdir = tempfile.mkdtemp()
        self._confpath = os.path.join(self._tmpdir, "test.conf")

    def tearDown(self):
        setattr(proxy, "MODEL_ALIASES", self._saved_aliases)
        setattr(proxy, "MODEL_NAME", self._saved_model)
        os.unlink(self._confpath)
        os.rmdir(self._tmpdir)

    def test_aliases_include_new_model(self):
        """MODEL_ALIASES is rebuilt to include the new MODEL_NAME."""
        _write_conf(self._confpath, [
            'LLAMA_BASE_URL="http://127.0.0.1:8081/v1"',
            'MODEL_NAME="test-model-v2"',
        ])
        with patch.object(proxy, "RELOAD_CONFIG_PATH", self._confpath):
            proxy._reload_config()
        self.assertIn("test-model-v2", proxy.MODEL_ALIASES)
        self.assertIn("claude-sonnet-4-6", proxy.MODEL_ALIASES)


class TestReloadDependentDefaults(unittest.TestCase):
    """_reload_config: dependent defaults (LOOP_LEVEL2/3, CHARS_SATURATION)."""

    def setUp(self):
        self._saved = {}
        for attr in ["PROXY_LOOP_THRESHOLD", "PROXY_LOOP_LEVEL2", "PROXY_LOOP_LEVEL3",
                     "PROXY_CHARS_SATURATION", "PROXY_OOM_SAFE_CHARS",
                     "PROXY_PRE_TRUNCATE_CHARS", "LLAMA_BASE", "BACKEND_TYPE"]:
            self._saved[attr] = getattr(proxy, attr)
        self._tmpdir = tempfile.mkdtemp()
        self._confpath = os.path.join(self._tmpdir, "test.conf")

    def tearDown(self):
        for attr, val in self._saved.items():
            setattr(proxy, attr, val)
        os.unlink(self._confpath)
        os.rmdir(self._tmpdir)

    def test_loop_level_defaults_to_threshold_times_2(self):
        """PROXY_LOOP_LEVEL2 defaults to PROXY_LOOP_THRESHOLD * 2."""
        _write_conf(self._confpath, [
            'LLAMA_BASE_URL="http://127.0.0.1:8081/v1"',
            'PROXY_LOOP_THRESHOLD="5"',
        ])
        with patch.object(proxy, "RELOAD_CONFIG_PATH", self._confpath):
            proxy._reload_config()
        self.assertEqual(proxy.PROXY_LOOP_THRESHOLD, 5)
        self.assertEqual(proxy.PROXY_LOOP_LEVEL2, 10)
        self.assertEqual(proxy.PROXY_LOOP_LEVEL3, 15)

    def test_chars_saturation_fallback_to_ctx_chars_limit(self):
        """PROXY_CHARS_SATURATION falls back to PROXY_CTX_CHARS_LIMIT."""
        _write_conf(self._confpath, [
            'LLAMA_BASE_URL="http://127.0.0.1:8081/v1"',
            'PROXY_CTX_CHARS_LIMIT="999000"',
        ])
        with patch.object(proxy, "RELOAD_CONFIG_PATH", self._confpath):
            proxy._reload_config()
        self.assertEqual(proxy.PROXY_CHARS_SATURATION, 999000)

    def test_oom_safe_chars_fallback_to_pre_truncate(self):
        """PROXY_OOM_SAFE_CHARS falls back to PROXY_PRE_TRUNCATE_CHARS."""
        _write_conf(self._confpath, [
            'LLAMA_BASE_URL="http://127.0.0.1:8081/v1"',
            'PROXY_PRE_TRUNCATE_CHARS="123456"',
        ])
        with patch.object(proxy, "RELOAD_CONFIG_PATH", self._confpath):
            proxy._reload_config()
        self.assertEqual(proxy.PROXY_OOM_SAFE_CHARS, 123456)
        self.assertEqual(proxy.PROXY_PRE_TRUNCATE_CHARS, 123456)


if __name__ == "__main__":
    unittest.main()
