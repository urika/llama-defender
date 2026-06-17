#!/usr/bin/env python3
"""统一模型对比评测 — 一键运行推理+工具+吞吐+质量四项测试"""
import time, json, sys, os, statistics, subprocess, urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

PROXY_URL = "http://127.0.0.1:4000/v1/messages"
HEADERS = {"Content-Type": "application/json", "x-api-key": "sk-1234", "anthropic-version": "2023-06-01"}

REASONING_TESTS = {
    "lockers_100": {
        "name": "100盏灯问题",
        "messages": [{"role": "user", "content": "有100盏灯(1-100)，初始全关。第k个人切换编号是k的倍数的灯。1)最后哪些灯亮着？为什么？2)1000盏灯呢？3)如果初始全开呢？"}],
        "max_tokens": 2000, "checks": ["完全平方数", "奇数", "偶数", "因数"],
    },
    "largest_prime": {
        "name": "10000以内最大质数",
        "messages": [{"role": "user", "content": "请找出10000以内最大的质数。要求：1.逐步推理方法 2.验证答案 3.确保是最大的"}],
        "max_tokens": 3000, "checks": ["9973", "试除", "验证"],
    },
}

CODE_TESTS = [
    {"name": "bubble_sort", "prompt": "写一个python冒泡排序", "max_tokens": 200},
    {"name": "fibonacci", "prompt": "写一个Python函数，计算斐波那契数列第n项", "max_tokens": 150},
    {"name": "binary_search", "prompt": "写一个二分查找函数", "max_tokens": 150},
]

TOOL_TEST = {"name": "get_weather", "description": "获取城市天气",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}

def _send(messages, max_tokens=1024, tools=None, temperature=0.0):
    body = {"model": "claude-sonnet-4-6", "max_tokens": max_tokens, "messages": messages, "temperature": temperature}
    if tools:
        if isinstance(tools, dict):
            body["tools"] = [{"name": k, "description": v["description"], "input_schema": v["input_schema"]} for k, v in tools.items()]
        else:
            body["tools"] = tools
    req = urllib.request.Request(PROXY_URL, data=json.dumps(body).encode(), headers=HEADERS, method="POST")
    t0 = time.perf_counter()
    resp = urllib.request.urlopen(req, timeout=300)
    elapsed = time.perf_counter() - t0
    return json.loads(resp.read().decode()), elapsed

def test_reasoning():
    results = {}
    for tid, test in REASONING_TESTS.items():
        print(f"  🧠 {test['name']}...", end=" ", flush=True)
        try:
            resp, elapsed = _send(test["messages"], max_tokens=test["max_tokens"])
            content = "".join(b.get("text","") for b in resp.get("content",[]) if b.get("type")=="text")
            tokens, stop = resp.get("usage",{}).get("output_tokens",0), resp.get("stop_reason","?")
            checks = sum(1 for c in test["checks"] if c in content)
            ok = checks >= len(test["checks"]) * 0.6
            tps = tokens / elapsed if elapsed else 0
            print(f"{'✅' if ok else '⚠️'} {tokens}tok {elapsed:.1f}s {tps:.0f}t/s checks={checks}/{len(test['checks'])}")
            results[tid] = {"passed": ok, "tokens": tokens, "elapsed": elapsed, "tps": tps, "stop": stop, "checks": checks}
        except Exception as e:
            print(f"❌ {e}")
            results[tid] = {"passed": False, "error": str(e)[:100]}
    return results

def test_tool_calling():
    print("  🔧 工具调用...", end=" ", flush=True)
    try:
        tools_arg = [{"name": "get_weather", "description": TOOL_TEST["description"], "input_schema": TOOL_TEST["input_schema"]}]
        resp, elapsed = _send([{"role": "user", "content": "北京天气怎么样？"}], max_tokens=100, tools={"get_weather": TOOL_TEST})
        tcs = [b for b in resp.get("content",[]) if b.get("type")=="tool_use"]
        ok = len(tcs) > 0 and tcs[0].get("name") == "get_weather"
        print(f"{'✅' if ok else '❌'} {resp.get('usage',{}).get('output_tokens',0)}tok {elapsed:.1f}s")
        return {"passed": ok, "elapsed": elapsed}
    except Exception as e:
        print(f"❌ {e}")
        return {"passed": False, "error": str(e)[:100]}

def test_throughput():
    results = []
    for test in CODE_TESTS:
        print(f"  ⚡ {test['name']}...", end=" ", flush=True)
        try:
            resp, elapsed = _send([{"role": "user", "content": test["prompt"]}], max_tokens=test["max_tokens"])
            tokens = resp.get("usage",{}).get("output_tokens",0)
            tps = tokens / elapsed if elapsed else 0
            print(f"{tokens}tok {elapsed:.1f}s {tps:.0f}t/s")
            results.append({"name": test["name"], "tokens": tokens, "elapsed": elapsed, "tps": tps})
        except Exception as e:
            print(f"❌ {e}")
    if results:
        print(f"  📊 平均: {statistics.mean(r['tps'] for r in results):.0f} tok/s")
    return results

def test_quality():
    print("  📋 bench_quality.py...")
    r = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "bench_quality.py")], capture_output=True, text=True, timeout=300, cwd=REPO_ROOT)
    for line in r.stdout.split("\n"):
        if "总计:" in line: print(f"  {line.strip()}")
    return {"raw": r.stdout}

def main():
    print("=" * 60)
    print("  统一模型对比评测")
    print(f"  目标: {PROXY_URL}")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    report = {"timestamp": datetime.now().isoformat()}
    print("\n── 1. 推理能力 ──");     report["reasoning"] = test_reasoning()
    print("\n── 2. 工具调用 ──");     report["tool_calling"] = test_tool_calling()
    print("\n── 3. 代码吞吐 ──");     report["throughput"] = test_throughput()
    print("\n── 4. 质量评测 ──");     report["quality"] = test_quality()

    out = os.path.join(REPO_ROOT, "logs", f"model-compare-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f: json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n📝 报告: {out}")
    return report

if __name__ == "__main__":
    main()
