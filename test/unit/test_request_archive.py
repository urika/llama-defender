"""Unit tests for request_archive — S-17/BR-4 R 期录制面 (s17-archive/v1.2).

校验分两层:
  1. writer 行为（snapshot/delta 判定、双 view 分片、response/骨架、
     去重、fail-open、配额驱逐的会话级单位 + L-18 活跃保护）;
  2. mirror_replay 校验器不变量复刻（首 snapshot turn=1 / delta turn
     严格 max+1 / response ≤ max_turn）——writer 产物必须能被
     swe-eval scripts/mirror_replay.py 的 fail-loud 校验拒收率 = 0。

v1.2（BR-4 tools 缺口闭合）: snapshot 行可带 tools/tool_choice；工具面
变化强制 snapshot（delta 不携带工具面）；legacy v1 行（无 tools）对消费
端合法——复刻校验接受双 schema 字符串。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import proxy_state as _ps
import request_archive as ra


def _body(user, n_extra=0, tools=None, tool_choice=None):
    msgs = [{"role": "user", "content": user}]
    msgs += [{"role": "user", "content": f"extra-{i}"} for i in range(n_extra)]
    body = {"model": "test-model", "system": "sys", "messages": msgs}
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


_TOOLS_A = [{"name": "bash", "description": "run shell",
             "input_schema": {"type": "object", "properties": {}}}]
_TOOLS_B = [{"name": "bash", "description": "run shell",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "edit", "description": "edit file",
             "input_schema": {"type": "object", "properties": {}}}]


def _read_rows(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _assert_mirror_valid(rows):
    """复刻 mirror_replay.validate_session 的 fail-loud 不变量。

    v1.2 起消费端接受双 schema 字符串（legacy v1 行无 tools=合法）。"""
    ok_schemas = ("s17-archive/v1", "s17-archive/v1.2")
    seen_snapshot = False
    max_turn = 0
    for i, rec in enumerate(rows):
        assert rec.get("schema") in ok_schemas, f"row{i} schema"
        assert rec.get("kind") in ("snapshot", "delta", "response", "meta"), f"row{i} kind"
        assert rec.get("view") in ("R", "R'"), f"row{i} view"
        kind, turn = rec["kind"], rec.get("turn")
        if kind == "meta":
            continue
        if kind == "snapshot":
            if not seen_snapshot:
                assert (turn or 1) == 1, f"row{i}: 首个 snapshot 非 turn=1"
            seen_snapshot = True
            max_turn = max(max_turn, turn or 1)
        elif kind == "delta":
            assert seen_snapshot, f"row{i}: delta 先于 snapshot"
            assert turn == max_turn + 1, f"row{i}: delta turn 断裂(期望 {max_turn+1} got {turn})"
            max_turn = max(max_turn, turn)
        elif kind == "response":
            assert (turn or 0) <= max_turn, f"row{i}: response turn 超界"


class RequestArchiveTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            patch.object(_ps, "PROXY_REQUEST_ARCHIVE_ENABLED", True),
            patch.object(_ps, "PROXY_REQUEST_ARCHIVE_DIR", self._tmp.name),
            patch.object(_ps, "PROXY_REQUEST_ARCHIVE_MAX_MB", 2048),
            patch.object(_ps, "PROXY_DIAG_ACTIVE_PROTECT_MIN", 360, create=True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self._reset_state()

    def tearDown(self):
        self._reset_state()

    def _reset_state(self):
        with ra._pending_lock:
            ra._pending.clear()
            ra._recorded.clear()
            ra._sessions.clear()
        # 本机 monotonic 为进程相对时钟——"到期"须用 now-间隔, 不能置 0
        ra._quota_checked_at = time.monotonic() - ra._QUOTA_CHECK_INTERVAL_S

    def _turn(self, rid, session, r_body, rp_body=None):
        ra.begin_request(rid, r_body)
        ra.record_turn(rid, session, rp_body if rp_body is not None else r_body)

    def _shard(self, session, view):
        name = session + (".R.jsonl" if view == "R" else ".Rp.jsonl")
        p = os.path.join(self._tmp.name, name)
        return p if os.path.exists(p) else None


class TestSnapshotDelta(RequestArchiveTestBase):
    def test_first_turn_snapshot_both_shards(self):
        self._turn("r1", "sessA", _body("hi"))
        for view in ("R", "Rp"):
            rows = _read_rows(self._shard("sessA", view))
            _assert_mirror_valid(rows)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "snapshot")
            self.assertEqual(rows[0]["turn"], 1)
            self.assertEqual(rows[0]["messages"], _body("hi")["messages"])
        rp_rows = _read_rows(self._shard("sessA", "Rp"))
        self.assertEqual(rp_rows[0]["view"], "R'")

    def test_suffix_append_records_delta(self):
        self._turn("r1", "sessA", _body("hi"))
        self._turn("r2", "sessA", _body("hi", n_extra=2))
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        self.assertEqual([r["kind"] for r in rows], ["snapshot", "delta"])
        self.assertEqual(rows[1]["turn"], 2)
        self.assertEqual(len(rows[1]["messages_delta"]), 2)

    def test_prefix_mismatch_falls_back_to_snapshot(self):
        # 客户端改写历史: 丢首条 → 非严格后缀 → snapshot 兜底
        self._turn("r1", "sessA", _body("hi", n_extra=2))
        rewritten = _body("rewritten")
        self._turn("r2", "sessA", rewritten)
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        self.assertEqual(rows[1]["kind"], "snapshot")
        self.assertEqual(rows[1]["messages"], rewritten["messages"])

    def test_identical_view_writes_snapshot_not_empty_delta(self):
        b = _body("hi")
        self._turn("r1", "sessA", b)
        self._turn("r2", "sessA", b)
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        self.assertEqual(rows[1]["kind"], "snapshot")

    def test_views_diverge_independently(self):
        # R 与 R' 视图内容不同(代理改写), 各自独立判定 delta/snapshot
        self._turn("r1", "sessA", _body("raw"), rp_body=_body("rewritten"))
        self._turn("r2", "sessA", _body("raw", n_extra=1),
                   rp_body=_body("rewritten", n_extra=1))
        for view in ("R", "Rp"):
            rows = _read_rows(self._shard("sessA", view))
            _assert_mirror_valid(rows)
            self.assertEqual([r["kind"] for r in rows], ["snapshot", "delta"])


class TestResponseRecords(RequestArchiveTestBase):
    def test_response_row_both_shards(self):
        self._turn("r1", "sessA", _body("hi"))
        ra.record_response("r1", {"stop_reason": "end_turn", "usage": {"output_tokens": 5},
                                  "content": [{"type": "text", "text": "hello"}]})
        for view in ("R", "Rp"):
            rows = _read_rows(self._shard("sessA", view))
            _assert_mirror_valid(rows)
            resp = [r for r in rows if r["kind"] == "response"]
            self.assertEqual(len(resp), 1)
            self.assertEqual(resp[0]["turn"], 1)
            self.assertEqual(resp[0]["response"]["content"][0]["text"], "hello")

    def test_oversize_response_truncated(self):
        self._turn("r1", "sessA", _body("hi"))
        big = {"stop_reason": "end_turn", "usage": {},
               "content": [{"type": "text", "text": "x" * 200000}]}
        ra.record_response("r1", big)
        rows = _read_rows(self._shard("sessA", "R"))
        resp = [r for r in rows if r["kind"] == "response"][0]
        self.assertTrue(resp["response"].get("truncated"))
        self.assertLess(len(json.dumps(resp["response"])), 200000)

    def test_stream_skeleton_on_finalize(self):
        ra.begin_request("r1", _body("hi"), is_stream=True)
        ra.record_turn("r1", "sessA", _body("hi"))
        ra.finalize_request("r1", 200)
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        resp = [r for r in rows if r["kind"] == "response"][0]
        self.assertTrue(resp["response"].get("truncated"))
        self.assertEqual(resp["response"].get("note"), "stream-not-materialized-v1")

    def test_error_finalize_no_response_row(self):
        self._turn("r1", "sessA", _body("hi"))
        ra.finalize_request("r1", 500)
        rows = _read_rows(self._shard("sessA", "R"))
        self.assertFalse([r for r in rows if r["kind"] == "response"])


class TestBuildStreamBody(RequestArchiveTestBase):
    """v1.1: 流式尾部聚合变量 → Anthropic body（挂点 4 的流式形态）."""

    def test_text_only(self):
        body = ra.build_stream_body("m1", "hello 世界", {}, "end_turn", 10, 5)
        self.assertEqual(body["stop_reason"], "end_turn")
        self.assertEqual(body["content"], [{"type": "text", "text": "hello 世界"}])
        self.assertEqual(body["usage"], {"input_tokens": 10, "output_tokens": 5})
        # mirror response_shape 消费口径: text_chars/stop_reason 可提取
        chars = sum(len(b.get("text", "")) for b in body["content"]
                    if b.get("type") == "text")
        self.assertEqual(chars, len("hello 世界"))

    def test_tool_calls_sorted_by_index(self):
        buf = {
            1: {"id": "b", "type": "function",
                "function": {"name": "Write", "arguments": '{"path":"a"}'}},
            0: {"id": "a", "type": "function",
                "function": {"name": "Read", "arguments": '{"f":"x"}'}},
        }
        body = ra.build_stream_body("m1", "plan", buf, "tool_use")
        self.assertEqual([b["name"] for b in body["content"] if b["type"] == "tool_use"],
                         ["Read", "Write"])
        self.assertEqual(body["content"][0]["text"], "plan")
        self.assertEqual(body["content"][1]["input"], {"f": "x"})

    def test_unparsable_args_kept_raw(self):
        buf = {0: {"id": "a", "type": "function",
                   "function": {"name": "Edit", "arguments": '{"cut'}}}  # 截断参数串
        body = ra.build_stream_body("m1", "", buf, "max_tokens")
        blk = body["content"][0]
        self.assertTrue(blk["input"].get("_unparsed"))
        self.assertEqual(blk["input"].get("_raw"), '{"cut')

    def test_empty_stream(self):
        body = ra.build_stream_body("m1", "", None, "end_turn")
        self.assertEqual(body["content"], [])
        self.assertEqual(body["usage"], {"input_tokens": 0, "output_tokens": 0})

    def test_materialized_response_row_replaces_skeleton(self):
        # 流式挂点与 finalize 的接续: 尾部 record_response 落了实体行后,
        # finalize 不再补骨架
        self._turn("r1", "sessA", _body("hi"))
        ra.record_response("r1", ra.build_stream_body(
            "m1", "done", {}, "end_turn", 3, 4))
        ra.finalize_request("r1", 200)
        rows = _read_rows(self._shard("sessA", "R"))
        resp = [r for r in rows if r["kind"] == "response"]
        self.assertEqual(len(resp), 1)
        self.assertNotIn("note", resp[0]["response"])
        self.assertEqual(resp[0]["response"]["content"][0]["text"], "done")


class TestTurnBookkeeping(RequestArchiveTestBase):
    def test_duplicate_record_turn_skipped(self):
        ra.begin_request("r1", _body("hi"))
        ra.record_turn("r1", "sessA", _body("hi"))
        ra.record_turn("r1", "sessA", _body("hi"))  # 内部重派兜底
        rows = _read_rows(self._shard("sessA", "R"))
        self.assertEqual(len(rows), 1)

    def test_unknown_request_skipped_fail_open(self):
        ra.record_turn("ghost", "sessA", _body("hi"))
        self.assertIsNone(self._shard("sessA", "R"))
        ra.record_response("ghost", {"content": []})
        ra.finalize_request("ghost", 200)  # 无档案, 不抛

    def test_disabled_flag_noop(self):
        with patch.object(_ps, "PROXY_REQUEST_ARCHIVE_ENABLED", False):
            self._turn("r1", "sessA", _body("hi"))
            ra.record_response("r1", {"content": []})
            ra.finalize_request("r1", 200)
        self.assertEqual(os.listdir(self._tmp.name), [])

    def test_restart_continuation_scans_and_snapshots(self):
        self._turn("r1", "sessA", _body("hi"))
        self._turn("r2", "sessA", _body("hi", n_extra=1))
        # 模拟重启: 进程内会话态清零, 分片仍在
        self._reset_state()
        self._turn("r3", "sessA", _body("hi", n_extra=2))
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        self.assertEqual([r["turn"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[2]["kind"], "snapshot")  # 续写首行必为 snapshot


class TestToolsRecording(RequestArchiveTestBase):
    """v1.2（BR-4）: tools/tool_choice 入档——tools 缺失致重放 tool_use=0 的闭合。

    mirror 重建口径复刻: payload=基线 snapshot（含工具面）＋依序 delta；
    writer 保证工具面变化必落 snapshot ⇒ delta 轮静默沿用基线工具面=安全。"""

    def test_first_turn_snapshot_carries_tools(self):
        self._turn("r1", "sessA", _body("hi", tools=_TOOLS_A,
                                        tool_choice={"type": "auto"}))
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        self.assertEqual(rows[0]["schema"], "s17-archive/v1.2")
        self.assertEqual(rows[0]["tools"], _TOOLS_A)
        self.assertEqual(rows[0]["tool_choice"], {"type": "auto"})

    def test_no_tools_no_key_no_crash(self):
        # 客户端未带工具面 → snapshot 行无 tools 键（与 legacy 区分靠 schema 串）
        self._turn("r1", "sessA", _body("hi"))
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        self.assertNotIn("tools", rows[0])
        self.assertNotIn("tool_choice", rows[0])

    def test_suffix_append_unchanged_tools_is_delta(self):
        # 工具面不变 → 消息后缀扩展仍落 delta（不重复携带 tools）
        self._turn("r1", "sessA", _body("hi", tools=_TOOLS_A))
        self._turn("r2", "sessA", _body("hi", n_extra=2, tools=_TOOLS_A))
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        self.assertEqual([r["kind"] for r in rows], ["snapshot", "delta"])
        self.assertNotIn("tools", rows[1])
        self.assertEqual(rows[0]["tools"], _TOOLS_A)

    def test_tools_change_forces_snapshot(self):
        # 消息是严格后缀扩展但工具集已变 → 必须 snapshot（delta 会静默沿用旧工具面）
        self._turn("r1", "sessA", _body("hi", tools=_TOOLS_A))
        self._turn("r2", "sessA", _body("hi", n_extra=1, tools=_TOOLS_B))
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        self.assertEqual(rows[1]["kind"], "snapshot")
        self.assertEqual(rows[1]["tools"], _TOOLS_B)

    def test_tools_appear_from_none_forces_snapshot(self):
        # 从无到有同样是工具面变化
        self._turn("r1", "sessA", _body("hi"))
        self._turn("r2", "sessA", _body("hi", n_extra=1, tools=_TOOLS_A))
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        self.assertEqual(rows[1]["kind"], "snapshot")
        self.assertEqual(rows[1]["tools"], _TOOLS_A)

    def test_tool_choice_change_forces_snapshot(self):
        self._turn("r1", "sessA", _body("hi", tools=_TOOLS_A,
                                        tool_choice={"type": "auto"}))
        self._turn("r2", "sessA", _body("hi", n_extra=1, tools=_TOOLS_A,
                                        tool_choice={"type": "any"}))
        rows = _read_rows(self._shard("sessA", "R"))
        _assert_mirror_valid(rows)
        self.assertEqual(rows[1]["kind"], "snapshot")
        self.assertEqual(rows[1]["tool_choice"], {"type": "any"})

    def test_mirror_rebuild_replication_with_tools(self):
        # mirror rebuild 复刻: 基线 snapshot 的工具面 + delta 消息 ⇒ 第 2 轮
        # 重放 payload 含工具定义（BR-4 缺口闭合后的预期形态）
        self._turn("r1", "sessA", _body("hi", tools=_TOOLS_A))
        self._turn("r2", "sessA", _body("hi", n_extra=2, tools=_TOOLS_A))
        rows = _read_rows(self._shard("sessA", "R"))
        snap = next(r for r in rows if r["kind"] == "snapshot")
        payload = {"model": snap.get("model"), "system": snap.get("system"),
                   "messages": list(snap.get("messages", []))}
        if snap.get("tools"):
            payload["tools"] = snap["tools"]
        for r in rows:
            if r["kind"] == "delta":
                payload["messages"] += r["messages_delta"]
        self.assertEqual(len(payload["messages"]), 3)
        self.assertEqual([t["name"] for t in payload["tools"]], ["bash"])
        self.assertEqual(payload["messages"][-1]["content"], "extra-1")

    def test_views_record_tools_independently(self):
        # R=原 body 带 tools；R'=改写 body 无 tools（管线剥除形态）——双视图各自判定
        self._turn("r1", "sessA", _body("hi", tools=_TOOLS_A),
                   rp_body=_body("rewritten"))
        r_rows = _read_rows(self._shard("sessA", "R"))
        rp_rows = _read_rows(self._shard("sessA", "Rp"))
        _assert_mirror_valid(r_rows)
        _assert_mirror_valid(rp_rows)
        self.assertEqual(r_rows[0]["tools"], _TOOLS_A)
        self.assertNotIn("tools", rp_rows[0])


class TestQuotaEviction(RequestArchiveTestBase):
    def _big_shard(self, session, size_mb, age_s):
        p = os.path.join(self._tmp.name, session + ".R.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"schema": "s17-archive/v1", "kind": "snapshot", "view": "R", '
                    '"turn": 1, "pad": "' + "x" * (size_mb * 1024 * 1024) + '"}\n')
        old = time.time() - age_s
        os.utime(p, (old, old))

    def test_eviction_removes_old_session_pair(self):
        self._big_shard("old-sess", 2, age_s=10 * 24 * 3600)
        with patch.object(_ps, "PROXY_REQUEST_ARCHIVE_MAX_MB", 1):
            ra._enforce_quota()
        self.assertIsNone(self._shard("old-sess", "R"))

    def test_active_protect_window_blocks_eviction(self):
        self._big_shard("hot-sess", 2, age_s=60)  # 1h 保护窗内
        with patch.object(_ps, "PROXY_REQUEST_ARCHIVE_MAX_MB", 1):
            ra._enforce_quota()
        self.assertIsNotNone(self._shard("hot-sess", "R"))  # 溢出接受, 不删证据


if __name__ == "__main__":
    unittest.main()
