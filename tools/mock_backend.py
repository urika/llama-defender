#!/usr/bin/env python3
"""Mock OpenAI-compatible backend for anthropic_proxy integration tests.

Endpoints:
  GET  /v1/models        → returns a single mock model
  POST /v1/chat/completions
      → returns a canned tool_use response (or plain text)
      → writes the received request body to logs/itest/mock_capture.jsonl

Configurable via env vars:
  MOCK_TOOL_NAME   (default: "Read")     tool name in canned response
  MOCK_TOOL_ARGS   (default: '{"file_path":"/nope.py"}')
  MOCK_PLAIN_TEXT  (default: empty)       when set, return plain text (no tool_use)
  MOCK_FINISH      (default: "tool_calls") finish_reason in response
  MOCK_USAGE_PROMPT      (default: 100)
  MOCK_USAGE_COMPLETION  (default: 20)

Run:
  python3 tools/mock_backend.py [PORT]
"""
import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPTURE_PATH = os.environ.get(
    "MOCK_CAPTURE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "logs", "itest", "mock_capture.jsonl"),
)
CAPTURE_LOCK = threading.Lock()

TOOL_NAME = os.environ.get("MOCK_TOOL_NAME", "Read")
TOOL_ARGS = os.environ.get("MOCK_TOOL_ARGS", '{"file_path":"/nope.py"}')
PLAIN_TEXT = os.environ.get("MOCK_PLAIN_TEXT", "")
FINISH = os.environ.get("MOCK_FINISH", "tool_calls")
USAGE_PROMPT = int(os.environ.get("MOCK_USAGE_PROMPT", "100"))
USAGE_COMPLETION = int(os.environ.get("MOCK_USAGE_COMPLETION", "20"))


def _build_response():
    """Return a canned OpenAI-format chat completion response."""
    if PLAIN_TEXT:
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": PLAIN_TEXT},
            }],
            "usage": {
                "prompt_tokens": USAGE_PROMPT,
                "completion_tokens": USAGE_COMPLETION,
            },
        }
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "choices": [{
            "finish_reason": FINISH,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_mock",
                    "type": "function",
                    "function": {"name": TOOL_NAME, "arguments": TOOL_ARGS},
                }],
            },
        }],
        "usage": {
            "prompt_tokens": USAGE_PROMPT,
            "completion_tokens": USAGE_COMPLETION,
        },
    }


def _write_capture(body):
    """Append a request body to the capture log (one JSON per line)."""
    os.makedirs(os.path.dirname(CAPTURE_PATH), exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(),
        "body": body,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with CAPTURE_LOCK:
        with open(CAPTURE_PATH, "a") as f:
            f.write(line)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args, **_kwargs):
        return

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send_json(200, {
                "object": "list",
                "data": [{"id": "mock", "object": "model"}],
            })
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._send_json(404, {"error": "not_found"})
            return
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n).decode("utf-8") if n else "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"_raw": raw}
        _write_capture(body)
        self._send_json(200, _build_response())


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8089
    print(f"mock backend listening on http://127.0.0.1:{port}", flush=True)
    print(f"capture path: {CAPTURE_PATH}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
