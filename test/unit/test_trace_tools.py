"""Unit tests for Phase B trace tools (trace_common / trace_query / trace_replay).

B1/B2 契约（logging-trajectory-improvement-design-20260820.md）:
跨流 join 键 request_id / session_key / turn;ledger 重放与 R14 端点同口径;
archive 兜底解析 truncated 轮跳过;diff 统计口径与 admin percentile 一致。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import trace_common as tc  # noqa: E402
import trace_query as tq  # noqa: E402
import trace_replay as tr  # noqa: E402


def _w(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class _FixtureBase(unittest.TestCase):
    """合成五流 fixture: 会话 sessA 3 轮(1 次失败) + 会话 sessB 对照。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trace_tools_")
        self.logs = os.path.join(self.tmp, "logs")
        self.store = tc.TraceStore(self.logs)

        # requests.jsonl(A1 schema: 含 session_id/request_id)
        _w(os.path.join(self.logs, "proxy_requests.jsonl"), [
            {"start_time": "2026-08-20T10:00:00", "end_time": "2026-08-20T10:00:05",
             "model": "claude-sonnet-4-6", "status": 200, "duration_ms": 5000,
             "input_chars": 1000, "output_chars": 50,
             "session_id": "sessA", "request_id": "r1"},
            {"start_time": "2026-08-20T10:01:00", "end_time": "2026-08-20T10:01:40",
             "model": "claude-sonnet-4-6", "status": 500, "duration_ms": 40000,
             "input_chars": 2000, "output_chars": 0,
             "session_id": "sessA", "request_id": "r2"},
            {"start_time": "2026-08-20T11:00:00", "end_time": "2026-08-20T11:00:03",
             "model": "claude-haiku-4-5", "status": 200, "duration_ms": 3000,
             "input_chars": 100, "output_chars": 10,
             "session_id": "sessB", "request_id": "r3"},
        ])
        # proxy_metrics.jsonl
        _w(os.path.join(self.logs, "proxy_metrics.jsonl"), [
            {"session_id": "sessA", "request_id": "r1", "status": 200,
             "duration_ms": 5000, "ttft_ms": 300.0, "error_type": None,
             "pipeline": {"backend_dispatcher": {"elapsed_ms": 4800}}},
            {"session_id": "sessA", "request_id": "r2", "status": 500,
             "duration_ms": 40000, "error_type": "BrokenPipeError",
             "error": "Connection reset", "pipeline": {}},
        ])
        # diag/sessions.jsonl(R16) —— 含 R9.1 ifc 段
        _w(os.path.join(self.logs, "diag", "sessions.jsonl"), [
            {"ts": "2026-08-20T10:00:05", "request_id": "r1", "session_key": "sessA",
             "turn": 1, "ttft_ms": 300.0, "duration_ms": 5000.0, "hit_ratio": 0.99,
             "feedback_injected": [], "route_target": "local",
             "actual_model": "m-a", "canonical_mismatch": False,
             "ifc": {"ile": False, "ile_kinds": [], "retention": 1.0,
                     "rationale_ratio": 1.0, "reread_pressure": 0,
                     "action_div": None, "manifest_lines": 0}},
            {"ts": "2026-08-20T10:01:40", "request_id": "r2", "session_key": "sessA",
             "turn": 2, "ttft_ms": None, "duration_ms": 40000.0, "hit_ratio": None,
             "feedback_injected": ["loop_l1"], "route_target": "local",
             "actual_model": "m-a", "canonical_mismatch": False,
             "ifc": {"ile": True, "ile_kinds": ["unit_drop"], "retention": 0.55,
                     "rationale_ratio": 0.4, "reread_pressure": 3,
                     "action_div": 0.12, "manifest_lines": 6}},
            {"ts": "2026-08-20T10:02:10", "request_id": "r2b", "session_key": "sessA",
             "turn": 3, "ttft_ms": 200.0, "duration_ms": 2000.0, "hit_ratio": 0.5,
             "feedback_injected": ["loop_l1", "reread_hard"], "route_target": "cloud",
             "actual_model": "deepseek-v4-flash", "canonical_mismatch": True,
             "ifc": {"ile": True, "ile_kinds": ["compress_drop"], "retention": 0.8,
                     "rationale_ratio": 0.9, "reread_pressure": 4,
                     "action_div": 0.05, "manifest_lines": 9}},
            {"ts": "2026-08-20T10:03:00", "request_id": "r4", "session_key": "sessA",
             "turn": 4, "ttft_ms": 100.0, "duration_ms": 1000.0, "hit_ratio": 0.9,
             "feedback_injected": [], "route_target": "local",
             "actual_model": "m-a", "canonical_mismatch": False,
             "ifc": {"ile": False, "ile_kinds": [], "retention": 1.0,
                     "rationale_ratio": 1.0, "reread_pressure": 0,
                     "action_div": None, "manifest_lines": 9}},
        ])
        # diag/hbe.jsonl(R9.2 影子探针: turn1/2 ok, turn3 skipped)
        _w(os.path.join(self.logs, "diag", "hbe.jsonl"), [
            {"event": "hbe_shadow", "session_key": "sessA", "turn": 1,
             "result": "ok", "h_mean_bits": 2.0, "coverage_mean": 0.97},
            {"event": "hbe_shadow", "session_key": "sessA", "turn": 2,
             "result": "ok", "h_mean_bits": 5.5, "coverage_mean": 0.95},
            {"event": "hbe_shadow", "session_key": "sessA", "turn": 3,
             "result": "skipped_lock"},
            # 伪迹: 工具调用型回答(H 极低但非信念清晰)——join 必须排除
            {"event": "hbe_shadow", "session_key": "sessA", "turn": 4,
             "result": "ok", "h_mean_bits": 0.055, "coverage_mean": 0.99,
             "answer_preview": "<tool_call>\n<function=Read>"},
        ])
        # diag/ledger/sessA.jsonl(A3 增量: turn1 一个动作, turn2 回填+同目标重复, turn3 mismatch 重建)
        _w(os.path.join(self.logs, "diag", "ledger", "sessA.jsonl"), [
            {"ts": "2026-08-20T10:00:05", "turn": 1, "mismatch": False, "msg_count": 1,
             "key_source": "header", "dropped": 0,
             "actions": [{"turn": 1, "tool": "WebSearch", "target": "q a",
                          "target_hash": "h1", "result_chars": None,
                          "handle": None, "_tid": "t1"}],
             "filled": [], "materials": []},
            {"ts": "2026-08-20T10:01:40", "turn": 2, "mismatch": False, "msg_count": 2,
             "key_source": "header", "dropped": 0,
             "actions": [{"turn": 2, "tool": "WebSearch", "target": "q a",
                          "target_hash": "h1", "result_chars": None,
                          "handle": None, "_tid": "t2"}],
             "filled": [{"_tid": "t1", "result_chars": 2048}],
             "materials": [{"path": "/out/a.md", "turn": 2, "via": "bash"}]},
            {"ts": "2026-08-20T10:02:10", "turn": 3, "mismatch": True, "msg_count": 3,
             "key_source": "header", "dropped": 0,
             "actions": [{"turn": 3, "tool": "Read", "target": "/new.py",
                          "target_hash": "h3", "result_chars": 100,
                          "handle": None, "_tid": "t3"}],
             "filled": [], "materials": []},
        ])
        # diag/archive/sessA.jsonl(sent_view; turn2 payload 含 tool_use, turn1 截断)
        _w(os.path.join(self.logs, "diag", "archive", "sessA.jsonl"), [
            {"ts": "2026-08-20T10:00:05", "turn": 1, "view": "sent", "injections": [],
             "chars": 1000, "payload_truncated": True, "model": "m-a",
             "route_target": "local", "messages": 2, "payload": "{\"max_tokens\":1"},
            {"ts": "2026-08-20T10:01:40", "turn": 2, "view": "sent",
             "injections": ["loop_l1"], "chars": 2000, "payload_truncated": False,
             "model": "m-a", "route_target": "local", "messages": 4,
             "payload": json.dumps({"messages": [
                 {"role": "assistant", "content": [
                     {"type": "tool_use", "id": "t1", "name": "WebSearch",
                      "input": {"query": "q a"}}]}]})},
            {"ts": "2026-08-20T10:02:10", "turn": 3, "view": "sent",
             "injections": ["loop_l1", "reread_hard"], "chars": 3000,
             "payload_truncated": False, "model": "m-a", "route_target": "cloud",
             "messages": 5,
             "payload": json.dumps({"messages": [
                 {"role": "assistant", "content": [
                     {"type": "tool_use", "id": "t3", "name": "Read",
                      "input": {"file_path": "/new.py"}}]}]})},
        ])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestTraceCommon(_FixtureBase):
    def test_iter_jsonl_skips_bad_lines(self):
        p = os.path.join(self.tmp, "bad.jsonl")
        with open(p, "w") as f:
            f.write('{"a":1}\nnot-json\n\n{"b":2}\n')
        self.assertEqual(list(tc.iter_jsonl(p)), [{"a": 1}, {"b": 2}])

    def test_replay_ledger_actions_dup_derivation(self):
        deltas = self.store.ledger_deltas("sessA")
        actions, materials, mismatch, dropped, dups = tc.replay_ledger_actions(deltas)
        # mismatch 全量重建: 只剩 turn3 的 Read;dropped=0
        self.assertEqual([(a["tool"], a["turn"]) for a in actions], [("Read", 3)])
        self.assertEqual(mismatch, 1)
        self.assertEqual(dropped, 0)
        # materials 在 mismatch 行被清空后未再新增 → 空
        self.assertEqual(materials, [])
        self.assertEqual(dups, [])

    def test_replay_ledger_actions_no_mismatch(self):
        deltas = [d for d in self.store.ledger_deltas("sessA") if not d.get("mismatch")]
        actions, materials, mismatch, dropped, dups = tc.replay_ledger_actions(deltas)
        self.assertEqual(len(actions), 2)
        self.assertEqual(mismatch, 0)
        # t1 跨轮回填生效
        by_tid = {a.get("turn"): a for a in actions}
        self.assertEqual(by_tid[1]["result_chars"], 2048)
        # 同 (tool,target_hash) → dup 2/2
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["count"], 2)
        self.assertEqual(actions[0]["dup"], 1)
        self.assertEqual(actions[1]["dup"], 2)
        self.assertEqual(len(materials), 1)

    def test_sessions_overview_union(self):
        ov = self.store.sessions_overview()
        self.assertIn("sessA", ov)
        self.assertIn("ledger-file", ov["sessA"]["sources"])
        self.assertIn("archive-file", ov["sessA"]["sources"])
        self.assertIn("diag", ov["sessA"]["sources"])

    def test_sanitize_key(self):
        self.assertEqual(tc.sanitize_key("../etc/passwd"), "___etc_passwd")
        self.assertEqual(tc.sanitize_key(""), "_anon")


