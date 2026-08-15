"""Unit tests for configuration unification (Phase 1).

Covers the new proxy_config helpers: get_default / get_registry_entry /
is_reloadable / list_unregistered_env_vars / validate_startup /
write_defaults_sh, and their consistency with proxy_state defaults.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import proxy_config


class TestGetDefault(unittest.TestCase):
    """get_default 按后端模式解析 CONFIG_REGISTRY 默认值。"""

    def test_get_default_local(self):
        self.assertEqual(
            proxy_config.get_default("PROXY_CHARS_GROWTH", "local"), "40000")
        self.assertEqual(
            proxy_config.get_default("PROXY_MEMORY_REJECT_THRESHOLD", "local"), "90")
        self.assertEqual(
            proxy_config.get_default("PROXY_DYNAMIC_CONCURRENT_MAX", "local"), "4")

    def test_get_default_cloud(self):
        self.assertEqual(
            proxy_config.get_default("PROXY_CHARS_GROWTH", "cloud"), "80000")
        self.assertEqual(
            proxy_config.get_default("PROXY_CHARS_OOM_DANGER", "cloud"), "1000000")
        self.assertEqual(
            proxy_config.get_default("PROXY_DYNAMIC_CONCURRENT_MAX", "cloud"), "8")

    def test_get_default_all_fallback(self):
        # 只有 "all" 默认值的 key 在两种模式下一致
        self.assertEqual(
            proxy_config.get_default("PROXY_OOM_SAFE_CHARS", "local"), "200000")
        self.assertEqual(
            proxy_config.get_default("PROXY_OOM_SAFE_CHARS", "cloud"), "200000")

    def test_get_default_unknown_key(self):
        self.assertEqual(proxy_config.get_default("PROXY_NO_SUCH_KEY", "local"), "")

    def test_get_default_backend_inference(self):
        # backend_type=None 时从 LLAMA_BASE_URL 推断
        with patch.dict(os.environ, {"LLAMA_BASE_URL": "https://api.deepseek.com/v1"}):
            self.assertEqual(proxy_config.get_default("PROXY_CHARS_GROWTH"), "80000")
        with patch.dict(os.environ, {"LLAMA_BASE_URL": "http://127.0.0.1:8081/v1"}):
            self.assertEqual(proxy_config.get_default("PROXY_CHARS_GROWTH"), "40000")

    def test_get_default_profile_override(self):
        # PROFILE_MAP 覆盖：aggressive 下 PROXY_OOM_SAFE_CHARS = 150000
        with patch.dict(os.environ, {"PROXY_COMPRESSION_PROFILE": "aggressive"}):
            self.assertEqual(
                proxy_config.get_default("PROXY_OOM_SAFE_CHARS", "local"), "150000")
        # balanced（默认）不覆盖
        with patch.dict(os.environ, {"PROXY_COMPRESSION_PROFILE": "balanced"}):
            self.assertEqual(
                proxy_config.get_default("PROXY_OOM_SAFE_CHARS", "local"), "200000")


class TestRegistryHelpers(unittest.TestCase):
    def test_get_registry_entry(self):
        entry = proxy_config.get_registry_entry("PROXY_CHARS_GROWTH")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["type"], "int")
        self.assertIsNone(proxy_config.get_registry_entry("PROXY_NO_SUCH_KEY"))

    def test_is_reloadable(self):
        self.assertTrue(proxy_config.is_reloadable("PROXY_CHARS_GROWTH"))
        # PORT 是 module scope（需重启）
        self.assertFalse(proxy_config.is_reloadable("PORT"))
        # 未注册的 key
        self.assertFalse(proxy_config.is_reloadable("PROXY_NO_SUCH_KEY"))

    def test_list_unregistered_env_vars(self):
        env = {
            "PROXY_CHARS_GROWTH": "40000",       # 已注册
            "PROXY_FOO_UNREGISTERED": "1",        # 未注册
            "LLAMA_BACKEND": "rapid-mlx",         # 已注册（后端启动参数）
            "RAPID_MLX_EXTRA_ARGS": "",           # 已注册（后端启动参数）
            "RAPID_MLX_FOO_UNREGISTERED": "1",    # 未注册
            "PATH": "/usr/bin",                   # 不在前缀内
        }
        self.assertEqual(
            proxy_config.list_unregistered_env_vars(env),
            ["PROXY_FOO_UNREGISTERED", "RAPID_MLX_FOO_UNREGISTERED"])

    def test_list_unregistered_env_vars_allowlist(self):
        env = {
            "HF_HUB_OFFLINE": "1",        # 非代理变量白名单
            "KIMI_API_KEY": "sk-test",    # *_API_KEY 后缀白名单
            "PROXY_FOO": "1",             # 未注册
        }
        # HF_HUB_OFFLINE / KIMI_API_KEY 不在 LLAMA_/PROXY_/RAPID_MLX_ 前缀内，
        # 此处主要验证白名单函数本身的行为。
        self.assertTrue(proxy_config._is_non_proxy_var("HF_HUB_OFFLINE"))
        self.assertTrue(proxy_config._is_non_proxy_var("KIMI_API_KEY"))
        self.assertTrue(proxy_config._is_non_proxy_var("CONFIG_NAME"))
        self.assertFalse(proxy_config._is_non_proxy_var("PROXY_FOO"))


class TestValidateStartup(unittest.TestCase):
    def test_validate_startup_clean(self):
        errors = proxy_config.validate_startup(
            env={"PROXY_CHARS_GROWTH": "40000", "PROXY_CLEAR_ENABLED": "true"},
            backend_type="local",
        )
        self.assertEqual(errors, [])

    def test_validate_startup_type_errors(self):
        env = {
            "PROXY_CHARS_GROWTH": "not_an_int",           # int 类型错误
            "PROXY_TEXT_LOOP_SIMILARITY": "not_a_float",  # float 类型错误
            "PROXY_CLEAR_ENABLED": "maybe",               # bool 类型错误
        }
        errors = proxy_config.validate_startup(env=env, backend_type="local")
        self.assertTrue(any("PROXY_CHARS_GROWTH should be int" in e for e in errors))
        self.assertTrue(any("PROXY_TEXT_LOOP_SIMILARITY should be float" in e for e in errors))
        self.assertTrue(any("PROXY_CLEAR_ENABLED should be bool" in e for e in errors))

    def test_validate_startup_bad_backend_type(self):
        errors = proxy_config.validate_startup(env={}, backend_type="quantum")
        self.assertTrue(any("BACKEND_TYPE" in e for e in errors))

    def test_validate_startup_unregistered_env(self):
        errors = proxy_config.validate_startup(
            env={"PROXY_TOTALLY_MADE_UP": "1"}, backend_type="local")
        self.assertTrue(any("PROXY_TOTALLY_MADE_UP" in e for e in errors))

    def test_validate_startup_conf_vars(self):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".conf", delete=False) as f:
            f.write('# 注释行\n')
            f.write('PROXY_CHARS_GROWTH="40000"\n')   # 已注册
            f.write('LLAMA_BACKEND="rapid-mlx"\n')      # 已注册（后端启动参数）
            f.write('RAPID_MLX_FOO="bar"\n')            # 未注册
            f.write('export PROXY_BAR="1"\n')           # 未注册（export 写法也要识别）
            f.write('CONFIG_NAME="test"\n')             # 非代理变量白名单，跳过
            f.write('HF_HUB_OFFLINE=1\n')               # 非代理变量白名单，跳过
            conf_path = f.name
        try:
            errors = proxy_config.validate_startup(
                env={}, active_conf_path=conf_path, backend_type="local")
            self.assertTrue(any("RAPID_MLX_FOO" in e for e in errors))
            self.assertTrue(any("PROXY_BAR" in e for e in errors))
            self.assertFalse(any("LLAMA_BACKEND" in e for e in errors))
            self.assertFalse(any("PROXY_CHARS_GROWTH" in e for e in errors))
            self.assertFalse(any("CONFIG_NAME" in e for e in errors))
            self.assertFalse(any("HF_HUB_OFFLINE" in e for e in errors))
        finally:
            os.unlink(conf_path)

    def test_validate_startup_strict_raises(self):
        with self.assertRaises(SystemExit):
            proxy_config.validate_startup(
                env={"PROXY_CHARS_GROWTH": "bad"}, backend_type="local", strict=True)


class TestWriteDefaultsSh(unittest.TestCase):
    def test_write_defaults_sh(self):
        with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as f:
            path = f.name
        try:
            # 已设置的变量不应写入文件
            with patch.dict(os.environ, {"PROXY_CHARS_GROWTH": "99999"}, clear=False):
                proxy_config.write_defaults_sh("local", path)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn('export PROXY_CHARS_EXPANSION="90000"', content)
            self.assertIn('export PROXY_MAX_CONCURRENT="1"', content)
            self.assertNotIn("PROXY_CHARS_GROWTH", content)

            # 生成的文件必须可被 bash source，且值符合预期
            result = subprocess.run(
                ["bash", "-c", f'source "{path}" && echo "$PROXY_CHARS_EXPANSION"'],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "90000")
        finally:
            os.unlink(path)

    def test_write_defaults_sh_cloud(self):
        with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as f:
            path = f.name
        try:
            proxy_config.write_defaults_sh("cloud", path)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn('export PROXY_CHARS_GROWTH="80000"', content)
            self.assertIn('export PROXY_DYNAMIC_CONCURRENT_MAX="8"', content)
        finally:
            os.unlink(path)


class TestProxyStateConsistency(unittest.TestCase):
    """proxy_state 的模块级默认值应与 CONFIG_REGISTRY 解析结果一致。"""

    def test_state_matches_registry(self):
        import proxy_state
        bt = "cloud" if proxy_state.IS_CLOUD else "local"
        self.assertEqual(
            proxy_state.PROXY_CHARS_GROWTH,
            int(proxy_config.get_default("PROXY_CHARS_GROWTH", bt)))
        self.assertEqual(
            proxy_state.PROXY_CHARS_EXPANSION,
            int(proxy_config.get_default("PROXY_CHARS_EXPANSION", bt)))
        self.assertEqual(
            proxy_state.PROXY_CHARS_SATURATION,
            int(proxy_config.get_default("PROXY_CHARS_SATURATION", bt)))
        self.assertEqual(
            proxy_state.PROXY_CHARS_OOM_DANGER,
            int(proxy_config.get_default("PROXY_CHARS_OOM_DANGER", bt)))
        self.assertEqual(
            proxy_state.PROXY_DYNAMIC_CONCURRENT_MAX,
            int(proxy_config.get_default("PROXY_DYNAMIC_CONCURRENT_MAX", bt)))

    def test_lazy_state_reexport(self):
        # proxy_config 惰性转发的共享状态与 proxy_state 是同一对象
        import proxy_state
        self.assertIs(proxy_config._state_lock, proxy_state._state_lock)
        self.assertIs(proxy_config._DEDUP_CACHE, proxy_state._DEDUP_CACHE)

    def test_no_circular_import_either_order(self):
        # 两种 import 顺序都必须可用（子进程隔离验证）
        for snippet in ("import proxy_config",
                        "import proxy_state",
                        "import proxy_config, proxy_state",
                        "import proxy_state, proxy_config"):
            result = subprocess.run(
                [sys.executable, "-c", snippet],
                capture_output=True, text=True, cwd=_REPO_ROOT)
            self.assertEqual(result.returncode, 0,
                             f"{snippet} failed: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
