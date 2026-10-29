#!/usr/bin/env python3
"""fnf fixture stub 后端（:8082）——记录代理出站请求并返回最小 anthropic 响应。

T2 仪器保真度门（2026-10-29）：fnf 转译发生在代理出站 payload 上
（tool_filter._translate_tool_result_errors_ex），因此 stub 捕获到的
请求体即断言对象。零共享状态：端口/日志均独立于生产实例。
"""
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPTURE = os.environ.get("FNFIX_CAPTURE", "/tmp/fnf_fixture_captures.jsonl")
PORT = int(os.environ.get("FNFIX_PORT", "8082"))

RESPONSE = {
    "id": "chatcmpl-stub", "object": "chat.completion", "created": 0,
    "model": "stub-fnf-fixture",
    "choices": [{"index": 0,
                 "message": {"role": "assistant", "content": "stub-ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", errors="replace")
        with open(CAPTURE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": self.path,
                                "body": json.loads(body) if body.startswith("{") else body},
                               ensure_ascii=False) + "\n")
        payload = json.dumps(RESPONSE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    os.makedirs(os.path.dirname(CAPTURE) or ".", exist_ok=True)
    open(CAPTURE, "w").close()
    print("stub backend on :%d, capture -> %s" % (PORT, CAPTURE), flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
