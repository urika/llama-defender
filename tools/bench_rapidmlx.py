#!/usr/bin/env python3
"""
Rapid-MLX 性能测试脚本

测试指标：
1. TTFT (Time To First Token) — 不同 prompt 长度
2. 生成速度 (tok/s) — 不同输出长度
3. Prefix cache 效果 — 连续对话场景
4. 性能衰减 — 连续多轮请求后的速度变化

用法:
    python3 tools/bench_rapidmlx.py           # 完整测试 (~5-10 分钟)
    python3 tools/bench_rapidmlx.py --quick   # 快速测试 (~2 分钟)
"""

import argparse
import http.client
import json
import os
import sys
import time
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8081
PROXY_PORT = 4000

PROMPTS = {
    "short": {
        "desc": "短提示 (50 tokens)",
        "messages": [{"role": "user", "content": "解释什么是 Python 的生成器。"}],
        "max_tokens": 100,
    },
    "medium": {
        "desc": "中等提示 (200 tokens)",
        "messages": [{"role": "user", "content": """
请详细解释 Python 中 asyncio 和 threading 的区别，包括：
1. 各自的适用场景
2. GIL 的影响
3. 性能对比
4. 代码示例
""".strip()}],
        "max_tokens": 200,
    },
    "long": {
        "desc": "长提示 (1000+ tokens)",
        "messages": [{"role": "user", "content": open(__file__).read()[:3000] + "\n\n请总结以上代码的功能和架构。"}],
        "max_tokens": 100,
    },
    "code_gen": {
        "desc": "代码生成 (500 tokens 输出)",
        "messages": [{"role": "user", "content": "实现一个完整的 LRU Cache，包含 get、put 方法，使用 Python 并添加类型提示和文档字符串。"}],
        "max_tokens": 500,
    },
}


def log(msg):
    print(f"  {msg}", flush=True)


def header(msg):
    print(f"\n{'='*60}", flush=True)
    print(f"  {msg}", flush=True)
    print(f"{'='*60}", flush=True)


def subheader(msg):
    print(f"\n  --- {msg} ---", flush=True)


