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
# 快速档 = prod 11 例（无大报文）＋ 旋钮咬合对 5 例（L-38 T-zero C1）
QUICK_PROD_IDS = ["TC01", "TC02", "TC03", "TC04", "TC05", "TC07", "TC14",
                  "TC15", "TC32", "TC34", "TC36"]
QUICK_BITE_IDS = ["TC30", "TC37", "TC38", "TC39", "TC40", "TC41"]
QUICK_IDS = QUICK_PROD_IDS + QUICK_BITE_IDS
# (ON 套件, OFF 套件, 旋钮键, (ON 值, OFF 值))——每对只许差这一个键
BITE_PAIRS = [
    ("autorecall", "autorecalloff", "PROXY_AUTO_RECALL_ENABLED",
     ("true", "false")),
    ("teststate", "teststateoff", "PROXY_TEST_STATE_ENABLED",
     ("true", "false")),
    ("meteron", "meteroff", "PROXY_HONEST_METERING", ("1", "0")),
]


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

    def test_quick_subset_has_no_big_payloads(self):
        sel, _ = ccr.select_cases(CASES, quick=True)
        self.assertEqual(sorted(c["id"] for c in sel), sorted(QUICK_IDS))
        self.assertEqual({c["suite"] for c in sel},
                         {"prod"} | {p[0] for p in BITE_PAIRS}
                         | {p[1] for p in BITE_PAIRS})
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
        self.assertEqual(sorted(c["id"] for c in sel2),
                         sorted(QUICK_PROD_IDS))


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
        # 登记册可为空（当前所有已知红锚均已转绿销账）；仅当有条目时逐条良构校验。
        for cid, kr in entries.items():
            for field in ccr.KNOWN_RED_REQUIRED:
                self.assertTrue(str(kr.get(field) or "").strip(),
                                "%s 的 known-red 缺 %s" % (cid, field))
            self.assertIsNotNone(ccr._parse_date(kr["expires"]),
                                 "%s 的 expires 不可解析" % cid)

    def test_tc32_red_anchor_retired_after_l36(self):
        """L-36 落地后 TC32 应已销账（红锚转绿不变量）；见 roadmap L-36。"""
        self.assertNotIn("known_red", BY_ID["TC32"])

    def test_known_red_registry_has_no_stale_green_entries(self):
        """登记册不得有「已到期」条目——到期即门报错（防长期挂账）。"""
        for cid, kr in {c["id"]: c["known_red"] for c in CASES
                        if c.get("known_red")}.items():
            self.assertGreaterEqual(ccr._parse_date(kr["expires"]),
                                    ccr._parse_date(ccr.today_str()),
                                    "%s known-red 已到期，需转绿或重估" % cid)


class TestSseResponseParsing(unittest.TestCase):
    """流式回程装配（L-80 诚实计量案例的唯一观察面，2026-10-13）。"""

    SSE = (
        'event: message_start\ndata: {"type": "message_start", "message": '
        '{"id": "msg_1", "usage": {"input_tokens": 1000, "output_tokens": 0}}}'
        '\n\n'
        'event: content_block_start\ndata: {"type": "content_block_start", '
        '"index": 0, "content_block": {"type": "text", "text": ""}}\n\n'
        'event: content_block_delta\ndata: {"type": "content_block_delta", '
        '"index": 0, "delta": {"type": "text_delta", "text": "ok"}}\n\n'
        'event: message_delta\ndata: {"type": "message_delta", "delta": '
        '{"stop_reason": "end_turn"}, "usage": {"output_tokens": 20}}\n\n'
        'event: message_stop\ndata: {"type": "message_stop"}\n\n')

    def test_sse_text_is_assembled_into_json_shaped_record(self):
        r = ccr.parse_case_response(self.SSE.encode("utf-8"))
        self.assertTrue(r["__sse__"])
        self.assertEqual(r["usage"]["input_tokens"], 1000)
        self.assertEqual(r["usage"]["output_tokens"], 20)
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertEqual([b["text"] for b in r["content"]], ["ok"])

    def test_json_body_is_still_plain_json(self):
        r = ccr.parse_case_response(b'{"usage": {"input_tokens": 5}}')
        self.assertNotIn("__sse__", r)
        self.assertEqual(r["usage"]["input_tokens"], 5)

    def test_unparseable_body_is_kept_raw(self):
        self.assertIn("__raw__", ccr.parse_case_response(b"<html>oops</html>"))

    def test_usage_assertion_min_max_exact(self):
        resp = {"usage": {"input_tokens": 1000}}
        for spec, want in (({"min": 900}, True), ({"min": 1001}, False),
                           ({"max": 800}, False), ({"max": 1000}, True),
                           ({"exact": 1000}, True), ({"exact": 1000 - 1}, False)):
            out = ccr.eval_expect({"resp_usage_input_tokens": spec}, 200, {},
                                  resp, [], "", None)
            self.assertEqual(out[0][3], want, spec)

    def test_usage_assertion_missing_field_is_false_not_error(self):
        out = ccr.eval_expect({"resp_usage_input_tokens": {"min": 1}}, 200, {},
                              {"content": []}, [], "", None)
        self.assertFalse(out[0][3])


