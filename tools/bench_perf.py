#!/usr/bin/env python3
"""
性能基准测试：TTFT / token/s / 并发 / 长上下文

测试流程（按顺序）:
  1. TTFT 基线：不同 context 大小下的冷启动和热启动 TTFT
  2. 生成速度：不同 max_tokens 下的 token/s
  3. 并发扩展：1/2/4 并发请求下的吞吐和延迟
  4. 长上下文：context 从 1K 增长到 40K tokens 的 TTFT 趋势
  5. 汇总报告

用法:
  python3 tools/bench_perf.py                        # 完整测试
  python3 tools/bench_perf.py --quick                 # 快速模式（仅核心场景）
  python3 tools/bench_perf.py --ttft-only             # 仅 TTFT
  python3 tools/bench_perf.py --speed-only            # 仅生成速度
  python3 tools/bench_perf.py --concurrency-only      # 仅并发
  python3 tools/bench_perf.py --long-ctx-only         # 仅长上下文
  python3 tools/bench_perf.py --override-concurrency=4  # 临时扩代理并发上限
  python3 tools/bench_perf.py --host http://localhost:4000

说明:
  --override-concurrency=N 临时覆盖 PROXY_MAX_CONCURRENT 配置值，
  用于测试超出当前代理限制的并发场景。不会修改配置文件。
"""

import urllib.request, json, time, sys, os, threading
from datetime import datetime

# ─── 配置 ────────────────────────────────────────────────
HOST = os.environ.get("LLAMA_HOST", "http://127.0.0.1:4000")
MODEL = "claude-sonnet-4-6"
HEADERS = {
    "Content-Type": "application/json",
    "x-api-key": "sk-1234",
    "anthropic-version": "2023-06-01",
}
API = f"{HOST}/v1/messages"

# 测试负载
SMALL_PROMPT = "Say hello in one sentence."
MEDIUM_PROMPT = """Write a detailed Python function that implements a binary search tree
with insert, delete, search, and traversal operations. Include comprehensive
docstrings and complexity analysis for each operation. The implementation
should handle edge cases like duplicate values and empty trees."""
LARGE_PROMPT = """Write a comprehensive technical document about the architecture and design
patterns used in modern distributed systems. Cover the following topics in detail:
1. Microservices vs Monolithic architectures (pros and cons)
2. Event-driven architecture patterns
3. CQRS and Event Sourcing
4. Circuit Breaker and Retry patterns
5. API Gateway patterns
6. Service Mesh architectures
7. Distributed tracing and observability
8. Data consistency models (strong vs eventual consistency)
9. Saga pattern for distributed transactions
10. Message queuing and stream processing

For each topic, provide real-world examples, code snippets where relevant,
and discuss the trade-offs involved in each architectural decision."""

LONG_CONTEXT = """This is a repeated context prefix used to simulate longer conversations. """ * 500


def req(messages, max_tokens=100):
    """发送请求并返回 (响应体, 耗时秒)"""
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
    }).encode()
    t0 = time.time()
    req = urllib.request.Request(API, data=body, headers=HEADERS, method="POST")
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
        elapsed = time.time() - t0
    except Exception as e:
        return {"error": str(e)}, time.time() - t0
    if "error" in resp:
        return resp, elapsed
    return resp, elapsed


def extract_ttft(resp_json):
    """从响应中提取 TTFT（近似为 elapsed 时间）"""
    return None  # 后端不返回 TTFT 字段，用 elapsed 近似


# ═══════════════════════════════════════════════════════════
#  1. TTFT 基准
# ═══════════════════════════════════════════════════════════

