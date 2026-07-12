"""Unit tests for TS-1: BM25 评分驱动压缩决策.

验收用例 (PRD-litellm-borrow §2 TS-1 + design §8 测试覆盖).
本文件为 TDD stub —— 函数签名 + assert 占位,W3 d1-d5 由工程师实现填充.

设计文档: docs/02-architecture-design/bm25-scoring-design-2026-07-05.md
"""
import inspect
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import content_compressor as cc


# ---------------------------------------------------------------------------
# Feature-detection gates — each test class is skipped until the matching
# symbol/signature is present in content_compressor. W3 d1-d5 progressively
# unlocks tests as engineers implement the API described in the design doc.
# ---------------------------------------------------------------------------

_TS1_HAS_BM25_SCORE = hasattr(cc, "bm25_score_message")
_TS1_HAS_EXTRACT_USER = hasattr(cc, "_extract_last_user_text")
_TS1_HAS_BM25_TOKENIZE = hasattr(cc, "_bm25_tokenize")
_TS1_HAS_UPDATE_IDF = hasattr(cc, "_update_idf")
_TS1_HAS_BM25_IDF = hasattr(cc, "_bm25_idf")

_sig = inspect.signature(cc.compress_tool_result) if hasattr(cc, "compress_tool_result") else None
_TS1_HAS_BM25_KWARGS = (_sig is not None and
                        "bm25_score" in _sig.parameters and
                        "bm25_drop_threshold" in _sig.parameters and
                        "bm25_keep_threshold" in _sig.parameters)


def _user_text(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _user_tool_result(tool_use_id, content="ok"):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]}


def _assistant_text(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# 用例 1-5: bm25_score_message 核心算法 (design §8 1-5; W3 d1)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _TS1_HAS_BM25_SCORE,
                 "TS-1 待 W3 d1-d2 实施 (PRD-litellm-borrow §6.2 W3)")
class TestBM25ScoreCore(unittest.TestCase):
    """bm25_score_message 边界与匹配核心 (design §8 #1-#5)."""

    def setUp(self):
        cc._BM25_IDF_MAP.clear()
        cc._BM25_IDF_DOC_FREQ.clear()
        cc._BM25_IDF_TOTAL_DOCS = 0

    def test_01_empty_message_returns_zero(self):
        """空消息 → bm25_score_message 返回 0.0 (design §6.1 边界)."""
        self.assertEqual(cc.bm25_score_message({}, "hello"), 0.0)

    def test_02_empty_query_returns_zero(self):
        """空 query → bm25_score_message 返回 0.0 (I-5 保底)."""
        msg = _user_text("hello world")
        self.assertEqual(cc.bm25_score_message(msg, ""), 0.0)

    def test_03_exact_match_high_score(self):
        """完全匹配 (query == msg text) → 高分 (design §8 #3)."""
        msg = _user_text("compress tool result")
        score = cc.bm25_score_message(msg, "compress tool result")
        self.assertGreater(score, 5.0,
                           "完全匹配应得高分 (Okapi BM25 经验值 > 5)")

    def test_04_no_overlap_zero_score(self):
        """完全无共同 token → 0 分 (design §8 #4)."""
        msg = _user_text("apple banana cherry")
        score = cc.bm25_score_message(msg, "zebra tiger lion")
        self.assertEqual(score, 0.0)

    def test_05_partial_match_mid_score(self):
        """1/4 token 共享 → 中分 (恰好低于完全匹配但高于无匹配)."""
        msg = _user_text("compress tool result file content")
        full = cc.bm25_score_message(msg, "compress tool result file content")
        partial = cc.bm25_score_message(msg, "compress zebra tiger lion")
        self.assertGreater(partial, 0.0,
                           "部分匹配不应得 0 分 (compress 是 1/4 token)")
        self.assertLess(partial, full,
                        "部分匹配分数应低于完全匹配")


# ---------------------------------------------------------------------------
# 用例 6-9: IDF 维护 (design §8 #6-#9; W3 d2)
# ---------------------------------------------------------------------------