class TestBiteSuites(unittest.TestCase):
    """旋钮咬合对不变式（L-38 T-zero C1；出典＝L-79 EXP-14 旋钮从未咬合）。

    一「只差一个旋钮」的套件对 ＋ 同载荷案例：两例结论相反才证明该旋钮真的
    咬合，否则批跑是构造性 null。本类机械守卫该结构，防止改坏（改动套件/
    案例/断言时先看这里）。
    """

    @staticmethod
    def _payload_normalized(case):
        blob = json.dumps({k: case.get(k) for k in
                           ("body", "bodies", "seed_archive", "seed_manifest")},
                          ensure_ascii=False, sort_keys=True)
        return blob.replace(case["session"], "<session>")

    def test_pair_profiles_differ_in_exactly_the_knob(self):
        for on, off, knob, (on_v, off_v) in BITE_PAIRS:
            self.assertIn(on, ccr.SUITES)
            self.assertIn(off, ccr.SUITES)
            a, b = ccr.SUITES[on], ccr.SUITES[off]
            diff = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
            self.assertEqual(diff, {knob},
                             "%s/%s 只许差 %s" % (on, off, knob))
            self.assertEqual(a[knob], on_v)
            self.assertEqual(b[knob], off_v)
            common = set(a) & set(b) - {knob}  # 旋钮本身由 diff 已覆盖
            self.assertEqual({k: a[k] for k in common},
                             {k: b[k] for k in common},
                             "共有键取值也不得漂移")

    def test_each_bite_suite_has_quick_case_with_discriminating_assertion(self):
        for on, off, _knob, _vals in BITE_PAIRS:
            for suite in (on, off):
                cases = [c for c in CASES if c["suite"] == suite]
                self.assertTrue(cases, "%s 无案例" % suite)
                for c in cases:
                    self.assertTrue(c.get("quick"),
                                    "%s 须进快速档（T-zero 3s 路径）" % c["id"])
                    self.assertTrue(set(c["expect"]) - {"resp_status"},
                                    "%s 只断言 resp_status 即假绿" % c["id"])

    def test_paired_cases_share_payload(self):
        for on, off, _knob, _vals in BITE_PAIRS:
            a = [c for c in CASES if c["suite"] == on]
            b = [c for c in CASES if c["suite"] == off]
            self.assertEqual(len(a), len(b), "%s/%s 案例数须一致" % (on, off))
            for x, y in zip(a, b):
                self.assertNotEqual(x["session"], y["session"])
                self.assertEqual(self._payload_normalized(x),
                                 self._payload_normalized(y),
                                 "%s/%s 必须同载荷（只差旋钮）" % (x["id"], y["id"]))

    def test_metering_pair_rides_streaming_path(self):
        """L-80 只在流式 message_start 生效（非流式直接透传后端 usage）——
        不置 stream 的计量案例必是假绿（2026-10-13 实测发现 2）。"""
        for cid in ("TC40", "TC41"):
            self.assertTrue(BY_ID[cid]["bodies"], "%s 需多轮 bodies" % cid)
            for b in BY_ID[cid]["bodies"]:
                self.assertTrue(b.get("stream"),
                                "%s 的报文须 stream=true" % cid)
            self.assertIn("resp_usage_input_tokens", BY_ID[cid]["expect"])
            self.assertTrue(BY_ID[cid].get("mock_usage"),
                            "%s 需区别于估算器的后端真值" % cid)

    def test_k1_pin_red_anchor_delisted_after_l95(self):
        """TC38 红锚已销账（L-95 落地转绿，2026-10-16 按门指示移出登记册）。

        原红锚断言（known_red.reason 含 TestStateStage/pipeline.py）随销账退役；
        本测改断言销账出处注记在案——若 TestStateStage 接线回退，TC38 将以
        普通用例身份真实失败（响亮红），known_red 豁免通道不再兜底。
        """
        kr = BY_ID["TC38"].get("known_red_delisted")
        self.assertIsNotNone(kr, "TC38 销账出处注记须在案")
        self.assertIn("L-95", kr)


if __name__ == "__main__":
    unittest.main()
