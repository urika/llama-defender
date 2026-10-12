#!/usr/bin/env python3
"""test_ctx_case_runner_gate.py — ctx-case 门语义测试（L-38② A1-A3，2026-10-13）。

覆盖：
  A1 exit 码契约（0 通过 / 1 意外失败 / 2 门自身错误）
  A2 known-red 四态（有效 xfail / 到期未转绿 / 登记册缺字段或日期不可解析 /
     已转绿未销账）＋真实案例集登记册良构
  A3 --quick 快速子集（单套件、无大报文）

纯逻辑测试：import 编排器但不启影子代理、不触网、不写盘。
"""
import json
import os
import sys
import unittest

_TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import ctx_case_runner as ccr  # noqa: E402

CASES = json.load(open(ccr.CASES_PATH, encoding="utf-8"))
BY_ID = {c["id"]: c for c in CASES}
TODAY = "2026-10-13"
QUICK_IDS = ["TC01", "TC02", "TC03", "TC04", "TC05", "TC07", "TC14",
             "TC15", "TC32", "TC34", "TC36"]


def _kr(expires="2026-12-31", reason="r", ref="ref"):
    return {"reason": reason, "ref": ref, "expires": expires}


def _gate(results, cases_by_id=None, today=TODAY):
    return ccr.evaluate_gate(results, cases_by_id or {}, today=today)


class TestSelectCases(unittest.TestCase):
    def test_default_skips_real_backend_cases(self):
        sel, sk = ccr.select_cases(CASES)
        self.assertEqual(len(sel), len(CASES) - 2)
        self.assertEqual({c["id"] for c, _ in sk}, {"TC24", "TC29"})

    def test_real_backend_keeps_probe_cases(self):
        sel, sk = ccr.select_cases(CASES, real_backend=True)
        self.assertEqual(len(sel), len(CASES))
        self.assertEqual(sk, [])

    def test_quick_subset_is_single_prod_suite_without_big_payloads(self):
        sel, _ = ccr.select_cases(CASES, quick=True)
        self.assertEqual(sorted(c["id"] for c in sel), sorted(QUICK_IDS))
        self.assertEqual({c["suite"] for c in sel}, {"prod"})
        # 大报文例（TC13 500KB 413 / TC28a huge）不得进快速档
        self.assertNotIn("TC13", [c["id"] for c in sel])

    def test_case_and_suite_filters(self):
        sel, sk = ccr.select_cases(CASES, want_case=["TC01", "TC24"])
        self.assertEqual([c["id"] for c in sel], ["TC01"])
        self.assertEqual([c["id"] for c, _ in sk], ["TC24"])
        sel2, _ = ccr.select_cases(CASES, want_suite=["micro"])
        self.assertTrue(sel2)
        self.assertTrue(all(c["suite"] == "micro" for c in sel2))

    def test_quick_combined_with_suite_filter(self):
        sel, _ = ccr.select_cases(CASES, quick=True, want_suite=["epoch"])
        self.assertEqual(sel, [])
        sel2, _ = ccr.select_cases(CASES, quick=True, want_suite=["prod"])
        self.assertEqual(sorted(c["id"] for c in sel2), sorted(QUICK_IDS))


class TestGateExitCodes(unittest.TestCase):
    def test_all_pass_is_exit_0(self):
        g = _gate([{"id": "TC01", "ok": True}, {"id": "TC02", "ok": True}],
                  {"TC01": BY_ID["TC01"], "TC02": BY_ID["TC02"]})
        self.assertEqual(g["exit_code"], 0)
        self.assertEqual(g["verdict"], "PASS")
        self.assertEqual(g["counts"]["pass"], 2)
        self.assertEqual(g["gate_errors"], [])

    def test_unexpected_failure_is_exit_1(self):
        g = _gate([{"id": "TC01", "ok": False}], {"TC01": BY_ID["TC01"]})
        self.assertEqual(g["exit_code"], 1)
        self.assertEqual(g["verdict"], "FAIL")
        self.assertEqual(g["counts"]["fail"], 1)
        self.assertEqual(g["gate_errors"], [])
        self.assertIn("未登记 known-red", g["failures"][0])

    def test_skip_is_counted_but_not_failure(self):
        g = _gate([{"id": "TC24", "skipped": True, "reason": "需 --real-backend"},
                   {"id": "TC01", "ok": True}], BY_ID)
        self.assertEqual(g["exit_code"], 0)
        self.assertEqual(g["counts"]["skip"], 1)
        self.assertEqual(g["cases"][0]["status"], "skip")

    def test_empty_result_set_is_pass(self):
        g = _gate([], {})
        self.assertEqual(g["exit_code"], 0)
        self.assertEqual(g["counts"]["pass"], 0)


