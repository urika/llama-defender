#!/usr/bin/env python3
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools import jev_mcp_server as jev


class _Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class TestJevRequestValidation(unittest.TestCase):
    def test_builds_official_payload(self):
        args = {
            "state": {"failed_payments": 3},
            "questions": {
                "urgent": {"type": "noul", "instructions": "Is this urgent?"},
                "team": {"type": "choice", "instructions": "Which team?",
                         "criteria": {"billing": "Payments", "support": "Other"}},
                "severity": {"type": "score", "instructions": "Rate severity.",
                             "criteria": ["low", "medium", "high"]},
            },
        }
        contract, send_q, body = jev._validate_request(args)
        payload = json.loads(body)
        self.assertEqual(contract, {})
        self.assertEqual(payload["model"], "typesafe/jev")
        self.assertEqual(payload["state"], args["state"])
        self.assertEqual(payload["questions"], args["questions"])

    def test_rejects_invalid_shapes_and_question_types(self):
        invalid = [
            None,
            {"state": "x", "questions": {}},
            {"state": "x", "questions": {"q": {"type": "text", "instructions": "x"}}},
            {"state": "x", "questions": {"q": {"type": "noul"}}},
            {"state": "x", "questions": {"q": {"type": "choice", "instructions": "x",
                                                       "criteria": ["not", "map"]}}},
            {"state": "x", "questions": {"q": {"type": "score", "instructions": "x",
                                                       "criteria": ["only one"]}}},
            {"state": "x", "questions": {"q": {"type": "noul", "instructions": "x",
                                                       "unexpected": True}}},
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                jev._validate_request(args)

    def test_enforces_question_count_and_request_size(self):
        with self.assertRaises(ValueError):
            jev._validate_request({"state": "x", "questions": {
                str(i): {"type": "noul", "instructions": "?"}
                for i in range(jev.MAX_QUESTIONS + 1)
            }})
        with patch.object(jev, "MAX_REQUEST_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                jev._validate_request({"state": "x" * 200,
                                      "questions": {"q": {"type": "noul",
                                                           "instructions": "?"}}})


