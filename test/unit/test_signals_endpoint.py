#!/usr/bin/env python3
"""R17 signals 端点单元测试：build_session_signals 聚合口径（契约 v1，集成契约 §3.3）。

覆盖：全字段映射 / 空记录 fail-open / H_BE 趋势与错误探针跳过 / 关联键回填。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import diagnostics  # noqa: E402
import signal_types  # noqa: E402


def _ifc_record(turn=7, retention=0.62, action_div=0.41, reread=2,
                ile=True, kinds=("reread",), conf="abc123"):
    return {
        "session_key": "sess0001",
        "turn": turn,
        "ts": "2026-09-02T10:00:00",
        "conf_hash": conf,
        "ifc": {
            "retention": retention,
            "rationale_ratio": 0.33,
            "reread_pressure": reread,
            "action_div": action_div,
            "ile": ile,
            "ile_kinds": list(kinds),
            "view_reset": False,
        },
    }


def _hbe(turn, h, result="ok"):
    rec = {"session_key": "sess0001", "turn": turn, "result": result}
    if result == "ok":
        rec["h_mean_bits"] = h
    return rec


class TestBuildSessionSignals(unittest.TestCase):
    """契约字段映射与 fail-open 语义。"""

    def test_full_mapping_from_last_record(self):
        records = [_ifc_record(turn=6, retention=0.9), _ifc_record(turn=7, retention=0.62)]
        hbe = [_hbe(5, 1.20), _hbe(6, 0.95)]
        snap = diagnostics.build_session_signals("sess0001", records, hbe)
        self.assertIsInstance(snap, signal_types.SignalSnapshot)
        self.assertEqual(snap["contract_version"], 1)
        self.assertEqual(snap["retention"], 0.62)          # 取末轮，不聚合
        self.assertEqual(snap["rationale_ratio"], 0.33)
        self.assertEqual(snap["action_diversity"], 0.41)
        self.assertEqual(snap["reread_pressure"], 2)
        self.assertTrue(snap["ile"])
        self.assertEqual(snap["ile_kinds"], ["reread"])
        self.assertFalse(snap["view_reset"])
        self.assertEqual(snap["h_be"], 0.95)               # 末条成功探针
        self.assertEqual(snap["h_be_trend"], -0.25)        # 0.95 - 1.20，负=熵降
        self.assertIsNone(snap["d_ledger"])                # 探针轮才有，口径诚实
        self.assertEqual(snap["cognitive_load"], 0.0)      # 契约默认（预留位）
        self.assertEqual(snap["config_fingerprint"], "abc123")
        self.assertEqual(snap["session_key"], "sess0001")
        self.assertEqual(snap["turn"], 7)

    def test_empty_records_fail_open(self):
        snap = diagnostics.build_session_signals("sess0001", [], [])
        self.assertIsNone(snap["retention"])
        self.assertIsNone(snap["h_be"])
        self.assertIsNone(snap["h_be_trend"])
        self.assertEqual(snap["reread_pressure"], 0)       # 契约默认
        self.assertFalse(snap["ile"])
        self.assertEqual(snap["ile_kinds"], [])
        self.assertEqual(snap["turn"], 0)
        self.assertEqual(snap["session_key"], "sess0001")

    def test_hbe_error_records_skipped(self):
        hbe = [_hbe(5, 1.10), _hbe(6, None, result="error"), _hbe(7, 0.80)]
        snap = diagnostics.build_session_signals("sess0001", [], hbe)
        self.assertEqual(snap["h_be"], 0.80)
        self.assertEqual(snap["h_be_trend"], -0.30)        # 跳过 error，0.80 - 1.10

    def test_single_hbe_no_trend(self):
        snap = diagnostics.build_session_signals("sess0001", [], [_hbe(3, 1.5)])
        self.assertEqual(snap["h_be"], 1.5)
        self.assertIsNone(snap["h_be_trend"])

    def test_ifc_none_fields_survive(self):
        rec = {"session_key": "s", "turn": 1, "ifc": None, "conf_hash": ""}
        snap = diagnostics.build_session_signals("s", [rec], [])
        self.assertIsNone(snap["retention"])
        self.assertIsNone(snap["action_diversity"])
        self.assertEqual(snap["config_fingerprint"], "")
        self.assertFalse(snap["ile"])


if __name__ == "__main__":
    unittest.main()
