#!/usr/bin/env python3
"""jev_quota.py 与 jev_mcp_server 额度面单测。

覆盖（对应 docs/06-reference-metrics/jev-quota.md 的落地验收）：
  1. 窗口解析：used/cap/百分比/重置时刻/剩余时长；判定 ok｜near｜exceeded；
  2. 退出码分级：0/3/4/5，且 **5（不可探测）与 4（超额）严格区分**；
  3. 降级：404/401/超时 → 「不可探测」而非崩溃或被误读为额度耗尽；
  4. 密钥纪律：任何输出/错误串都不含密钥；
  5. MCP 面：tools/list 暴露两个工具；`jev_usage` 返回结构化额度；429 错误体
     白名单提取（只取 message、截断、脱敏），不放行响应体其余部分。
"""
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools import jev_quota as jq       # noqa: E402
from tools import jev_mcp_server as jev  # noqa: E402

KEY = "secret-value-abc"
CREDITS = {
    "credits": {"belowThreshold": False, "creditThreshold": 0,
                "monthlyCredits": 55.18, "purchasedCredits": 0, "freeCredits": 0},
    "windowLimits": {"limited": True, "exceeded": None,
                     "fiveHour": {"used": 0.0034, "cap": 16, "exceeded": False,
                                  "resetAt": 1790502607690},
                     "weekly": {"used": 24.8156, "cap": 40, "exceeded": False,
                                "resetAt": 1791041816259}},
}


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _opener(body):
    def open_(req, timeout=None):
        return _Resp(body)
    return open_


class TestWindowParsing(unittest.TestCase):
    def _snap(self, body=None, **kw):
        return jq.fetch_usage(KEY, opener=_opener(body or CREDITS), **kw)

    def test_fields_and_percentages(self):
        snap = self._snap()
        fh, wk = snap["windows"]["fiveHour"], snap["windows"]["weekly"]
        self.assertEqual((fh["cap"], wk["cap"]), (16, 40))
        self.assertEqual(fh["pct"], 0.0)
        self.assertEqual(wk["pct"], 62.0)
        self.assertIn("reset_at", fh)
        self.assertIsInstance(fh["resets_in_s"], float)
        self.assertEqual(snap["credits"]["remaining_monthly"], 55.18)

    def test_verdict_ok_near_exceeded(self):
        near = json.loads(json.dumps(CREDITS))
        near["windowLimits"]["weekly"]["used"] = 33.0          # 82.5%
        self.assertEqual(self._snap(near)["verdict"], "near")
        over = json.loads(json.dumps(CREDITS))
        over["windowLimits"]["fiveHour"]["exceeded"] = True
        self.assertEqual(self._snap(over)["verdict"], "exceeded")
        self.assertEqual(self._snap()["verdict"], "ok")

    def test_exit_codes_distinguish_undetectable_from_exceeded(self):
        self.assertEqual(jq.exit_code_for("ok"), 0)
        self.assertEqual(jq.exit_code_for("near"), 3)
        self.assertEqual(jq.exit_code_for("exceeded"), 4)
        self.assertEqual(jq.exit_code_for("undetectable"), 5)
        self.assertNotEqual(jq.exit_code_for("undetectable"),
                            jq.exit_code_for("exceeded"))

    def test_plan_block_is_optional_and_failure_tolerant(self):
        self.assertNotIn("plan", self._snap())
        def bad(req, timeout=None):
            if jq.SUBSCRIPTIONS_PATH in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))
            return _Resp(CREDITS)
        snap = jq.fetch_usage(KEY, opener=bad, with_plan=True)
        self.assertEqual(snap["windows"]["weekly"]["cap"], 40)   # 计划失败不影响额度
        self.assertIn("error", snap["plan"])


