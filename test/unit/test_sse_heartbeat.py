#!/usr/bin/env python3
"""#51-B1 SSE 心跳单测: 周期注释行 / stop 幂等 / 断连哨兵 / beats 计数。

运行: cd llama.cpp && python3 -m pytest test/unit/test_sse_heartbeat.py -q
"""

import io
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from anthropic_proxy import Handler  # noqa: E402
import anthropic_proxy  # noqa: E402


class _FakeWfile(io.RawIOBase):
    """记录写入内容; 可编程抛 BrokenPipeError。"""

    def __init__(self):
        self.writes = []
        self.fail_after = None  # 第 N 次写后开始抛错(1-based)

    def write(self, b):
        if self.fail_after is not None and len(self.writes) >= self.fail_after:
            raise BrokenPipeError("fake client gone")
        self.writes.append(b)
        return len(b)

    def flush(self):
        pass


class _DelayedResp:
    """模拟冷 prefill: 数据延迟经真实 socket 到达(v4 select 路径需要
    fp.raw._sock 与真 select 语义, socketpair 全真模拟)。"""

    def __init__(self, delay, lines):
        import socket
        a, b = socket.socketpair()
        self._writer = a
        self.fp = b.makefile("rb")
        self.closed = False
        threading.Timer(delay, self._emit, args=(tuple(lines),)).start()

    def _emit(self, lines):
        try:
            for l in lines:
                self._writer.sendall(l)
            self._writer.close()
        except Exception:
            pass

    def readline(self):
        return self.fp.readline()

    def __iter__(self):
        while True:
            l = self.readline()
            if not l:
                return
            yield l

    def close(self):
        self.closed = True
        try:
            self.fp.close()
            self._writer.close()
        except Exception:
            pass


class TestHeartbeatRelayIntegration(unittest.TestCase):
    """#51-B1 v2: 心跳挂中继首行等待(非 urlopen)。"""

    def setUp(self):
        self._saved = anthropic_proxy._ps.PROXY_SSE_HEARTBEAT_S
        anthropic_proxy._ps.PROXY_SSE_HEARTBEAT_S = 0.05

    def tearDown(self):
        anthropic_proxy._ps.PROXY_SSE_HEARTBEAT_S = self._saved

    def _handler(self, wanted):
        h = object.__new__(Handler)
        h.wfile = _FakeWfile()
        h._sse_heartbeat_wanted = wanted
        h._client_disconnected = False
        return h

    def test_heartbeat_beats_while_waiting_first_line(self):
        h = self._handler(True)
        resp = _DelayedResp(0.3, [b"data: {}\n\n"])
        out = b"".join(h._heartbeat_lines(resp))
        self.assertEqual(out, b"data: {}\n\n")        # 数据字节原样透传
        ka = [w for w in h.wfile.writes if w.startswith(b":")]
        self.assertGreaterEqual(len(ka), 2)            # 等待期间有心跳

    def test_no_heartbeat_when_not_wanted(self):
        h = self._handler(False)
        resp = _DelayedResp(0.15, [b"data: {}\n\n"])
        out = b"".join(h._heartbeat_lines(resp))
        self.assertEqual(out, b"data: {}\n\n")
        self.assertEqual(h.wfile.writes, [])           # 零写入 = 无心跳


if __name__ == "__main__":
    unittest.main()
