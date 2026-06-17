#!/usr/bin/env python3
"""多 Agent 并发测试 — 模拟 2-3 个 Claude Code 实例同时工作"""
import requests
import time
import statistics
import concurrent.futures

PROXY_URL = "http://127.0.0.1:4000/v1/messages"
HEADERS = {
    "Content-Type": "application/json",
    "x-api-key": "sk-1234",
    "anthropic-version": "2023-06-01",
}

# 模拟真实编码场景：不同 agent 做不同任务，上下文逐渐增长
AGENTS = [
    {"name": "Agent-A(重构)", "turns": 6, "ctx_chars": 2000,
     "tasks": ["重构 UserService 类", "添加单元测试", "修复类型错误", "提取接口", "优化查询", "更新文档"]},
    {"name": "Agent-B(Debug)", "turns": 6, "ctx_chars": 3000,
     "tasks": ["排查登录超时", "分析慢查询", "检查内存泄漏", "验证修复", "添加日志", "回归测试"]},
    {"name": "Agent-C(Feature)", "turns": 6, "ctx_chars": 1500,
     "tasks": ["实现分页功能", "添加过滤器", "写API文档", "处理边界条件", "性能优化", "代码审查"]},
]


def build_payload(agent, turn):
    task = agent["tasks"][turn % len(agent["tasks"])]
    ctx_padding = "// 前序对话上下文 " * (turn * agent["ctx_chars"] // 100)
    return {
        "model": "claude-sonnet-4-6",
        "max_tokens": 150,
        "temperature": 0.0,
        "messages": [
            {"role": "user", "content": f"{ctx_padding}\n任务: {task}\n请给出代码方案。"}
        ],
    }


def agent_session(agent, start_delay=0):
    """单个 agent 的完整会话（多轮对话）"""
    time.sleep(start_delay)
    results = []
    for turn in range(agent["turns"]):
        payload = build_payload(agent, turn)
        t0 = time.perf_counter()
        try:
            resp = requests.post(PROXY_URL, headers=HEADERS, json=payload, timeout=120)
            elapsed = time.perf_counter() - t0
            if resp.status_code == 200:
                data = resp.json()
                tokens = data.get("usage", {}).get("output_tokens", 0)
                results.append((True, elapsed, tokens, turn))
            else:
                results.append((False, elapsed, 0, turn))
        except Exception:
            results.append((False, time.perf_counter() - t0, 0, turn))
    return agent["name"], results


def run_multi_agent_test(n_agents):
    agents = AGENTS[:n_agents]
    print(f"\n{'='*60}")
    print(f"  {n_agents} 个 Agent 并发工作")
    print(f"{'='*60}")

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_agents) as executor:
        futures = [
            executor.submit(agent_session, agent, i * 0.5)
            for i, agent in enumerate(agents)
        ]
        all_results = {f.result()[0]: f.result()[1] for f in futures}

    total_elapsed = time.perf_counter() - t0
    print(f"\n  {'Agent':<18} {'成功率':<8} {'P50延迟':<10} {'P90延迟':<10} {'总tokens':<10}")
    print(f"  {'-'*54}")
    total_tokens = 0
    all_latencies = []
    for name, results in all_results.items():
        success = [r for r in results if r[0]]
        fail = [r for r in results if not r[0]]
        lats = [r[1] for r in success]
        tokens = sum(r[2] for r in success)
        total_tokens += tokens
        all_latencies.extend(lats)
        lats.sort()
        p50 = lats[len(lats)//2] if lats else 0
        p90 = lats[int(len(lats)*0.9)] if lats else 0
        print(f"  {name:<18} {len(success)}/{len(results):<5}  {p50:.2f}s{'':<5} {p90:.2f}s{'':<5} {tokens:<10}")
        if fail:
            for f in fail[:1]:
                print(f"    ❌ turn={f[3]} ({f[1]:.1f}s)")

    all_latencies.sort()
    print(f"\n  📊 系统汇总:")
    print(f"  总耗时: {total_elapsed:.1f}s")
    print(f"  总 tokens: {total_tokens}")
    print(f"  聚合吞吐: {total_tokens/total_elapsed:.1f} tok/s")
    if all_latencies:
        print(f"  全局 P50: {all_latencies[len(all_latencies)//2]:.2f}s")
        print(f"  全局 P90: {all_latencies[int(len(all_latencies)*0.9)]:.2f}s")

    return {
        "n_agents": n_agents,
        "total_elapsed": total_elapsed,
        "throughput": total_tokens / total_elapsed,
        "p50": all_latencies[len(all_latencies)//2] if all_latencies else 0,
        "success_rate": sum(1 for r in all_results.values() for x in r if x[0]) / max(1, sum(len(r) for r in all_results.values())),
    }


def main():
    print("=" * 60)
    print("  多 Agent 并发可行性测试")
    print(f"  目标: {PROXY_URL}")
    print(f"  模拟: Claude Code 编码会话（多轮对话 + 递增上下文）")
    print("=" * 60)

    print("\n⏳ 基准测试 (1 Agent)...")
    base = run_multi_agent_test(1)
    time.sleep(2)

    r2 = run_multi_agent_test(2)
    time.sleep(2)

    r3 = run_multi_agent_test(3)

    print(f"\n{'='*60}")
    print(f"  📊 对比")
    print(f"{'='*60}")
    print(f"  {'Agent数':<10} {'总耗时':<10} {'聚合吞吐':<12} {'P50延迟':<10} {'成功率':<8}")
    print(f"  {'-'*50}")
    for r in [base, r2, r3]:
        print(f"  {r['n_agents']:<10} {r['total_elapsed']:.1f}s{'':<5} {r['throughput']:.1f} tok/s{'':<4} {r['p50']:.2f}s{'':<5} {r['success_rate']*100:.0f}%")

    speedup_2 = base["total_elapsed"] * 2 / r2["total_elapsed"] if r2["total_elapsed"] > 0 else 0
    speedup_3 = base["total_elapsed"] * 3 / r3["total_elapsed"] if r3["total_elapsed"] > 0 else 0
    print(f"\n  💡 结论:")
    print(f"  2 Agent 并行效率: {speedup_2:.1%} (理想 100% = 无互相阻塞)")
    print(f"  3 Agent 并行效率: {speedup_3:.1%} (理想 100%)")


if __name__ == "__main__":
    main()
