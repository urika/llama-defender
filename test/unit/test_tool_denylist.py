#!/usr/bin/env python3
"""test_tool_denylist.py — L-26 纵深防御：代理层工具黑名单剥离。"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import proxy_state as _ps
from tool_filter import _apply_tool_denylist

TOOLS = [{"name": "Bash"}, {"name": "Read"}, {"name": "WebFetch"},
         {"name": "WebSearch"}, {"name": "Edit"}]


class TestToolDenylist(unittest.TestCase):

    def test_strip_named_tools(self):
        _ps.PROXY_TOOLS_DENYLIST = "WebFetch,WebSearch"
        kept, denied = _apply_tool_denylist(TOOLS)
        self.assertEqual(denied, 2)
        self.assertEqual({t["name"] for t in kept}, {"Bash", "Read", "Edit"})

    def test_empty_denylist_noop(self):
        _ps.PROXY_TOOLS_DENYLIST = ""
        kept, denied = _apply_tool_denylist(TOOLS)
        self.assertEqual(denied, 0)
        self.assertEqual(len(kept), 5)

    def test_nonexistent_tool_noop(self):
        _ps.PROXY_TOOLS_DENYLIST = "NoSuchTool"
        kept, denied = _apply_tool_denylist(TOOLS)
        self.assertEqual(denied, 0)
        self.assertEqual(len(kept), 5)

    def test_empty_tools(self):
        _ps.PROXY_TOOLS_DENYLIST = "WebFetch"
        kept, denied = _apply_tool_denylist([])
        self.assertEqual(kept, [])
        self.assertEqual(denied, 0)

    def test_all_tools_denied(self):
        _ps.PROXY_TOOLS_DENYLIST = "Bash,Read,WebFetch,WebSearch,Edit"
        kept, denied = _apply_tool_denylist(TOOLS)
        self.assertEqual(denied, 5)
        self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main()
