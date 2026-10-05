#!/usr/bin/env python3
"""L-61（2026-10-05）: ctx_recall miss 文案重试邀请有界化。

出典: ctx_recall P0(i) 激活验收探针两次复现——模型收到"先换关键词重试一次"
文案后立即换词再发 ctx_recall（召回空转自持，无需幽灵工具）；L-61 行方向=
显式"至多一次"计数／或去邀请，并与 P0(ii) fail_streak 干预联动。

本测试钉死（防止回退为无界邀请）:
  ① miss（窗口核验=不在当前上下文）分支含显式上界"至多再换一次关键词"
  ② 含计数/连败联动陈述（连续未命中会累积计数）
  ③ 不再出现旧的无界邀请句式"先换关键词重试一次 recall；仍无再重读"
  ④ P0(ii) 连败干预文案（loop_detection Lev1）仍不邀请换词重试（反面断言保持）
  ⑤ 其余两态文案（命中当前上下文 / 无窗口信息）不受本次改动影响
"""
import unittest

import ctx_recall as cr

MISS_OLD_UNBOUNDED = "先换关键词重试一次 recall；仍无再重读"


class MissBoundedTest(unittest.TestCase):
    def _miss_text(self):
        # lines=[] → miss；window_hint 非 None 且 found 假 → "不在当前上下文"分支
        return cr.format_recall_result([], "gpfs mount filter", session_key=None,
                                       window_hint={"found": False})

    def test_explicit_bound_present(self):
        text = self._miss_text()
        self.assertIn("至多再换一次关键词", text)

    def test_counter_linkage_stated(self):
        text = self._miss_text()
        self.assertIn("累积计数", text)
        self.assertIn("停止重试", text)

    def test_no_unbounded_invitation(self):
        text = self._miss_text()
        self.assertNotIn(MISS_OLD_UNBOUNDED, text)

    def test_window_found_branch_unchanged(self):
        text = cr.format_recall_result(
            [], "q", session_key=None,
            window_hint={"found": True, "index": 2, "total": 9, "hits": 1})
        self.assertIn("仍在你的当前上下文中", text)
        self.assertNotIn("至多再换一次关键词", text)

    def test_no_window_hint_branch_unchanged(self):
        text = cr.format_recall_result([], "q", session_key=None, window_hint=None)
        self.assertIn("只检索", text)
        self.assertNotIn("至多再换一次关键词", text)


class P0IIInterventionConsistencyTest(unittest.TestCase):
    """P0(ii) Lev1 干预文案保持'不邀请换词重试'（与 L-61 有界化同向）。"""

    def test_intervention_text_not_inviting(self):
        import loop_detection as ld
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        msgs2, _, level, tool_name = ld._apply_loop_intervention(
            msgs, [], 1, {},
            ctx_recall_meta={"fail_streak": 3, "misses": 3, "fallbacks": 0,
                             "last_query": "abc", "fresh": True})
        self.assertEqual(level, 1)
        text = str(msgs2[-1])
        self.assertIn("不要再换关键词重试", text)
        self.assertNotIn("至多再换一次关键词", text)


if __name__ == "__main__":
    unittest.main()