def send_request(messages, max_tokens, stream=True, port=BACKEND_PORT):
    """发送请求，返回 (result_dict, ttft_ms, total_ms)"""
    body = {
        "model": "mlx-community/Qwen3.6-35B-A3B-4bit",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.6,
        "stream": stream,
    }

    ttft_ms = None
    total_ms = None
    full_content = ""
    prompt_tokens = 0
    completion_tokens = 0
    t0 = time.time()

    try:
        conn = http.client.HTTPConnection(BACKEND_HOST, port, timeout=300)
        conn.request("POST", "/v1/chat/completions",
                     body=json.dumps(body),
                     headers={"Content-Type": "application/json", "Authorization": "Bearer sk-1234"})
        resp = conn.getresponse()

        # 流式解析
        if stream:
            buffer = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk

                while b"data: " in buffer:
                    idx = buffer.find(b"data: ")
                    if idx == -1:
                        break
                    end = buffer.find(b"\n\n", idx)
                    if end == -1:
                        break

                    line = buffer[idx + 6:end].strip()
                    buffer = buffer[end + 2:]

                    if line == b"[DONE]":
                        continue

                    try:
                        data = json.loads(line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue

                    choices = data.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            if ttft_ms is None:
                                ttft_ms = (time.time() - t0) * 1000
                            full_content += content

                    usage = data.get("usage", {})
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                        completion_tokens = usage.get("completion_tokens", completion_tokens)

        else:
            # 非流式
            body_data = resp.read()
            data = json.loads(body_data.decode("utf-8", errors="replace"))
            total_ms = (time.time() - t0) * 1000
            ttft_ms = total_ms  # 非流式没有 TTFT 区分
            if "choices" in data and data["choices"]:
                full_content = data["choices"][0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            conn.close()
            return {
                "content": full_content,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }, ttft_ms, total_ms

        total_ms = (time.time() - t0) * 1000
        conn.close()

    except Exception as e:
        total_ms = (time.time() - t0) * 1000
        return {"error": str(e)}, ttft_ms, total_ms

    return {
        "content": full_content,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }, ttft_ms, total_ms


def test_ttft(prompts, repeat=3):
    """测试不同 prompt 长度下的 TTFT"""
    header("测试 1: TTFT (Time To First Token)")

    results = {}
    for key, p in prompts.items():
        subheader(p["desc"])
        ttfts = []
        for i in range(repeat):
            resp, ttft, total = send_request(p["messages"], p["max_tokens"])
            if "error" in resp:
                log(f"❌ 错误: {resp['error']}")
                continue
            ttfts.append(ttft)
            ttft_str = f"{ttft:.0f}" if ttft is not None else "N/A"
            total_str = f"{total:.0f}" if total is not None else "N/A"
            log(f"  第 {i+1} 次: TTFT={ttft_str}ms, 总耗时={total_str}ms, "
                f"prompt={resp['prompt_tokens']}tok, output={resp['completion_tokens']}tok")
            time.sleep(1)

        if ttfts:
            avg = sum(ttfts) / len(ttfts)
            results[key] = {"avg_ttft_ms": round(avg, 1), "samples": ttfts}
            log(f"📊 平均 TTFT: {avg:.0f}ms")

    return results


def test_generation_speed(prompts, repeat=3):
    """测试生成速度"""
    header("测试 2: 生成速度 (tokens/sec)")

    results = {}
    for key, p in prompts.items():
        if p["max_tokens"] < 50:
            continue
        subheader(p["desc"])
        speeds = []
        for i in range(repeat):
            resp, ttft, total = send_request(p["messages"], p["max_tokens"])
            if "error" in resp:
                log(f"❌ 错误: {resp['error']}")
                continue
            comp = resp["completion_tokens"]
            gen_time = (total - (ttft or 0)) / 1000 if total and ttft else 0
            speed = comp / gen_time if gen_time > 0 else 0
            speeds.append(speed)
            ttft_str = f"{ttft:.0f}" if ttft is not None else "N/A"
            log(f"  第 {i+1} 次: {comp}tok / {gen_time:.1f}s = {speed:.1f} tok/s "
                f"(TTFT={ttft_str}ms)")
            time.sleep(1)

        if speeds:
            avg = sum(speeds) / len(speeds)
            results[key] = {"avg_tok_per_sec": round(avg, 1), "samples": speeds}
            log(f"📊 平均生成速度: {avg:.1f} tok/s")

    return results


def test_prefix_cache():
    """测试 prefix cache 效果：连续发送相同 system+tools 前缀的请求"""
    header("测试 3: Prefix Cache 效果")

    # 构建一个带有长 system prompt 的对话
    system_msg = "你是一个专业的 Python 编程助手。" * 50  # ~400 tokens
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": "写一行代码打印 'hello world'"},
    ]

    results = []
    for i in range(3):
        resp, ttft, total = send_request(messages, 50)
        if "error" in resp:
            log(f"❌ 错误: {resp['error']}")
            continue
        results.append({"ttft": ttft, "total": total, "prompt_tokens": resp["prompt_tokens"]})
        log(f"  第 {i+1} 次: TTFT={ttft:.0f}ms, prompt={resp['prompt_tokens']}tok")
        time.sleep(0.5)

    if len(results) >= 2:
        first_ttft = results[0]["ttft"]
        last_ttft = results[-1]["ttft"]
        if first_ttft and last_ttft:
            improvement = (1 - last_ttft / first_ttft) * 100
            log(f"📊 Cache 效果: 首次 TTFT={first_ttft:.0f}ms, 末次 TTFT={last_ttft:.0f}ms, 改善 {improvement:.0f}%")

    return results


def test_performance_decay(rounds=10):
    """测试连续多轮请求后的性能衰减"""
    header(f"测试 4: 性能衰减 ({rounds} 轮连续请求)")

    messages = [{"role": "user", "content": "用一句话解释什么是机器学习。"}]
    max_tokens = 100

    speeds = []
    for i in range(rounds):
        resp, ttft, total = send_request(messages, max_tokens)
        if "error" in resp:
            log(f"❌ 第 {i+1} 轮错误: {resp['error']}")
            continue
        comp = resp["completion_tokens"]
        gen_time = (total - (ttft or 0)) / 1000
        speed = comp / gen_time if gen_time > 0 else 0
        speeds.append(speed)
        log(f"  第 {i+1:2d}/{rounds} 轮: {speed:.1f} tok/s  (TTFT={ttft:.0f}ms)")

    if len(speeds) >= 3:
        first = speeds[0]
        last = speeds[-1]
        avg_first3 = sum(speeds[:3]) / 3
        avg_last3 = sum(speeds[-3:]) / 3
        decay = (1 - avg_last3 / avg_first3) * 100 if avg_first3 > 0 else 0
        log(f"📊 衰减分析: 前3轮平均={avg_first3:.1f} tok/s, 后3轮平均={avg_last3:.1f} tok/s, 衰减 {decay:.1f}%")

    return speeds


def test_proxy_overhead():
    """对比直接请求后端 vs 请求代理的延迟"""
    header("测试 5: 代理层开销")

    messages = [{"role": "user", "content": "你好"}]

    # 直接请求后端 (非流式)
    _, ttft_direct, total_direct = send_request(messages, 20, stream=False, port=BACKEND_PORT)
    time.sleep(1)

    # 请求代理 (非流式)
    _, ttft_proxy, total_proxy = send_request(messages, 20, stream=False, port=PROXY_PORT)

    td = ttft_direct or 0
    tp = ttft_proxy or 0
    log(f"  直接后端: TTFT={td:.0f}ms, 总耗时={total_direct:.0f}ms")
    log(f"  代理层:   TTFT={tp:.0f}ms, 总耗时={total_proxy:.0f}ms")
    if td > 0 and tp > 0:
        overhead = tp - td
        log(f"📊 代理开销: TTFT +{overhead:.0f}ms ({overhead/td*100:.0f}%)")

    return {
        "direct": {"ttft": td, "total": total_direct},
        "proxy": {"ttft": tp, "total": total_proxy},
    }


def print_summary(all_results):
    header("📊 测试汇总")

    # TTFT
    ttft_data = all_results.get("ttft", {})
    if ttft_data:
        print("\n  TTFT (Time To First Token):")
        print(f"  {'场景':<20} {'平均 (ms)':<12} {'样本数'}")
        print("  " + "-" * 40)
        for k, v in ttft_data.items():
            print(f"  {k:<20} {v['avg_ttft_ms']:<12.0f} {len(v['samples'])}")

    # 生成速度
    speed_data = all_results.get("speed", {})
    if speed_data:
        print("\n  生成速度:")
        print(f"  {'场景':<20} {'平均 (tok/s)':<15} {'样本数'}")
        print("  " + "-" * 40)
        for k, v in speed_data.items():
            print(f"  {k:<20} {v['avg_tok_per_sec']:<15.1f} {len(v['samples'])}")

    # Prefix cache
    cache_data = all_results.get("cache", [])
    if cache_data:
        print(f"\n  Prefix Cache:")
        for i, r in enumerate(cache_data):
            marker = " ✅ HIT" if i > 0 and r['ttft'] < cache_data[0]['ttft'] * 0.5 else ""
            print(f"    第 {i+1} 次: TTFT={r['ttft']:.0f}ms{marker}")

    # 衰减
    decay_data = all_results.get("decay", [])
    if decay_data:
        print(f"\n  性能衰减 ({len(decay_data)} 轮):")
        first3 = sum(decay_data[:3]) / 3
        last3 = sum(decay_data[-3:]) / 3 if len(decay_data) >= 3 else 0
        print(f"    前3轮: {first3:.1f} tok/s")
        print(f"    后3轮: {last3:.1f} tok/s")
        if first3 > 0:
            print(f"    衰减:  {(1-last3/first3)*100:.1f}%")

    # 代理开销
    proxy_data = all_results.get("proxy_overhead", {})
    if proxy_data:
        d = proxy_data.get("direct", {})
        p = proxy_data.get("proxy", {})
        print(f"\n  代理层开销:")
        print(f"    直接后端 TTFT: {d.get('ttft', 0):.0f}ms")
        print(f"    代理层 TTFT:   {p.get('ttft', 0):.0f}ms")


def main():
    parser = argparse.ArgumentParser(description="Rapid-MLX 性能测试")
    parser.add_argument("--quick", action="store_true", help="快速模式 (减少重复次数)")
    parser.add_argument("--test", choices=["ttft", "speed", "cache", "decay", "proxy", "all"], default="all", help="指定测试项目")
    args = parser.parse_args()

    repeat = 1 if args.quick else 3
    decay_rounds = 5 if args.quick else 10

    header("Rapid-MLX v0.6.71 性能测试")
    log(f"后端: {BACKEND_HOST}:{BACKEND_PORT}")
    log(f"代理: {BACKEND_HOST}:{PROXY_PORT}")
    log(f"快速模式: {'是' if args.quick else '否'}")

    all_results = {}

    if args.test in ("ttft", "all"):
        all_results["ttft"] = test_ttft(PROMPTS, repeat=repeat)

    if args.test in ("speed", "all"):
        all_results["speed"] = test_generation_speed(PROMPTS, repeat=repeat)

    if args.test in ("cache", "all"):
        all_results["cache"] = test_prefix_cache()

    if args.test in ("decay", "all"):
        all_results["decay"] = test_performance_decay(rounds=decay_rounds)

    if args.test in ("proxy", "all"):
        all_results["proxy_overhead"] = test_proxy_overhead()

    print_summary(all_results)

    # 保存结果
    result_file = os.path.join(REPO_ROOT, "logs", f"bench-rapidmlx-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(result_file), exist_ok=True)
    with open(result_file, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    log(f"📝 结果已保存到: {result_file}")

    print("\n✅ 测试完成", flush=True)


if __name__ == "__main__":
    main()
