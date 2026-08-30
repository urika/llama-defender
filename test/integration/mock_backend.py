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
  MOCK_TIMINGS_PROMPT_N        (default: empty) when set, add llama-server style
                               `timings` object (prompt_n/prompt_ms/predicted_*)
                               — R13/R16 diagnostics integration tests
  MOCK_TIMINGS_PREDICTED_N     (default: 10)

Run:
  python3 test/integration/mock_backend.py [PORT]
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
                 "..", "..", "logs", "itest", "mock_capture.jsonl"),
)
CAPTURE_LOCK = threading.Lock()

TOOL_NAME = os.environ.get("MOCK_TOOL_NAME", "Read")
TOOL_ARGS = os.environ.get("MOCK_TOOL_ARGS", '{"file_path":"/nope.py"}')
PLAIN_TEXT = os.environ.get("MOCK_PLAIN_TEXT", "")
FINISH = os.environ.get("MOCK_FINISH", "tool_calls")
USAGE_PROMPT = int(os.environ.get("MOCK_USAGE_PROMPT", "100"))
USAGE_COMPLETION = int(os.environ.get("MOCK_USAGE_COMPLETION", "20"))
TIMINGS_PROMPT_N = os.environ.get("MOCK_TIMINGS_PROMPT_N", "")
TIMINGS_PREDICTED_N = int(os.environ.get("MOCK_TIMINGS_PREDICTED_N", "10"))

# 序列模式(IFC-3 微轮测试): MOCK_SEQ_FILE 指向 JSON 数组, 每个 POST 依序消费
# 一个 spec(最后一个重复)。spec: {"tool_call": {"id","name","arguments"}} 或
# {"text": "..."}。未设置时保持原有单响应 env 脚本行为。
SEQ_FILE = os.environ.get("MOCK_SEQ_FILE", "")
_SEQ = []
_SEQ_IDX = {"n": 0}
_SEQ_LOCK = threading.Lock()
if SEQ_FILE and os.path.exists(SEQ_FILE):
    with open(SEQ_FILE, encoding="utf-8") as _f:
        _SEQ = json.load(_f)


def _next_spec():
    """序列模式: 返回第 N 个 spec(越界重复最后一个); 空序列返回 None。"""
    if not _SEQ:
        return None
    with _SEQ_LOCK:
        i = min(_SEQ_IDX["n"], len(_SEQ) - 1)
        _SEQ_IDX["n"] += 1
        return _SEQ[i]


def _timings():
    """llama-server style timings object (empty dict when not configured)."""
    if not TIMINGS_PROMPT_N:
        return {}
    return {
        "prompt_n": int(TIMINGS_PROMPT_N),
        "prompt_ms": 123.4,
        "predicted_n": TIMINGS_PREDICTED_N,
        "predicted_ms": 45.6,
    }


def _build_response():
    """Return a canned OpenAI-format chat completion response."""
    spec = _next_spec()
    if spec is not None:
        if "tool_call" in spec:
            tcs = spec["tool_call"]
            return {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": tcs.get("id", "call_seq"),
                            "type": "function",
                            "function": {"name": tcs.get("name", "ctx_recall"),
                                         "arguments": tcs.get("arguments", "{}")},
                        }],
                    },
                }],
                "usage": {"prompt_tokens": USAGE_PROMPT,
                          "completion_tokens": USAGE_COMPLETION},
            }
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant",
                            "content": spec.get("text", "")},
            }],
            "usage": {"prompt_tokens": USAGE_PROMPT,
                      "completion_tokens": USAGE_COMPLETION},
        }
    timings = _timings()
    if PLAIN_TEXT:
        resp = {
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
        if timings:
            resp["timings"] = timings
        return resp
    resp = {
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
    if timings:
        resp["timings"] = timings
    return resp


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
        if body.get("stream"):
            self._send_stream_response()
            return
        self._send_json(200, _build_response())

    def _send_stream_response(self):
        """SSE 流式响应：2 个 content delta + 终块（usage + timings）+ [DONE]。

        序列模式(MOCK_SEQ_FILE)下按 spec 流式: tool_call spec 流
        tool_calls delta(名称/参数两段), text spec 流文本 delta。"""
        spec = _next_spec()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def _chunk(delta, finish=None, extra=None):
            obj = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if extra:
                obj.update(extra)
            return f"data: {json.dumps(obj)}\n\n".encode("utf-8")

        final_extra = {"usage": {"prompt_tokens": USAGE_PROMPT,
                                 "completion_tokens": USAGE_COMPLETION}}
        timings = _timings()
        if timings:
            final_extra["timings"] = timings

        if spec is not None:
            chunks = [_chunk({"role": "assistant", "content": ""})]
            if "tool_call" in spec:
                tcs = spec["tool_call"]
                chunks.append(_chunk({"tool_calls": [{
                    "index": 0, "id": tcs.get("id", "call_seq"),
                    "type": "function",
                    "function": {"name": tcs.get("name", "ctx_recall"),
                                 "arguments": ""},
                }]}))
                args = tcs.get("arguments", "{}")
                # 参数分两段流式(模拟真实增量拼装路径)
                mid = max(1, len(args) // 2)
                chunks.append(_chunk({"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": args[:mid]},
                }]}))
                chunks.append(_chunk({"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": args[mid:]},
                }]}))
                chunks.append(_chunk({}, finish="tool_calls", extra=final_extra))
            else:
                text = spec.get("text", "")
                if text:
                    mid = max(1, len(text) // 2)
                    chunks.append(_chunk({"content": text[:mid]}))
                    chunks.append(_chunk({"content": text[mid:]}))
                chunks.append(_chunk({}, finish="stop", extra=final_extra))
            chunks.append(b"data: [DONE]\n\n")
            for c in chunks:
                self.wfile.write(c)
                self.wfile.flush()
            return

        chunks = [
            _chunk({"role": "assistant", "content": ""}),
            _chunk({"content": "hello "}),
            _chunk({"content": "diag"}),
            _chunk({}, finish="stop", extra=final_extra),
            b"data: [DONE]\n\n",
        ]
        for c in chunks:
            self.wfile.write(c)
            self.wfile.flush()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8089
    print(f"mock backend listening on http://127.0.0.1:{port}", flush=True)
    print(f"capture path: {CAPTURE_PATH}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
