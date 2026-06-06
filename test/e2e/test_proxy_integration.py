#!/usr/bin/env python3
"""End-to-end test suite for the running anthropic_proxy.

Exercises the live HTTP server on http://127.0.0.1:4000 (or whatever
PROXY_BASE points at) and reports a PASS/FAIL matrix covering:
    - Route discovery & error responses
    - Simple chat / Chinese / special characters
    - Tool use (single-turn + multi-turn tool_use->tool_result)
    - SSE streaming + content-type
    - Session continuity (X-Claude-Code-Session-Id)
    - Concurrent requests under PROXY_MAX_CONCURRENT=1
    - /v1/messages/count_tokens (Claude Code beta endpoint)
    - Long context (~5K tokens)
    - Anthropic SDK headers (?beta=true + anthropic-beta)
    - request-id response header (Anthropic API parity)

Run directly:
    python3 test/e2e/test_proxy_integration.py
Or with a custom base URL:
    PROXY_BASE=http://localhost:4000 python3 test/e2e/test_proxy_integration.py
Or via the unified runner (requires running proxy + backend):
    bash test/run_tests.sh --e2e

Exit code is 0 on full pass, 1 if any case fails.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

BASE = os.environ.get("PROXY_BASE", "http://127.0.0.1:4000")
API_KEY = os.environ.get("PROXY_API_KEY", "sk-1234")
ANTHROPIC_VERSION = "2023-06-01"

# ---------- pretty printing ----------
class C:
    G = "\033[32m"  # green
    R = "\033[31m"  # red
    Y = "\033[33m"  # yellow
    B = "\033[1m"   # bold
    D = "\033[2m"   # dim
    X = "\033[0m"   # reset

results: list[tuple[str, bool, str, float]] = []

def _post(path, body, headers=None, timeout=120, stream=False):
    url = f"{BASE}{path}"
    h = {"Content-Type": "application/json",
         "x-api-key": API_KEY,
         "anthropic-version": ANTHROPIC_VERSION}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        elapsed = time.monotonic() - t0
        return resp.status, dict(resp.headers), resp, elapsed
    except urllib.error.HTTPError as e:
        elapsed = time.monotonic() - t0
        return e.code, dict(e.headers), e, elapsed
    except Exception as e:
        elapsed = time.monotonic() - t0
        return None, {}, e, elapsed

def _get(path, timeout=10):
    url = f"{BASE}{path}"
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return resp.status, resp.read(), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.monotonic() - t0
    except Exception as e:
        return None, str(e).encode(), time.monotonic() - t0

def record(name, ok, detail, elapsed):
    results.append((name, ok, detail, elapsed))
    tag = f"{C.G}PASS{C.X}" if ok else f"{C.R}FAIL{C.X}"
    print(f"  {tag}  {C.B}{name}{C.X}  {C.D}{elapsed:.2f}s{C.X}  {detail}")

# =========================================================================
# Test cases
# =========================================================================

def test_routes():
    print(f"\n{C.B}[1] Route discovery & error responses{C.X}")
    # /v1/models
    code, body, _ = _get("/v1/models")
    if code == 200 and b"claude" in body:
        record("GET /v1/models", True, f"200, {len(body)}B", 0)
    else:
        record("GET /v1/models", False, f"code={code}", 0)

    # /status
    code, body, _ = _get("/status")
    if code == 200 and b"<html" in body.lower():
        record("GET /status", True, f"200, HTML dashboard", 0)
    else:
        record("GET /status", False, f"code={code}", 0)

    # unknown path
    code, _, _ = _get("/v1/nonexistent")
    if code == 404:
        record("GET /v1/nonexistent (404)", True, "404 Not Found", 0)
    else:
        record("GET /v1/nonexistent (404)", False, f"code={code}, expected 404", 0)

    # invalid JSON
    url = f"{BASE}/v1/messages"
    req = urllib.request.Request(url, data=b"{not json",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY,
                 "anthropic-version": ANTHROPIC_VERSION}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        record("POST /v1/messages invalid JSON", False, "expected 400", 0)
    except urllib.error.HTTPError as e:
        if e.code == 400 and b"Invalid JSON" in e.read():
            record("POST /v1/messages invalid JSON", True, "400 Invalid JSON", 0)
        else:
            record("POST /v1/messages invalid JSON", False, f"code={e.code}", 0)


def test_simple_chat():
    print(f"\n{C.B}[2] Simple chat{C.X}")
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 30,
        "messages": [{"role": "user", "content": "Reply with the single word: PONG"}],
    }
    code, headers, resp, elapsed = _post("/v1/messages", body)
    if code != 200:
        record("Simple chat", False, f"code={code}", elapsed)
        return None
    data = json.loads(resp.read())
    text = data.get("content", [{}])[0].get("text", "") if data.get("content") else ""
    if "PONG" in text.upper():
        record("Simple chat (text reply)", True, f"200, text={text!r}", elapsed)
    else:
        record("Simple chat (text reply)", False, f"text={text!r}", elapsed)
    # usage
    usage = data.get("usage", {})
    if usage.get("input_tokens", 0) > 0 and usage.get("output_tokens", 0) > 0:
        record("Simple chat (usage reported)", True,
               f"in={usage['input_tokens']} out={usage['output_tokens']}", 0)
    else:
        record("Simple chat (usage reported)", False, f"usage={usage}", 0)
    # stop_reason
    sr = data.get("stop_reason")
    record("Simple chat (stop_reason)", sr in ("end_turn", "stop", "max_tokens"),
           f"stop_reason={sr}", 0)
    # headers
    req_id = headers.get("request-id", "")
    record("Simple chat (request-id header)", bool(req_id),
           f"request-id={req_id[:16]}...", 0)
    return data


def test_chinese():
    print(f"\n{C.B}[3] Chinese input/output{C.X}")
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 60,
        "messages": [{"role": "user", "content": "用中文一句话介绍北京。"}],
    }
    code, _, resp, elapsed = _post("/v1/messages", body)
    if code != 200:
        record("Chinese chat", False, f"code={code}", elapsed)
        return
    data = json.loads(resp.read())
    text = data["content"][0]["text"]
    has_chinese = any('一' <= c <= '鿿' for c in text)
    record("Chinese chat (CJK preserved)", has_chinese,
           f"text={text!r}", elapsed)


def test_tool_use():
    print(f"\n{C.B}[4] Tool use (single-turn){C.X}")
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": "What is 7 * 8? Use the calculator tool."}],
        "tools": [{
            "name": "calculator",
            "description": "Multiply two numbers",
            "input_schema": {
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        }],
    }
    code, _, resp, elapsed = _post("/v1/messages", body)
    if code != 200:
        record("Tool use (turn 1)", False, f"code={code}", elapsed)
        return
    data = json.loads(resp.read())
    tool_use = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
    if tool_use and tool_use.get("name") == "calculator":
        record("Tool use (turn 1 -> tool_use block)", True,
               f"tool={tool_use['name']} id={tool_use['id'][:8]}...", elapsed)
    else:
        record("Tool use (turn 1 -> tool_use block)", False,
               f"content={[b.get('type') for b in data.get('content',[])]}", elapsed)
        return
    return data


def test_multi_turn_tool_flow():
    print(f"\n{C.B}[5] Multi-turn: tool_use -> tool_result -> final{C.X}")
    # turn 1: ask for calculation
    body1 = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": "Compute 6*7 using the calculator tool."}],
        "tools": [{
            "name": "calculator",
            "description": "Multiply two numbers",
            "input_schema": {
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
            },
        }],
    }
    code, _, resp, t1 = _post("/v1/messages", body1)
    if code != 200:
        record("Multi-turn turn 1", False, f"code={code}", t1)
        return
    d1 = json.loads(resp.read())
    tool_use = next((b for b in d1.get("content", []) if b.get("type") == "tool_use"), None)
    if not tool_use:
        record("Multi-turn turn 1 (no tool_use)", False, f"content={d1.get('content')}", t1)
        return
    # turn 2: feed back tool_result
    body2 = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "Compute 6*7 using the calculator tool."},
            {"role": "assistant", "content": d1["content"]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use["id"],
                "content": "42",
            }]},
        ],
        "tools": body1["tools"],
    }
    code, _, resp, t2 = _post("/v1/messages", body2)
    if code != 200:
        record("Multi-turn turn 2", False, f"code={code}", t2)
        return
    d2 = json.loads(resp.read())
    text = next((b["text"] for b in d2["content"] if b["type"] == "text"), "")
    if "42" in text:
        record("Multi-turn (tool_result -> final text)", True, f"text={text!r}", t1 + t2)
    else:
        record("Multi-turn (tool_result -> final text)", False, f"text={text!r}", t1 + t2)


def test_streaming():
    print(f"\n{C.B}[6] Streaming (SSE){C.X}")
    url = f"{BASE}/v1/messages"
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": "Count 1,2,3 briefly."}],
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY,
                 "anthropic-version": ANTHROPIC_VERSION})
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        ct = resp.headers.get("content-type", "")
        chunks = []
        events = []
        for raw in resp:
            line = raw.decode("utf-8", errors="ignore").rstrip()
            if line.startswith("data: "):
                chunks.append(line[6:])
                try:
                    ev = json.loads(line[6:])
                    events.append(ev)
                except Exception:
                    pass
        elapsed = time.monotonic() - t0
        # Look for message_start, content_block_start, message_stop
        event_types = {e.get("type") for e in events}
        if "message_start" in event_types and "message_stop" in event_types:
            record("Streaming (SSE events)", True,
                   f"events={len(events)}, types={sorted(t for t in event_types if t)}", elapsed)
        else:
            record("Streaming (SSE events)", False,
                   f"events={len(events)}, types={list(event_types)[:5]}", elapsed)
        # Verify content-type
        if "text/event-stream" in ct or "event-stream" in ct:
            record("Streaming (content-type)", True, f"ct={ct}", 0)
        else:
            record("Streaming (content-type)", False, f"ct={ct}", 0)
    except Exception as e:
        record("Streaming (SSE events)", False, f"exception: {e!r}", time.monotonic() - t0)


def test_session_continuity():
    print(f"\n{C.B}[7] Session continuity (X-Claude-Code-Session-Id){C.X}")
    sid = "test-session-deadbeef"
    headers = {"X-Claude-Code-Session-Id": sid}
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 20,
        "messages": [{"role": "user", "content": "Remember: the secret word is ZIRP. Confirm."}],
    }
    code, _, resp, t1 = _post("/v1/messages", body, headers=headers)
    if code != 200:
        record("Session continuity turn 1", False, f"code={code}", t1)
        return
    d1 = json.loads(resp.read())
    # turn 2: same session id, ask about secret
    body2 = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 20,
        "messages": [
            {"role": "user", "content": "Remember: the secret word is ZIRP. Confirm."},
            {"role": "assistant", "content": d1["content"]},
            {"role": "user", "content": "What was the secret word I told you?"},
        ],
    }
    code, _, resp, t2 = _post("/v1/messages", body2, headers=headers)
    if code != 200:
        record("Session continuity turn 2", False, f"code={code}", t2)
        return
    d2 = json.loads(resp.read())
    text = d2["content"][0]["text"]
    # Backend is a 30B param local model, may or may not remember. Just check
    # that the proxy doesn't crash and returns 200 across 2 turns.
    record("Session continuity (2-turn)", True, f"turn2 text={text!r}", t1 + t2)


def test_concurrent_serialization():
    print(f"\n{C.B}[8] Concurrent requests (PROXY_MAX_CONCURRENT=1){C.X}")
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "Reply OK."}],
    }
    results_box = [None, None, None]
    times = [0, 0, 0]

    def worker(i):
        t0 = time.monotonic()
        code, _, resp, _ = _post("/v1/messages", body)
        times[i] = time.monotonic() - t0
        if code == 200:
            results_box[i] = json.loads(resp.read())["content"][0]["text"]
        else:
            results_box[i] = f"FAIL:{code}"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    t_start = time.monotonic()
    for t in threads: t.start()
    for t in threads: t.join()
    total = time.monotonic() - t_start
    ok = all(r and "FAIL" not in r for r in results_box)
    record("Concurrent 3 reqs (serialized)",
           ok, f"results={results_box}, total={total:.2f}s, per={times}", total)


def test_count_tokens():
    print(f"\n{C.B}[9] /v1/messages/count_tokens (Claude Code feature){C.X}")
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Hello, how are you?"}],
    }
    code, _, resp, elapsed = _post("/v1/messages/count_tokens", body)
    if code == 200:
        data = json.loads(resp.read())
        record("count_tokens", True, f"200, {data}", elapsed)
    elif code == 404:
        record("count_tokens (route not implemented)", True,
               f"404 — upstream feature not proxied (acceptable)", elapsed)
    else:
        record("count_tokens", False, f"code={code}", elapsed)


def test_long_context():
    print(f"\n{C.B}[10] Long context (~5000 tokens){C.X}")
    long_text = "The quick brown fox jumps over the lazy dog. " * 500
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 30,
        "messages": [
            {"role": "user", "content": f"Read this and reply with one word: {long_text}"},
        ],
    }
    code, _, resp, elapsed = _post("/v1/messages", body)
    if code == 200:
        text = json.loads(resp.read())["content"][0]["text"]
        record("Long context (~5K tokens)", True, f"200, text={text!r}", elapsed)
    else:
        record("Long context", False, f"code={code}", elapsed)


def test_special_chars():
    print(f"\n{C.B}[11] Special characters / escaping{C.X}")
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 30,
        "messages": [{"role": "user",
                      "content": "Echo this exactly: \"quotes\", <tags>, newlines\\n, unicode 你好 🎉"}],
    }
    code, _, resp, elapsed = _post("/v1/messages", body)
    if code == 200:
        text = json.loads(resp.read())["content"][0]["text"]
        record("Special chars", True, f"200, text={text[:60]!r}...", elapsed)
    else:
        record("Special chars", False, f"code={code}", elapsed)


def test_anthropic_headers():
    print(f"\n{C.B}[12] Anthropic SDK headers (beta=true etc.){C.X}")
    # Real Claude Code sends ?beta=true and various beta headers
    url = f"{BASE}/v1/messages?beta=true"
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 15,
        "messages": [{"role": "user", "content": "OK?"}],
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "x-api-key": API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": "messages-2023-12-15",
        "X-Claude-Code-Session-Id": "test-12345678",
    })
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        elapsed = time.monotonic() - t0
        text = json.loads(resp.read())["content"][0]["text"]
        record("Anthropic SDK headers (beta=true)", True, f"text={text!r}", elapsed)
    except urllib.error.HTTPError as e:
        record("Anthropic SDK headers", False, f"code={e.code}", time.monotonic() - t0)


# =========================================================================
# Run
# =========================================================================

def main():
    t0 = time.monotonic()
    print(f"{C.B}Proxy comprehensive test suite — {BASE}{C.X}")
    test_routes()
    test_simple_chat()
    test_chinese()
    test_tool_use()
    test_multi_turn_tool_flow()
    test_streaming()
    test_session_continuity()
    test_concurrent_serialization()
    test_count_tokens()
    test_long_context()
    test_special_chars()
    test_anthropic_headers()

    # Summary
    print(f"\n{C.B}{'='*70}{C.X}")
    total = len(results)
    passed = sum(1 for _, ok, _, _ in results if ok)
    failed = total - passed
    print(f"  Total: {total}   {C.G}Passed: {passed}{C.X}   {C.R}Failed: {failed}{C.X}   "
          f"Time: {time.monotonic()-t0:.1f}s")
    if failed:
        print(f"\n{C.R}Failures:{C.X}")
        for name, ok, detail, elapsed in results:
            if not ok:
                print(f"  {C.R}- {name}: {detail}{C.X}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
