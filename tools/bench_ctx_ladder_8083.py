#!/usr/bin/env python3
"""
上下文长度 × 性能 阶梯压测(直连 rapid-mlx 后端)

测量不同输入上下文长度下的:
- TTFT(首 token 延迟,流式)
- prefill 速度 (prompt_tokens / TTFT)
- decode 吞吐 (completion_tokens / 生成时间)
- 冷/热对比:同 body 立即重发,第二遍吃前缀缓存
- 后端 RSS 峰值

阶梯覆盖 PFlash 阈值(32K tokens)两侧,可看到压缩拐点。

用法: python3 tools/bench_ctx_ladder.py [--max-tokens 256]
依赖: Python 3 stdlib only
"""

import argparse
import http.client
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from oom_ladder_test import generate_payload, RssSampler, BACKEND_LOG  # noqa: E402

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8083
CHARS_PER_TOKEN = 5.15  # 本机实测校准(oom_ladder 100K 步)


def detect_model():
    """从后端 /v1/models 自动识别当前加载的模型 ID"""
    try:
        conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=10)
        conn.request("GET", "/v1/models")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return data["data"][0]["id"]
    except Exception:
        return os.environ.get("BENCH_MODEL", "unknown")


MODEL = os.environ.get("BENCH_MODEL") or detect_model()

def get_backend_pid():
    """副本改动: 按 8083 端口找 pid(原版 pgrep 'rapid-mlx serve' 会抓到生产 8081)"""
    import subprocess as _sp
    try:
        r = _sp.run(["lsof", "-ti", "tcp:8083"], capture_output=True, text=True, timeout=5)
        line = r.stdout.strip().split("\n")[0].strip()
        return int(line) if line else None
    except Exception:
        return None

from oom_ladder_test import RssSampler, generate_payload  # noqa: E402 (原 import 兼容)


# 目标 prompt tokens 阶梯
LADDER = [512, 2_048, 4_096, 8_192, 16_384, 32_768]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def send_stream(payload_text, max_tokens, question=None):
    """流式请求,返回 (ttft_s, decode_tok_s, prompt_tokens, completion_tokens, ok, err)"""
    # 强制长输出的问题,保证 decode 吞吐有可统计的 token 数
    if question is None:
        question = ("请忽略上文内容,直接写一篇不少于500字的中文说明文,"
                    "系统介绍深度学习的基本概念、主要分支与典型应用。")
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": payload_text + "\n\n" + question}],
    }
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    t0 = time.time()
    ttft = None
    t_last = None
    prompt_tokens = completion_tokens = None
    err = None
    try:
        conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=1200)
        conn.request("POST", "/v1/chat/completions", body=raw,
                     headers={"Content-Type": "application/json",
                              "Authorization": "Bearer sk-1234"})
        resp = conn.getresponse()
        if resp.status != 200:
            return None, None, None, None, False, f"HTTP {resp.status}: {resp.read()[:200]}"
        buf = b""
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                for line in frame.split(b"\n"):
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        continue
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    usage = ev.get("usage")
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                        completion_tokens = usage.get("completion_tokens", completion_tokens)
                    choices = ev.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        if delta.get("content"):
                            now = time.time()
                            if ttft is None:
                                ttft = now - t0
                            t_last = now
        conn.close()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    if ttft is None:
        return None, None, prompt_tokens, completion_tokens, False, err or "no content"
    decode_time = (t_last - t0 - ttft) if t_last else 0
    decode_toks = (completion_tokens / decode_time) if (completion_tokens and decode_time > 0.05) else None
    return ttft, decode_toks, prompt_tokens, completion_tokens, True, err


