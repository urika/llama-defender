"""Unit tests for tools/context_query.py (context unit reference queries)."""
import json
import os
import sys
import tempfile
import unittest

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import context_query as cq  # noqa: E402


def _w(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


_COMPRESS_SPAN = {
    "trace_id": "tr_1", "span_id": "sp_compress_1", "name": "content_compressor",
    "kind": "internal", "status": "ok", "duration_ms": 8.7,
    "context": {
        "before_units": 3, "after_units": 3, "compressed_units": 1,
        "dropped_units": 0, "pair_integrity": True,
        "changed_units": [{"anchor": "r:call_1", "kind": "tool_result",
                           "action": "compressed",
                           "before_chars": 12480, "after_chars": 2484}],
    },
}
_TRUNC_SPAN = {
    "trace_id": "tr_1", "span_id": "sp_trunc_1", "name": "context_truncator",
    "kind": "internal", "status": "ok", "duration_ms": 2.1,
    "context": {
        "before_units": 10, "after_units": 6, "compressed_units": 0,
        "dropped_units": 4, "pair_integrity": True,
        "changed_units": [{"anchor": "h:aa", "kind": "text", "action": "dropped",
                           "before_chars": 500, "after_chars": 0}],
    },
}


class ContextQueryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="context_query_")
        self.logs = os.path.join(self.tmp, "logs")
        # metrics: request r1 携带 spans + context
        _w(os.path.join(self.logs, "proxy_metrics.jsonl"), [
            {"request_id": "r1", "session_id": "sessA", "trace_id": "tr_1",
             "status": 200, "compression_ratio": 0.2,
             "spans": [dict(_COMPRESS_SPAN), dict(_TRUNC_SPAN)]},
        ])
        # diag per-turn: turn1=r1, turn2=r2(无压缩), 均含 trace
        _w(os.path.join(self.logs, "diag", "sessions.jsonl"), [
            {"ts": "2026-09-03T10:00:01", "request_id": "r1",
             "session_key": "sessA", "turn": 1, "hit_ratio": 0.9,
             "feedback_injected": [], "ifc": {"retention": 0.6, "ile_kinds": ["compress_drop"]},
             "trace": {"trace_id": "tr_1", "spans": [dict(_COMPRESS_SPAN)]}},
            {"ts": "2026-09-03T10:00:05", "request_id": "r2",
             "session_key": "sessA", "turn": 2, "hit_ratio": None,
             "feedback_injected": [], "ifc": {"retention": 1.0, "ile_kinds": []},
             "trace": {"trace_id": "tr_2", "spans": []}},
        ])
        # manifest: turn1 一条 compressed, 一条 fifo_drop
        _w(os.path.join(self.logs, "diag", "manifest", "sessA.jsonl"), [
            {"turn": 1, "reason": "compressed", "anchor": "r:call_1",
             "kind": "tool_result", "tool": "Read",
             "handle": {"type": "path", "value": "/a.py"},
             "size_chars": 12480, "head": "..."},
            {"turn": 1, "reason": "fifo_drop", "anchor": "h:aa",
             "kind": "text", "tool": "", "handle": None,
             "size_chars": 500, "head": "..."},
        ])

    def test_query_request(self):
        q = cq.query_request("r1", self.logs)
        self.assertEqual(q["request_id"], "r1")
        self.assertEqual(q["session_key"], "sessA")
        self.assertEqual(q["turn"], 1)
        ctx = q["context"]
        self.assertEqual(ctx["compressed_units"], 1)
        self.assertEqual(ctx["dropped_units"], 4)
        self.assertEqual(ctx["saved_chars"], 12480 - 2484)
        self.assertEqual(ctx["dropped_chars"], 500)
        self.assertEqual(q["manifest_count"], 2)
        self.assertEqual(q["manifest_reasons"], {"compressed": 1, "fifo_drop": 1})
        self.assertEqual(q["manifest_sample"][0]["tool"], "Read")

    def test_query_request_unknown(self):
        q = cq.query_request("nope", self.logs)
        self.assertEqual(q["manifest_count"], 0)
        self.assertEqual(q["context"]["compressed_units"], 0)

    def test_query_session_per_turn(self):
        q = cq.query_session("sessA", self.logs)
        self.assertEqual(q["turns"], 2)
        self.assertEqual(q["per_turn"][0]["context"]["compressed_units"], 1)
        self.assertEqual(q["per_turn"][0]["manifest_reasons"]["fifo_drop"], 1)
        # turn2 无变化
        self.assertEqual(q["per_turn"][1]["context"]["compressed_units"], 0)
        self.assertEqual(q["per_turn"][1]["context"]["dropped_units"], 0)

    def test_query_session_turn_filter(self):
        q = cq.query_session("sessA", self.logs, turn=2)
        self.assertEqual(q["turns"], 1)
        self.assertEqual(q["per_turn"][0]["turn"], 2)

    def test_query_span(self):
        q = cq.query_span("sp_compress_1", self.logs)
        self.assertEqual(q["found_in"], "metrics")
        self.assertEqual(q["session_key"], "sessA")
        self.assertEqual(q["span"]["name"], "content_compressor")
        self.assertEqual(q["span"]["context"]["changed_units"][0]["anchor"], "r:call_1")
        missing = cq.query_span("sp_nope", self.logs)
        self.assertIsNone(missing["found_in"])


if __name__ == "__main__":
    unittest.main()
