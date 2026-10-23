#!/usr/bin/env python3
"""DEF-313 探针：验证 tools 块是否位于 prompt 前缀、工具集抖动是否击穿缓存。

背景
----
Qwen3.6 chat template 把 tools 块渲染在 prompt **最前**：

    <|im_start|>system
    # Tools
    You have access to the following functions:
    <tools>{tool json}...</tools>
    If you choose to call a function ONLY reply ...
    <IMPORTANT>Reminder: ...</IMPORTANT>
    <|im_end|>
    <|im_start|>system   ← 真实 system 在 tools 之后
    ...

因此 tools 块是前缀头部：工具集/顺序/数量任一变化 → 整条前缀失效 →
全额冷 prefill（splash 后端 ≈786 tok/s）。

用法
----
    python3 tools/probe_tool_prefix_cache.py [--base http://127.0.0.1:8081]

期望结果（tools 块在缓存键内）
------------------------------
    同集重发 → 快（命中）；换名 / 多一个工具 → 慢（全冷）。
"""
import argparse
import json
import time
import urllib.request

DEFAULT_MODEL = "incoai/Qwen3.6-35B-A3B-Splash"


def mk_tools(names, desc_len=80):
    return [{
        "type": "function",
        "function": {
            "name": n,
            "description": "d" * desc_len,
            "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
        },
    } for n in names]


def call(base, model, tools, tag):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Say hi."}],
        "max_tokens": 4,
        "temperature": 0,
        "stream": False,
    }
    if tools is not None:
        body["tools"] = tools
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t = time.time()
    r = json.loads(urllib.request.urlopen(req, timeout=300).read())
    u = r.get("usage", {})
    dt = time.time() - t
    print(f"{tag:<24} prompt={u.get('prompt_tokens')} "
          f"cached={u.get('cached_tokens')} ({dt:.2f}s)")
    return dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8081")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    a = [f"tool_{i:02d}" for i in range(15)]
    b = [f"other_{i:02d}" for i in range(15)]
    c = a[:14] + ["zzz_extra"]

    call(args.base, args.model, None, "1 no-tools")
    call(args.base, args.model, None, "2 no-tools again")
    call(args.base, args.model, mk_tools(a), "3 tools A")
    call(args.base, args.model, mk_tools(a), "4 tools A again")
    call(args.base, args.model, mk_tools(b), "5 tools B (diff)")
    call(args.base, args.model, mk_tools(a), "6 tools A again")
    call(args.base, args.model, mk_tools(c), "7 tools A+1 extra")


if __name__ == "__main__":
    main()
