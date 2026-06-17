#!/usr/bin/env python3
"""Proxy 并发压力测试 — 评估 PROXY_MAX_CONCURRENT 最优值"""
import requests
import time
import statistics
import concurrent.futures
import sys
import json

PROXY_URL = "http://127.0.0.1:4000/v1/messages"
HEADERS = {
    "Content-Type": "application/json",
    "x-api-key": "sk-1234",
    "anthropic-version": "2023-06-01",
}

# 测试参数
CONCURRENCY_LEVELS = [1, 2, 4, 6, 8, 12, 16]
PAYLOAD_TEMPLATE = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 100,
    "temperature": 0.0,
    "messages": [{"role": "user", "content": "用中文简短回答：什么是{TOPIC}？"}],
}
# 不同话题确保每次请求 payload 不同，避免代理去重
TOPICS = [
    "快速排序", "二分查找", "哈希表", "动态规划",
    "深度优先搜索", "广度优先搜索", "贪心算法", "回溯算法",
    "链表", "二叉树", "堆栈", "队列", "图论",
    "字符串匹配", "拓扑排序", "最短路径", "最小生成树",
    "并查集", "前缀树", "布隆过滤器",
]


def single_request(seq_id=0, timeout=120):
    """单次请求，返回 (success, latency_s, output_tokens, error_msg)"""
    topic = TOPICS[seq_id % len(TOPICS)]
    payload = {
        **PAYLOAD_TEMPLATE,
        "messages": [{"role": "user", "content": f"用中文简短回答：什么是{topic}？"}],
    }
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            PROXY_URL,
            headers=HEADERS,
            json=payload,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - t0
        if resp.status_code == 200:
            data = resp.json()
            tokens = 0
            if "usage" in data:
                tokens = data["usage"].get("output_tokens", 0)
            elif "content" in data:
                for block in data.get("content", []):
                    tokens += len(block.get("text", "").split())
            return (True, elapsed, tokens, None)
        else:
            return (False, elapsed, 0, f"HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return (False, elapsed, 0, str(e)[:100])


def run_concurrency_test(n_workers, n_requests=20):
    """指定并发数运行测试"""
    print(f"\n{'='*60}")
    print(f"  并发数: {n_workers}, 总请求: {n_requests}")
    print(f"{'='*60}")

    results = []
    t0 = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(single_request, i) for i in range(n_requests)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    total_elapsed = time.perf_counter() - t0

    successes = [r for r in results if r[0]]
    failures = [r for r in results if not r[0]]
    latencies = [r[1] for r in successes]
    total_tokens = sum(r[2] for r in successes)

    print(f"  成功率: {len(successes)}/{n_requests} ({100*len(successes)/n_requests:.0f}%)")
    if failures:
        print(f"  ❌ 失败: {len(failures)}")
        for f in failures[:3]:
            print(f"     {f[3][:80]}")
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p90 = latencies[int(len(latencies) * 0.9)]
        p99 = latencies[int(len(latencies) * 0.99)]
        print(f"  延迟 P50: {p50:.2f}s  P90: {p90:.2f}s  P99: {p99:.2f}s  Max: {max(latencies):.2f}s")
        print(f"  吞吐: {total_tokens / total_elapsed:.1f} tok/s  |  {len(successes) / total_elapsed:.2f} req/s")
        print(f"  平均单请求: {statistics.mean(latencies):.2f}s")

    return {
        "concurrency": n_workers,
        "success_rate": len(successes) / n_requests,
        "p50": latencies[len(latencies) // 2] if latencies else None,
        "p90": latencies[int(len(latencies) * 0.9)] if latencies else None,
        "p99": latencies[int(len(latencies) * 0.99)] if latencies else None,
        "max_latency": max(latencies) if latencies else None,
        "throughput_tps": total_tokens / total_elapsed if total_elapsed > 0 else 0,
        "throughput_rps": len(successes) / total_elapsed if total_elapsed > 0 else 0,
        "total_elapsed": total_elapsed,
        "failures": len(failures),
    }


def main():
    print("=" * 60)
    print("  Proxy 并发压力测试")
    print(f"  目标: {PROXY_URL}")
    print(f"  测试并发级别: {CONCURRENCY_LEVELS}")
    print("=" * 60)

    # 先做一次预热
    print("\n⏳ 预热中...")
    single_request()
    time.sleep(1)

    summary = []
    for n in CONCURRENCY_LEVELS:
        result = run_concurrency_test(n)
        summary.append(result)
        time.sleep(2)  # 冷却

    # 汇总
    print(f"\n{'='*60}")
    print("  📊 汇总对比")
    print(f"{'='*60}")
    print(f"{'并发':<6} {'成功率':<8} {'P50':<8} {'P90':<8} {'吞吐tok/s':<11} {'请求/s':<8}")
    print("-" * 55)
    best_tps = 0
    best_concurrency = 1
    for r in summary:
        print(f"{r['concurrency']:<6} {r['success_rate']*100:.0f}%     "
              f"{r['p50']:.2f}s   {r['p90']:.2f}s   "
              f"{r['throughput_tps']:<11.1f} {r['throughput_rps']:.2f}")
        if r["throughput_tps"] > best_tps:
            best_tps = r["throughput_tps"]
            best_concurrency = r["concurrency"]

    print(f"\n  🏆 最优并发: {best_concurrency} (吞吐 {best_tps:.1f} tok/s)")
    print(f"  💡 建议 PROXY_MAX_CONCURRENT={best_concurrency}")


if __name__ == "__main__":
    main()
