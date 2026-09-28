#!/usr/bin/env python3
"""test_l59_recall_cue_gating.py — L-59 折叠召回 cue 门控（recall_tool_available）。

验证：折叠面板的召回 cue（RECALL_CUE + anchor 键行）按请求 tools 是否含
ctx_recall 门控——缺省 True（现行为，fail-open）；旗标 False 时 cue 与键行
均不注入且折叠本体不受影响。出典=EXP-12 轮1：cue 指示模型调用客户端白名单
外的工具 → 单会话 38 次连环 "No such tool available" 空转（跨批 25/25 全错）。
"""
import os
import tempfile
import unittest

import proxy_state as _ps
import memory_stores as ms

import context_engine as ce


def _round(i):
    body = "round %d payload " % i + "x" * 3000
    return [{"role": "user", "content": [{"type": "text", "text": body}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok %d" % i}]}]


class TestL59CueGating(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="l59_cue_")
        self._orig_dir = _ps._DIAG_DIR
        _ps._DIAG_DIR = self._tmp
        ms.MANIFEST.reset()

    def tearDown(self):
        _ps._DIAG_DIR = self._orig_dir
        ms.MANIFEST.reset()

    def _fold_once(self, recall_available):
        """驱使一次真实折叠，返回折叠面板文本。recall_available=None=不设旗标。"""
        st = ce.EngineStore()
        sess = st.get_or_create("sess_l59_%s" % recall_available)
        if recall_available is not None:
            sess.recall_tool_available = recall_available
        msgs = []
        for i in range(8):
            msgs.extend(_round(i))
            sess.absorb(msgs)
        # 触发参数：全量视图 est×1.6 ≈ 9.6K > 5K ⇒ 触发折叠；折叠后视图
        # （面板 + 2 保留轮 ≈ 1.5-2K）≤ 5K ⇒ 容量复检通过（不触硬上限）
        triggered, final = sess.maybe_epoch(5000, 2)
        self.assertTrue(triggered, "应触发折叠")
        self.assertIsNotNone(final, "折叠后视图不应为 None")
        panels = [m for m in final if m.get("_ctx_engine_epoch")]
        self.assertTrue(panels, "折叠面板消息应存在")
        return panels[0]["content"][0]["text"]

    def test_cue_present_when_flag_default(self):
        # 缺省（不设旗标）=现行为：cue 与键行照常注入
        text = self._fold_once(None)
        self.assertIn("use ctx_recall to recover", text)

    def test_cue_present_when_tool_available(self):
        text = self._fold_once(True)
        self.assertIn("use ctx_recall to recover", text)

    def test_cue_absent_when_tool_unavailable(self):
        text = self._fold_once(False)
        self.assertNotIn("ctx_recall", text)
        self.assertNotIn("r:", text.split("[context-engine epoch")[1].split("\n", 1)[1]
                         if "[context-engine epoch" in text else text)

    def test_fold_intact_when_gated(self):
        # 门控只去 cue，折叠本体（收编台账/面板头）不受影响
        text = self._fold_once(False)
        self.assertIn("collapsed", text)
        self.assertIn("earlier rounds collapsed", text)


if __name__ == "__main__":
    unittest.main()
