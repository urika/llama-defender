#!/usr/bin/env python3
"""probe_prefix_cache.py — DEF-308(L-13) 决定性小实验：prefix cache 行为探针。

目的：把「prefix cache 击穿」的三因从日志旁证升级为受控实验实证。
直连后端（绕过代理，排除代理状态干扰），共用生产后端 :8081，
**必须在 EXP 批跑空闲窗口运行**（会占用 _llama_lock，插队会污染实验）。

三组实验（各 N 轮连发，max_tokens=1，量测墙钟≈prefill 主导）：
  EXP1 递增   prompt 逐轮追加尾部块 → 预期整条 HIT（shared=前轮长度）
              → 证 hybrid 整条复用能力存在（非架构失效）
  EXP2 收缩   首轮全文，随后各轮把某个中部块替换为更短的占位
              → 预期 MISS 且日志 LCP unavailable shared≈前轮长度
              → 复现主因（视图收缩 × non_trimmable 整条匹配零容忍）
  EXP3 恒定   同一 prompt 原样连发 → 预期稳定 HIT
              → 对照组（排除模型加载/热身噪声）

证据源：响应墙钟 + llama-server.log 尾部 cache_fetch 行（脚本自动摘取）。
用法：
  python3 tools/probe_prefix_cache.py               # 全部三组
  python3 tools/probe_prefix_cache.py --rounds 4    # 每组轮数（默认 4）
  python3 tools/probe_prefix_cache.py --block-chars 8000
"""
import argparse
import json
import os
import subprocess
import time
import urllib.request

BACKEND = os.environ.get("PF_BACKEND", "http://127.0.0.1:8081/v1")
MODEL = os.environ.get("PF_MODEL", "")  # 空 = 后端默认模型


def _chat(prompt, max_tokens=1, timeout=600):
    body = {"max_tokens": max_tokens, "stream": False,
            "messages": [{"role": "user", "content": prompt}]}
    if MODEL:
        body["model"] = MODEL
    req = urllib.request.Request(
        BACKEND.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()
    return time.time() - t0


def _block(tag, chars):
    # 内容带轮次唯一词, 避免不同组间意外共享前缀语义
    lines = ["%s line %05d: %s" % (tag, i, "x" * 60) for i in range(chars // 76)]
    return "\n".join(lines)


def _tail_log_markers(n=6):
    """摘取 llama-server.log 尾部 cache_fetch 判定行（HIT/MISS/LCP）。"""
    try:
        out = subprocess.run(
            ["grep", "-E", "cache_fetch.*(HIT|MISS|LCP)", "logs/llama-server.log"],
            capture_output=True, text=True, timeout=10).stdout
        return [l[-110:] for l in out.strip().splitlines()[-n:]]
    except Exception:
        return []


def run_group(name, prompts, results):
    print("== %s ==" % name)
    for i, p in enumerate(prompts, 1):
        dt = _chat(p)
        results.append((name, i, len(p), dt))
        print("  round %d: prompt=%d chars  wall=%.1fs" % (i, len(p), dt))
    for line in _tail_log_markers(len(prompts)):
        print("  [backend] %s" % line)
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--block-chars", type=int, default=8000,
                    help="单块字符数（递增步长/收缩替换体量）")
    args = ap.parse_args()

    results = []
    base = _block("BASE", args.block_chars * 4)

    # EXP1 递增: base + 逐轮追加唯一尾部块
    inc = [base]
    for i in range(1, args.rounds):
        inc.append(base + "\n" + _block("INC%d" % i, args.block_chars))
    run_group("EXP1 递增（预期整条 HIT）", inc, results)

    # EXP2 收缩: 全文 + 逐轮把最后一个块换成更短的占位（视图缩短）
    shrink = [base]
    for i in range(1, args.rounds):
        cut = base[:len(base) - i * args.block_chars] + \
            _block("SHR%d" % i, 200)
        shrink.append(cut)
    run_group("EXP2 收缩（预期 MISS 复现主因）", shrink, results)

    # EXP3 恒定对照
    run_group("EXP3 恒定（对照）", [base] * args.rounds, results)

    print("== 汇总（组/轮/prompt chars/墙钟秒）==")
    for r in results:
        print("  %s r%d %d %.1f" % r)
    print("""
判读口径：
  EXP1 墙钟应显著低于首轮（整条 HIT, 增量 prefill）→ 非架构失效
  EXP2 各轮墙钟持续高企（MISS）→ 主因复现（收缩 × non_trimmable）
  EXP3 稳定 HIT → 对照组
三组结果与 llama-server.log cache_fetch 行交叉核对后回填 DEF-308。""")


if __name__ == "__main__":
    main()
