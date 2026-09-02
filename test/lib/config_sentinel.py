#!/usr/bin/env python3
"""config_sentinel.py — 配置恢复哨兵(进程内包装 unittest discover)。

用法(替代直接调 unittest discover):
    python3 test/lib/config_sentinel.py <unit_dir>

流程: 导入 proxy_state 捕获基线 → unittest discover 跑全部用例 →
对比基线断言零漂移。漂移 = 某个测试改了全局配置属性未恢复(跨测试
串扰源), 以退出码 1 + 漂移清单上报。

基线时点 = proxy_state 导入完成后、任何测试模块执行前——即模块级
默认值是"干净态"。
"""
import sys
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import proxy_state as _ps  # noqa: E402  基线在测试模块执行前捕获
from test.lib.config_fixture import ConfigRestoreSentinel  # noqa: E402


def main():
    unit_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "test", "unit")
    sentinel = ConfigRestoreSentinel().capture()

    suite = unittest.defaultTestLoader.discover(unit_dir, pattern="test_*.py")
    # verbosity=2: 逐用例行(test_x .. ok)——run_tests.sh run_unit 据此 grep 计数
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    drifted = sentinel.verify()
    if drifted:
        print("\n[CONFIG SENTINEL][FAIL] proxy_state 配置漂移(测试改全局属性未恢复):")
        for attr, (captured, now) in sorted(drifted.items()):
            print(f"  {attr}: 基线={captured!r} → 现值={now!r}")
        print("  修复: 测试内用 test.lib.config_fixture.patch_config() 替代裸赋值")
        return 1
    if result.failures or result.errors:
        # 用例本身失败时仍以用例结果为主, 但提示排查配置面
        pass
    print("[CONFIG SENTINEL] 零漂移 ✓")
    return 0 if (result.wasSuccessful()) else 1


if __name__ == "__main__":
    sys.exit(main())
