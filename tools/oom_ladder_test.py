#!/usr/bin/env python3
"""
OOM 边界阶梯测试 — 绕过代理直连 rapid-mlx 后端

与 context_stress_test.py 的区别:
- 直连后端(默认 8081),不经过代理的 80K 字符截断和云端路由,
  测的是模型/引擎的真实 OOM 边界
- 阶梯式递增输入 token 数,每步记录实际 prompt_tokens、耗时、RSS 峰值
- 每步请求开头注入唯一 nonce,破坏前缀缓存,确保全额 prefill
- 出现 HTTP 错误 / 进程消失 / Metal OOM 日志特征即停

用法:
    python3 tools/oom_ladder_test.py                # 完整阶梯
    python3 tools/oom_ladder_test.py --max-tokens-step 140000  # 只测到 140K

依赖: Python 3 stdlib only
"""

import argparse
import http.client
import json
import os
import random
import subprocess
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
BACKEND_LOG = os.path.join(REPO_ROOT, "logs", "llama-server.log")

BACKEND_HOST = os.environ.get("LADDER_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("LADDER_PORT", "8081"))

# 阶梯:目标 prompt tokens。60K 起步(约等于现行代理 80K chars 上限的实际量级)
LADDER = [60_000, 100_000, 140_000, 180_000, 220_000, 250_000]

REQUEST_TIMEOUT = 900          # 单步超时(秒),长 prefill 很慢
MAX_OUTPUT_TOKENS = 16         # 只验证能生成,重点在 prefill 内存
COOLDOWN_BETWEEN_STEPS = 5     # 秒

WORDS = (
    "system memory cache kernel vector matrix tensor pipeline buffer stream "
    "thread socket packet frame block cluster shard replica ledger anchor "
    "signal noise filter probe sensor relay bridge router gateway module "
    "driver engine parser compiler linker loader runtime sandbox container "
    "silver copper iron quartz cedar maple river harbor meadow valley "
    "obsidian granite basalt marble ember cinder frost bloom thorn petal"
).split()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def generate_payload(target_chars: int, nonce: str, seed: int) -> str:
    """生成指定字符数、内容随机的文本(防前缀缓存命中)"""
    rng = random.Random(seed)
    parts = [f"nonce-{nonce}\n"]
    count = len(parts[0])
    section = 1
    while count < target_chars:
        words = " ".join(rng.choice(WORDS) for _ in range(60))
        part = f"\n## Section {section} (id={rng.randrange(10**9)})\n{words}\n"
        parts.append(part)
        count += len(part)
        section += 1
    text = "".join(parts)
    return text[:target_chars]


def get_backend_pid():
    try:
        r = subprocess.run(["pgrep", "-f", "rapid-mlx serve"],
                           capture_output=True, text=True, timeout=5)
        line = r.stdout.strip().split("\n")[0].strip()
        return int(line) if line else None
    except Exception:
        return None


def get_rss_gb(pid):
    try:
        r = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip()) / (1024 ** 2)
    except Exception:
        return None


class RssSampler(threading.Thread):
    """后台线程:每秒采样后端进程 RSS,记录峰值"""
    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid = pid
        self.peak_gb = 0.0
        self.current_gb = 0.0
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            v = get_rss_gb(self.pid)
            if v is not None:
                self.current_gb = v
                self.peak_gb = max(self.peak_gb, v)
            self._stop_event.wait(1.0)

    def stop(self):
        self._stop_event.set()


def read_new_log_lines(offset):
    """读取日志文件 offset 之后的新内容"""
    try:
        with open(BACKEND_LOG, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", errors="replace"), os.path.getsize(BACKEND_LOG)
    except Exception:
        return "", offset


OOM_SIGNATURES = ("Insufficient Memory", "[METAL]", "command buffer", "kIOGPUCommandBuffer")


def send_step(target_tokens, chars_per_token, step_idx):
    """发送一级阶梯请求,返回结果 dict"""
    nonce = f"{time.time_ns()}-{step_idx}"
    target_chars = int(target_tokens * chars_per_token)
    payload = generate_payload(target_chars, nonce, seed=step_idx * 7919 + 13)
    body = {
        "model": "qwen3.8-27b-4bit",
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
        "messages": [{
            "role": "user",
            "content": payload + "\n\nReply with exactly: OK",
        }],
    }
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    log_offset = os.path.getsize(BACKEND_LOG) if os.path.exists(BACKEND_LOG) else 0
    pid = get_backend_pid()
    sampler = RssSampler(pid) if pid else None
    if sampler:
        sampler.start()

    t0 = time.time()
    result = {
        "target_tokens": target_tokens,
        "request_bytes": len(raw),
        "rss_before_gb": get_rss_gb(pid) if pid else None,
    }
    try:
        conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=REQUEST_TIMEOUT)
        conn.request("POST", "/v1/chat/completions", body=raw,
                     headers={"Content-Type": "application/json",
                              "Authorization": "Bearer sk-1234"})
        resp = conn.getresponse()
        resp_body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        result["http_status"] = resp.status
        result["total_s"] = round(time.time() - t0, 1)
        if resp.status == 200:
            data = json.loads(resp_body)
            usage = data.get("usage", {})
            result["prompt_tokens"] = usage.get("prompt_tokens")
            result["completion_tokens"] = usage.get("completion_tokens")
            result["success"] = True
        else:
            result["success"] = False
            result["error_body"] = resp_body[:500]
    except Exception as e:
        result["total_s"] = round(time.time() - t0, 1)
        result["success"] = False
        result["error_body"] = f"{type(e).__name__}: {e}"
    finally:
        if sampler:
            sampler.stop()
            sampler.join(timeout=3)
            result["rss_peak_gb"] = round(sampler.peak_gb, 2)
            result["rss_after_gb"] = round(sampler.current_gb, 2)

    # 检查本步期间日志是否出现 Metal OOM 特征
    new_log, _ = read_new_log_lines(log_offset)
    hits = [s for s in OOM_SIGNATURES if s in new_log]
    result["metal_oom_in_log"] = bool(hits)
    if hits:
        result["success"] = False
    # 进程是否还活着/换 PID
    result["pid_alive"] = get_backend_pid() == pid if pid else None
    return result


