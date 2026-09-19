#!/usr/bin/env python3
"""test_lcp_trace_analyzer.py — LCP 遥测分析器测试（0920-12 双格式兼容加固）。

覆盖：本地补丁 0.12.12 口径 / 上游 0.14.3 两种命中口径（LCP + supersequence）/
失配两变体（non_trimmable / unrewind）/ trivial 分型 / 检查点粒度损失指标 /
归档行过滤。字段语义以 0.14.3 wheel 源码为准（tokens_saved += position →
复用量 = resumed_at，shared 是 LCP 上限）。
"""
import os
import sys
import tempfile
import unittest

_TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import lcp_trace_analyzer as lta  # noqa: E402


class TestParseLines(unittest.TestCase):
    def test_local_patch_format(self):
        """0.12.12 本地补丁口径：B=复用位置，saved_pct 按请求总长(B+remaining)。"""
        out = lta.parse_lines([
            "[cache_fetch] LCP snapdown: B=8192 reused=8192 remaining=1808 "
            "(entry_len=10000 lcp=9000)\n"])
        self.assertEqual(out["snap_hits"], 1)
        self.assertEqual(out["snap_hits_v143"], 0)
        self.assertEqual(out["snap_reused"], [8192])
        self.assertEqual(out["snap_saved_pct"], [81.9])

    def test_v143_lcp_semantics(self):
        """0.14.3 口径：复用量 = resumed_at（不是 shared）；gap = shared − resumed_at。"""
        out = lta.parse_lines([
            "[cache_fetch] LCP snapped to checkpoint: shared=9000 entry_len=10000 "
            "resumed_at=8192 remaining=1808\n"])
        self.assertEqual(out["snap_hits"], 1)
        self.assertEqual(out["snap_hits_v143"], 1)
        self.assertEqual(out["snap_reused"], [8192])       # 非 9000
        self.assertEqual(out["snap_saved_pct"], [81.9])    # 8192/10000
        self.assertEqual(out["checkpoint_gap"], [808])

    def test_v143_supersequence_path(self):
        """0.14.3 新增 supersequence 命中路径（requested/entry_len/resumed_at）。"""
        out = lta.parse_lines([
            "[cache_fetch] supersequence snapped to checkpoint: requested=5000 "
            "entry_len=8000 resumed_at=4096\n"])
        self.assertEqual(out["snap_hits"], 1)
        self.assertEqual(out["snap_hits_supersequence"], 1)
        self.assertEqual(out["snap_reused"], [4096])
        self.assertEqual(out["snap_remaining"], [904])
        self.assertEqual(out["snap_saved_pct"], [81.9])    # 4096/5000

    def test_miss_both_variants(self):
        """失配两变体都要计入（原实现只认 non_trimmable 变体）。"""
        out = lta.parse_lines([
            "[cache_fetch] LCP unavailable: shared=14017 entry_len=14094 "
            "requested_len=15060 non_trimmable=True\n",
            "[cache_fetch] LCP unavailable: shared=9000 entry_len=10000 "
            "cache could not rewind exactly by 1000 tokens\n",
            "[cache_fetch] LCP unavailable: shared=100 entry_len=200 "
            "requested_len=300 non_trimmable=True\n",   # trivial 小条目
        ])
        self.assertEqual(out["miss_total"], 3)
        self.assertEqual(out["miss_meaningful"], 2)
        self.assertEqual(out["miss_trivial"], 1)
        variants = {s["variant"] for s in out["miss_meaningful_samples"]}
        self.assertEqual(variants, {"non_trimmable", "unrewind"})
        first = out["miss_meaningful_samples"][0]
        self.assertEqual(first["requested_len"], 15060)
        self.assertNotIn("requested_len", out["miss_meaningful_samples"][1])

    def test_boundary_line(self):
        out = lta.parse_lines([
            "[lcp_boundaries] candidates=16 boundaries=[13848] total=14263 msgs=18\n"])
        self.assertEqual(out["boundary_events"], 1)
        self.assertEqual(out["boundary_candidates"], 16)
        self.assertEqual(out["boundary_total_tokens"], 14263)


class TestSummarize(unittest.TestCase):
    def test_mixed_report_keys_and_rates(self):
        lines = [
            "[cache_fetch] LCP snapdown: B=8192 reused=8192 remaining=1808 "
            "(entry_len=10000 lcp=9000)\n",
            "[cache_fetch] LCP snapped to checkpoint: shared=9000 entry_len=10000 "
            "resumed_at=8192 remaining=1808\n",
            "[cache_fetch] supersequence snapped to checkpoint: requested=5000 "
            "entry_len=8000 resumed_at=4096\n",
            "[cache_fetch] LCP unavailable: shared=14017 entry_len=14094 "
            "requested_len=15060 non_trimmable=True\n",
        ]
        rep = lta.summarize(lta.parse_lines(lines))
        self.assertEqual(rep["snap_hits"], 3)
        self.assertEqual(rep["misses"], 1)
        self.assertEqual(rep["hit_rate_pct"], 75.0)
        self.assertEqual(rep["snap_hits_v143_upstream"], 1)
        self.assertEqual(rep["snap_hits_supersequence"], 1)
        self.assertEqual(rep["checkpoint_gap_p50"], 808)
        # 接口稳定性：既有键保留
        for key in ("lcp_events", "boundary_events", "nontrivial_hit_rate_pct",
                    "reused_tokens_total", "saved_pct_p50", "miss_meaningful_samples"):
            self.assertIn(key, rep)


class TestArchiveFilter(unittest.TestCase):
    def test_supersequence_lines_archived(self):
        """归档过滤须包含 supersequence 命中行（原 filter 会漏）。"""
        content = (
            "noise line\n"
            "[cache_fetch] LCP snapped to checkpoint: shared=1 entry_len=2 resumed_at=1 remaining=1\n"
            "[cache_fetch] supersequence snapped to checkpoint: requested=5 entry_len=8 resumed_at=4\n"
            "[lcp_boundaries] candidates=1 boundaries=[4] total=10 msgs=2\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            matched, raw = lta.read_matched_lines(path)
            self.assertEqual(len(raw), 4)
            self.assertEqual(len(matched), 3)
            self.assertTrue(any("supersequence snapped" in ln for ln in matched))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
