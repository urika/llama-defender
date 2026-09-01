#!/usr/bin/env python3
"""test_config_fixture.py — 配置夹具 + 恢复哨兵测试。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from test.lib.config_fixture import patch_config, ConfigRestoreSentinel
import proxy_state as _ps


class TestPatchConfig(unittest.TestCase):

    def test_override_and_restore(self):
        orig = _ps.PROXY_PD_ENABLED
        with patch_config(PROXY_PD_ENABLED=False):
            self.assertFalse(_ps.PROXY_PD_ENABLED)
        self.assertEqual(_ps.PROXY_PD_ENABLED, orig)

    def test_restore_on_exception(self):
        orig = _ps.PROXY_TOOL_FILTER_MAX
        with self.assertRaises(RuntimeError):
            with patch_config(PROXY_TOOL_FILTER_MAX=99):
                raise RuntimeError("boom")
        self.assertEqual(_ps.PROXY_TOOL_FILTER_MAX, orig)

    def test_multi_key(self):
        orig_pd = _ps.PROXY_PD_ENABLED
        orig_keep = _ps.PROXY_CTX_KEEP_MESSAGES
        with patch_config(PROXY_PD_ENABLED=False, PROXY_CTX_KEEP_MESSAGES=7):
            self.assertFalse(_ps.PROXY_PD_ENABLED)
            self.assertEqual(_ps.PROXY_CTX_KEEP_MESSAGES, 7)
        self.assertTrue(_ps.PROXY_PD_ENABLED)
        self.assertEqual(_ps.PROXY_CTX_KEEP_MESSAGES, orig_keep)

    def test_unknown_attr_rejected(self):
        with self.assertRaises(AttributeError):
            with patch_config(PROXY_TYPo_KEY=1):
                pass

    def test_extra_attrs_covered(self):
        # TOOL_ALWAYS_KEEP 等非 RELOAD_SPEC 属性也可改写恢复
        orig = _ps.TOOL_ALWAYS_KEEP
        with patch_config(TOOL_ALWAYS_KEEP=("X",)):
            self.assertEqual(_ps.TOOL_ALWAYS_KEEP, ("X",))
        self.assertEqual(_ps.TOOL_ALWAYS_KEEP, orig)


class TestConfigRestoreSentinel(unittest.TestCase):

    def test_clean_run_zero_drift(self):
        s = ConfigRestoreSentinel().capture()
        _ps.PROXY_PD_ENABLED = not _ps.PROXY_PD_ENABLED  # 改了又改回
        _ps.PROXY_PD_ENABLED = not _ps.PROXY_PD_ENABLED
        self.assertEqual(s.verify(), {})

    def test_drift_detected(self):
        s = ConfigRestoreSentinel().capture()
        orig = _ps.PROXY_MAX_CONCURRENT
        _ps.PROXY_MAX_CONCURRENT = orig + 1  # 模拟测试忘记恢复
        try:
            drifted = s.verify()
            self.assertIn("PROXY_MAX_CONCURRENT", drifted)
            self.assertEqual(drifted["PROXY_MAX_CONCURRENT"], (orig, orig + 1))
        finally:
            _ps.PROXY_MAX_CONCURRENT = orig

    def test_attrs_window(self):
        s = ConfigRestoreSentinel(attrs=("PROXY_PD_ENABLED",)).capture()
        orig = _ps.PROXY_MAX_CONCURRENT
        _ps.PROXY_MAX_CONCURRENT = orig + 5  # 窗口外 → 不报
        try:
            self.assertEqual(s.verify(), {})
        finally:
            _ps.PROXY_MAX_CONCURRENT = orig

    def test_verify_before_capture_raises(self):
        with self.assertRaises(RuntimeError):
            ConfigRestoreSentinel().verify()


if __name__ == "__main__":
    unittest.main()
