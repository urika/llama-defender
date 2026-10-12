#!/usr/bin/env python3
"""test_ctx_engine_cloud_exempt.py — 云端会话 epoch 豁免（0923 缺口补完）。"""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import pipeline as pl
import proxy_state as ps


class _FakeCtx:
    session_id = "s1"
    tools_list = [{"name": "Read"}]
    _agent_model_tier = "sonnet"
    body = {}                     # _context_exempt 读 body._x_proxy_context_managed_by
    model = "claude-sonnet-4-6"
    _route_target = "local"
    _route_header_override = ""


class TestCloudSessionExempt(unittest.TestCase):
    """0923: 豁免判定在 _context_exempt 源头（补会话路由记忆时序缺口）。"""

    def _stage(self):
        return pl.ContextEngineStage()

    def test_exempt_true_with_session_cloud_memory(self):
        """会话路由记忆=cloud → _context_exempt True（0.5 时 _route_target 恒 local）。"""
        ctx = _FakeCtx()
        ctx._route_target = "local"   # 0.5 时序：恒 local
        ctx._route_header_override = ""
        with mock.patch.object(ps, "PROXY_CLOUD_CM_ENABLED", False), \
             mock.patch.object(ps, "_SESSION_ROUTE_MAP", {"s1": "cloud"}):
            self.assertTrue(pl._context_exempt(ctx))

    def test_not_exempt_local_memory(self):
        ctx = _FakeCtx()
        ctx._route_target = "local"
        ctx._route_header_override = ""
        with mock.patch.object(ps, "PROXY_CLOUD_CM_ENABLED", False), \
             mock.patch.object(ps, "_SESSION_ROUTE_MAP", {"s1": "local"}):
            self.assertFalse(pl._context_exempt(ctx))

    def test_cm_enabled_overrides_memory(self):
        """CLOUD_CM_ENABLED=true → 云端也交代理管理（有意形态）。"""
        ctx = _FakeCtx()
        ctx._route_target = "local"
        ctx._route_header_override = ""
        with mock.patch.object(ps, "PROXY_CLOUD_CM_ENABLED", True), \
             mock.patch.object(ps, "_SESSION_ROUTE_MAP", {"s1": "cloud"}):
            self.assertFalse(pl._context_exempt(ctx))

    def test_stage_should_run_with_exempt(self):
        """集成：exempt=True → should_run False（0.5 跳过）。"""
        ctx = _FakeCtx()
        ctx._route_target = "local"
        ctx._route_header_override = ""
        with mock.patch.object(ps, "PROXY_CTX_ENGINE_ENABLED", True), \
             mock.patch.object(ps, "PROXY_CLOUD_CM_ENABLED", False), \
             mock.patch.object(ps, "_SESSION_ROUTE_MAP", {"s1": "cloud"}), \
             mock.patch.object(pl, "_context_exempt", return_value=True):
            self.assertFalse(self._stage().should_run(ctx))


if __name__ == "__main__":
    unittest.main(verbosity=2)
