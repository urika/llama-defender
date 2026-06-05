#!/usr/bin/env python3
"""
Agent 编程场景基准测试

模拟 Claude Code 的真实 agent 工作流，测试：
1. 代码理解（Read → 分析）
2. 代码搜索（Grep → 定位）
3. 代码编辑（Edit → 修改）
4. 代码生成（Write → 新文件）
5. 多轮工具调用链（Read → Edit → Bash）
6. 长上下文累积（模拟 rounds 策略前后对比）
7. Prefix cache 命中率（连续请求稳定性）

通过代理层 (Anthropic API → OpenAI) 发送，测试完整链路。

用法:
    python3 tools/bench_agent.py           # 完整测试
    python3 tools/bench_agent.py --quick   # 快速测试
    python3 tools/bench_agent.py --test cache  # 只跑 cache 测试
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 4000
BACKEND_PORT = 8081

MINI_TOOLS = [
    {
        "name": "Read",
        "description": "Read a file from the filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to read"},
                "offset": {"type": "integer", "description": "Line offset"},
                "limit": {"type": "integer", "description": "Max lines"}
            },
            "required": ["file_path"]
        }
    },
    {
        "name": "Write",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["file_path", "content"]
        }
    },
    {
        "name": "Edit",
        "description": "Edit a file by replacing strings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"}
            },
            "required": ["file_path", "old_string", "new_string"]
        }
    },
    {
        "name": "Bash",
        "description": "Run a bash command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "Grep",
        "description": "Search file contents with regex.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"}
            },
            "required": ["pattern"]
        }
    }
]

try:
    _real_body = json.load(open("/tmp/anthropic_request_body.json"))
    FULL_TOOLS = _real_body.get("tools", MINI_TOOLS)
except Exception:
    FULL_TOOLS = MINI_TOOLS

SYSTEM_PROMPT_SHORT = "You are a helpful coding assistant."
SYSTEM_PROMPT_AGENT = (
    "You are an interactive agent that helps users with software engineering tasks. "
    "You have access to tools for reading, writing, editing files and running commands. "
    "Use the instructions below and the tools available to you to assist the user."
)
SYSTEM_PROMPT_FULL = None  # Loaded from real request body


def load_real_system_prompt():
    global SYSTEM_PROMPT_FULL
    try:
        with open("/tmp/anthropic_request_body.json") as f:
            body = json.load(f)
        SYSTEM_PROMPT_FULL = body.get("system")
    except Exception:
        SYSTEM_PROMPT_FULL = None


def header(msg):
    print(f"\n{'=' * 70}", flush=True)
    print(f"  {msg}", flush=True)
    print(f"{'=' * 70}", flush=True)


def sub(msg):
    print(f"\n  --- {msg} ---", flush=True)


def log(msg):
    print(f"  {msg}", flush=True)


def send_anthropic_request(messages, tools=None, system=None, max_tokens=1024,
                           stream=True, expect_tool_use=False):
    body = {
        "model": "claude-sonnet-4-6",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.6,
        "stream": stream,
    }
    if tools:
        body["tools"] = tools
    if system:
        body["system"] = system

    url = f"http://{PROXY_HOST}:{PROXY_PORT}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": "sk-test",
        "anthropic-version": "2023-06-01",
    }

    t0 = time.time()
    ttft_ms = None
    full_content = ""
    tool_calls = []
    prompt_tokens = 0
    completion_tokens = 0
    stop_reason = ""

    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        total_ms = (time.time() - t0) * 1000
        return {"error": f"HTTP {e.code}: {err_body[:200]}"}, None, total_ms

    if stream:
        buffer = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buffer += chunk

            while b"\n" in buffer:
                line_end = buffer.find(b"\n")
                line = buffer[:line_end].strip()
                buffer = buffer[line_end + 1:]

                if not line or not line.startswith(b"data: "):
                    continue
                payload = line[6:]
                if payload == b"[DONE]":
                    continue

                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                evt_type = evt.get("type", "")

                if evt_type == "content_block_start":
                    if ttft_ms is None:
                        ttft_ms = (time.time() - t0) * 1000
                    block = evt.get("content_block", {})
                    if block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        })

                elif evt_type == "content_block_delta":
                    delta = evt.get("delta", {})
                    if delta.get("type") == "text_delta":
                        full_content += delta.get("text", "")
                    elif delta.get("type") == "input_json_delta" and tool_calls:
                        tool_calls[-1]["input_str"] = tool_calls[-1].get("input_str", "") + delta.get("partial_json", "")

                elif evt_type == "message_delta":
                    delta = evt.get("delta", {})
                    stop_reason = delta.get("stop_reason", "")
                    usage = evt.get("usage", {})
                    if usage.get("output_tokens"):
                        completion_tokens = usage["output_tokens"]
                    if usage.get("input_tokens"):
                        prompt_tokens = usage["input_tokens"]

                elif evt_type == "message_start":
                    msg = evt.get("message", {})
                    usage = msg.get("usage", {})
                    prompt_tokens = usage.get("input_tokens", 0)

                elif evt_type == "message_stop":
                    pass
    else:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        total_ms = (time.time() - t0) * 1000
        ttft_ms = total_ms
        full_content = ""
        for b in data.get("content", []):
            if b.get("type") == "text":
                full_content += b.get("text", "")
            elif b.get("type") == "tool_use":
                tool_calls.append(b)
        prompt_tokens = data.get("usage", {}).get("input_tokens", 0)
        completion_tokens = data.get("usage", {}).get("output_tokens", 0)
        stop_reason = data.get("stop_reason", "")
        return {
            "content": full_content,
            "tool_calls": tool_calls,
            "stop_reason": stop_reason,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }, ttft_ms, total_ms

    total_ms = (time.time() - t0) * 1000

    # 解析流式累积的 input_json_delta
    for tc in tool_calls:
        if "input_str" in tc and not tc.get("input"):
            try:
                tc["input"] = json.loads(tc["input_str"])
            except Exception:
                tc["input"] = {}

    return {
        "content": full_content,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }, ttft_ms, total_ms


def make_tool_result(content, tool_use_id="call_placeholder"):
    return {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }]
    }


def make_tool_call(name, arguments, call_id=None):
    import random
    if call_id is None:
        call_id = f"call_{random.randint(0, 0xffffff):06x}"
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": f"Using {name} to proceed."},
            {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
        ]
    }


SAMPLE_PYTHON_CODE = '''"""Simple task manager module."""
import json
from pathlib import Path
from datetime import datetime


class TaskManager:
    def __init__(self, storage_path="tasks.json"):
        self.storage_path = Path(storage_path)
        self.tasks = self._load()

    def _load(self):
        if self.storage_path.exists():
            with open(self.storage_path) as f:
                return json.load(f)
        return []

    def _save(self):
        with open(self.storage_path, "w") as f:
            json.dump(self.tasks, f, indent=2)

    def add_task(self, title, description="", priority="medium"):
        task = {
            "id": len(self.tasks) + 1,
            "title": title,
            "description": description,
            "priority": priority,
            "completed": False,
            "created_at": datetime.now().isoformat(),
        }
        self.tasks.append(task)
        self._save()
        return task

    def complete_task(self, task_id):
        for task in self.tasks:
            if task["id"] == task_id:
                task["completed"] = True
                task["completed_at"] = datetime.now().isoformat()
                self._save()
                return task
        return None

    def get_pending(self):
        return [t for t in self.tasks if not t["completed"]]

    def get_by_priority(self, priority):
        return [t for t in self.tasks if t["priority"] == priority]
'''


def test_code_understanding(repeat=1):
    header("测试 1: 代码理解 (Read → 分析)")
    results = []

    for i in range(repeat):
        messages = [
            {"role": "user", "content": f"Read and analyze this code. Explain what it does and identify any bugs:\n\n```\n{SAMPLE_PYTHON_CODE}\n```"}
        ]
        resp, ttft, total = send_anthropic_request(messages, max_tokens=500)
        if "error" in resp:
            log(f"  ❌ Error: {resp['error'][:100]}")
            continue
        quality = len(resp.get("content", "")) > 50
        log(f"  Round {i+1}: TTFT={ttft:.0f}ms total={total:.0f}ms "
            f"prompt={resp.get('prompt_tokens', '?')} output={resp.get('completion_tokens', '?')} "
            f"quality={'✅' if quality else '❌'}")
        results.append({"ttft": ttft, "total": total, "quality": quality,
                        "prompt_tokens": resp.get("prompt_tokens", 0),
                        "output_tokens": resp.get("completion_tokens", 0)})
        time.sleep(0.5)

    return results


def test_code_search(repeat=1):
    header("测试 2: 代码搜索 (模拟 Grep 工具调用)")
    results = []

    for i in range(repeat):
        messages = [
            {"role": "user", "content": "Search for all occurrences of 'datetime' in the codebase and list the files."},
            make_tool_call("Grep", {"pattern": "datetime", "path": "/Users/jinsongwang/APP/llama.cpp"}, "call_grep1"),
            make_tool_result("tools/bench_rapidmlx.py:2:from datetime import datetime\ntools/bench_agent.py:5:import datetime\nsrc/task_manager.py:7:from datetime import datetime", "call_grep1"),
        ]
        resp, ttft, total = send_anthropic_request(messages, tools=MINI_TOOLS, max_tokens=300)
        if "error" in resp:
            log(f"  ❌ Error: {resp['error'][:100]}")
            continue
        content = resp.get("content", "")
        quality = "datetime" in content.lower() and len(content) > 30
        log(f"  Round {i+1}: TTFT={ttft:.0f}ms total={total:.0f}ms "
            f"prompt={resp.get('prompt_tokens', '?')} output={resp.get('completion_tokens', '?')} "
            f"quality={'✅' if quality else '❌'}")
        results.append({"ttft": ttft, "total": total, "quality": quality,
                        "prompt_tokens": resp.get("prompt_tokens", 0),
                        "output_tokens": resp.get("completion_tokens", 0)})
        time.sleep(0.5)

    return results


def test_code_edit(repeat=1):
    header("测试 3: 代码编辑 (Edit → 修改)")
    results = []

    for i in range(repeat):
        messages = [
            {"role": "user", "content": "The TaskManager.add_task method has a bug: task IDs can collide if tasks are deleted. Fix it."},
            make_tool_call("Read", {"file_path": "src/task_manager.py"}, "call_read1"),
            make_tool_result(SAMPLE_PYTHON_CODE, "call_read1"),
        ]
        resp, ttft, total = send_anthropic_request(messages, tools=MINI_TOOLS, max_tokens=500)
        if "error" in resp:
            log(f"  ❌ Error: {resp['error'][:100]}")
            continue
        content = resp.get("content", "")
        quality = ("max" in content.lower() or "uuid" in content.lower() or "len" in content.lower()) and len(content) > 50
        log(f"  Round {i+1}: TTFT={ttft:.0f}ms total={total:.0f}ms "
            f"prompt={resp.get('prompt_tokens', '?')} output={resp.get('completion_tokens', '?')} "
            f"quality={'✅' if quality else '❌'}")
        results.append({"ttft": ttft, "total": total, "quality": quality,
                        "prompt_tokens": resp.get("prompt_tokens", 0),
                        "output_tokens": resp.get("completion_tokens", 0)})
        time.sleep(0.5)

    return results


def test_code_generation(repeat=1):
    header("测试 4: 代码生成 (Write → 新文件)")
    results = []

    for i in range(repeat):
        messages = [
            {"role": "user", "content": "Create a Python module 'cache_manager.py' that implements a TTL cache with: get(key), set(key, value, ttl), delete(key), cleanup(). Use only stdlib."}
        ]
        resp, ttft, total = send_anthropic_request(messages, max_tokens=800)
        if "error" in resp:
            log(f"  ❌ Error: {resp['error'][:100]}")
            continue
        content = resp.get("content", "")
        has_code = "def " in content and ("class " in content or "get" in content)
        has_ttl = "ttl" in content.lower() or "expire" in content.lower() or "time" in content.lower()
        quality = has_code and has_ttl
        log(f"  Round {i+1}: TTFT={ttft:.0f}ms total={total:.0f}ms "
            f"prompt={resp.get('prompt_tokens', '?')} output={resp.get('completion_tokens', '?')} "
            f"quality={'✅' if quality else '❌'} ({'code+ttl' if quality else 'missing ' + ('code ' if not has_code else '') + ('ttl ' if not has_ttl else '')})")
        results.append({"ttft": ttft, "total": total, "quality": quality,
                        "prompt_tokens": resp.get("prompt_tokens", 0),
                        "output_tokens": resp.get("completion_tokens", 0)})
        time.sleep(0.5)

    return results


def test_multi_turn_tool_chain(repeat=1):
    header("测试 5: 多轮工具调用链 (Read → Edit → Bash)")
    results = []

    for i in range(repeat):
        messages = [
            {"role": "user", "content": "Add error handling to the TaskManager._save method and verify it works by running the tests."},
            make_tool_call("Read", {"file_path": "src/task_manager.py"}, "call_r1"),
            make_tool_result(SAMPLE_PYTHON_CODE, "call_r1"),
            make_tool_call("Edit", {"file_path": "src/task_manager.py",
                                    "old_string": "    def _save(self):\n        with open(self.storage_path, \"w\") as f:\n            json.dump(self.tasks, f, indent=2)",
                                    "new_string": "    def _save(self):\n        try:\n            with open(self.storage_path, \"w\") as f:\n                json.dump(self.tasks, f, indent=2)\n        except (IOError, OSError) as e:\n            raise RuntimeError(f\"Failed to save tasks: {e}\") from e"},
             "call_e1"),
            make_tool_result("File edited successfully.", "call_e1"),
            make_tool_call("Bash", {"command": "python3 -c \"from task_manager import TaskManager; tm = TaskManager('/tmp/test_tm.json'); tm.add_task('test'); print('OK')\""}, "call_b1"),
            make_tool_result("OK", "call_b1"),
        ]
        resp, ttft, total = send_anthropic_request(messages, tools=MINI_TOOLS, max_tokens=300)
        if "error" in resp:
            log(f"  ❌ Error: {resp['error'][:100]}")
            continue
        content = resp.get("content", "")
        quality = len(content) > 20
        log(f"  Round {i+1}: TTFT={ttft:.0f}ms total={total:.0f}ms "
            f"prompt={resp.get('prompt_tokens', '?')} output={resp.get('completion_tokens', '?')} "
            f"quality={'✅' if quality else '❌'}")
        results.append({"ttft": ttft, "total": total, "quality": quality,
                        "prompt_tokens": resp.get("prompt_tokens", 0),
                        "output_tokens": resp.get("completion_tokens", 0)})
        time.sleep(0.5)

    return results


def test_long_context_with_rounds(rounds_count=5):
    header(f"测试 6: 长上下文累积 (模拟 {rounds_count} 轮 agent 对话)")
    results = []

    system = [{"type": "text", "text": SYSTEM_PROMPT_AGENT}]

    for turn in range(rounds_count):
        base_messages = [
            {"role": "user", "content": f"I need to build a REST API for a todo app. Let's start."},
        ]
        fake_history = []
        for r in range(turn):
            call_id = f"call_{r:04d}"
            fake_history.append(make_tool_call("Read", {"file_path": f"src/handler_{r}.py"}, call_id))
            fake_history.append(make_tool_result(f"# handler_{r}.py\nimport json\n\nclass Handler{r}:\n    pass\n", call_id))
            fake_history.append(make_tool_call("Edit", {"file_path": f"src/handler_{r}.py", "old_string": "pass", "new_string": f"def handle(self, request):\n    return json.dumps({{'status': 'ok', 'handler': {r}}})"}, f"call_e{r:04d}"))
            fake_history.append(make_tool_result("File edited successfully.", f"call_e{r:04d}"))
            fake_history.append(make_tool_call("Bash", {"command": f"python3 -m pytest tests/test_handler_{r}.py -v"}, f"call_b{r:04d}"))
            fake_history.append(make_tool_result(f"test_handler_{r}.py::test_handle PASSED\n1 passed in 0.01s", f"call_b{r:04d}"))

        current_msg = {"role": "user", "content": f"Now implement handler_{turn} with POST and DELETE endpoints."}
        all_messages = base_messages + fake_history + [current_msg]

        resp, ttft, total = send_anthropic_request(
            all_messages, tools=MINI_TOOLS, system=system,
            max_tokens=300, stream=True
        )

        if "error" in resp:
            log(f"  ❌ Turn {turn+1} error: {resp['error'][:100]}")
            continue

        msg_count = len(all_messages)
        prompt_tok = resp.get("prompt_tokens", 0)
        cached_approx = max(0, prompt_tok - 500)
        content = resp.get("content", "")
        quality = len(content) > 30 and ("def " in content or "POST" in content.upper() or "DELETE" in content.upper())

        log(f"  Turn {turn+1}: msgs={msg_count} prompt={prompt_tok} TTFT={ttft:.0f}ms "
            f"total={total:.0f}ms output={resp.get('completion_tokens', '?')} "
            f"quality={'✅' if quality else '❌'}")

        results.append({
            "turn": turn + 1,
            "msg_count": msg_count,
            "prompt_tokens": prompt_tok,
            "ttft_ms": ttft,
            "total_ms": total,
            "output_tokens": resp.get("completion_tokens", 0),
            "quality": quality,
        })
        time.sleep(1)

    if len(results) >= 2:
        first_ttft = results[0]["ttft_ms"]
        last_ttft = results[-1]["ttft_ms"]
        first_prompt = results[0]["prompt_tokens"]
        last_prompt = results[-1]["prompt_tokens"]
        log(f"\n  📊 长上下文 TTFT 变化: {first_ttft:.0f}ms → {last_ttft:.0f}ms "
            f"(prompt: {first_prompt} → {last_prompt})")

    return results


def test_prefix_cache_stability(rounds=5):
    header(f"测试 7: Prefix Cache 稳定性 ({rounds} 轮连续请求)")
    results = []

    system = SYSTEM_PROMPT_FULL if SYSTEM_PROMPT_FULL else [
        {"type": "text", "text": SYSTEM_PROMPT_AGENT}
    ]
    tools = FULL_TOOLS if len(FULL_TOOLS) > 5 else MINI_TOOLS

    base_msg = {"role": "user", "content": "List the files in the current directory."}

    for i in range(rounds):
        messages = [base_msg]
        resp, ttft, total = send_anthropic_request(
            messages, tools=tools, system=system,
            max_tokens=100, stream=True
        )

        if "error" in resp:
            log(f"  ❌ Round {i+1} error: {resp['error'][:100]}")
            continue

        prompt_tok = resp.get("prompt_tokens", 0)
        log(f"  Round {i+1}: TTFT={ttft:.0f}ms prompt={prompt_tok} "
            f"output={resp.get('completion_tokens', '?')}")
        results.append({
            "round": i + 1,
            "ttft_ms": ttft,
            "total_ms": total,
            "prompt_tokens": prompt_tok,
            "output_tokens": resp.get("completion_tokens", 0),
        })
        time.sleep(1)

    if len(results) >= 2:
        first_ttft = results[0]["ttft_ms"]
        cached_ttfts = [r["ttft_ms"] for r in results[1:]]
        avg_cached = sum(cached_ttfts) / len(cached_ttfts) if cached_ttfts else 0
        improvement = (1 - avg_cached / first_ttft) * 100 if first_ttft > 0 else 0
        log(f"\n  📊 Cache 效果: 首次={first_ttft:.0f}ms, 后续平均={avg_cached:.0f}ms, 改善={improvement:.0f}%")

    return results


def test_real_agent_scenario(rounds=3):
    header(f"测试 8: 真实 Agent 场景 (Claude Code 格式, {rounds} 轮)")

    with open("/tmp/anthropic_request_body.json") as f:
        real_body = json.load(f)

    system = real_body.get("system")
    tools = real_body.get("tools")

    results = []

    for i in range(rounds):
        short_msgs = real_body["messages"][:20]
        resp, ttft, total = send_anthropic_request(
            short_msgs, tools=tools, system=system,
            max_tokens=200, stream=True
        )

        if "error" in resp:
            log(f"  ❌ Round {i+1} error: {resp['error'][:100]}")
            continue

        prompt_tok = resp.get("prompt_tokens", 0)
        content = resp.get("content", "")
        quality = len(content) > 20

        log(f"  Round {i+1}: TTFT={ttft:.0f}ms total={total:.0f}ms "
            f"prompt={prompt_tok} output={resp.get('completion_tokens', '?')} "
            f"quality={'✅' if quality else '❌'}")
        results.append({
            "round": i + 1,
            "ttft_ms": ttft,
            "total_ms": total,
            "prompt_tokens": prompt_tok,
            "output_tokens": resp.get("completion_tokens", 0),
            "quality": quality,
        })
        time.sleep(1)

    if len(results) >= 2:
        first_ttft = results[0]["ttft_ms"]
        cached_ttfts = [r["ttft_ms"] for r in results[1:]]
        avg_cached = sum(cached_ttfts) / len(cached_ttfts) if cached_ttfts else 0
        improvement = (1 - avg_cached / first_ttft) * 100 if first_ttft > 0 else 0
        log(f"\n  📊 真实场景 Cache: 首次={first_ttft:.0f}ms, 后续平均={avg_cached:.0f}ms, 改善={improvement:.0f}%")

    return results


def test_tool_call_accuracy(repeat=3):
    header(f"测试 9: 工具调用准确性 ({repeat} 次)")

    test_cases = [
        {
            "name": "Read tool",
            "messages": [
                {"role": "user", "content": "Read the file /tmp/test.py and show me its contents."}
            ],
            "expect_tool": "Read",
            "expect_args": {"file_path": True},
        },
        {
            "name": "Bash tool",
            "messages": [
                {"role": "user", "content": "Run 'git status' in the current directory."}
            ],
            "expect_tool": "Bash",
            "expect_args": {"command": True},
        },
        {
            "name": "Edit tool",
            "messages": [
                {"role": "user", "content": "Use the Edit tool to replace 'hello' with 'world' in file /tmp/test.py. Do not read the file first."},
            ],
            "expect_tool": "Edit",
            "expect_args": {"file_path": True, "old_string": True, "new_string": True},
        }
    ]

    results = []
    for tc in test_cases:
        sub(tc["name"])
        success = 0
        for i in range(repeat):
            resp, ttft, total = send_anthropic_request(
                tc["messages"], tools=MINI_TOOLS, max_tokens=300,
                stream=True, expect_tool_use=True
            )
            if "error" in resp:
                log(f"    ❌ Error: {resp['error'][:80]}")
                continue

            content = resp.get("content", "")
            tool_calls = resp.get("tool_calls", [])
            has_expected_tool = any(
                tc["expect_tool"].lower() == t.get("name", "").lower()
                for t in tool_calls
            )
            # 也检查参数是否包含预期字段
            matched_tool = next(
                (t for t in tool_calls if tc["expect_tool"].lower() == t.get("name", "").lower()),
                None
            )
            if matched_tool and tc.get("expect_args"):
                tool_input = matched_tool.get("input", {})
                if not tool_input and "input_str" in matched_tool:
                    try:
                        tool_input = json.loads(matched_tool["input_str"])
                    except Exception:
                        tool_input = {}
                has_expected_tool = has_expected_tool and all(
                    k in tool_input for k in tc["expect_args"]
                )

            display = '✅' if has_expected_tool else '❌'
            if not has_expected_tool:
                if tool_calls:
                    names = [t.get('name', '?') for t in tool_calls]
                    display += f" (got tools: {names}, content: {content[:40]!r})"
                else:
                    display += f" (got content: {content[:60]!r})"

            log(f"    Run {i+1}: TTFT={ttft:.0f}ms output={resp.get('completion_tokens', '?')} "
                f"tool_call={display}")
            if has_expected_tool:
                success += 1
            time.sleep(0.5)

        rate = success / repeat * 100
        log(f"    📊 准确率: {success}/{repeat} ({rate:.0f}%)")
        results.append({"test": tc["name"], "success": success, "total": repeat, "rate": rate})

    return results


def print_summary(all_results):
    header("📊 测试汇总")

    print(f"\n  {'测试':<25} {'TTFT(ms)':<12} {'Prompt':<10} {'Output':<10} {'质量':<8} {'说明'}")
    print(f"  {'-' * 80}")

    for key, data in all_results.items():
        if isinstance(data, list) and data and isinstance(data[0], dict):
            ttfts = [r.get("ttft_ms", r.get("ttft", 0)) for r in data if r.get("ttft_ms") or r.get("ttft")]
            prompts = [r.get("prompt_tokens", 0) for r in data]
            outputs = [r.get("output_tokens", r.get("completion_tokens", 0)) for r in data]
            qualities = [r.get("quality", False) for r in data if "quality" in r]
            quality_rate = f"{sum(qualities)/len(qualities)*100:.0f}%" if qualities else "N/A"

            avg_ttft = sum(ttfts) / len(ttfts) if ttfts else 0
            avg_prompt = sum(prompts) / len(prompts) if prompts else 0
            avg_output = sum(outputs) / len(outputs) if outputs else 0

            first_ttft = ttfts[0] if ttfts else 0
            avg_later = sum(ttfts[1:]) / len(ttfts[1:]) if len(ttfts) > 1 else 0
            cache_info = f"cache {(1-avg_later/first_ttft)*100:.0f}%" if first_ttft > 0 and avg_later > 0 else ""

            print(f"  {key:<25} {avg_ttft:<12.0f} {avg_prompt:<10.0f} {avg_output:<10.0f} {quality_rate:<8} {cache_info}")

    total_tests = sum(len(v) for v in all_results.values() if isinstance(v, list))
    total_ok = sum(1 for v in all_results.values() if isinstance(v, list)
                   for r in v if r.get("quality") is True)
    log(f"\n  总计: {total_tests} 次请求, {total_ok} 次质量达标 "
        f"({total_ok/total_tests*100:.0f}%)" if total_tests > 0 else "")


def main():
    parser = argparse.ArgumentParser(description="Agent 编程场景基准测试")
    parser.add_argument("--quick", action="store_true", help="快速模式")
    parser.add_argument("--test", default="all",
                        choices=["understand", "search", "edit", "generate",
                                 "multiturn", "longctx", "cache", "real", "tools",
                                 "all"],
                        help="指定测试项")
    args = parser.parse_args()

    repeat = 1 if args.quick else 2
    rounds = 3 if args.quick else 5

    load_real_system_prompt()

    header("Agent 编程场景基准测试")
    log(f"代理: {PROXY_HOST}:{PROXY_PORT}")
    log(f"快速模式: {'是' if args.quick else '否'}")
    log(f"工具数量: {len(FULL_TOOLS)} (full), {len(MINI_TOOLS)} (mini)")

    all_results = {}

    tests = {
        "understand": (test_code_understanding, {"repeat": repeat}),
        "search": (test_code_search, {"repeat": repeat}),
        "edit": (test_code_edit, {"repeat": repeat}),
        "generate": (test_code_generation, {"repeat": repeat}),
        "multiturn": (test_multi_turn_tool_chain, {"repeat": repeat}),
        "longctx": (test_long_context_with_rounds, {"rounds_count": rounds}),
        "cache": (test_prefix_cache_stability, {"rounds": rounds}),
        "real": (test_real_agent_scenario, {"rounds": repeat + 1}),
        "tools": (test_tool_call_accuracy, {"repeat": repeat}),
    }

    if args.test == "all":
        for name, (fn, kwargs) in tests.items():
            all_results[name] = fn(**kwargs)
    else:
        fn, kwargs = tests[args.test]
        all_results[args.test] = fn(**kwargs)

    print_summary(all_results)

    result_file = os.path.join(REPO_ROOT, "logs", f"bench-agent-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(result_file), exist_ok=True)
    with open(result_file, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    log(f"\n📝 结果已保存: {result_file}")

    print("\n✅ 测试完成", flush=True)


if __name__ == "__main__":
    main()