class TestTraceQuery(_FixtureBase):
    def test_join_turn_rows(self):
        rows = tq._join_turn_rows(self.store, "sessA")
        self.assertEqual(len(rows), 4)
        r2 = rows[1]
        self.assertEqual(r2["request_id"], "r2")
        self.assertEqual(r2["status"], 500)             # metrics join
        self.assertEqual(r2["error_type"], "BrokenPipeError")
        self.assertEqual(r2["feedback_injected"], ["loop_l1"])
        self.assertEqual(r2["ledger_actions"], 1)        # ledger join
        self.assertTrue(rows[2]["canonical_mismatch"])

    def test_cmd_failures_filters(self):
        out = io.StringIO()
        args = Namespace(status=None, since=None, session="sessA", limit=50, json=True)
        with redirect_stdout(out):
            rows = tq.cmd_failures(self.store, args)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], "r2")
        self.assertEqual(rows[0]["error_type"], "BrokenPipeError")
        # --status 200 语义: 200 被排除在失败之外 → 空
        args2 = Namespace(status=["200"], since=None, session=None, limit=50, json=True)
        with redirect_stdout(io.StringIO()):
            rows2 = tq.cmd_failures(self.store, args2)
        self.assertEqual(rows2, [])

    def test_cmd_request_detail(self):
        out = io.StringIO()
        args = Namespace(request_id="r2", json=True)
        with redirect_stdout(out):
            detail = tq.cmd_request(self.store, args)
        self.assertEqual(detail["request"]["status"], 500)
        self.assertEqual(detail["metrics"]["error_type"], "BrokenPipeError")
        self.assertEqual(detail["diag"]["turn"], 2)
        # ledger_delta 按 (session, turn) 对齐: turn2 = WebSearch 重复动作
        self.assertEqual(detail["ledger_delta"]["actions"][0]["tool"], "WebSearch")

    def test_cmd_last_session_filter(self):
        with redirect_stdout(io.StringIO()):
            args = Namespace(hours=0, session="sessB", limit=10, json=True)
            rows = tq.cmd_last(self.store, args)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], "r3")

    def test_cli_json_smoke(self):
        out = io.StringIO()
        with redirect_stdout(out):
            tq.main(["--logs-dir", self.logs, "--json", "show", "sessA"])
        rows = json.loads(out.getvalue())
        self.assertEqual(len(rows), 4)