class TestDegradation(unittest.TestCase):
    def _run(self, exc):
        def open_(req, timeout=None):
            raise exc
        with mock.patch.object(jq, "load_key", return_value=KEY), \
                mock.patch.object(jq.urllib.request, "urlopen", open_), \
                mock.patch("sys.stdout", new=io.StringIO()) as out:
            code = jq.main(["--json"])
            return code, out.getvalue()

    def test_http_errors_are_undetectable_not_exhausted(self):
        for status in (401, 404, 429):
            exc = urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(b"{}"))
            code, text = self._run(exc)
            self.assertEqual(code, jq.EXIT_UNDETECTABLE)
            payload = json.loads(text)
            self.assertEqual(payload["verdict"], "undetectable")
            self.assertNotIn("exhausted", payload["verdict"])

    def test_network_error_is_undetectable(self):
        code, text = self._run(urllib.error.URLError("boom"))
        self.assertEqual(code, jq.EXIT_UNDETECTABLE)
        self.assertEqual(json.loads(text)["verdict"], "undetectable")

    def test_missing_key_is_undetectable(self):
        with mock.patch.object(jq, "load_key", return_value=""), \
                mock.patch("sys.stdout", new=io.StringIO()) as out:
            code = jq.main([])
        self.assertEqual(code, jq.EXIT_UNDETECTABLE)
        self.assertIn("CMD_API_KEY", out.getvalue())


class TestKeyDiscipline(unittest.TestCase):
    def test_key_never_appears_in_output_or_errors(self):
        def leak(req, timeout=None):
            raise urllib.error.HTTPError("u", 500, "err " + KEY, {}, io.BytesIO(b""))
        with mock.patch.object(jq, "load_key", return_value=KEY), \
                mock.patch.object(jq.urllib.request, "urlopen", leak), \
                mock.patch("sys.stdout", new=io.StringIO()) as out:
            jq.main(["--json"])
        self.assertNotIn(KEY, out.getvalue())

    def test_load_key_prefers_env_then_zshrc(self):
        self.assertEqual(jq.load_key({"CMD_API_KEY": "k1"}), "k1")
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "zshrc")
            with open(p, "w", encoding="utf-8") as f:
                f.write("export OTHER=1\nexport CMD_API_KEY='k2'\n")
            with mock.patch.object(jq.os.path, "expanduser", return_value=p):
                self.assertEqual(jq.load_key({}), "k2")


class TestMcpQuotaFace(unittest.TestCase):
    def test_tools_list_exposes_both_tools(self):
        listed = jev._handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([t["name"] for t in listed["result"]["tools"]],
                         [jev.TOOL_NAME, jev.QUOTA_TOOL_NAME])

    def test_jev_usage_returns_structured_snapshot(self):
        snap = {"verdict": "ok", "windows": {"fiveHour": {"used": 0.0, "cap": 16}}}
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": jev.QUOTA_TOOL_NAME, "arguments": {}}}
        with mock.patch.object(jev, "_call_usage", return_value=snap) as call:
            resp = jev._handle_message(msg, KEY)
        call.assert_called_once_with(KEY)
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(resp["result"]["structuredContent"], snap)

    def test_unknown_tool_still_rejected(self):
        resp = jev._handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "nope"}}, KEY)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_http_error_detail_extracts_only_message(self):
        body = json.dumps({"error": {"message": "You've reached your 5-hour usage limit. "
                                                "Your limit resets at 3:00 PM.",
                                     "type": "rate_limit_error",
                                     "code": "rate_limit_error",
                                     "secret_field": KEY}}).encode()
        exc = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(body))
        detail = jev._http_error_detail(exc, KEY)
        self.assertIn("resets at 3:00 PM", detail)
        self.assertNotIn("secret_field", detail)     # 白名单：只取 message
        self.assertNotIn(KEY, detail)                # 脱敏：密钥不回显

    def test_http_error_detail_is_robust_to_garbage_and_long_bodies(self):
        exc = urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b"<html>nope</html>"))
        self.assertEqual(jev._http_error_detail(exc, KEY), "")
        huge = json.dumps({"message": "A" * 5000}).encode()
        exc2 = urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(huge))
        self.assertLessEqual(len(jev._http_error_detail(exc2, KEY)),
                             jev.MAX_ERROR_MESSAGE_CHARS)

    def test_call_jev_surfaces_window_message_on_429(self):
        body = json.dumps({"error": {"message": "5-hour usage limit reached; resets at 3:00 PM"}}).encode()
        exc = urllib.error.HTTPError(jev.API_URL, 429, "Too Many Requests", {}, io.BytesIO(body))
        with mock.patch.object(jev.urllib.request, "urlopen", side_effect=exc):
            with self.assertRaisesRegex(RuntimeError, "resets at 3:00 PM"):
                jev._call_jev({"state": "x", "questions": {
                    "q": {"type": "noul", "instructions": "?"}}}, KEY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
