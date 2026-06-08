#!/usr/bin/env python3
"""
合并 Promptfoo 评估结果与代理日志指标，生成统一报告。

用法:
  python3 tools/promptfoo_report_merge.py \
      --promptfoo logs/experiments/promptfoo-A-xxx.json \
      --log logs/experiments/A-xxx.log \
      --output logs/experiments/merged_report_xxx.md
"""
import argparse
import json
import re
from collections import Counter
from datetime import datetime


def parse_proxy_log(path):
    """解析代理日志，提取核心指标。"""
    with open(path, "r", errors="replace") as f:
        lines = f.readlines()

    reqs = []
    clears = []
    truncs = []
    tools_list = []
    errors = []

    for line in lines:
        m = re.search(
            r"\[(\d{2}:\d{2}:\d{2})\].*\[REQ_SUMMARY\] chars=(\d+) tools=(\d+)",
            line,
        )
        if m:
            reqs.append(
                {"time": m.group(1), "chars": int(m.group(2)), "tools": int(m.group(3))}
            )

        m = re.search(r"(\d+) tool_results cleared, (\d+) chars freed", line)
        if m:
            clears.append({"count": int(m.group(1)), "chars": int(m.group(2))})

        m = re.search(r"(\d+) messages dropped, (\d+) chars removed", line)
        if m:
            truncs.append({"msgs": int(m.group(1)), "chars": int(m.group(2))})

        m = re.search(r"-> Tools: \[(.*?)\]", line)
        if m:
            for name in m.group(1).replace("'", "").split(", "):
                tools_list.append(name.strip())

        if "error:" in line.lower() or "Exception" in line:
            errors.append(line.strip()[:200])

    if not reqs:
        return None

    chars_list = [r["chars"] for r in reqs]
    growth_list = [chars_list[i] - chars_list[i - 1] for i in range(1, len(chars_list))]
    avg_growth = round(sum(growth_list) / len(growth_list)) if growth_list else 0

    return {
        "total_requests": len(reqs),
        "total_chars": sum(chars_list),
        "avg_chars": round(sum(chars_list) / len(chars_list)),
        "max_chars": max(chars_list),
        "min_chars": min(chars_list),
        "req_size_growth": avg_growth,
        "tool_clears": len(clears),
        "cleared_chars_total": sum(c["chars"] for c in clears),
        "truncations": len(truncs),
        "errors": len(errors),
        "tool_freq": dict(Counter(tools_list).most_common(10)),
    }


def parse_promptfoo_json(path):
    """解析 Promptfoo 评估结果 JSON。"""
    with open(path, "r", errors="replace") as f:
        data = json.load(f)

    results = data.get("results", {}).get("results", [])
    total = len(results)
    passed = sum(1 for r in results if r.get("success", False))
    failed = total - passed

    # 统计各断言类型
    assert_stats = {}
    for r in results:
        for comp in r.get("gradingResult", {}).get("componentResults", []):
            t = comp.get("assertion", {}).get("type", "unknown")
            p = comp.get("pass", False)
            if t not in assert_stats:
                assert_stats[t] = {"pass": 0, "fail": 0}
            assert_stats[t]["pass" if p else "fail"] += 1

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / total * 100, 1) if total else 0,
        "assert_stats": assert_stats,
        "description": data.get("results", {}).get("config", {}).get("description", ""),
        "group": data.get("results", {}).get("evalId", ""),
    }


def generate_merged_report(promptfoo_data, proxy_data, output_path):
    """生成统一 Markdown 报告。"""
    with open(output_path, "w") as f:
        f.write("# A/B 统一评估报告\n\n")
        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("> 本报告合并了 **Promptfoo 固定 prompt 回归测试** 与 **代理层性能指标**。\n\n")

        # Promptfoo 结果
        f.write("## 一、Promptfoo 回归测试结果\n\n")
        f.write(f"- **描述**: {promptfoo_data.get('description', 'N/A')}\n")
        f.write(f"- **总测试数**: {promptfoo_data['total']}\n")
        f.write(f"- **通过**: {promptfoo_data['passed']}\n")
        f.write(f"- **失败**: {promptfoo_data['failed']}\n")
        f.write(f"- **通过率**: {promptfoo_data['pass_rate']}%\n\n")

        if promptfoo_data["assert_stats"]:
            f.write("### 断言统计\n\n")
            f.write("| 断言类型 | 通过 | 失败 |\n")
            f.write("|----------|------|------|\n")
            for t, s in promptfoo_data["assert_stats"].items():
                f.write(f"| {t} | {s['pass']} | {s['fail']} |\n")
            f.write("\n")

        # 代理层指标
        if proxy_data:
            f.write("## 二、代理层性能指标\n\n")
            f.write("| 指标 | 数值 |\n")
            f.write("|------|------|\n")
            f.write(f"| 总请求数 | {proxy_data['total_requests']} |\n")
            f.write(f"| 总字符数 | {proxy_data['total_chars']:,} |\n")
            f.write(f"| 平均请求大小 | {proxy_data['avg_chars']:,} chars |\n")
            f.write(f"| 最大请求 | {proxy_data['max_chars']:,} chars |\n")
            f.write(f"| 平均增长/轮 | {proxy_data['req_size_growth']:,} chars |\n")
            f.write(f"| 工具清理次数 | {proxy_data['tool_clears']} |\n")
            f.write(f"| 累计清理字符 | {proxy_data['cleared_chars_total']:,} |\n")
            f.write(f"| 上下文截断次数 | {proxy_data['truncations']} |\n")
            f.write(f"| 错误数 | {proxy_data['errors']} |\n")
            f.write("\n")

            if proxy_data["tool_freq"]:
                f.write("### 工具调用频率 TOP10\n\n")
                f.write("| 工具 | 次数 |\n")
                f.write("|------|------|\n")
                for name, count in proxy_data["tool_freq"].items():
                    f.write(f"| {name} | {count} |\n")
                f.write("\n")
        else:
            f.write("## 二、代理层性能指标\n\n")
            f.write("> 未提供代理日志，或日志中无 REQ_SUMMARY 记录。\n\n")

        # 结论
        f.write("## 三、评估结论\n\n")
        if proxy_data:
            f.write("- 代理层指标来自 `anthropic_proxy.py` 日志解析\n")
        f.write("- Promptfoo 结果来自固定 prompt 回归测试\n")
        f.write("- A/B 对比请分别运行两组后对比本报告\n\n")

    print(f"统一报告已生成: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Merge Promptfoo results with proxy log metrics"
    )
    parser.add_argument(
        "--promptfoo", required=True, help="Path to promptfoo output JSON"
    )
    parser.add_argument("--log", help="Path to proxy log file")
    parser.add_argument("--output", required=True, help="Output markdown report path")
    args = parser.parse_args()

    promptfoo_data = parse_promptfoo_json(args.promptfoo)
    proxy_data = parse_proxy_log(args.log) if args.log else None
    generate_merged_report(promptfoo_data, proxy_data, args.output)


if __name__ == "__main__":
    main()