def test_ttft(quick=False):
    print("\n" + "=" * 60)
    print("  1. TTFT 基准测试")
    print("=" * 60)

    scenarios = [("small", SMALL_PROMPT), ("medium", MEDIUM_PROMPT)]
    if not quick:
        scenarios.append(("large", LARGE_PROMPT))
        # 长上下文测试
        scenarios.append(("5k_ctx", [{"role": "user", "content": LONG_CONTEXT[:5000] + "\n\nSummarize."}]))
        scenarios.append(("10k_ctx", [{"role": "user", "content": LONG_CONTEXT[:10000] + "\n\nSummarize."}]))
    
    results = []
    
    for name, prompt in scenarios:
        msgs = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
        
        # 冷启动
        resp, elapsed_cold = req(msgs, max_tokens=50)
        in_tok = resp.get("usage", {}).get("input_tokens", "?")
        out_tok = resp.get("usage", {}).get("output_tokens", "?")
        if "error" in resp:
            print(f"  ❌ [{name}] cold: ERROR {resp['error']}")
            continue
        
        # 热启动（精确相同请求，验证缓存）
        resp, elapsed_warm = req(msgs, max_tokens=50)
        in_tok_w = resp.get("usage", {}).get("input_tokens", "?")
        out_tok_w = resp.get("usage", {}).get("output_tokens", "?")
        
        ratio = f"{elapsed_warm/elapsed_cold*100:.0f}%" if elapsed_cold > 0 else "?"
        
        print(f"  [{name:>8}] cold={elapsed_cold:5.1f}s  warm={elapsed_warm:5.1f}s  "
              f"ratio={ratio}  in={in_tok}  out={out_tok}")
        
        results.append({
            "name": name,
            "cold_ttft": round(elapsed_cold, 2),
            "warm_ttft": round(elapsed_warm, 2),
            "ratio": round(elapsed_warm/elapsed_cold*100, 1) if elapsed_cold > 0 else 0,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
        })
    
    return results


# ═══════════════════════════════════════════════════════════
#  2. 生成速度 (token/s)
# ═══════════════════════════════════════════════════════════

def test_speed(quick=False):
    print("\n" + "=" * 60)
    print("  2. 生成速度 (token/s)")
    print("=" * 60)

    targets = [50, 200, 500] if not quick else [200]
    results = []

    for max_tok in targets:
        resp, elapsed = req([{"role": "user", "content": MEDIUM_PROMPT}], max_tokens=max_tok)
        if "error" in resp:
            print(f"  ❌ max_tokens={max_tok}: ERROR {resp['error']}")
            continue
        
        in_tok = resp.get("usage", {}).get("input_tokens", 0)
        out_tok = resp.get("usage", {}).get("output_tokens", 0)
        
        # 从 schedule log 预估 prefill 时间（~0.5s per 1K tokens for full prefill）
        # 生成时间 = elapsed - prefill_estimate
        # token/s = out_tok / gen_time
        # 保守：假设首 token 后剩余时间全为生成时间
        gen_speed = out_tok / max(elapsed - 1.0, 0.1) if elapsed > 1.0 else out_tok / elapsed
        
        print(f"  max_tokens={max_tok:>3}: {elapsed:5.1f}s  in={in_tok:>3}  out={out_tok:>3}  "
              f"gen≈{gen_speed:5.1f} tok/s")
        
        results.append({
            "max_tokens": max_tok,
            "elapsed": round(elapsed, 2),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "gen_speed": round(gen_speed, 1),
        })
    
    # 多轮递增测试（模拟 agentic 场景）
    print(f"\n  --- 多轮递增（agentic 模拟）{'快速' if quick else ''} ---")
    rounds = 3 if quick else 6
    conversation = [{"role": "user", "content": SMALL_PROMPT}]
    round_results = []
    
    for i in range(rounds):
        resp, elapsed = req(conversation, max_tokens=200)
        if "error" in resp:
            print(f"  ❌ Round {i}: ERROR {resp['error']}")
            break
        
        text = resp["content"][0]["text"]
        in_tok = resp.get("usage", {}).get("input_tokens", 0)
        out_tok = resp.get("usage", {}).get("output_tokens", 0)
        gen_speed = out_tok / max(elapsed - 1.5, 0.1) if elapsed > 1.5 else out_tok / elapsed
        
        # 检查是否有缓存命中
        cached = ""
        if i > 0:
            prev_elapsed = round_results[-1]["elapsed"]
            if elapsed < prev_elapsed * 0.8:
                cached = " 🟢 缓存"
            elif elapsed > prev_elapsed * 1.5:
                cached = " 🔴 降速"
        
        print(f"  Round {i}: {elapsed:5.1f}s  in={in_tok:>4}  out={out_tok:>3}  "
              f"gen≈{gen_speed:5.1f} tok/s{cached}")
        
        round_results.append({
            "round": i,
            "elapsed": round(elapsed, 2),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "gen_speed": round(gen_speed, 1),
        })
        
        conversation.append({"role": "assistant", "content": text})
        conversation.append({"role": "user", "content": f"Continue round {i}. Add more detail."})
    
    return results, round_results


