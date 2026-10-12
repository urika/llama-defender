#!/usr/bin/env python3
"""Session Analysis「上下文动态质量」区块的账本加载回归（2026-10-12）。

背景（DEF 型实案）：diag 台账增量行里，mismatch 行（客户端历史失配→全量
重建）携带的是**完整**动作列表，重放须先清空再灌（session_ledger.
_load_from_file / tools.trace_common.replay_ledger_actions 同款语义）。
页面加载器曾裸拼接全部行，把重建行里的历史动作重复计数——实测把健康会话
（转录口径 M2 0.26%）的 M2 抬到 72.9%、Top1 同键 ×6（真实 ×2）、三分段
中段虚零，并触发 M2 预警线。

本测试钉住三件事：重建行不重复计数；filled 行回填 result_chars；真实重复
（无重建行）仍被计入 M2——修复不得抹掉真信号。
"""
import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import admin_server
import proxy_state


def _act(turn, tool, th, rc, target):
    return {"turn": turn, "tool": tool, "target": target,
            "target_hash": th, "result_chars": rc, "_tid": th + ":1"}


class TestCtxLedgerReplay(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_dir = os.path.join(self._tmp.name, "logs", "diag", "ledger")
        os.makedirs(self.ledger_dir)
        orig = proxy_state._SCRIPT_DIR
        proxy_state._SCRIPT_DIR = self._tmp.name
        self.addCleanup(setattr, proxy_state, "_SCRIPT_DIR", orig)

    def _write(self, sid, rows):
        with open(os.path.join(self.ledger_dir, sid + ".jsonl"), "w",
                  encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_mismatch_rebuild_not_double_counted(self):
        """重建行（mismatch=True）重放=清空再灌: 历史动作只算一次。"""
        sid = "sess-rebuild"
        first = [_act(3, "Read", "h1", 20000, "a.md"),
                 _act(3, "Read", "h2", 20000, "b.md")]
        self._write(sid, [
            {"turn": 3, "mismatch": False, "actions": first},
            {"turn": 5, "mismatch": True, "actions": first + [
                _act(5, "Bash", "h3", 20000, "ls -la")]},
        ])
        acts = admin_server._load_ctx_actions(sid)
        self.assertEqual([a[2] for a in acts], ["h1", "h2", "h3"])

    def test_m2_not_inflated_by_rebuild_rows(self):
        """实案形态: 同一动作块被 mismatch 行重灌 3 次。

        裸拼接下 12 行里 9 行是副本 → M2 75%、滑窗破 50 预警线；
        正确重放 → n=3、M2=0%。
        """
        sid = "sess-m2"
        block = [_act(3, "Read", "h1", 100000, "a.md"),
                 _act(3, "Read", "h2", 100000, "b.md"),
                 _act(3, "Read", "h3", 100000, "c.md")]
        rows = [{"turn": 3, "mismatch": False, "actions": block}]
        for t in (5, 13, 14):  # 三次客户端失配重建
            rows.append({"turn": t, "mismatch": True, "actions": block})
        self._write(sid, rows)
        m = admin_server._ctx_metrics_series(sid)
        self.assertEqual(m["n_actions"], 3)
        self.assertEqual(m["m2_overall"], 0.0)
        self.assertLess(max(m["m2_win"]), 50.0)
        self.assertEqual(m["top_count"], 1)

    def test_true_duplicates_still_counted(self):
        """无重建行时同 (target_hash, result_chars) 重复仍计入 M2（保真信号）。"""
        sid = "sess-real-dup"
        self._write(sid, [
            {"turn": 2, "mismatch": False, "actions": [
                _act(2, "Bash", "hg", 1000, "git diff"),
                _act(3, "Bash", "hg", 1000, "git diff"),
            ]},
        ])
        m = admin_server._ctx_metrics_series(sid)
        self.assertEqual(m["n_actions"], 2)
        self.assertEqual(m["m2_overall"], 50.0)
        self.assertEqual(m["streak_max"], 2)
        self.assertEqual(m["top_count"], 2)

    def test_filled_rows_backfill_result_chars(self):
        """跨请求到达的 tool_result 经 filled 行回填（result_chars=None→现值）。"""
        sid = "sess-filled"
        self._write(sid, [
            {"turn": 2, "mismatch": False, "actions": [
                {"turn": 2, "tool": "Read", "target": "x.md",
                 "target_hash": "hx", "result_chars": None, "_tid": "tid1"}]},
            {"turn": 3, "mismatch": False, "actions": [],
             "filled": [{"_tid": "tid1", "result_chars": 1234}]},
        ])
        acts = admin_server._load_ctx_actions(sid)
        self.assertEqual(acts[0][3], 1234)

    def test_filled_after_rebuild_still_applied(self):
        """重建行清空后，早期 filled 行不得错配到重建后的同 _tid 动作。"""
        sid = "sess-rebuild-filled"
        self._write(sid, [
            {"turn": 2, "mismatch": False, "actions": [
                _act(2, "Read", "h1", 111, "a.md")]},
            {"turn": 3, "mismatch": False, "actions": [],
             "filled": [{"_tid": "h1:1", "result_chars": 999}]},
            {"turn": 5, "mismatch": True, "actions": [
                _act(5, "Read", "h1", 222, "a.md")]},
        ])
        acts = admin_server._load_ctx_actions(sid)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0][3], 222)  # 重建后的新值（重建时结果已在窗口内）


if __name__ == "__main__":
    unittest.main()
