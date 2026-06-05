#!/usr/bin/env python3
"""
Claude Code 语义行为自动分析器
从 anthropic_proxy.py 日志中提取语义模式

用法:
    python3 tools/analyze_claude_semantics.py [日志文件路径]
    默认读取 logs/anthropic_proxy.log
"""

import json
import re
import sys
from collections import Counter
from datetime import datetime


def parse_log(filepath):
    """解析代理日志，提取结构化事件。"""
    events = []
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 匹配 [HH:MM:SS] [sess=xxx] 或 [HH:MM:SS] 前缀
            m = re.match(r'\[(\d{2}:\d{2}:\d{2})\](?:\s+\[sess=([\w-]+)\])?\s+(.*)', line)
            if not m:
                continue
            ts, sess, msg = m.group(1), m.group(2), m.group(3)
            events.append({"ts": ts, "sess": sess, "msg": msg})
    return events


def analyze_session_isolation(events):
    """分析会话隔离：有多少独立会话在并发。"""
    sessions = Counter(e["sess"] for e in events if e["sess"])
    print("=== 会话隔离语义 ===")
    print(f"活跃会话数: {len(sessions)}")
    for sess, count in sessions.most_common(10):
        print(f"  [sess={sess}]: {count} 条日志")
    print()


def analyze_request_patterns(events):
    """分析请求模式：流式 vs 非流式，工具调用频率。"""
    handling = [e for e in events if "Handling model=" in e["msg"]]
    stream_true = sum(1 for e in handling if "stream=True" in e["msg"])
    stream_false = sum(1 for e in handling if "stream=False" in e["msg"])

    streamed = [e for e in events if "Streamed text=" in e["msg"]]
    text_only = sum(1 for e in streamed if re.search(r'text=(\d+) chars, tools=0', e["msg"]))
    tools_only = sum(1 for e in streamed if re.search(r'text=0 chars, tools=[1-9]', e["msg"]))
    mixed = len(streamed) - text_only - tools_only

    print("=== 请求模式语义 ===")
    print(f"stream=True:  {stream_true} ({stream_true/(stream_true+stream_false)*100:.0f}%)")
    print(f"stream=False: {stream_false} ({stream_false/(stream_true+stream_false)*100:.0f}%)")
    print()
    print("响应类型分布:")
    print(f"  纯文本:     {text_only}")
    print(f"  纯工具:     {tools_only}")
    print(f"  文本+工具:  {mixed}")
    print()


def analyze_context_growth(events):
    """分析上下文增长模式。"""
    summaries = []
    for e in events:
        m = re.search(r'chars=(\d+)\s+tools=(\d+)', e["msg"])
        if m and "REQ_SUMMARY" in e["msg"]:
            summaries.append({
                "ts": e["ts"],
                "sess": e["sess"],
                "chars": int(m.group(1)),
                "tools": int(m.group(2)),
            })

    if len(summaries) < 2:
        print("=== 上下文增长语义 ===")
        print("数据不足")
        print()
        return

    print("=== 上下文增长语义 ===")
    print(f"总请求数: {len(summaries)}")
    print(f"上下文范围: {summaries[0]['chars']} → {summaries[-1]['chars']} chars")

    # 计算每轮增量
    deltas = []
    for i in range(1, len(summaries)):
        if summaries[i]["sess"] == summaries[i-1]["sess"]:
            deltas.append(summaries[i]["chars"] - summaries[i-1]["chars"])

    if deltas:
        avg_delta = sum(deltas) / len(deltas)
        print(f"平均每轮增量: {avg_delta:.0f} chars")

        # 找稳定阶段（连续3轮增量相近）
        stable = []
        for i in range(2, len(deltas)):
            if abs(deltas[i] - deltas[i-1]) < 1000 and abs(deltas[i-1] - deltas[i-2]) < 1000:
                stable.append(deltas[i])
        if stable:
            print(f"稳定阶段平均增量: {sum(stable)/len(stable):.0f} chars/轮")
            print(f"  （这代表 Claude Code 的'认知工作单元'大小）")
    print()


def analyze_tool_clearing(events):
    """分析工具清理模式。"""
    clearings = [e for e in events if "Tool clearing:" in e["msg"]]
    if not clearings:
        return

    print("=== 记忆管理语义 ===")
    print(f"Tool clearing 触发次数: {len(clearings)}")

    total_freed = 0
    for e in clearings[-5:]:
        m = re.search(r'(\d+) tool_results cleared, ([\d,]+) chars freed', e["msg"])
        if m:
            total_freed += int(m.group(2).replace(',', ''))
            print(f"  [sess={e['sess']}] 清理 {m.group(1)} 个结果，释放 {m.group(2)} chars")

    print(f"最近 5 次累计释放: {total_freed:,} chars")
    print("  （Claude Code 的'遗忘'不是渐进的，是代理层的批量删除）")
    print()


def analyze_model_switching(events):
    """分析模型切换行为。"""
    models = Counter()
    for e in events:
        m = re.search(r'Handling model=([\w-]+)', e["msg"])
        if m:
            models[m.group(1)] += 1

    if models:
        print("=== 模型选择语义 ===")
        for model, count in models.most_common():
            print(f"  {model}: {count} 次")
        if len(models) > 1:
            print("  （检测到模型切换，可能是任务复杂度变化或用户手动切换）")
        print()


def main():
    filepath = sys.argv[1] if len(sys.argv) > 1 else "logs/anthropic_proxy.log"
    try:
        events = parse_log(filepath)
    except FileNotFoundError:
        print(f"日志文件不存在: {filepath}")
        sys.exit(1)

    if not events:
        print("日志文件为空或格式不匹配")
        sys.exit(1)

    print(f"=== Claude Code 语义行为分析报告 ===")
    print(f"数据源: {filepath}")
    print(f"总日志行数: {len(events)}")
    print(f"时间范围: {events[0]['ts']} → {events[-1]['ts']}")
    print()

    analyze_session_isolation(events)
    analyze_request_patterns(events)
    analyze_context_growth(events)
    analyze_tool_clearing(events)
    analyze_model_switching(events)

    print("=== 核心语义洞察 ===")
    sessions = set(e["sess"] for e in events if e["sess"])
    if len(sessions) > 1:
        print(f"• 检测到 {len(sessions)} 个并发会话 — Claude Code 多窗口/多项目并行")
    else:
        print("• 单一会话 — 用户专注于单一任务流")

    handling = [e for e in events if "Handling model=" in e["msg"]]
    stream_true = sum(1 for e in handling if "stream=True" in e["msg"])
    if stream_true / max(len(handling), 1) > 0.8:
        print("• 高频 stream=True — 当前处于长文本生成阶段（代码/文档编写）")
    else:
        print("• 大量 stream=False — 当前处于工具密集型阶段（调试/验证）")

    clearings = [e for e in events if "Tool clearing:" in e["msg"]]
    if clearings:
        print("• Tool clearing 已触发 — 上下文膨胀，早期记忆被批量删除")
        print("  → 建议：监控是否出现重复 Read（同一文件被多次读取）")

    print()
    print("=== 一句话总结 ===")
    print("Claude Code 不是在'回答'你，而是在'执行'你委托的任务。")
    print("它的每一行日志都是行动的痕迹，不是对话的记录。")


if __name__ == "__main__":
    main()