# ═══════════════════════════════════════════════════════════
#  3. 并发扩展
# ═══════════════════════════════════════════════════════════

def _get_proxy_max_concurrent():
    """从 active.conf 读取代理并发上限"""
    try:
        with open("configs/active.conf") as f:
            for line in f:
                if line.startswith("PROXY_MAX_CONCURRENT="):
                    val = line.split("=", 1)[1].strip().strip('"')
                    return int(val)
    except Exception:
        pass
    return None


def test_concurrency(quick=False):
    print("\n" + "=" * 60)
    print("  3. 并发扩展性")
    print("=" * 60)
    
    # 检测代理并发上限
    proxy_limit = _get_proxy_max_concurrent()
    if proxy_limit is None:
        # 从 active.conf 实际路径尝试
        try:
            active = os.readlink("configs/active.conf")
            conf_path = f"configs/{active}"
            with open(conf_path) as f:
                for line in f:
                    if line.startswith("PROXY_MAX_CONCURRENT="):
                        val = line.split("=", 1)[1].strip().strip('"')
                        proxy_limit = int(val)
                        break
        except Exception:
            pass
    
    if proxy_limit is None:
        proxy_limit = 1  # local backend 默认值
    
    print(f"  检测到代理并发上限: PROXY_MAX_CONCURRENT={proxy_limit}")
    
    # 检查是否有 --override-concurrency 参数
    override = None
    for arg in sys.argv:
        if arg.startswith("--override-concurrency="):
            override = int(arg.split("=", 1)[1])
            print(f"  ⚠️  通过 --override-concurrency={override} 临时覆盖")
            break
    
    max_conc = override or proxy_limit
    
    # 确定测试级别
    if quick:
        levels = [1]
        if max_conc >= 2:
            levels.append(min(2, max_conc))
    else:
        levels = [1]
        if max_conc >= 2:
            levels.append(min(2, max_conc))
        if max_conc >= 4:
            levels.append(min(4, max_conc))
        if max_conc >= 8:
            levels.append(8)
    
    # 去重
    levels = sorted(set(l for l in levels if l <= max_conc))
    
    print(f"  测试并发数: {levels}")
    if max(levels) < max_conc:
        print(f"  ⚠️  最高测试 {max(levels)} 并发（代理上限 {max_conc}，如需更高请设 --override-concurrency=N）")
    if max(levels) == 1:
        print(f"  ⚠️  当前代理仅允许 1 并发，如需并发测试请修改 config 中 PROXY_MAX_CONCURRENT")
    
    results = []
    
    for conc in levels:
        print(f"\n  --- 并发={conc} {'(超出代理上限)' if conc > proxy_limit else ''}---")
        lock = threading.Lock()
        thread_results = []
        errors = 0
        rate_limited = 0
        
        def worker(idx):
            nonlocal errors, rate_limited
            try:
                resp, elapsed = req([{"role": "user", "content": MEDIUM_PROMPT}], max_tokens=200)
            except Exception as e:
                with lock:
                    errors += 1
                    thread_results.append({"idx": idx, "error": str(e), "elapsed": 0})
                return
            with lock:
                if "error" in resp:
                    err_msg = str(resp.get("error", {}))
                    if "429" in err_msg or "duplicate" in err_msg.lower() or "Too Many" in err_msg:
                        rate_limited += 1
                    else:
                        errors += 1
                    thread_results.append({"idx": idx, "error": err_msg, "elapsed": round(elapsed, 2)})
                else:
                    in_tok = resp.get("usage", {}).get("input_tokens", 0)
                    out_tok = resp.get("usage", {}).get("output_tokens", 0)
                    thread_results.append({
                        "idx": idx, "elapsed": round(elapsed, 2),
                        "in": in_tok, "out": out_tok,
                    })
        
        t0 = time.time()
        threads = []
        for i in range(conc):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        total_time = time.time() - t0
        
        # 统计
        latencies = [r["elapsed"] for r in thread_results if "elapsed" in r and r.get("error") is None]
        total_out = sum(r.get("out", 0) for r in thread_results if "out" in r)
        avg_lat = sum(latencies) / len(latencies) if latencies else 0
        p95_lat = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0
        throughput = total_out / total_time if total_time > 0 else 0
        success = conc - errors - rate_limited
        
        print(f"    总耗时: {total_time:.1f}s  请求: {conc}  成功: {success}  限流: {rate_limited}  错误: {errors}")
        if latencies:
            print(f"    平均延迟: {avg_lat:.1f}s  P95: {p95_lat:.1f}s")
            print(f"    总输出: {total_out} tokens  吞吐: {throughput:.1f} tok/s")
        for r in thread_results:
            if "error" in r:
                tag = "⛔限流" if "429" in str(r.get("error","")) else "❌错误"
                print(f"      [{r['idx']}] {tag} {r['error']}")
            else:
                print(f"      [{r['idx']}] {r['elapsed']:5.1f}s  in={r['in']:>3}  out={r['out']:>3}")
        
        results.append({
            "concurrency": conc,
            "total_time": round(total_time, 2),
            "avg_latency": round(avg_lat, 2) if latencies else None,
            "p95_latency": round(p95_lat, 2) if latencies else None,
            "throughput_tok_s": round(throughput, 1),
            "success": success,
            "rate_limited": rate_limited,
            "errors": errors,
            "proxy_limit": proxy_limit,
        })
    
    return results


