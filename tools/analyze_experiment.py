#!/usr/bin/env python3
"""
Analyze experiment logs from anthropic_proxy.py.

注意：A/B 对比报告已迁移到 Promptfoo。
本脚本仅保留单日志解析功能，供 collect 阶段和 Promptfoo 集成使用。

用法：
  python3 analyze_experiment.py --log <logfile> --output <json> --group A --task '...' --exp-id '...'
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


# A/B 对比报告已迁移到 Promptfoo。详见 tools/promptfoo_eval.sh


def main():
    parser = argparse.ArgumentParser(
        description="Parse single proxy log and output metrics JSON."
    )
    parser.add_argument("--log", required=True, help="Single log file to analyze")
    parser.add_argument("--output", help="Output JSON file")
    parser.add_argument("--group", help="Experiment group label (A/B)")
    parser.add_argument("--task", help="Task description")
    parser.add_argument("--exp-id", help="Experiment ID")
    args = parser.parse_args()

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
    return summary


if __name__ == "__main__":
    main()