@unittest.skipIf(not (_TS1_HAS_BM25_SCORE and _TS1_HAS_BM25_IDF and _TS1_HAS_UPDATE_IDF),
                 "TS-1 待 W3 d1-d2 实施 (PRD-litellm-borrow §6.2 W3)")
class TestBM25IDF(unittest.TestCase):
    """IDF 维护与跨请求复用 (design §8 #6-#9)."""

    def setUp(self):
        cc._BM25_IDF_MAP.clear()
        cc._BM25_IDF_DOC_FREQ.clear()
        cc._BM25_IDF_TOTAL_DOCS = 0

    def test_06_long_msg_lower_score_than_short_for_same_query(self):
        """长 msg 相同 query 比短 msg 分低 (b 长度归一) (design §4.1 b 参数)."""
        query = "compress"
        short = _user_text("compress")
        long_ = _user_text("compress " + "padding " * 50)
        score_short = cc.bm25_score_message(short, query)
        score_long = cc.bm25_score_message(long_, query)
        self.assertLess(score_long, score_short,
                        "b=0.75 长度归一使长文档得分较低")

    def test_07_high_freq_token_low_idf(self):
        """高频 token IDF 低 (饱和效应) (design §8 #7).
        多次包含同一词后 IDF 收敛,该词贡献降低."""
        common = "frequent_token_in_many"
        rare = "rare_token_only_once"
        cc._update_idf([_user_text(common + " " + common + " " + common)])
        cc._update_idf([_user_text(common + " " + common + " " + common)])
        cc._update_idf([_user_text(common + " " + rare)])
        idf_common = cc._bm25_idf(common)
        idf_rare = cc._bm25_idf(rare)
        self.assertLess(idf_common, idf_rare,
                        "高频 token IDF 应低于低频 token")

    def test_08_low_freq_token_high_idf(self):
        """低频 token IDF 高 (design §8 #8).
        只出现一次的 token IDF 接近 log(N+1) ~ 0.69 (N=2)."""
        cc._update_idf([_user_text("only_once_word_xyz123")])
        idf = cc._bm25_idf("only_once_word_xyz123")
        self.assertGreater(idf, 0.0,
                           "低频 token IDF 应 > 0")

    def test_09_unknown_token_returns_default_idf(self):
        """未登录词 → 默认 IDF (无膨胀) (design §6.1/§8 #9).
        不应抛 KeyError, 应返回 0 或基于前缀展开的近似值."""
        try:
            val = cc._bm25_idf("nonexistent_token_zzz")
            self.assertIsInstance(val, (int, float))
        except KeyError:
            self.fail("_bm25_idf not raise KeyError on unknown token")


# ---------------------------------------------------------------------------
# 用例 10-12: 前缀展开 + 中文 (design §8 #10-#12; W3 d2)
# ---------------------------------------------------------------------------

@unittest.skipIf(not (_TS1_HAS_BM25_SCORE and _TS1_HAS_BM25_TOKENIZE),
                 "TS-1 待 W3 d2 实施 (PRD-litellm-borrow §6.2 W3)")
class TestBM25TokenizeI18n(unittest.TestCase):
    """前缀展开与中文分词 (design §8 #10-#12)."""

    def test_10_prefix_expansion_matches_compressing_vs_compressed(self):
        """前缀展开: 'compressing' 与 'compressed' 通过 4-char 前缀 'comp' 共享 (design §4.4)."""
        msg = _user_text("compressing the file")
        score = cc.bm25_score_message(msg, "compressed output")
        # 前缀展开应捕捉 comp 共享;无展开则 0 分
        self.assertGreater(score, 0.0,
                           "前缀展开应让 'compressing'↔'compressed' 有非零匹配")

    def test_11_chinese_single_char_tokenization(self):
        """中文单字切分: '压缩算法' query 匹配 '压缩 techniques' (design §4.4 中文)."""
        msg = _user_text("压缩技术在本地推理中")
        score = cc.bm25_score_message(msg, "压缩算法")
        self.assertGreater(score, 0.0,
                           "中文单字切分应让共享 '压缩' 两字产生非零分")

    def test_12_mixed_chinese_english_query(self):
        """中英混合: 'BM25 压缩' 匹配 'compression 压缩' (design §8 #12)."""
        msg = _user_text("compression 压缩 设计文档")
        score = cc.bm25_score_message(msg, "BM25 压缩")
        self.assertGreater(score, 0.0,
                           "中英混合 query 应通过中文单字匹配 '压缩'")