# ═══════════════════════════════════════════════════════════
#  4. 长上下文性能
# ═══════════════════════════════════════════════════════════

def _build_context(tokens):
    """构建指定 token 数（近似）的 context。1 token ≈ 4 chars for English"""
    chars_needed = tokens * 4
    # 用多样化的句子避免纯重复
    lines = []
    for i in range(100):
        lines.append(f"Section {i}: This is a paragraph of text that provides context for the conversation. "
                     f"It contains various facts and figures about topic {i}. "
                     f"The number {i * 7} appears frequently in this discussion, "
                     f"along with references to item_{i % 50} and process_{i % 30}.")
    template = "\n\n".join(lines)
    repeats = max(1, chars_needed // len(template) + 1)
    full = (template + "\n\n") * repeats
    return full[:chars_needed]


def test_long_context(quick=False):
    print("\n" + "=" * 60)
    print("  4. 长上下文性能（TTFT vs Context Size）")
    print("=" * 60)

    # 测试上下文大小（token 估算）
    if quick:
        sizes = [1000, 5000]
    else:
        sizes = [1000, 5000, 10000, 20000, 40000, 100000, 200000]
    
    print(f"  测试上下文: {', '.join(f'{s//1000}K' if s >= 1000 else str(s) for s in sizes)}")
    print(f"  ⚠️  100K/200K 上下文需要 400K-800K 字符的请求体，可能触发 PROXY_PRE_TRUNCATE_CHARS=200000")
    print()
    
    results = []
    
    for ctx_tokens in sizes:
        context = _build_context(ctx_tokens)
        prompt = context + "\n\nBased on the above, what are the main themes discussed?"
        msgs = [{"role": "user", "content": prompt}]
        
        # 冷启动
        t0 = time.time()
        resp, elapsed_cold = req(msgs, max_tokens=50)
        ttft_cold = time.time() - t0
        if "error" in resp:
            print(f"  ❌ [{ctx_tokens:>5} tok] cold: ERROR {resp['error']}")
            continue
        
        in_tok = resp.get("usage", {}).get("input_tokens", 0)
        
        # 热启动（精确相同请求，验证长上下文缓存）
        resp, elapsed_warm = req(msgs, max_tokens=50)
        ttft_warm = time.time() - t0
        in_tok_w = resp.get("usage", {}).get("input_tokens", 0)
        
        ratio = f"{elapsed_warm/elapsed_cold*100:.0f}%" if elapsed_cold > 0 else "?"
        prefill_per_sec = f"{in_tok/elapsed_cold:.0f}" if elapsed_cold > 0 else "?"
        
        cache_mark = ""
        if elapsed_warm < elapsed_cold * 0.7:
            cache_mark = " 🟢 缓存命中"
        elif elapsed_warm > elapsed_cold * 1.2:
            cache_mark = " 🔴 降速"
        
        print(f"  [{ctx_tokens:>5} tok] cold={elapsed_cold:>5.1f}s  warm={elapsed_warm:>5.1f}s  "
              f"ratio={ratio}  in_tok={in_tok_w}{cache_mark}")
        
        if not quick:
            # 生成速度：让模型生成长输出
            resp2, elapsed_gen = req(msgs, max_tokens=200)
            out_tok = resp2.get("usage", {}).get("output_tokens", 0)
            gen_speed = out_tok / max(elapsed_gen - elapsed_cold, 0.1) if elapsed_gen > elapsed_cold else 0
            print(f"         gen: {elapsed_gen:>5.1f}s  out={out_tok:>3}  "
                  f"gen_speed≈{gen_speed:>5.1f} tok/s  "
                  f"prefill_speed={prefill_per_sec:>4} tok/s")
        else:
            gen_speed = 0
            prefill_per_sec = 0
        
        results.append({
            "context_tokens": ctx_tokens,
            "cold_ttft": round(elapsed_cold, 2),
            "warm_ttft": round(elapsed_warm, 2),
            "ratio": round(elapsed_warm/elapsed_cold*100, 1) if elapsed_cold > 0 else 0,
            "input_tokens": in_tok_w,
            "gen_speed": round(gen_speed, 1) if gen_speed else None,
            "prefill_speed": prefill_per_sec,
        })
    
    # TTFT vs Context Size 趋势图
    if len(results) >= 3:
        print(f"\n  --- TTFT 随 Context 增长趋势 ---")
        print(f"  {'Context(tok)':>14} {'冷TTFT':>8} {'热TTFT':>8} {'增长系数':>8}")
        for r in results:
            growth = "基准"
            if results[0]["cold_ttft"] > 0:
                ratio = round(r["cold_ttft"] / results[0]["cold_ttft"], 1)
                growth = f"{ratio:.1f}x"
            print(f"  {r['context_tokens']:>8} tok    {r['cold_ttft']:>6.1f}s   {r['warm_ttft']:>6.1f}s   {growth:>8}")
    
    return results


# ═══════════════════════════════════════════════════════════
#  报告
# ═══════════════════════════════════════════════════════════

def report(ttft, speed, concurrency, long_ctx=None):
    print("\n" + "=" * 60)
    print("  性能测试报告")
    print("=" * 60)
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  端点: {HOST}")
    print()
    
    if ttft:
        print("  ┌─ TTFT ──────────────────────────────────────┐")
        for r in ttft:
            ratio_s = f"({r['ratio']}% of cold)" if r['ratio'] else ""
            print(f"  │ {r['name']:>8}  cold={r['cold_ttft']:>5.1f}s  "
                  f"warm={r['warm_ttft']:>5.1f}s  {ratio_s:<20} │")
        print("  └──────────────────────────────────────────────┘")
        print()
    
    if speed:
        sp, rounds = speed
        if sp:
            print("  ┌─ 生成速度 ─────────────────────────────────┐")
            for r in sp:
                print(f"  │ max_tokens={r['max_tokens']:>3}  {r['elapsed']:>5.1f}s  "
                      f"in={r['input_tokens']:>3}  out={r['output_tokens']:>3}  "
                      f"{r['gen_speed']:>5.1f} tok/s  │")
            print("  └──────────────────────────────────────────────┘")
            print()
        
        if rounds:
            print("  ┌─ 多轮递增 ────────────────────────────────┐")
            for r in rounds:
                print(f"  │ Round {r['round']}: {r['elapsed']:>5.1f}s  "
                      f"in={r['input_tokens']:>4}  out={r['output_tokens']:>3}  "
                      f"{r['gen_speed']:>5.1f} tok/s  │")
            print("  └──────────────────────────────────────────────┘")
            print()
    
    if concurrency:
        print("  ┌─ 并发扩展 ──────────────────────────────────┐")
        print("  │ 并发数  总耗时   成功  限流   平均延迟  吞吐   │")
        for r in concurrency:
            s = r.get("success", r["concurrency"] - r.get("errors", 0))
            rl = r.get("rate_limited", 0)
            lat = f"{r['avg_latency']:>5.1f}s" if r.get("avg_latency") else "  N/A "
            tp = f"{r['throughput_tok_s']:>5.1f}" if r.get("throughput_tok_s") else "  N/A"
            print(f"  │  {r['concurrency']:>2}       {r['total_time']:>5.1f}s   "
                  f"{s:>2}   {rl:>2}    {lat}  {tp} tok/s  │")
        print("  └──────────────────────────────────────────────┘")
        print()
    
    if long_ctx:
        print("  ┌─ 长上下文 ──────────────────────────────────┐")
        print("  │ Context    冷TTFT   热TTFT   增长    Prefill速│")
        baseline = long_ctx[0]["cold_ttft"] if long_ctx else 0
        for r in long_ctx:
            growth = f"{r['cold_ttft']/baseline:.1f}x" if baseline > 0 else "-"
            pps = r.get("prefill_speed", "-")
            print(f"  │ {r['context_tokens']:>5} tok   {r['cold_ttft']:>6.1f}s  "
                  f"{r['warm_ttft']:>6.1f}s  {growth:>6}  {str(pps):>4}  │")
        print("  └──────────────────────────────────────────────┘")


def save_json(ttft, speed, concurrency, long_ctx=None):
    data = {
        "timestamp": datetime.now().isoformat(),
        "host": HOST,
        "ttft": ttft,
        "speed": {"single": speed[0] if speed else [], "multi_round": speed[1] if speed else []},
        "concurrency": concurrency,
        "long_context": long_ctx,
    }
    path = f"logs/perf-bench-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    os.makedirs("logs", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n  结果已保存: {path}")


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    quick = "--quick" in sys.argv
    ttft_only = "--ttft-only" in sys.argv
    speed_only = "--speed-only" in sys.argv
    conc_only = "--concurrency-only" in sys.argv
    long_ctx_only = "--long-ctx-only" in sys.argv
    
    # 覆盖 host
    for arg in sys.argv:
        if arg.startswith("--host="):
            HOST = arg.split("=", 1)[1]
            API = f"{HOST}/v1/messages"
    
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)
    
    print(f"性能基准测试 {'(快速模式)' if quick else ''}")
    print(f"{'='*60}")
    
    ttft = None
    speed = None
    concurrency = None
    long_ctx = None
    
    run_all = not (ttft_only or speed_only or conc_only or long_ctx_only)
    
    if ttft_only or run_all:
        ttft = test_ttft(quick=quick)
    
    if speed_only or run_all:
        speed = test_speed(quick=quick)
    
    if conc_only or run_all:
        concurrency = test_concurrency(quick=quick)
    
    if long_ctx_only or run_all:
        long_ctx = test_long_context(quick=quick)
    
    report(ttft, speed, concurrency, long_ctx)
    save_json(ttft, speed, concurrency, long_ctx)
