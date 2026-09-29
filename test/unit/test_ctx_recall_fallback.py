#!/usr/bin/env python3
"""test_ctx_recall_fallback.py — P0(2026-09-28) ctx_recall 非死局回退回归锚。

背景（exp-20260928-splash-efficacy 实测）：模型调用 ctx_recall（代理侧代答工具，
Claude Code 客户端工具面无它）→ 客户端回 "No such tool available" 的 error
tool_result → 代理本应在 rewrite_ctx_recall_results 里改写为真实检索结果。实测
3 个 run 共 47 次调用 47/47 原样透传，改写一次未兑现且零日志（顶层 except: pass
+ 内层无 except 覆盖），模型把该 error 当逃生门反复调用（081154 烧 38 轮）。

本测试钉死 P0 契约：检索/格式化异常、缺 query 时，error tool_result 一律被替换
成有界回退文案（含语义重锚），且诊断落 stderr；顶层异常不再静默。
"""
import contextlib
import io
import shutil
import tempfile
import unittest

import proxy_state as _ps

import ctx_recall as cr
import memory_stores as ms

_ERROR_TOOL_RESULT = "<tool_use_error>No such tool available: ctx_recall</tool_use_error>"


class TestCtxRecallNonDeadend(unittest.TestCase):
    """覆盖 content_compressor.rewrite_ctx_recall_results 的异常/缺参与顶层诊断分支。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ifc_recall_fallback_")
        self._orig_dir = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._tmp
        self._orig_pd = getattr(_ps, "PROXY_PD_ENABLED", True)
        _ps.PROXY_PD_ENABLED = True
        self._orig_lookup = cr.lookup
        self._orig_format = cr.format_recall_result
        ms.MANIFEST.reset()

    def tearDown(self):
        _ps._DIAG_DIR = self._orig_dir
        _ps.PROXY_PD_ENABLED = self._orig_pd
        cr.lookup = self._orig_lookup
        cr.format_recall_result = self._orig_format
        ms.MANIFEST.reset()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _msgs(self, tool_input):
        return [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "rc1", "name": "ctx_recall",
                 "input": tool_input}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "rc1",
                 "content": [{"type": "text", "text": _ERROR_TOOL_RESULT}]}]},
        ]

    def _rewrite(self, messages, session_key="s-p0-fallback"):
        from content_compressor import rewrite_ctx_recall_results
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = rewrite_ctx_recall_results(messages, session_key)
        return out, err.getvalue()

    @staticmethod
    def _tool_result_text(messages):
        for msg in messages:
            if msg.get("role") != "user":
                continue
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    if isinstance(content, list):
                        return " ".join(b.get("text", "") for b in content
                                        if isinstance(b, dict))
                    return content or ""
        return ""

    def test_lookup_exception_lands_bounded_fallback(self):
        """检索抛异常 → 不再留原始 error，落有界回退 + stderr 诊断。"""
        def _boom(*_a, **_kw):
            raise RuntimeError("fts index unavailable")

        cr.lookup = _boom
        out, err = self._rewrite(self._msgs({"query": "def run(self, terms)"}))
        text = self._tool_result_text(out)

        self.assertNotIn("No such tool available", text)
        self.assertIn("ctx_recall 检索不可用", text)
        self.assertIn("勿重复此查询", text)
        self.assertIn("Grep", text)              # 换工具出口
        self.assertIn("写下你认为", text)          # 语义重锚
        self.assertIn("[ctx_recall-rewrite] 已落有界回退", err)
        self.assertIn("lookup/format 异常", err)

    def test_format_exception_lands_bounded_fallback(self):
        """格式化抛异常 → 同样落有界回退（异常覆盖整段检索+格式化）。"""
        def _boom(*_a, **_kw):
            raise ValueError("bad format arg")

        cr.format_recall_result = _boom
        out, err = self._rewrite(self._msgs({"query": "def run(self, terms)"}))
        text = self._tool_result_text(out)

        self.assertNotIn("No such tool available", text)
        self.assertIn("ctx_recall 检索不可用", text)
        self.assertIn("bad format arg", text)
        self.assertIn("[ctx_recall-rewrite] 已落有界回退", err)

    def test_missing_query_lands_bounded_fallback(self):
        """缺 query → 不再 continue 留原始 error，落有界回退 + 诊断。"""
        out, err = self._rewrite(self._msgs({"limit": 8}))
        text = self._tool_result_text(out)

        self.assertNotIn("No such tool available", text)
        self.assertIn("ctx_recall 检索不可用", text)
        self.assertIn("缺少 query 参数", text)
        self.assertIn("[ctx_recall-rewrite] 已落有界回退", err)

    def test_str_content_block_is_replaced_too(self):
        """content 为 str 形态（非 list）也替换，不留原始 error。"""
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "rc1", "name": "ctx_recall",
                 "input": {"limit": 8}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "rc1",
                 "content": _ERROR_TOOL_RESULT}]},
        ]
        out, _err = self._rewrite(messages)
        text = self._tool_result_text(out)

        self.assertNotIn("No such tool available", text)
        self.assertIn("ctx_recall 检索不可用", text)

    def test_outer_exception_logs_fail_open(self):
        """顶层异常 fail-open 但必须留痕（此前 except: pass 完全无声）。"""
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "rc1", "name": "ctx_recall",
                 "input": ["malformed"]}]},          # 非 dict → .get 抛 AttributeError
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "rc1",
                 "content": [{"type": "text", "text": _ERROR_TOOL_RESULT}]}]},
        ]
        out, err = self._rewrite(messages)

        self.assertIs(out, messages)                 # fail-open：原样返回不阻断
        self.assertIn("[ctx_recall-rewrite] fail-open 异常", err)

    def test_success_path_rewrites_and_stays_silent(self):
        """成功路径：正常改写成检索结果，且**不得**落任何诊断噪声。

        回归锚（2026-09-29 激活验收实测）：`if changed:` 分支原 touch
        proxy_state._DIAG_ENABLED（属性不存在）→ 每次成功改写都抛
        AttributeError 被外层吞掉；P0 加上诊断后，成功路径会被误报成
        "fail-open 异常"，污染判读。
        """
        ms.record_dropped_messages(
            "s-p0-hit", turn=1, reason="fifo_drop",
            messages=[{"role": "user", "content": [
                {"type": "text",
                 "text": "def run(self, terms, variables, **kwargs): pass"}]}])
        out, err = self._rewrite(self._msgs({"query": "def run(self, terms)"}),
                                 "s-p0-hit")
        text = self._tool_result_text(out)

        self.assertIn("命中", text)                  # 正常命中改写
        self.assertNotIn("ctx_recall 检索不可用", text)
        self.assertNotIn("ctx_recall-rewrite", err)   # 成功路径零诊断
        self.assertNotIn("fail-open 异常", err)

    def test_no_ctx_recall_call_is_untouched(self):
        """无 ctx_recall 调用 → 快速返回，零诊断（回归锚：不误伤正常路径）。"""
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/src/a.py"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": _ERROR_TOOL_RESULT}]}]},
        ]
        out, err = self._rewrite(messages)

        self.assertEqual(_ERROR_TOOL_RESULT, self._tool_result_text(out))
        self.assertNotIn("ctx_recall-rewrite", err)


if __name__ == "__main__":
    unittest.main()
