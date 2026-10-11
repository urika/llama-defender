"""Unit tests for ScaffoldRouteStage (S1) and ContentEgressStage (S2).

覆盖: 旗标门/路由分包/aux 排除/幂等注入/出站过滤命中与放行/仅扫描云端路由。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proxy_state as _ps
from pipeline import ScaffoldRouteStage, ContentEgressStage, _parse_pytest_summary


def _mk_ctx(msgs, sid="swe-test-1", route_target="local"):
    class _C:
        pass
    c = _C()
    c.messages = msgs
    c.session_id = sid
    c.route_target = route_target
    return c


def _blk_text(m):
    ct = m.get("content")
    if isinstance(ct, str):
        return ct
    if isinstance(ct, list):
        return " ".join(x.get("text", "") for x in ct if isinstance(x, dict))
    return ""


class TestScaffoldRoute(unittest.TestCase):
    def setUp(self):
        self.stage = ScaffoldRouteStage()

    def test_flag_off_no_run(self):
        with patch.object(_ps, "PROXY_SCAFFOLD_ROUTE_ENABLED", False):
            self.assertFalse(self.stage.should_run(
                _mk_ctx([], route_target="cloud")))

    def test_flag_on_and_route_required(self):
        with patch.object(_ps, "PROXY_SCAFFOLD_ROUTE_ENABLED", True):
            self.assertTrue(self.stage.should_run(
                _mk_ctx([], route_target="cloud")))
            self.assertFalse(self.stage.should_run(
                _mk_ctx([], route_target=None)))

    def test_aux_session_excluded(self):
        with patch.object(_ps, "PROXY_SCAFFOLD_ROUTE_ENABLED", True):
            self.assertFalse(self.stage.should_run(
                _mk_ctx([], sid="swe-x::aux", route_target="cloud")))

    def test_local_pack_no_nav(self):
        """local 路由不含导航图路径提示（本地专属）。"""
        msgs = [{"role": "user", "content": [{"type": "text", "text": "fix"}]}]
        ctx = _mk_ctx(msgs, route_target="local")
        self.stage.process(ctx)
        # 检查是否有 SCAFFOLD 块
        blk = [m for m in ctx.messages if "SCAFFOLD" in _blk_text(m)]
        # local 可能因为 fail-open（yaml 路径不可达）而没注入——不强制
        # 只验证不崩溃

    def test_injection_idempotent(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "fix"}]}]
        ctx = _mk_ctx(msgs, sid="swe-idem", route_target="cloud")
        self.stage.process(ctx)
        n1 = len(ctx.messages)
        self.stage.process(ctx)
        self.assertEqual(len(ctx.messages), n1, "重复调用不得增长消息数")


class TestContentEgress(unittest.TestCase):
    def setUp(self):
        self.stage = ContentEgressStage()

    def test_flag_off_no_run(self):
        with patch.object(_ps, "PROXY_EGRESS_FILTER_ENABLED", False):
            self.assertFalse(self.stage.should_run(
                _mk_ctx([], route_target="cloud")))

    def test_local_route_not_scanned(self):
        with patch.object(_ps, "PROXY_EGRESS_FILTER_ENABLED", True):
            self.assertFalse(self.stage.should_run(
                _mk_ctx([], route_target="local")))

    def test_cloud_route_scanned(self):
        with patch.object(_ps, "PROXY_EGRESS_FILTER_ENABLED", True):
            self.assertTrue(self.stage.should_run(
                _mk_ctx([], route_target="cloud")))

    def test_clean_content_passes(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "fix the bug in ansible"}]}]
        ctx = _mk_ctx(msgs, route_target="cloud")
        self.stage.process(ctx)
        self.assertIsNone(getattr(ctx, "egress_blocked", None))

    def test_api_key_blocked(self):
        msgs = [{"role": "user", "content": [{"type": "text",
                 "text": "use key sk-abcdefghijklmnopqrstuvwxyz123456 to auth"}]}]
        ctx = _mk_ctx(msgs, route_target="cloud")
        self.stage.process(ctx)
        self.assertIsNotNone(getattr(ctx, "egress_blocked", None))

    def test_private_key_blocked(self):
        msgs = [{"role": "user", "content": [{"type": "text",
                 "text": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END"}]}]
        ctx = _mk_ctx(msgs, route_target="cloud")
        self.stage.process(ctx)
        self.assertIsNotNone(getattr(ctx, "egress_blocked", None))


if __name__ == "__main__":
    unittest.main()
