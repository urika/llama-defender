#!/usr/bin/env python3
"""kapitsa 投机解码三配置对照测量（2026-10-29）

口径（对齐 kapitsa 官方 poster / 本仓既有记录）：
  - 4K prompt + 512 token 答案，T=0.6
  - 另测 short prompt（~100 tok）+ 512 答案
  - decode = output_tokens / (末 token 时间 - 首 token 时间)，TTFT = 首 token 时间 - 请求开始
只依赖标准库（urllib），SSE 逐行读。
用法: bench_kapitsa.py <base_url> <label> [repeats]
"""
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/v1"
LABEL = sys.argv[2] if len(sys.argv) > 2 else "run"
REPEATS = int(sys.argv[3]) if len(sys.argv) > 3 else 2

PARA = ("The reference note explains how prefix caching reuses KV state across turns "
        "and why speculative decoding turns one verify pass into several tokens. ")
LONG = PARA * 140      # ~4K tokens
SHORT = "请用两句话解释前缀缓存与投机解码的关系。"


def bench(prompt, max_tokens=512, stream=True):
    body = json.dumps({
        "model": "x",
        "messages": [{"role": "user", "content": prompt + "\n\n[nonce %f]" % time.time()}],
        "max_tokens": max_tokens,
        "temperature": 0.6,
        "stream": stream,
    }).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    ttft = None
    n_chunks = 0
    chars = 0
    usage_out = None
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage_out = obj["usage"].get("completion_tokens")
            for ch in (obj.get("choices") or []):
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    if ttft is None:
                        ttft = time.time() - t0
                    n_chunks += 1
                    chars += len(piece)
    total = time.time() - t0
    out = usage_out or n_chunks
    dec_window = max(total - (ttft or 0), 1e-6)
    return {"ttft": round(ttft or 0, 3), "total": round(total, 2),
            "out_tokens": out, "chars": chars,
            "decode_tps": round(out / dec_window, 1)}


def run_case(name, prompt, max_tokens=512):
    res = [bench(prompt, max_tokens) for _ in range(REPEATS)]
    best = max(res, key=lambda r: r["decode_tps"])
    avg = sum(r["decode_tps"] for r in res) / len(res)
    print(f"  [{LABEL}] {name:<22} decode {best['decode_tps']:>6.1f} tok/s "
          f"(avg {avg:.1f}) | TTFT {best['ttft']:.2f}s | out {best['out_tokens']} | "
          f"total {best['total']}s | n={REPEATS}")


if __name__ == "__main__":
    run_case("4K prompt / 512 out", LONG)
    run_case("short prompt / 512 out", SHORT)