class TestIfcDimension(_FixtureBase):
    """R9.1/R9.2 信息面 join 与 Phase 2 效度脚手架。"""

    def test_join_carries_ifc_and_hbe(self):
        rows = tq._join_turn_rows(self.store, "sessA")
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["h_be"], 2.0)          # ok 探针 join
        self.assertEqual(rows[1]["h_be"], 5.5)
        self.assertIsNone(rows[2]["h_be"])              # skipped_lock 不 join
        self.assertIsNone(rows[3]["h_be"])              # <tool_call> 伪迹被过滤        self.assertEqual(rows[1]["ifc_kinds"], ["unit_drop"])
        self.assertEqual(rows[1]["retention"], 0.55)
        self.assertEqual(rows[2]["manifest_lines"], 9)
        self.assertTrue(rows[1]["ifc_ile"])

    def test_hbe_by_session_only_indexes_ok(self):
        idx = self.store.hbe_by_session()
        self.assertEqual(len(idx.get("sessA") or []), 4)  # 全量行(按 turn 升序)
        ok = [r for r in idx["sessA"] if r.get("result") == "ok"]
        self.assertEqual([r["turn"] for r in ok], [1, 2, 4])

    def test_spearman_basics(self):
        self.assertEqual(tc.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertEqual(tc.spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)
        self.assertIsNone(tc.spearman([1, 2], [2, 1]))   # 样本不足
        self.assertIsNone(tc.spearman([1, 1, 1], [1, 2, 3]))  # 零方差
        # ties 平均秩: [1,1,2,3] vs [1,2,3,4] → r ≈ 0.9487
        r = tc.spearman([1, 1, 2, 3], [1, 2, 3, 4])
        self.assertIsNotNone(r)
        self.assertLess(abs(r - 0.9487), 0.001)

    def test_validity_summary_pure_function(self):
        rows = [
            {"h_be": 1.0, "retention": 1.0, "rationale_ratio": 1.0,
             "ifc_ile": False, "feedback_injected": [], "status": 200},
            {"h_be": 2.0, "retention": 0.8, "rationale_ratio": 0.9,
             "ifc_ile": True, "feedback_injected": [], "status": 200},
            {"h_be": 3.0, "retention": 0.6, "rationale_ratio": 0.7,
             "ifc_ile": True, "feedback_injected": ["loop_l1"], "status": 200},
            {"h_be": 4.0, "retention": 0.4, "rationale_ratio": 0.5,
             "ifc_ile": False, "feedback_injected": [], "status": 200},
        ]
        s = tq._ifc_validity_summary(rows)
        self.assertEqual(s["turns"], 4)
        self.assertEqual(s["hbe_samples"], 4)
        self.assertEqual(s["spearman_hbe_vs_loss"], 1.0)   # 完全单调
        self.assertEqual(s["failure_rate_after_ile"], 0.5)  # 2 个 ile 后继, 1 失败
        self.assertEqual(s["failure_rate_baseline"], 0.25)  # 仅 turn3(loop_l1)
        self.assertEqual(s["ile_turns"], 2)

    def test_cmd_ifc_json_and_table(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = tq.cmd_ifc(self.store, Namespace(session="sessA", limit=5, json=True))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["summary"]["ile_turns"], 2)
        self.assertEqual(out[0]["summary"]["hbe_samples"], 2)
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            self.assertIsNone(tq.cmd_ifc(
                self.store, Namespace(session="sessA", limit=5, json=False)))
        text = buf2.getvalue()
        self.assertIn("IFC 信息面", text)
        self.assertIn("效度", text)
        self.assertIn("5.5", text)


class TestTraceReplay(_FixtureBase):



    def test_build_timeline_join(self):
        rows = tr.build_timeline(self.store, "sessA")
        self.assertEqual(len(rows), 3)
        t2 = rows[1]
        self.assertEqual(t2["turn"], 2)
        self.assertEqual(t2["injections"], ["loop_l1"])
        self.assertEqual(t2["ttft_ms"], None)           # diag join: r2 无 ttft
        self.assertEqual(t2["ledger_actions"], 1)
        self.assertTrue(rows[0]["payload_truncated"])
        self.assertEqual(rows[2]["route_target"], "cloud")

    def test_build_actions_ledger_priority(self):
        actions, meta, dups = tr.build_actions(self.store, "sessA")
        self.assertEqual(meta["source"], "ledger")
        self.assertEqual(len(actions), 1)               # mismatch 重建后只剩 Read
        self.assertEqual(actions[0]["source"], "ledger")

    def test_build_actions_archive_fallback(self):
        # 无 ledger 的会话: 从 archive payload 提取(截断轮跳过)
        os.remove(os.path.join(self.logs, "diag", "ledger", "sessA.jsonl"))
        self.store._ledger_by_session = {}
        actions, meta, dups = tr.build_actions(self.store, "sessA")
        self.assertEqual(meta["source"], "archive-fallback")
        self.assertEqual(meta["truncated_turns_skipped"], 1)   # turn1 截断
        tools = [(a["tool"], a["target"]) for a in actions]
        self.assertIn(("WebSearch", "q a"), tools)             # turn2 payload
        self.assertIn(("Read", "/new.py"), tools)              # turn3 payload

    def test_stats_block_percentiles(self):
        rows = tr.build_timeline(self.store, "sessA")
        stats = tr._stats_block(rows)
        self.assertEqual(stats["turns"], 3)
        self.assertEqual(stats["injection_histogram"], {"loop_l1": 2, "reread_hard": 1})
        self.assertEqual(stats["truncated_payload_turns"], 1)
        # durations 2000/5000/40000 → p50=5000
        self.assertEqual(stats["duration_p50_ms"], 5000)

    def test_diff_command(self):
        out = io.StringIO()
        args = Namespace(key_a="sessA", key_b="sessA", turns_a=2, turns_b=1,
                         from_turn_a=None, from_turn_b=None, json=True)
        with redirect_stdout(out):
            result = tr.cmd_diff(self.store, args)
        a, b = result["a"], result["b"]
        self.assertEqual(a["timeline"]["turns"], 2)     # --turns-a 2
        self.assertEqual(b["timeline"]["turns"], 1)     # --turns-b 1

    def test_html_output(self):
        out_path = os.path.join(self.tmp, "replay.html")
        args = Namespace(session_key="sessA", output=out_path)
        with redirect_stdout(io.StringIO()):
            tr.cmd_html(self.store, args)
        with open(out_path, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("sessA", html)
        self.assertIn("loop_l1", html)
        # ledger 优先 + mismatch 全量重建 → 只剩 turn3 的 Read(设计口径)
        self.assertIn("Read", html)
        self.assertIn("/new.py", html)
        self.assertIn("时间线", html)


if __name__ == "__main__":
    unittest.main()