class TestKnownRedRegistry(unittest.TestCase):
    def test_valid_known_red_failure_is_xfail_exit_0(self):
        cases = {"TC32": {"id": "TC32", "known_red": _kr()}}
        g = _gate([{"id": "TC32", "ok": False}], cases)
        self.assertEqual(g["exit_code"], 0)
        self.assertEqual(g["counts"]["xfail"], 1)
        self.assertEqual(g["counts"]["fail"], 0)
        self.assertEqual(g["cases"][0]["status"], "xfail")

    def test_expired_known_red_is_gate_error(self):
        cases = {"TC32": {"id": "TC32", "known_red": _kr(expires="2026-10-12")}}
        g = _gate([{"id": "TC32", "ok": False}], cases)
        self.assertEqual(g["exit_code"], 2)
        self.assertEqual(g["verdict"], "GATE_ERROR")
        self.assertEqual(g["counts"]["expired"], 1)
        self.assertIn("到期仍未转绿", g["gate_errors"][0])

    def test_expiry_is_inclusive_of_expiry_day(self):
        cases = {"TC32": {"id": "TC32", "known_red": _kr(expires=TODAY)}}
        g = _gate([{"id": "TC32", "ok": False}], cases)
        self.assertEqual(g["exit_code"], 0)
        self.assertEqual(g["counts"]["xfail"], 1)

    def test_xpass_is_gate_error_so_registry_gets_cleaned(self):
        cases = {"TC32": {"id": "TC32", "known_red": _kr()}}
        g = _gate([{"id": "TC32", "ok": True}], cases)
        self.assertEqual(g["exit_code"], 2)
        self.assertEqual(g["counts"]["xpass"], 1)
        self.assertEqual(g["counts"]["pass"], 0)
        self.assertIn("请销账", g["gate_errors"][0])

    def test_missing_field_is_gate_error(self):
        cases = {"TC32": {"id": "TC32", "known_red": _kr(reason="")}}
        g = _gate([{"id": "TC32", "ok": False}], cases)
        self.assertEqual(g["exit_code"], 2)
        self.assertEqual(g["counts"]["invalid"], 1)
        self.assertIn("缺字段", g["gate_errors"][0])

    def test_unparseable_expiry_is_gate_error(self):
        cases = {"TC32": {"id": "TC32", "known_red": _kr(expires="12/31/2026")}}
        g = _gate([{"id": "TC32", "ok": False}], cases)
        self.assertEqual(g["exit_code"], 2)
        self.assertEqual(g["counts"]["invalid"], 1)
        self.assertIn("不是 YYYY-MM-DD", g["gate_errors"][0])

    def test_gate_error_takes_precedence_over_plain_failure(self):
        cases = {"TC32": {"id": "TC32", "known_red": _kr(expires="2020-01-01")}}
        g = _gate([{"id": "TC32", "ok": False}, {"id": "TC01", "ok": False}],
                  dict(cases, TC01=BY_ID["TC01"]))
        self.assertEqual(g["exit_code"], 2)
        self.assertEqual(g["counts"]["fail"], 1)
        self.assertEqual(g["counts"]["expired"], 1)


class TestRealCaseSetRegistry(unittest.TestCase):
    def test_every_known_red_entry_is_wellformed(self):
        entries = {c["id"]: c["known_red"] for c in CASES if c.get("known_red")}
        self.assertTrue(entries, "案例集里应至少有一条 known-red 登记")
        for cid, kr in entries.items():
            for field in ccr.KNOWN_RED_REQUIRED:
                self.assertTrue(str(kr.get(field) or "").strip(),
                                "%s 的 known-red 缺 %s" % (cid, field))
            self.assertIsNotNone(ccr._parse_date(kr["expires"]),
                                 "%s 的 expires 不可解析" % cid)

    def test_tc32_red_anchor_is_the_registered_known_red(self):
        self.assertIn("known_red", BY_ID["TC32"])
        self.assertIn("L-36", BY_ID["TC32"]["known_red"]["ref"])

    def test_known_red_registry_has_no_stale_green_entries(self):
        """登记册不得有「已到期」条目——到期即门报错（防长期挂账）。"""
        for cid, kr in {c["id"]: c["known_red"] for c in CASES
                        if c.get("known_red")}.items():
            self.assertGreaterEqual(ccr._parse_date(kr["expires"]),
                                    ccr._parse_date(ccr.today_str()),
                                    "%s known-red 已到期，需转绿或重估" % cid)


if __name__ == "__main__":
    unittest.main()
