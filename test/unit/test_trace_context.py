import unittest

import trace_context


class TestTraceContext(unittest.TestCase):
    def tearDown(self):
        trace_context.clear()

    def test_parse_traceparent(self):
        value = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
        self.assertEqual(trace_context.parse_traceparent(value),
                         "tr_0123456789abcdef0123456789abcdef")
        self.assertIsNone(trace_context.parse_traceparent("invalid"))

    def test_request_and_nested_spans(self):
        state = trace_context.begin("req_1")
        root = state["root_span_id"]
        span = trace_context.start_span("compression")
        record = trace_context.finish_span(span, attributes={"saved_chars": 42})
        trace = trace_context.finish_request()
        self.assertTrue(trace["trace_id"].startswith("tr_"))
        self.assertEqual(trace["request_id"], "req_1")
        self.assertEqual(trace["span_count"], 1)
        self.assertEqual(trace["spans"][0]["parent_span_id"], root)
        self.assertEqual(record["attributes"]["saved_chars"], 42)

    def test_traceparent_is_honored(self):
        trace_context.begin(
            "req_2",
            traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        )
        self.assertEqual(
            trace_context.current()["trace_id"],
            "tr_0123456789abcdef0123456789abcdef",
        )

    def test_finish_span_persists_context(self):
        trace_context.begin("req_3")
        span = trace_context.start_span("content_compressor")
        trace_context.finish_span(
            span,
            context={"compressed_units": 1, "changed_units": [{"anchor": "r:x"}]},
        )
        summary = trace_context.finish_request()
        self.assertEqual(summary["spans"][0]["context"]["compressed_units"], 1)
        self.assertEqual(summary["spans"][0]["context"]["changed_units"][0]["anchor"], "r:x")


class TestContextDelta(unittest.TestCase):
    def _unit(self, anchor, kind, size):
        return {anchor: {"anchor": anchor, "kind": kind, "size_chars": size}}

    def test_compressed_unit_detected(self):
        before = self._unit("r:call_1", "tool_result", 12000)
        after = self._unit("r:call_1", "tool_result", 1800)
        delta = trace_context.context_delta(before, after)
        self.assertEqual(delta["compressed_units"], 1)
        self.assertEqual(delta["dropped_units"], 0)
        self.assertEqual(delta["changed_units"][0]["action"], "compressed")
        self.assertEqual(delta["changed_units"][0]["before_chars"], 12000)
        self.assertTrue(delta["pair_integrity"])

    def test_atomic_pair_drop_keeps_integrity(self):
        before = {**self._unit("u:call_1", "tool_use", 60),
                  **self._unit("r:call_1", "tool_result", 5000)}
        delta = trace_context.context_delta(before, {})
        self.assertEqual(delta["dropped_units"], 2)
        self.assertEqual(delta["pair_dropped"], 1)
        self.assertTrue(delta["pair_integrity"])

    def test_one_sided_drop_breaks_integrity(self):
        before = {**self._unit("u:call_1", "tool_use", 60),
                  **self._unit("r:call_1", "tool_result", 5000)}
        after = self._unit("r:call_1", "tool_result", 5000)  # use survives alone
        delta = trace_context.context_delta(before, after)
        self.assertEqual(delta["dropped_units"], 1)
        self.assertFalse(delta["pair_integrity"])

    def test_changed_units_capped(self):
        before = {}
        after = {}
        for i in range(30):
            before[f"h:msg{i}"] = {"anchor": f"h:msg{i}", "kind": "text",
                                   "size_chars": 1000}
        delta = trace_context.context_delta(before, after, limit=20)
        self.assertEqual(delta["changed_total"], 30)
        self.assertEqual(len(delta["changed_units"]), 20)
        self.assertTrue(delta["changed_truncated"])


if __name__ == "__main__":
    unittest.main()
