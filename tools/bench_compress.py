#!/usr/bin/env python3
"""Benchmark _compress_middle_with_llm latency with realistic payloads."""
import json
import time
import urllib.request
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from anthropic_proxy import _compress_middle_with_llm, _merge_summaries_with_llm, LLAMA_BASE, MODEL_NAME

def _make_messages(n, chars_per_msg=500):
    """Generate realistic agentic coding messages."""
    msgs = []
    files = ["board.js", "game.py", "utils.ts", "config.json", "index.html",
             "style.css", "app.py", "models.py", "views.py", "test_board.py"]
    errors = [
        "TypeError: countLiberties is not a function",
        "ReferenceError: Canvas is not defined",
        "SyntaxError: unexpected token '}'",
        "AttributeError: 'NoneType' object has no attribute 'group'",
        "ImportError: No module named 'django.core'",
    ]

    for i in range(n):
        role = "assistant" if i % 2 == 0 else "user"
        fname = files[i % len(files)]
        err = errors[i % len(errors)]

        if role == "assistant":
            if i % 4 == 0:
                content = [
                    {"type": "text", "text": f"Let me read {fname} to understand the current implementation."},
                    {"type": "tool_use", "id": f"call_{i:04d}", "name": "Read",
                     "input": {"file_path": f"/project/src/{fname}"}}
                ]
            elif i % 4 == 1:
                content = [
                    {"type": "text", "text": f"I see the issue. The function needs to handle edge cases. Let me fix it."},
                    {"type": "tool_use", "id": f"call_{i:04d}", "name": "Edit",
                     "input": {"file_path": f"/project/src/{fname}", "old_string": "def handle():",
                               "new_string": "def handle(ctx=None):"}}
                ]
            elif i % 4 == 2:
                content = [
                    {"type": "text", "text": f"Running tests to verify the fix."},
                    {"type": "tool_use", "id": f"call_{i:04d}", "name": "Bash",
                     "input": {"command": f"cd /project && python -m pytest tests/test_{fname.replace('.','_')}.py -v"}}
                ]
            else:
                content = [
                    {"type": "text", "text": f"Found {err}. Need to add proper null checks before accessing properties."},
                    {"type": "tool_use", "id": f"call_{i:04d}", "name": "Write",
                     "input": {"file_path": f"/project/src/{fname}", "content": "x" * chars_per_msg}}
                ]
        else:
            code_content = "\n".join([f"  line_{j}: some_code_here();" for j in range(min(20, chars_per_msg // 25))])
            if i % 3 == 0:
                content = [
                    {"type": "tool_result", "tool_use_id": f"call_{i-1:04d}",
                     "content": f"// {fname} - full file content\n{code_content}\n// end of file"}
                ]
            elif i % 3 == 1:
                content = [
                    {"type": "tool_result", "tool_use_id": f"call_{i-1:04d}",
                     "content": f"PASS test_basic\nPASS test_edge\nFAIL test_error\n{err}\n1 failed, 2 passed"}
                ]
            else:
                content = [
                    {"type": "tool_result", "tool_use_id": f"call_{i-1:04d}",
                     "content": f"Error: {err}\n  at line 42 in {fname}\n  at processRequest (app.py:123)"}
                ]
        msgs.append({"role": role, "content": content})
    return msgs


def bench_compress_llm(msgs, label):
    try:
        conversation_text = []
        for msg in msgs:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            parts.append(b.get("text", "")[:300])
                        elif b.get("type") == "tool_use":
                            name = b.get("name", "")
                            inp = b.get("input", {})
                            parts.append(f"[tool:{name}({json.dumps(inp, ensure_ascii=False)[:200]})]")
                        elif b.get("type") == "tool_result":
                            tc = b.get("content", "")
                            if isinstance(tc, str):
                                parts.append(f"[result:{tc[:200]}]")
                text = " ".join(parts)
            elif isinstance(content, str):
                text = content[:300]
            else:
                continue
            conversation_text.append(f"{role}: {text}")

        conv_str = "\n".join(conversation_text)
        if len(conv_str) > 8000:
            conv_str = conv_str[:8000] + "...[truncated]"

        prompt = (
            "Summarize the following coding session into these XML sections. "
            "Be concise. Keep error messages verbatim. Keep file paths. Remove narration.\n\n"
            "<current_focus>What is being worked on (1-2 sentences)</current_focus>\n"
            "<errors_solutions>\n"
            "For each non-trivial error encountered, output ONE entry in this EXACT format:\n"
            "  - Error: <short verbatim error message or symptom>\n"
            "    Root cause: <why it happened — 1 sentence>\n"
            "    Fix: <what was done to resolve it — 1 sentence>\n"
            "    Avoidance: <what to verify next time to prevent recurrence — 1 sentence or 'N/A'>\n"
            "If no errors: output 'none'.\n"
            "</errors_solutions>\n"
            "<code_state>Current file states, key code signatures (function names, important constants)</code_state>\n"
            "<decisions>Architecture/design decisions and the reason behind each</decisions>\n"
            "<pending>Unfinished tasks, blockers, and what is needed to unblock each</pending>\n\n"
            f"Session log ({len(msgs)} messages):\n{conv_str}"
        )

        payload = {
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024,
            "temperature": 0.3,
            "stream": False,
        }
        t0 = time.monotonic()
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{LLAMA_BASE}/chat/completions",
            data=req_data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        elapsed = time.monotonic() - t0

        text = ""
        for choice in result.get("choices", []):
            msg = choice.get("message", {})
            text += msg.get("content", "")
        usage = result.get("usage", {})
        pt = usage.get("prompt_tokens", "?")
        ct = usage.get("completion_tokens", "?")
        result_len = len(text) if text.strip() else 0
        input_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)
        print(f"  {label}: {elapsed:.1f}s  prompt_tokens={pt}  completion_tokens={ct}  result={result_len}c  input={input_chars}c ({len(msgs)} msgs)")
        if text.strip():
            print(f"    preview: {text.strip()[:200]}...")
        return elapsed, text
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"  {label}: {elapsed:.1f}s  ERROR: {e}")
        return elapsed, None


def bench_merge_llm(old, new, label):
    prompt = (
        "Merge these two session summaries into one concise summary. "
        "Keep all errors, file states, and decisions. Remove redundancy.\n\n"
        f"<previous_summary>\n{old[:3000]}\n</previous_summary>\n\n"
        f"<new_summary>\n{new[:3000]}\n</new_summary>"
    )
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 800,
        "temperature": 0.3,
        "stream": False,
    }
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{LLAMA_BASE}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        elapsed = time.monotonic() - t0
        text = ""
        for choice in result.get("choices", []):
            msg = choice.get("message", {})
            text += msg.get("content", "")
        usage = result.get("usage", {})
        pt = usage.get("prompt_tokens", "?")
        ct = usage.get("completion_tokens", "?")
        print(f"  {label}: {elapsed:.1f}s  prompt_tokens={pt}  completion_tokens={ct}  result={len(text)}c")
        if text.strip():
            print(f"    preview: {text.strip()[:200]}...")
        return elapsed, text
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"  {label}: {elapsed:.1f}s  ERROR: {e}")
        return elapsed, None


