"""配置漂移检测单测（tools/flag_drift.py）+ conf 解析行尾注释回归。

背景（2026-10-29）：两次事故同源——conf 面自洽、运行面陈旧：
  · br4 录制面：换模重建 conf 丢旗标，进程 env 里是 false → 录制失明
  · ctx_recall 死环：conf 08:40:01 已置 MICRO_TURN=true，代理 08:39:39 启动 → 进程 false
本文件锁死「漂移判定 + 归因分类（reload 可修 / 需重启）」。
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import flag_drift


class ParseProcessEnvTest(unittest.TestCase):

    def test_parses_trailing_env_tokens(self):
        line = ("  1234 /usr/bin/python3 anthropic_proxy.py "
                "PROXY_PD_ENABLED=true PROXY_PD_MICRO_TURN_ENABLED=false HOME=/Users/x")
        env = flag_drift.parse_process_env(line)
        self.assertEqual(env["PROXY_PD_ENABLED"], "true")
        self.assertEqual(env["PROXY_PD_MICRO_TURN_ENABLED"], "false")
        self.assertEqual(env["HOME"], "/Users/x")

    def test_ignores_cmdline_args_and_lowercase(self):
        line = "  9 /bin/zsh -c --flag=1 foo=bar PROXY_X=1"
        env = flag_drift.parse_process_env(line)
        self.assertIn("PROXY_X", env)
        self.assertNotIn("foo", env)     # 小写 key 不算环境变量
        self.assertNotIn("--flag", env)


class DiffTest(unittest.TestCase):

    def test_no_drift_when_equal(self):
        self.assertEqual(flag_drift.diff({"PROXY_A": "true"}, {"PROXY_A": "true"}), [])

    def test_drift_when_process_missing(self):
        d = flag_drift.diff({"PROXY_A": "true"}, {})
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["key"], "PROXY_A")
        self.assertIn("缺省", d[0]["proc"])

    def test_drift_when_value_differs(self):
        d = flag_drift.diff({"PROXY_A": "true"}, {"PROXY_A": "false"})
        self.assertEqual(d[0]["conf"], "true")
        self.assertEqual(d[0]["proc"], "false")

    def test_bool_and_string_normalization(self):
        # conf 里 "TRUE" 引号写法 与 进程 "true" 应视为一致
        self.assertEqual(flag_drift.diff({"PROXY_A": '"TRUE"'}, {"PROXY_A": "true"}), [])

    def test_fix_classification_and_order(self):
        d = flag_drift.diff(
            {"PROXY_RELOAD_OK": "false", "PROXY_NEED_RESTART": "false"},
            {"PROXY_RELOAD_OK": "true", "PROXY_NEED_RESTART": "true"},
            reloadable_keys={"PROXY_RELOAD_OK"})
        self.assertEqual([x["key"] for x in d], ["PROXY_NEED_RESTART", "PROXY_RELOAD_OK"])
        self.assertEqual(d[0]["fix"], "restart")
        self.assertEqual(d[1]["fix"], "reload")

    def test_extra_process_keys_are_not_drift(self):
        """进程里多出来的键（manage.sh 派生注入）不算漂移，避免噪声。"""
        self.assertEqual(flag_drift.diff({"PROXY_A": "1"},
                                         {"PROXY_A": "1", "PROXY_B": "2"}), [])

    def test_non_flag_keys_ignored(self):
        d = flag_drift.diff({"PATH": "/usr/bin", "PROXY_A": "1"}, {"PATH": "/bin"})
        self.assertEqual([x["key"] for x in d], ["PROXY_A"])

    def test_summarize_mentions_both_classes(self):
        d = flag_drift.diff({"PROXY_R": "1", "PROXY_S": "1"},
                            {}, reloadable_keys={"PROXY_R"})
        s = flag_drift.summarize(d)
        self.assertIn("需重启", s)
        self.assertIn("可 reload", s)
        self.assertEqual(flag_drift.summarize([]), "")


class ConfInlineCommentTest(unittest.TestCase):
    """回归：无引号值 + 行尾注释必须剥干净（2026-10-29 实测被吞进值里）。"""

    def _parse(self, content):
        import proxy_state as ps
        fd, path = tempfile.mkstemp(suffix=".conf")
        os.close(fd)
        try:
            with open(path, "w") as f:
                f.write(content)
            return ps._parse_conf_env(path)
        finally:
            os.remove(path)

    def test_unquoted_with_trailing_comment(self):
        got = self._parse('export PROXY_COMPRESS_ENABLED=false   # 上下文工程引擎接管\n')
        self.assertEqual(got["PROXY_COMPRESS_ENABLED"], "false")

    def test_unquoted_hash_inside_value_kept(self):
        got = self._parse('export PROXY_TAG=abc#def\n')
        self.assertEqual(got["PROXY_TAG"], "abc#def")

    def test_quoted_with_comment(self):
        got = self._parse('export PROXY_A="x y"  # note\n')
        self.assertEqual(got["PROXY_A"], "x y")

    def test_quoted_hash_inside_kept(self):
        got = self._parse('export PROXY_A="a # b"\n')
        self.assertEqual(got["PROXY_A"], "a # b")


if __name__ == "__main__":
    unittest.main()