def main():
    ap = argparse.ArgumentParser(description="OOM 边界阶梯测试(直连后端)")
    ap.add_argument("--max-tokens-step", type=int, default=LADDER[-1],
                    help="阶梯上限(默认 250000)")
    args = ap.parse_args()

    ladder = [t for t in LADDER if t <= args.max_tokens_step]

    print("=" * 60)
    print("  OOM 边界阶梯测试 — 直连 rapid-mlx 后端")
    print(f"  后端: http://{BACKEND_HOST}:{BACKEND_PORT}")
    print(f"  阶梯: {[f'{t//1000}K' for t in ladder]}")
    print(f"  单步超时: {REQUEST_TIMEOUT}s,输出上限: {MAX_OUTPUT_TOKENS} tok")
    print("=" * 60, flush=True)

    # Preflight
    pid = get_backend_pid()
    if not pid:
        print("❌ 找不到 rapid-mlx serve 进程", file=sys.stderr)
        sys.exit(1)
    try:
        conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=10)
        conn.request("GET", "/v1/models")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        log(f"✅ 后端正常 (PID {pid}, RSS {get_rss_gb(pid):.1f}GB)")
    except Exception as e:
        print(f"❌ 后端不可用: {e}", file=sys.stderr)
        sys.exit(1)

    results = []
    chars_per_token = 4.0   # 初始估计,每步按实际 prompt_tokens 校准
    boundary = None
    for i, target in enumerate(ladder):
        log(f"STEP 目标 {target//1000}K tokens (约 {int(target*chars_per_token)//1000}K chars)...")
        r = send_step(target, chars_per_token, i)
        results.append(r)

        if r.get("prompt_tokens"):
            chars_per_token = r["request_bytes"] / r["prompt_tokens"]  # 校准

        if r["success"]:
            log(f"  ✅ prompt={r['prompt_tokens']} tok, 耗时={r['total_s']}s, "
                f"RSS峰值={r.get('rss_peak_gb')}GB")
        else:
            log(f"  ❌ 失败: status={r.get('http_status')}, "
                f"metal_oom={r['metal_oom_in_log']}, 进程存活={r.get('pid_alive')}")
            log(f"  错误: {r.get('error_body', '')[:200]}")
            boundary = results[-2]["prompt_tokens"] if len(results) > 1 and results[-2].get("prompt_tokens") else None
            break
        time.sleep(COOLDOWN_BETWEEN_STEPS)

    print("\n" + "=" * 60)
    print("  📊 阶梯测试结果")
    print(f"  {'目标':>8} {'实际prompt':>12} {'耗时':>8} {'RSS峰值':>10} {'Metal特征':>8}")
    print("  " + "-" * 56)
    for r in results:
        tgt = f"{r['target_tokens']//1000}K"
        pt = str(r.get("prompt_tokens") or "-")
        dur = f"{r['total_s']}s"
        peak = f"{r.get('rss_peak_gb', '-')}GB"
        oom = "⚠️" if r["metal_oom_in_log"] else ("✅" if r["success"] else "❌")
        print(f"  {tgt:>8} {pt:>12} {dur:>8} {peak:>10} {oom:>8}")
    print("=" * 60)

    if boundary:
        print(f"\n  🔴 OOM/失败边界: 最后成功 prompt ≈ {boundary} tokens")
    elif results and all(r["success"] for r in results):
        print(f"\n  🟢 全部通过,未触边界(最高 {results[-1].get('prompt_tokens')} tokens)")

    out = os.path.join(REPO_ROOT, "logs",
                       f"oom-ladder-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    log(f"📝 结果已保存: {out}")

    sys.exit(0 if all(r["success"] for r in results) else 1)


if __name__ == "__main__":
    main()
