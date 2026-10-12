"""测试包标记（2026-10-12，L-94）。

存在理由：CPython 标准库自带 `test` 包（`lib/pythonX.Y/test/__init__.py`，**regular
package**）。本仓 `test/` 此前无 `__init__.py` ⇒ PEP 420 命名空间包在路径扫描中输给
后出现的 stdlib regular package，于是 `from test.lib.config_fixture import ...`
（`test/lib/config_sentinel.py:22`）解析到 stdlib 的 `test`，报
`ModuleNotFoundError: No module named 'test.lib'` ⇒ `.githooks/pre-commit` 的
fast unit 层（`bash test/run_tests.sh --unit`）**每次提交必红**，只能靠
`SKIP_TESTS=1` 绕过（等于该层长期失效，配置恢复哨兵不再生效）。

本文件使 REPO/test 成为 regular package：REPO 在 sys.path 首位（
`config_sentinel.py:18-19` 显式插入）⇒ 本仓 `test` 胜出。

注意：**不要**给 `test/unit/`／`test/integration/` 加 `__init__.py`——
`unittest discover` 的 top_level_dir 目前停在那一层（无 `__init__.py`），
测试模块以裸名导入、同目录兄弟模块可直接 `import`；加了会改变导入根。
"""