class TestJevApiCall(unittest.TestCase):
    def setUp(self):
        self.args = {"state": "Payments failed", "questions": {
            "urgent": {"type": "noul", "instructions": "Is it urgent?"}}}

    def test_sends_bearer_key_and_returns_json(self):
        body = json.dumps({"model": "typesafe/jev", "answers": {"urgent": {
            "type": "noul", "noul": 0.95}}, "usage": {"input_tokens": 8}}).encode()
        with patch.object(jev.urllib.request, "urlopen", return_value=_Response(body)) as open_url:
            result = jev._call_jev(self.args, "secret-value")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, jev.API_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-value")
        self.assertEqual(json.loads(request.data)["model"], "typesafe/jev")
        self.assertEqual(result["answers"]["urgent"]["noul"], 0.95)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 60)

    def test_missing_key_does_not_send_request(self):
        with patch.object(jev.urllib.request, "urlopen") as open_url:
            with self.assertRaisesRegex(RuntimeError, "CMD_API_KEY is not set"):
                jev._call_jev(self.args, "")
        open_url.assert_not_called()

    def test_http_error_does_not_expose_response_or_key(self):
        error = urllib.error.HTTPError(jev.API_URL, 401, "Unauthorized", {}, io.BytesIO(b"secret-value"))
        with patch.object(jev.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as raised:
                jev._call_jev(self.args, "secret-value")
        self.assertNotIn("secret-value", str(raised.exception))

    def test_rejects_unexpected_api_response(self):
        with patch.object(jev.urllib.request, "urlopen",
                          return_value=_Response(b'{"model":"other","answers":{}}')):
            with self.assertRaisesRegex(RuntimeError, "expected decision schema"):
                jev._call_jev(self.args, "secret-value")


class TestEvidenceContract(unittest.TestCase):
    """v1.1 证据契约：requires_evidence 声明 + 缺证契约弃权 + 溯源。"""

    def test_evidence_present_semantics(self):
        state = {"base_commit_api": {"ConfigManager": "(conf_file, defs_file)"},
                 "fix_diff": "remove py3compat shim"}
        self.assertTrue(jev._evidence_present(state, "base_commit_api"))
        self.assertTrue(jev._evidence_present(state, "base_commit_api.ConfigManager"))
        self.assertTrue(jev._evidence_present(state, "fix_diff"))
        self.assertTrue(jev._evidence_present(state, "remove py3compat"))
        self.assertFalse(jev._evidence_present(state, "api_surface"))
        self.assertTrue(jev._evidence_present("text mentioning fix_diff", "fix_diff"))
        self.assertTrue(jev._evidence_present([{"a": 1}, "see fix_diff"], "fix_diff"))
        self.assertFalse(jev._evidence_present({"a": 1}, "fix_diff"))

    def test_requires_evidence_shape_validation(self):
        bad = [[], [""], [123], "fix_diff", {"fix_diff": 1}]
        for declared in bad:
            args = {"state": "x", "questions": {"q": {
                "type": "noul", "instructions": "?", "requires_evidence": declared}}}
            with self.subTest(declared=declared), self.assertRaises(ValueError):
                jev._validate_request(args)

    def test_missing_evidence_bypasses_http(self):
        args = {"state": {"test_source": "..." ,"fix_diff": "d"},
                "questions": {"api_ok": {"type": "noul", "instructions": "?",
                                         "requires_evidence": ["base_commit_api"]}}}
        # base_commit_api 不在 state → 契约弃权；绝不能发 HTTP（无证据不作答）
        with patch.object(jev.urllib.request, "urlopen") as open_url:
            result = jev._call_jev(args, "secret-value")
        open_url.assert_not_called()
        ans = result["answers"]["api_ok"]
        self.assertEqual(ans["answer_class"], "missing_evidence")
        self.assertEqual(ans["missing_evidence"], ["base_commit_api"])
        self.assertEqual(ans["source"], "contract")
        self.assertEqual(result["provenance"]["model"], "typesafe/jev")
        self.assertIn("state_sha256", result["provenance"])

    def test_partial_missing_strips_declared_key_and_annotates(self):
        args = {"state": {"base_commit_api": {"ConfigManager": "..."}},
                "questions": {
                    "grounded": {"type": "noul", "instructions": "?",
                                 "requires_evidence": ["base_commit_api"]},
                    "plain": {"type": "noul", "instructions": "?"}}}
        body = json.dumps({"model": "typesafe/jev", "answers": {
            "grounded": {"type": "noul", "noul": 0.9},
            "plain": {"type": "noul", "noul": 0.9}}}).encode()
        with patch.object(jev.urllib.request, "urlopen",
                          return_value=_Response(body)) as open_url:
            result = jev._call_jev(args, "secret-value")
        sent = json.loads(open_url.call_args.args[0].data)
        # 官方 API 拒收未知键：requires_evidence 必须剥离后才外发
        self.assertNotIn("requires_evidence", sent["questions"]["grounded"])
        self.assertEqual(result["answers"]["grounded"]["answer_class"], "answer")
        self.assertEqual(result["answers"]["plain"]["answer_class"], "answer")
        self.assertIn("provenance", result)


class TestJevMcpProtocol(unittest.TestCase):
    def test_initialize_and_tool_discovery(self):
        initialized = jev._handle_message({"jsonrpc": "2.0", "id": 1,
            "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}, "")
        self.assertEqual(initialized["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", initialized["result"]["capabilities"])
        listed = jev._handle_message({"jsonrpc": "2.0", "id": 2,
                                     "method": "tools/list"}, "")
        # 2026-09-27：新增额度工具 jev_usage（只读元数据）后为两个工具。
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]],
                         [jev.TOOL_NAME, jev.QUOTA_TOOL_NAME])
        self.assertEqual(listed["result"]["tools"][0]["inputSchema"], jev.TOOL["inputSchema"])

    def test_tool_call_returns_structured_result(self):
        api_result = {"model": "typesafe/jev", "answers": {"urgent": {
            "type": "noul", "noul": 0.9}}}
        message = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": jev.TOOL_NAME,
                              "arguments": {"state": "x", "questions": {
                                  "urgent": {"type": "noul", "instructions": "Urgent?"}}}}}
        with patch.object(jev, "_call_jev", return_value=api_result) as call:
            response = jev._handle_message(message, "secret-value")
        call.assert_called_once_with(message["params"]["arguments"], "secret-value")
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], api_result)
        self.assertEqual(json.loads(result["content"][0]["text"]), api_result)

    def test_tool_errors_are_mcp_tool_errors_and_key_is_not_echoed(self):
        message = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                   "params": {"name": jev.TOOL_NAME, "arguments": {}}}
        with patch.object(jev, "_call_jev", side_effect=RuntimeError("HTTP 401")):
            response = jev._handle_message(message, "secret-value")
        self.assertTrue(response["result"]["isError"])
        self.assertNotIn("secret-value", json.dumps(response))
        self.assertIn("HTTP 401", response["result"]["content"][0]["text"])

    def test_notifications_have_no_response_and_unknown_methods_fail(self):
        self.assertIsNone(jev._handle_message({"jsonrpc": "2.0",
                                               "method": "notifications/initialized"}, ""))
        response = jev._handle_message({"jsonrpc": "2.0", "id": 5, "method": "unknown"}, "")
        self.assertEqual(response["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main(verbosity=2)