def bench_raw_llm(prompt_chars, max_tokens, label):
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": "x" * prompt_chars + "\nSummarize this in 2 sentences."}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": False,
    }
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{LLAMA_BASE}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        elapsed = time.monotonic() - t0
        output = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = result.get("usage", {})
        pt = usage.get("prompt_tokens", "?")
        ct = usage.get("completion_tokens", "?")
        print(f"  {label}: {elapsed:.1f}s  prompt_tokens={pt}  completion_tokens={ct}  output={len(output)} chars")
        return elapsed
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"  {label}: {elapsed:.1f}s  ERROR: {e}")
        return elapsed


if __name__ == "__main__":
    print("=" * 70)
    print("LLM Compression Latency Benchmark")
    print(f"Backend: {LLAMA_BASE}")
    print("=" * 70)

    # Test 1: Raw LLM latency baseline
    print("\n--- Raw LLM baseline (no proxy processing) ---")
    bench_raw_llm(500, 256, "tiny (500c in, 256 out)")
    bench_raw_llm(2000, 512, "small (2Kc in, 512 out)")
    bench_raw_llm(4000, 1024, "medium (4Kc in, 1024 out)")
    bench_raw_llm(8000, 1024, "full (8Kc in, 1024 out)")

    # Test 2: _compress_middle_with_llm with different message counts
    print("\n--- _compress_middle_with_llm (actual function) ---")
    for n in [10, 20, 40, 60]:
        msgs = _make_messages(n, chars_per_msg=500)
        bench_compress_llm(msgs, f"n={n}")

    # Test 3: _merge_summaries_with_llm
    print("\n--- _merge_summaries_with_llm ---")
    old_summary = (
        "<current_focus>Building Go/Weiqi game board rendering</current_focus>\n"
        "<errors_solutions>\n"
        "  - Error: TypeError: countLiberties is not a function\n"
        "    Root cause: Method was defined as standalone function\n"
        "    Fix: Converted to class method on Board\n"
        "</errors_solutions>\n"
        "<code_state>board.js: Board class with placeStone, countLiberties, getGroup; game.py: GameController with makeMove</code_state>\n"
        "<decisions>Using Canvas API for board rendering, 19x19 grid with coordinate labels</decisions>\n"
        "<pending>Implement ko rule, add score calculation, integrate with UI</pending>"
    )
    new_summary = (
        "<current_focus>Adding ko rule and scoring to Go game</current_focus>\n"
        "<errors_solutions>\n"
        "  - Error: ReferenceError: Canvas is not defined\n"
        "    Root cause: Test running in Node.js without DOM\n"
        "    Fix: Added jsdom mock in test setup\n"
        "</errors_solutions>\n"
        "<code_state>board.js: Added ko detection; game.py: Added score calculation with territory counting</code_state>\n"
        "<decisions>Territory scoring using flood-fill algorithm, Japanese rules</decisions>\n"
        "<pending>UI integration, pass/resign buttons, game history</pending>"
    )
    bench_merge_llm(old_summary, new_summary, "merge (2 summaries ~1500c)")

    print("\n" + "=" * 70)
    print("Done.")