def run_one(target_tokens, max_tokens, idx):
    """一档:先冷(nonce 破缓存)后热(同 body 重发)"""
    nonce = f"bench-{time.time_ns()}-{idx}"
    payload = generate_payload(int(target_tokens * CHARS_PER_TOKEN), nonce, seed=idx * 331 + 7)

    pid = get_backend_pid()
    sampler = RssSampler(pid) if pid else None
    if sampler:
        sampler.start()

    out = {"target": target_tokens}
    # 冷
    ttft, dtoks, ptok, ctok, ok, err = send_stream(payload, max_tokens)
    out.update(cold_ttft=ttft, cold_decode_toks=dtoks, prompt_tokens=ptok,
               completion_tokens=ctok, cold_ok=ok, cold_err=err)
    # 热(同 payload,前缀缓存应全命中)
    if ok:
        time.sleep(1)
        ttft2, dtoks2, ptok2, ctok2, ok2, err2 = send_stream(payload, max_tokens)
        out.update(warm_ttft=ttft2, warm_decode_toks=dtoks2, warm_ok=ok2, warm_err=err2)

    if sampler:
        sampler.stop()
        sampler.join(timeout=3)
        out["rss_peak_gb"] = round(sampler.peak_gb, 2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=256, help="每档生成 token 数")
    args = ap.parse_args()

    print("=" * 78)
    print("  上下文长度 × 性能 阶梯压测")
    print(f"  后端: http://{BACKEND_HOST}:{BACKEND_PORT}  模型: {MODEL}")
    print(f"  阶梯: {[f'{t//1024}K' if t >= 1024 else t for t in LADDER]}  生成: {args.max_tokens} tok/档")
    print("=" * 78, flush=True)

    if not get_backend_pid():
        print("❌ 后端未运行", file=sys.stderr)
        sys.exit(1)

    results = []
    for i, tgt in enumerate(LADDER):
        label = f"{tgt//1024}K" if tgt >= 1024 else str(tgt)
        log(f"STEP {label} ...")
        r = run_one(tgt, args.max_tokens, i)
        results.append(r)
        if r.get("cold_ok"):
            log(f"  冷: TTFT={r['cold_ttft']:.1f}s decode={r['cold_decode_toks'] or 0:.1f} tok/s "
                f"(prompt={r['prompt_tokens']})")
            if r.get("warm_ok"):
                log(f"  热: TTFT={r['warm_ttft']:.2f}s decode={r['warm_decode_toks'] or 0:.1f} tok/s")
        else:
            log(f"  ❌ {r.get('cold_err')}")
        time.sleep(4)

    print("\n" + "=" * 96)
    print(f"  {'上下文':>8} {'实际prompt':>10} {'冷TTFT':>9} {'prefill速度':>11} "
          f"{'冷decode':>9} {'热TTFT':>8} {'热decode':>9} {'RSS峰值':>8}")
    print("  " + "-" * 88)
    for r in results:
        tgt = f"{r['target']//1024}K" if r['target'] >= 1024 else str(r['target'])
        ptok = r.get("prompt_tokens")
        prefill = f"{ptok/r['cold_ttft']:.0f}/s" if (ptok and r.get("cold_ttft")) else "-"
        cold_ttft = f"{r['cold_ttft']:.1f}s" if r.get("cold_ttft") else "-"
        cold_dec = f"{r['cold_decode_toks']:.1f}/s" if r.get("cold_decode_toks") else "-"
        warm_ttft = f"{r['warm_ttft']:.2f}s" if r.get("warm_ttft") else "-"
        warm_dec = f"{r['warm_decode_toks']:.1f}/s" if r.get("warm_decode_toks") else "-"
        rss = f"{r['rss_peak_gb']}GB" if r.get("rss_peak_gb") else "-"
        print(f"  {tgt:>8} {ptok or '-':>10} {cold_ttft:>9} {prefill:>11} "
              f"{cold_dec:>9} {warm_ttft:>8} {warm_dec:>9} {rss:>8}")
    print("=" * 96)

    out_path = os.path.join(BACKEND_LOG and os.path.dirname(BACKEND_LOG),
                            f"bench-ctx-ladder-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    log(f"📝 结果已保存: {out_path}")


if __name__ == "__main__":
    main()
