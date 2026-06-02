#!/usr/bin/env python3
"""
Analyze experiment logs from anthropic_proxy.py.
Supports two modes:
  1. Single log analysis: --log --output --group --task --exp-id
  2. A/B comparison:     --a --b --report
"""
import argparse
import json
import re
from collections import Counter


def parse_log(path):
    with open(path, "r", errors="replace") as f:
        lines = f.readlines()

    reqs = []
    clears = []
    truncs = []
    tools_list = []
    errors = []
    stream_texts = []

    for line in lines:
        m = re.search(
            r"\[(\d{2}:\d{2}:\d{2})\].*\[REQ_SUMMARY\] chars=(\d+) tools=(\d+)",
            line,
        )
        if m:
            reqs.append(
                {
                    "time": m.group(1),
                    "chars": int(m.group(2)),
                    "tools": int(m.group(3)),
                }
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

        m = re.search(r"Streamed text=(\d+) chars, tools=(\d+)", line)
        if m:
            stream_texts.append(
                {"text": int(m.group(1)), "tools": int(m.group(2))}
            )

        if "error:" in line.lower() or "Exception" in line:
            errors.append(line.strip()[:200])

    return {
        "reqs": reqs,
        "clears": clears,
        "truncs": truncs,
        "tools": Counter(tools_list),
        "errors": errors,
        "streams": stream_texts,
    }


def summarize(data, label):
    reqs = data["reqs"]
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    if not reqs:
        print("  No requests found.")
        return {}

    chars_list = [r["chars"] for r in reqs]
    tools_list = [r["tools"] for r in reqs]

    # 计算请求大小增长趋势
    growth_list = []
    for i in range(1, len(chars_list)):
        growth_list.append(chars_list[i] - chars_list[i-1])
    avg_growth = round(sum(growth_list) / len(growth_list)) if growth_list else 0

    result = {
        "total_requests": len(reqs),
        "total_chars": sum(chars_list),
        "avg_chars": round(sum(chars_list) / len(chars_list)),
        "max_chars": max(chars_list),
        "min_chars": min(chars_list),
        "avg_tools": round(sum(tools_list) / len(tools_list), 1),
        "req_size_growth": avg_growth,
        "tool_clears": len(data["clears"]),
        "cleared_chars_total": sum(c["chars"] for c in data["clears"]),
        "truncations": len(data["truncs"]),
        "errors": len(data["errors"]),
        "tool_freq": dict(data["tools"].most_common(10)),
    }

    print(f"  总请求数:        {result['total_requests']}")
    print(f"  总字符数:        {result['total_chars']:,}")
    print(f"  平均请求大小:    {result['avg_chars']} chars")
    print(f"  最大请求:        {result['max_chars']:,}")
    print(f"  最小请求:        {result['min_chars']:,}")
    print(f"  平均增长/轮:     {result['req_size_growth']:,} chars")
    print(f"  平均工具数:      {result['avg_tools']}")

    if data["clears"]:
        avg_clear = sum(c["chars"] for c in data["clears"]) // len(data["clears"])
        print(f"  工具清理次数:    {result['tool_clears']}")
        print(f"  累计清理字符:    {result['cleared_chars_total']:,} chars")
        print(f"  平均清理:        {avg_clear} chars")
    else:
        print(f"  工具清理:        未触发")

    if data["truncs"]:
        print(f"  上下文截断次数:  {result['truncations']}")
    else:
        print(f"  上下文截断:      未触发")

    if data["streams"]:
        avg_text = sum(s["text"] for s in data["streams"]) // len(data["streams"])
        print(f"  流式响应数:      {len(data['streams'])}")
        print(f"  平均文本输出:    {avg_text} chars")

    if data["errors"]:
        print(f"  错误数:          {result['errors']}")

    print(f"\n  工具调用 TOP 10:")
    for name, count in data["tools"].most_common(10):
        print(f"    {name:30s}: {count:3d}")

    return result


def compare(a_data, b_data):
    print(f"\n{'='*60}")
    print(f"  A vs B 对比")
    print(f"{'='*60}")

    a_reqs = a_data["reqs"]
    b_reqs = b_data["reqs"]

    if a_reqs and b_reqs:
        a_chars = [r["chars"] for r in a_reqs]
        b_chars = [r["chars"] for r in b_reqs]
        print(
            f"  请求数:          A={len(a_reqs)}  B={len(b_reqs)}  "
            f"差异={len(b_reqs) - len(a_reqs):+d}"
        )
        print(
            f"  平均请求大小:    A={sum(a_chars) // len(a_reqs)}  "
            f"B={sum(b_chars) // len(b_reqs)}"
        )
        print(f"  最大请求:        A={max(a_chars):,}  B={max(b_chars):,}")

    a_clears = len(a_data["clears"])
    b_clears = len(b_data["clears"])
    print(f"  工具清理次数:    A={a_clears}  B={b_clears}  差异={b_clears - a_clears:+d}")

    a_cleared = sum(c["chars"] for c in a_data["clears"])
    b_cleared = sum(c["chars"] for c in b_data["clears"])
    print(f"  累计清理字符:    A={a_cleared:,}  B={b_cleared:,}  差异={b_cleared - a_cleared:+d}")

    a_errors = len(a_data["errors"])
    b_errors = len(b_data["errors"])
    print(f"  错误数:          A={a_errors}  B={b_errors}  差异={b_errors - a_errors:+d}")


def generate_report(a_analysis, b_analysis, a_log, b_log, report_path):
    with open(report_path, "w") as f:
        f.write("# A/B 对比实验报告\n\n")
        f.write(f"生成时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        f.write("## A 组（本地后端）\n\n")
        f.write(f"```json\n{json.dumps(a_analysis, indent=2, ensure_ascii=False)}\n```\n\n")

        f.write("## B 组（DeepSeek 云端）\n\n")
        f.write(f"```json\n{json.dumps(b_analysis, indent=2, ensure_ascii=False)}\n```\n\n")

        f.write("## 对比摘要\n\n")
        f.write("| 指标 | A 组 | B 组 | 差异 |\n")
        f.write("|------|------|------|------|\n")

        report_keys = [
            ("total_requests", "总请求数"),
            ("avg_chars", "平均请求大小(chars)"),
            ("max_chars", "最大请求(chars)"),
            ("req_size_growth", "平均每轮增长(chars)"),
            ("tool_clears", "工具清理次数"),
            ("cleared_chars_total", "累计清理字符"),
            ("truncations", "上下文截断次数"),
            ("errors", "错误数"),
        ]
        for key, label in report_keys:
            a_val = a_analysis.get(key, 0)
            b_val = b_analysis.get(key, 0)
            diff = b_val - a_val
            f.write(f"| {label} | {a_val} | {b_val} | {diff:+} |\n")

        f.write("\n## 原始日志\n\n")
        f.write(f"- A 组: `{a_log}`\n")
        f.write(f"- B 组: `{b_log}`\n")

    print(f"Report written to {report_path}")


def main():
    parser = argparse.ArgumentParser(description="Analyze experiment proxy logs")
    parser.add_argument("--log", help="Single log file to analyze")
    parser.add_argument("--output", help="Output JSON file for single analysis")
    parser.add_argument("--group", help="Experiment group label (A/B)")
    parser.add_argument("--task", help="Task description")
    parser.add_argument("--exp-id", help="Experiment ID")
    parser.add_argument("--a", help="Log file for group A")
    parser.add_argument("--b", help="Log file for group B")
    parser.add_argument("--report", help="Output markdown report path")
    args = parser.parse_args()

    if args.log:
        # Single log analysis mode
        data = parse_log(args.log)
        summary = summarize(data, f"组 {args.group or '?'} ({args.exp_id or 'unknown'})")
        if args.output:
            with open(args.output, "w") as f:
                json.dump(
                    {
                        "group": args.group,
                        "task": args.task,
                        "exp_id": args.exp_id,
                        "summary": summary,
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
            print(f"Analysis saved to {args.output}")
        return

    if args.a and args.b:
        a_data = parse_log(args.a)
        b_data = parse_log(args.b)
        summarize(a_data, "A 组（本地后端）")
        summarize(b_data, "B 组（DeepSeek 云端）")
        compare(a_data, b_data)
        if args.report:
            a_analysis = summarize(a_data, "A 组（本地后端）")
            b_analysis = summarize(b_data, "B 组（DeepSeek 云端）")
            generate_report(a_analysis, b_analysis, args.a, args.b, args.report)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
