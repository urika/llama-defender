"""F4-a 测试状态账本（IFC-9）离线夹具测试。

设计: swe-eval/docs/fnf-f4-design-20261030.md §一（测试状态增量证据）
登记: docs/07-project-board/v0.7.0-information-plane.md IFC-9
消费: swe-eval scripts/quality_metrics.py A4 首绿轮次/A5 测试状态改善率
旗标 PROXY_FNF_TEST_STATUS_ENABLED 缺省关 = 零解析零日志零账本。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxy_state
import tool_filter as tf


def tu(i, name, inp):
    return {"role": "assistant",
            "content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}


def tr(i, content):
    return {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": i, "content": content}]}


SUMMARY_RED = ("============================= FAILURES =============================\n"
               "AssertionError: assert 1 == 2\n1 failed, 5 passed in 0.12s\n")


class FnftestStatusLedgerTest(unittest.TestCase):
    def setUp(self):
        tf._TEST_STATUS.clear()
        tf._TEST_STATUS_SEEN.clear()
        self.flag = patch.object(proxy_state, "PROXY_FNF_TEST_STATUS_ENABLED", True)

    def test_flag_off_zero_footprint(self):
        """旗标缺省关 = 零账本零改写（消息原样透传）。"""
        raw = "1 failed, 5 passed in 0.12s"
        msgs = [tu("t1", "Bash", {"command": "pytest -q"}), tr("t1", raw)]
        out, _ = tf._translate_tool_result_errors_ex(
            msgs, session_id="sA", profile="")
        self.assertEqual(tf._TEST_STATUS, {})
        self.assertEqual(out[1]["content"][0]["content"], raw)

    def test_bash_summary_recorded_tuid_dedup(self):
        """记账一次；客户端全量重发历史（同 tuid 重现）不重复记账；新 tuid 追加。"""
        with self.flag:
            msgs = [tu("t1", "Bash", {"command": "pytest -q"}),
                    tr("t1", SUMMARY_RED)]
            tf._translate_tool_result_errors_ex(msgs, session_id="sA", profile="")
            self.assertEqual(len(tf._TEST_STATUS["sA"]), 1)
            tf._translate_tool_result_errors_ex(msgs, session_id="sA", profile="")
            self.assertEqual(len(tf._TEST_STATUS["sA"]), 1)
            msgs2 = msgs + [tu("t2", "Bash", {"command": "pytest -q"}),
                            tr("t2", "5 passed in 0.30s")]
            tf._translate_tool_result_errors_ex(msgs2, session_id="sA", profile="")
        hist = tf._TEST_STATUS["sA"]
        self.assertEqual([r["failed"] for r in hist], [1, 0])
        self.assertEqual([r["passed"] for r in hist], [5, 5])

    def test_first_green_failed_zero(self):
        """A4 信号：全绿行无 failed 记号 → failed=0（不是漏记）。"""
        with self.flag:
            msgs = [tu("g1", "Bash", {"command": "pytest -q"}),
                    tr("g1", "===================== 2138 passed in 21.13s =====================")]
            tf._translate_tool_result_errors_ex(msgs, session_id="sG", profile="")
        rec = tf._TEST_STATUS["sG"][0]
        self.assertEqual(rec["failed"], 0)
        self.assertEqual(rec["passed"], 2138)

    def test_collection_errors_count_as_failed(self):
        """errors 并入 failed：「2 errors, 5 passed」不得冒充全绿。"""
        with self.flag:
            msgs = [tu("e1", "Bash", {"command": "pytest -q"}),
                    tr("e1", "2 errors, 5 passed in 1.00s")]
            tf._translate_tool_result_errors_ex(msgs, session_id="sE", profile="")
        self.assertEqual(tf._TEST_STATUS["sE"][0]["failed"], 2)
        self.assertEqual(tf._TEST_STATUS["sE"][0]["passed"], 5)

    def test_non_bash_not_recorded(self):
        """只认 Bash 结果——Read 测试日志文件带回摘要行不入账（防假读数）。"""
        with self.flag:
            msgs = [tu("r1", "Read", {"file_path": "/tmp/ci.log"}),
                    tr("r1", "1 failed, 5 passed in 0.12s")]
            tf._translate_tool_result_errors_ex(msgs, session_id="sB", profile="")
        self.assertNotIn("sB", tf._TEST_STATUS)

    def test_non_summary_text_not_recorded(self):
        """摘要行锚：无「counts in <秒>s」行形态的正文数字不误触。"""
        with self.flag:
            msgs = [tu("n1", "Bash", {"command": "grep failed CHANGES"}),
                    tr("n1", "3 failed attempts were made\n done")]
            tf._translate_tool_result_errors_ex(msgs, session_id="sC", profile="")
        self.assertNotIn("sC", tf._TEST_STATUS)

    def test_pure_skip_line_not_recorded(self):
        """纯 skip/warning 行无绿/红信号，不入账。"""
        with self.flag:
            msgs = [tu("k1", "Bash", {"command": "pytest -q"}),
                    tr("k1", "3 skipped, 1 warning in 0.20s")]
            tf._translate_tool_result_errors_ex(msgs, session_id="sK", profile="")
        self.assertNotIn("sK", tf._TEST_STATUS)

    def test_hist_capped_at_32(self):
        """每会话账本只留最近 32 条（旧摘要滑出）。"""
        with self.flag:
            msgs = []
            for i in range(40):
                msgs += [tu(f"c{i}", "Bash", {"command": "pytest -q"}),
                         tr(f"c{i}", f"{i} failed, 1 passed in 0.1s")]
            tf._translate_tool_result_errors_ex(msgs, session_id="sH", profile="")
        hist = tf._TEST_STATUS["sH"]
        self.assertEqual(len(hist), tf._FNF_TS_HIST_MAX)
        self.assertEqual(hist[0]["failed"], 40 - tf._FNF_TS_HIST_MAX)
        self.assertEqual(hist[-1]["failed"], 39)

    def test_session_scoped(self):
        """会话隔离：同 tuid 不跨会话；各会话独立时间序。"""
        with self.flag:
            msgs = [tu("x1", "Bash", {"command": "pytest"}),
                    tr("x1", "2 failed, 3 passed in 0.5s")]
            tf._translate_tool_result_errors_ex(msgs, session_id="sX", profile="")
            tf._translate_tool_result_errors_ex(msgs, session_id="sY", profile="")
        self.assertEqual(len(tf._TEST_STATUS["sX"]), 1)
        self.assertEqual(len(tf._TEST_STATUS["sY"]), 1)

    def test_log_line_contract(self):
        """日志行契约：test_status=failed=N,passed=M,hist=K（swe-eval 消费正则锚）。"""
        with self.flag, patch("proxy_logging.log") as mock_log:
            msgs = [tu("l1", "Bash", {"command": "pytest"}),
                    tr("l1", "1 failed, 5 passed in 0.12s")]
            tf._translate_tool_result_errors_ex(msgs, session_id="sL", profile="")
        self.assertTrue(mock_log.called)
        line = mock_log.call_args[0][0]
        self.assertIn("test_status=failed=1,passed=5,hist=1", line)

    def test_no_session_no_ledger(self):
        """无会话 id（会话头缺失）不记账——账本按会话分桶是消费前提。"""
        with self.flag:
            msgs = [tu("s1", "Bash", {"command": "pytest"}),
                    tr("s1", "1 failed, 5 passed in 0.12s")]
            tf._translate_tool_result_errors_ex(msgs, session_id="", profile="")
        self.assertEqual(tf._TEST_STATUS, {})


class FnftestStatusParseTest(unittest.TestCase):
    """解析器单测：摘要行形态矩阵。"""

    def test_multiline_anchor_variants(self):
        cases = {
            "1 failed, 2137 passed, 3 xfailed in 12.34s": (1, 2137),
            "======= 2 failed, 10 passed in 5s =======": (2, 10),
            "  2158 passed in 21.13s": (0, 2158),
            "5 passed in 0.30s": (0, 5),
            "1 failed, 2138 passed, 4 skipped, 2 warnings in 3.20s": (1, 2138),
            "no tests ran in 0.05s": None,
            "3 skipped, 1 warning in 0.20s": None,
        }
        for line, want in cases.items():
            got = tf._fnf_parse_pytest_summary(line)
            if want is None:
                self.assertIsNone(got, line)
            else:
                self.assertEqual((got[0], got[1]), want, line)

    def test_history_reader_returns_copy(self):
        tf._TEST_STATUS["sZ"] = [{"failed": 1, "passed": 2, "raw": "x"}]
        h = tf._fnf_test_status_history("sZ")
        h.append({"failed": 9, "passed": 9, "raw": "y"})
        self.assertEqual(len(tf._TEST_STATUS["sZ"]), 1)


if __name__ == "__main__":
    unittest.main()
