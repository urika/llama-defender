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

from anthropic_proxy import _SSEHeartbeat, Handler  # noqa: E402
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
    """模拟冷 prefill: 首行延迟到达, 之后立即结束。"""

    def __init__(self, delay, lines):
        self._delay = delay
        self._lines = lines
        self.closed = False

    def close(self):
        self.closed = True

    def __iter__(self):
        time.sleep(self._delay)
        for l in self._lines:
            yield l


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
        out = list(h._heartbeat_lines(resp))
        self.assertEqual(out, [b"data: {}\n\n"])       # 数据行原样透传
        ka = [w for w in h.wfile.writes if w.startswith(b":")]
        self.assertGreaterEqual(len(ka), 2)            # 首行等待期间有心跳

    def test_no_heartbeat_when_not_wanted(self):
        h = self._handler(False)
        resp = _DelayedResp(0.15, [b"data: {}\n\n"])
        out = list(h._heartbeat_lines(resp))
        self.assertEqual(out, [b"data: {}\n\n"])
        self.assertEqual(h.wfile.writes, [])           # 零写入 = 无心跳线程


class TestSSEHeartbeat(unittest.TestCase):

    def test_periodic_keepalive_comments(self):
        w = _FakeWfile()
        hb = _SSEHeartbeat(w, interval=0.05)
        hb.start()
        time.sleep(0.28)  # ~5 个间隔
        hb.stop()
        self.assertGreaterEqual(hb.beats, 3)
        for chunk in w.writes:
            self.assertEqual(chunk, b": keepalive\n\n")  # 注释行格式完备

    def test_stop_is_idempotent_and_thread_exits(self):
        w = _FakeWfile()
        hb = _SSEHeartbeat(w, interval=0.05)
        hb.start()
        hb.stop()
        hb.stop()  # 幂等
        self.assertFalse(hb._thread.is_alive())

    def test_broken_pipe_marks_client_disconnected(self):
        w = _FakeWfile()
        w.fail_after = 1  # 首次心跳成功, 第二次抛错
        hb = _SSEHeartbeat(w, interval=0.05)
        hb.start()
        for _ in range(40):
            if hb.client_disconnected():
                break
            time.sleep(0.05)
        hb.stop()
        self.assertTrue(hb.client_disconnected())
        self.assertEqual(hb.beats, 1)  # 失败的那次不计 beats


if __name__ == "__main__":
    unittest.main()
