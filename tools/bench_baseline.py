#!/usr/bin/env python3
"""
模型效能基线对比工具

以 DeepSeek-v4-flash 为云端基线，评估本地模型配置的相对效能。

使用方式:
  # 1. 建立基线（在 DeepSeek 云端模式下运行）
  python3 tools/bench_baseline.py --save-baseline deepseek-flash

  # 2. 评估当前模型（在本地模型模式下运行）
  python3 tools/bench_baseline.py --compare deepseek-flash

  # 3. 查看所有基线
  python3 tools/bench_baseline.py --list

  # 4. 快速对比（仅质量评测，跳过性能）
  python3 tools/bench_baseline.py --compare deepseek-flash --quick

评估维度:
  - 质量评分 (Quality): 14 项代码/数学/指令/格式/常识测试通过率
  - 首 Token 延迟 (TTFT): 小/中上下文的冷启动延迟
  - 生成速度 (Gen Speed): tokens/second
  - 长上下文能力 (Long Context): 不同上下文大小的 TTFT 增长趋势
  - 成本效率 (Cost Efficiency): 质量分/延迟/内存的综合评分

注意:
  - 云端基线的 TTFT 包含网络延迟（~50-200ms），不可直接与本地比较绝对值
  - 应比较"相对比率"（local_TTFT / cloud_TTFT）而非绝对值
  - 生成速度受网络影响较小，可直接比较
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
BASELINE_DIR = os.path.join(REPO_ROOT, "logs", "baselines")

HOST = os.environ.get("LLAMA_HOST", "http://127.0.0.1:4000")
HEADERS = {"Content-Type": "application/json", "x-api-key": "test", "anthropic-version": "2023-06-01"}

QUALITY_TESTS = {
    "code_hello": {"prompt": "写一个Python函数，计算斐波那契数列第n项，返回整数。", "expected": "def fib", "category": "代码"},
    "code_sort": {"prompt": "写一个快速排序函数，要求原地排序，返回排序后的列表。", "expected": "def quicksort", "category": "代码"},
    "code_class": {"prompt": "写一个栈类 Stack，包含 push、pop、is_empty 方法。", "expected": "class Stack", "category": "代码"},
    "code_recursive": {"prompt": "写一个递归函数计算二叉树深度。", "expected": "def depth", "category": "代码"},
    "math_basic": {"prompt": "计算: 123 + 456 = ?", "expected": "579", "category": "数学"},
    "math_word": {"prompt": "小明有15个苹果，给了小红7个，又买了5个，现在小明有多少个苹果？", "expected": "13", "category": "数学"},
    "math_prime": {"prompt": "判断 17 是不是质数，并解释原因。", "expected": "质数", "category": "数学"},
    "instr_json": {"prompt": '用JSON格式返回你的名字和年龄，格式: {"name": "...", "age": ...}', "expected": '"name"', "category": "指令"},
    "instr_list": {"prompt": "列出3种水果，每行一个，用数字编号。", "expected": "1.", "category": "指令"},
    "instr_constrain": {"prompt": "回答问题时不要包含'是'这个字，直接给出答案。2+2等于多少？", "expected": "4", "category": "指令"},
    "fmt_json": {"prompt": "返回一个有效的JSON对象，包含字段: city=北京, population=2000万", "expected": '"city"', "category": "格式"},
    "fmt_markdown": {"prompt": "用Markdown表格展示: 苹果 红, 香蕉 黄, 葡萄 紫", "expected": "|", "category": "格式"},
    "common_day": {"prompt": "白天太阳在天空中，晚上看不到太阳的原因是什么？", "expected": "地球", "category": "常识"},
    "common_water": {"prompt": "水在0度会变成什么？", "expected": "冰", "category": "常识"},
}

TTFT_PROMPTS = {
    "small": "Say hello.",
    "medium": "Write a Python function that takes a list of integers and returns the sum of all even numbers. Include type hints and a docstring.",
    "large": "Explain the following concepts in detail: 1) Object-oriented programming principles (encapsulation, inheritance, polymorphism, abstraction) 2) Design patterns (singleton, factory, observer, strategy) 3) SOLID principles 4) Clean code practices 5) Testing strategies (unit, integration, e2e). Provide code examples for each.",
}


def _send(messages, max_tokens=200):
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "stream": False,
        "messages": messages,
    }).encode("utf-8")
    req = urllib.request.Request(HOST + "/v1/messages", data=body, headers=HEADERS)
    t0 = time.perf_counter()
    resp = urllib.request.urlopen(req, timeout=120)
    elapsed = time.perf_counter() - t0
    data = json.loads(resp.read())
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage", {})
    return text, elapsed, usage


def run_quality():
    print("  ── 质量评测 (14 项) ──")
    results = {}
    cat_scores = {}
    for tid, test in QUALITY_TESTS.items():
        cat = test["category"]
        try:
            text, elapsed, usage = _send([{"role": "user", "content": test["prompt"]}], max_tokens=300)
            passed = test["expected"].lower() in text.lower()
            results[tid] = {"passed": passed, "elapsed": round(elapsed, 2), "category": cat}
            cat_scores.setdefault(cat, {"passed": 0, "total": 0})
            cat_scores[cat]["total"] += 1
            if passed:
                cat_scores[cat]["passed"] += 1
            status = "✅" if passed else "❌"
            print(f"    {status} {tid:<20} {elapsed:.1f}s")
        except Exception as e:
            results[tid] = {"passed": False, "error": str(e)[:80], "category": cat}
            cat_scores.setdefault(cat, {"passed": 0, "total": 0})
            cat_scores[cat]["total"] += 1
            print(f"    ❌ {tid:<20} ERROR: {str(e)[:50]}")

    total_passed = sum(1 for r in results.values() if r.get("passed"))
    total = len(results)
    print(f"\n    总计: {total_passed}/{total} ({100*total_passed/total:.1f}%)")
    for cat, sc in sorted(cat_scores.items()):
        print(f"      {cat}: {sc['passed']}/{sc['total']}")
    return {"results": results, "categories": cat_scores, "total_passed": total_passed, "total": total}


def run_perf(quick=False):
    print("\n  ── 性能评测 ──")
    perf = {}

    # TTFT
    print("    TTFT:")
    for name, prompt in TTFT_PROMPTS.items():
        if quick and name == "large":
            continue
        try:
            text, elapsed, usage = _send([{"role": "user", "content": prompt}], max_tokens=50)
            perf[f"ttft_{name}"] = round(elapsed, 2)
            print(f"      {name:<8} {elapsed:.2f}s  out={usage.get('output_tokens',0)}tok")
        except Exception as e:
            perf[f"ttft_{name}"] = None
            print(f"      {name:<8} ERROR: {str(e)[:50]}")

    # Generation speed
    print("    生成速度:")
    try:
        text, elapsed, usage = _send(
            [{"role": "user", "content": "Write a detailed Python tutorial covering variables, loops, functions, classes, and error handling. Be thorough."}],
            max_tokens=500,
        )
        out_tokens = usage.get("output_tokens", 0)
        tps = out_tokens / elapsed if elapsed else 0
        perf["gen_speed_tps"] = round(tps, 1)
        perf["gen_elapsed"] = round(elapsed, 2)
        perf["gen_tokens"] = out_tokens
        print(f"      {out_tokens} tokens in {elapsed:.1f}s = {tps:.1f} tok/s")
    except Exception as e:
        perf["gen_speed_tps"] = None
        print(f"      ERROR: {str(e)[:50]}")

    return perf


def get_config_info():
    """Get current model/backend info from the proxy."""
    try:
        req = urllib.request.Request(HOST + "/v1/models", headers=HEADERS)
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        models = [m["id"] for m in data.get("data", [])]
        # The active model is usually the last non-claude entry
        active = [m for m in models if not m.startswith("claude") and m != "default"]
        return {"models": models, "active_model": active[0] if active else "unknown"}
    except:
        return {"models": [], "active_model": "unknown"}


def run_benchmark(quick=False):
    """Run full benchmark suite and return results dict."""
    config = get_config_info()
    print(f"\n  模型: {config['active_model']}")
    print(f"  端点: {HOST}")

    quality = run_quality()
    perf = run_perf(quick=quick) if not quick else run_perf(quick=True)

    return {
        "timestamp": datetime.now().isoformat(),
        "config": config,
        "quality": quality,
        "perf": perf,
    }


def save_baseline(name, results):
    os.makedirs(BASELINE_DIR, exist_ok=True)
    path = os.path.join(BASELINE_DIR, f"{name}.json")
    results["baseline_name"] = name
    with open(path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ 基线已保存: {path}")
    return path


def load_baseline(name):
    path = os.path.join(BASELINE_DIR, f"{name}.json")
    if not os.path.exists(path):
        print(f"  ❌ 基线不存在: {path}")
        sys.exit(1)
    with open(path) as f:
        return json.loads(f.read())


def list_baselines():
    os.makedirs(BASELINE_DIR, exist_ok=True)
    files = sorted(os.listdir(BASELINE_DIR))
    json_files = [f for f in files if f.endswith(".json")]
    if not json_files:
        print("  无已保存的基线。")
        return
    print(f"  已保存的基线 ({len(json_files)}):\n")
    for f in json_files:
        path = os.path.join(BASELINE_DIR, f)
        with open(path) as fh:
            d = json.loads(fh.read())
        q = d.get("quality", {})
        model = d.get("config", {}).get("active_model", "?")
        p = q.get("total_passed", 0)
        t = q.get("total", 14)
        perf = d.get("perf", {})
        ttft = perf.get("ttft_small", "?")
        tps = perf.get("gen_speed_tps", "?")
        print(f"    {f:<30} model={model:<35} quality={p}/{t}  ttft={ttft}s  speed={tps}t/s")


def compare(current, baseline):
    """Compare current results against baseline and print report."""
    bl_q = baseline.get("quality", {})
    bl_perf = baseline.get("perf", {})
    bl_model = baseline.get("config", {}).get("active_model", "?")

    cur_q = current.get("quality", {})
    cur_perf = current.get("perf", {})
    cur_model = current.get("config", {}).get("active_model", "?")

    bl_total_p = bl_q.get("total_passed", 0)
    bl_total_t = bl_q.get("total", 14)
    cur_total_p = cur_q.get("total_passed", 0)
    cur_total_t = cur_q.get("total", 14)

    bl_rate = bl_total_p / bl_total_t * 100 if bl_total_t else 0
    cur_rate = cur_total_p / cur_total_t * 100 if cur_total_t else 0

    print("\n" + "=" * 70)
    print("  模型效能对比报告")
    print("=" * 70)
    print(f"\n  {'指标':<25} {'基线':<20} {'当前':<20} {'差异':<15}")
    print(f"  {'':─<25} {'':─<20} {'':─<20} {'':─<15}")
    print(f"  {'模型':<25} {bl_model[:20]:<20} {cur_model[:20]:<20}")

    # Quality
    q_diff = cur_rate - bl_rate
    q_sign = "↑" if q_diff > 0 else ("↓" if q_diff < 0 else "=")
    print(f"  {'质量评分':<25} {bl_total_p}/{bl_total_t} ({bl_rate:.1f}%){'':<5} {cur_total_p}/{cur_total_t} ({cur_rate:.1f}%){'':<5} {q_sign} {abs(q_diff):.1f}%")

    bl_cats = bl_q.get("categories", {})
    cur_cats = cur_q.get("categories", {})

    # Normalize category names (bench_quality.py uses 代码生成/数学推理/ etc.)
    CAT_ALIASES = {
        "代码生成": "代码", "数学推理": "数学", "指令遵循": "指令",
        "格式正确性": "格式", "常识推理": "常识",
    }
    def norm_cats(cats):
        result = {}
        for k, v in cats.items():
            nk = CAT_ALIASES.get(k, k)
            if nk in result:
                result[nk]["passed"] += v.get("passed", 0)
                result[nk]["total"] += v.get("total", 0)
            else:
                result[nk] = {"passed": v.get("passed", 0), "total": v.get("total", 0)}
        return result

    bl_cats = norm_cats(bl_cats)
    cur_cats = norm_cats(cur_cats)

    for cat in sorted(set(list(bl_cats.keys()) + list(cur_cats.keys()))):
        bl_c = bl_cats.get(cat, {"passed": 0, "total": 0})
        cur_c = cur_cats.get(cat, {"passed": 0, "total": 0})
        bl_str = f"{bl_c['passed']}/{bl_c['total']}"
        cur_str = f"{cur_c['passed']}/{cur_c['total']}"
        diff = cur_c["passed"] - bl_c["passed"]
        sign = "↑" if diff > 0 else ("↓" if diff < 0 else "=")
        print(f"    {cat:<23} {bl_str:<20} {cur_str:<20} {sign} {abs(diff)}")

    # Performance
    print()
    for perf_key, label in [("ttft_small", "TTFT 小"), ("ttft_medium", "TTFT 中"), ("ttft_large", "TTFT 大")]:
        bl_v = bl_perf.get(perf_key)
        cur_v = cur_perf.get(perf_key)
        if bl_v and cur_v:
            ratio = cur_v / bl_v if bl_v else 0
            if ratio < 1:
                sign = f"⚡ {ratio:.1f}x 快"
            elif ratio > 1:
                sign = f"🐢 {ratio:.1f}x 慢"
            else:
                sign = "= 相同"
            print(f"  {label:<25} {bl_v}s{'':<16} {cur_v}s{'':<16} {sign}")
        elif bl_v or cur_v:
            print(f"  {label:<25} {bl_v or 'N/A':<20} {cur_v or 'N/A':<20} N/A")

    bl_tps = bl_perf.get("gen_speed_tps")
    cur_tps = cur_perf.get("gen_speed_tps")
    if bl_tps and cur_tps:
        ratio = cur_tps / bl_tps if bl_tps else 0
        if ratio > 1:
            sign = f"⚡ {ratio:.1f}x 快"
        elif ratio < 1:
            sign = f"🐢 {ratio:.1f}x 慢"
        else:
            sign = "= 相同"
        print(f"  {'生成速度':<25} {bl_tps}t/s{'':<14} {cur_tps}t/s{'':<14} {sign}")

    # Efficiency score (quality per unit time)
    print("\n  ── 效能评分 ──")
    bl_ttft = bl_perf.get("ttft_small", 999)
    cur_ttft = cur_perf.get("ttft_small", 999)
    if bl_ttft and cur_ttft and bl_ttft > 0:
        # Quality score normalized: (quality% / TTFT) * 100
        bl_eff = bl_rate / bl_ttft * 10
        cur_eff = cur_rate / cur_ttft * 10
        print(f"    基线效率指数: {bl_eff:.1f} (质量{bl_rate:.0f}% / TTFT {bl_ttft}s × 10)")
        print(f"    当前效率指数: {cur_eff:.1f} (质量{cur_rate:.0f}% / TTFT {cur_ttft}s × 10)")
        if bl_eff > 0:
            ratio = cur_eff / bl_eff
            print(f"    相对效率:     {ratio:.2f}x ({'优于' if ratio > 1 else '劣于'}基线)")

    # Summary recommendation
    print("\n  ── 结论 ──")
    if cur_rate >= bl_rate * 0.95 and cur_ttft <= bl_ttft * 3:
        print("    ✅ 当前模型质量接近基线，延迟可接受")
        print("    建议: 适合日常使用，成本更低")
    elif cur_rate >= bl_rate * 0.85:
        print("    ⚠️  当前模型质量略低于基线，但可用")
        print("    建议: 适合非关键任务；复杂推理建议切换基线")
    else:
        print("    ❌ 当前模型质量明显低于基线")
        print("    建议: 关键任务应使用基线模型")

    # Local vs cloud note
    bl_backend = "cloud" if "deepseek" in bl_model.lower() or "openai" in bl_model.lower() else "local"
    cur_backend = "cloud" if "deepseek" in cur_model.lower() or "openai" in cur_model.lower() else "local"
    if bl_backend == "cloud" and cur_backend == "local":
        print(f"\n    💡 本地模型优势: ¥0/请求 (vs 云端 ~¥0.004/请求)")
        print(f"    💡 本地模型劣势: 并发=1 (vs 云端 4), 上下文受限 (vs 云端 1M+)")
        daily_cost = 50 * 0.004  # 50 requests/day estimate
        print(f"    💡 日均 50 请求可省: ~¥{daily_cost:.1f}/天 = ~¥{daily_cost*30:.0f}/月")


def main():
    parser = argparse.ArgumentParser(description="模型效能基线对比工具")
    parser.add_argument("--save-baseline", metavar="NAME", help="保存当前模型结果为基线")
    parser.add_argument("--compare", metavar="NAME", help="与指定基线对比")
    parser.add_argument("--list", action="store_true", help="列出所有已保存基线")
    parser.add_argument("--quick", action="store_true", help="快速模式（跳过大上下文TTFT）")
    args = parser.parse_args()

    if args.list:
        list_baselines()
        return

    print("=" * 70)
    print("  模型效能基线对比工具")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    if args.save_baseline:
        results = run_benchmark(quick=args.quick)
        save_baseline(args.save_baseline, results)
        return

    if args.compare:
        results = run_benchmark(quick=args.quick)
        baseline = load_baseline(args.compare)
        compare(results, baseline)
        return

    # Default: just run benchmark
    results = run_benchmark(quick=args.quick)
    print("\n  使用 --save-baseline NAME 保存为基线")
    print("  使用 --compare NAME 与基线对比")


if __name__ == "__main__":
    main()