# ---------------------------------------------------------------------------
# 用例 13-15: _extract_last_user_text (design §8 #13-#15; W3 d4)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _TS1_HAS_EXTRACT_USER,
                 "TS-1 待 W3 d4 实施 (PRD-litellm-borrow §6.2 W3)")
class TestExtractLastUserText(unittest.TestCase):
    """query 抽取 (design §8 #13-#15, §4.2)."""

    def test_13_no_user_returns_empty(self):
        """无 user 消息 → 返回 '' (design §8 #13 边界)."""
        msgs = [_assistant_text("hello"), _assistant_text("world")]
        self.assertEqual(cc._extract_last_user_text(msgs), "")

    def test_14_last_user_only_tool_result_skips_to_prev_user_text(self):
        """最后 user 只有 tool_result 时向前找 user text (design §4.2 约束)."""
        msgs = [
            _user_text("original question"),
            _assistant_text("ok"),
            _user_tool_result("t1"),
        ]
        self.assertEqual(cc._extract_last_user_text(msgs), "original question")

    def test_15_all_users_only_tool_results_returns_empty(self):
        """全部 user 都只有 tool_result → 返回 '' (I-5 保底) (design §8 #15)."""
        msgs = [
            _user_tool_result("t1"),
            _assistant_text("reply"),
            _user_tool_result("t2"),
        ]
        self.assertEqual(cc._extract_last_user_text(msgs), "")


# ---------------------------------------------------------------------------
# 用例 16-18: compress_tool_result BM25 加载 (design §8 #16-#18; W3 d3)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _TS1_HAS_BM25_KWARGS,
                 "TS-1 待 W3 d3 实施 (PRD-litellm-borrow §6.2 W3)")
class TestCompressToolResultBM25Integration(unittest.TestCase):
    """compress_tool_result 新增 3 个 bm25 kwarg (design §8 #16-#18, §4.5)."""

    def test_16_low_bm25_score_forces_aggressive_compression(self):
        """bm25_score=0.3 (< drop_threshold=0.5) 强制压到 ~30% 原长 (design §4.5)."""
        # 长 content (远超 PROXY_COMPRESS_THRESHOLD),text 类型,user query 无相关 token
        content = "irrelevant_log_padding_" * 300   # ~6900 chars
        result = cc.compress_tool_result(
            content,
            bm25_score=0.3,
            bm25_drop_threshold=0.5,
            bm25_keep_threshold=3.5,
        )
        self.assertLess(result["ratio"], 0.5,
                        "低 BM25 分数应强制压到 < 50% 原长")

    def test_17_high_bm25_score_skips_compression_even_above_threshold(self):
        """bm25_score=4.0 (>= keep_threshold=3.5) 不压,即使 len 远超 threshold (design §4.5)."""
        content = "critical_relevant_content_" * 300   # ~6900 chars
        result = cc.compress_tool_result(
            content,
            bm25_score=4.0,
            bm25_drop_threshold=0.5,
            bm25_keep_threshold=3.5,
        )
        self.assertGreaterEqual(result["ratio"], 0.95,
                                "高 BM25 分数应跳过压缩 (保留原文)")
        self.assertEqual(result["strategy"], "none",
                         "strategy='none' 表示 BM25 keep 跳过")

    def test_18_bm25_score_none_falls_back_to_existing_path(self):
        """bm25_score=None → 走现有 threshold + content_type 路径 (I-1 无回归)."""
        content = '{"key": "value_" * 100}'   # ~710 chars, 长 JSON
        result_without_bm25 = cc.compress_tool_result(content)
        result_with_none = cc.compress_tool_result(
            content,
            bm25_score=None,
            bm25_drop_threshold=0.5,
            bm25_keep_threshold=3.5,
        )
        self.assertEqual(result_without_bm25["ratio"],
                         result_with_none["ratio"],
                         "bm25_score=None 应与不传 bm25 kwarg 行为一致")


# ---------------------------------------------------------------------------
# 整体执行入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()