#!/usr/bin/env python3
"""DEF-309/EXP-3 前置③：云端上下文管理豁免语义单测（_context_exempt）。

语义矩阵：
  cloud 路由(含 stage-0 header override 形态) + 旗标 on  → 不豁免(代理管理)
  cloud 路由(含 header override 形态) + 旗标 off → 豁免(透传, 旧行为)
  local 路由                                    → 永不豁免
  目录元数据 context_managed_by / 请求头覆盖      → 最高优先级, 压过旗标
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import proxy_state as _ps  # noqa: E402
from pipeline import _context_exempt  # noqa: E402
from test.lib.config_fixture import patch_config  # noqa: E402


def _ctx(route="local", header="", body=None):
    b = {"model": "glm-5.3-flash-cn"}
    if body:
        b.update(body)
    from pipeline import PipelineContext
    ctx = PipelineContext(body=b)
    ctx._route_target = route
    ctx._route_header_override = header
    return ctx


class TestCloudCmExempt(unittest.TestCase):
    def test_cloud_flag_on_proxy_manages(self):
        with patch_config(PROXY_CLOUD_CM_ENABLED=True):
            self.assertFalse(_context_exempt(_ctx(route="cloud")))
            self.assertFalse(_context_exempt(_ctx(header="cloud")))  # stage-0.5 时序形态

    def test_cloud_flag_off_passthrough(self):
        with patch_config(PROXY_CLOUD_CM_ENABLED=False):
            self.assertTrue(_context_exempt(_ctx(route="cloud")))
            self.assertTrue(_context_exempt(_ctx(header="cloud")))

    def test_local_never_exempt_by_route(self):
        with patch_config(PROXY_CLOUD_CM_ENABLED=True):
            self.assertFalse(_context_exempt(_ctx(route="local")))

    def test_catalog_metadata_beats_flag(self):
        body = {"_x_proxy_context_managed_by": "client"}
        with patch_config(PROXY_CLOUD_CM_ENABLED=True):
            self.assertTrue(_context_exempt(_ctx(route="cloud", body=body)))
        body2 = {"_x_proxy_context_managed_by": "proxy"}
        with patch_config(PROXY_CLOUD_CM_ENABLED=False):
            self.assertFalse(_context_exempt(_ctx(route="cloud", body=body2)))


if __name__ == "__main__":
    unittest.main()
