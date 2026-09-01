#!/usr/bin/env python3
"""ab_status.py — A/B(欠拉修复)一键状态与效果检查点。

用法: python3 tools/ab_status.py
检查点:
  A. 机制端到端: 首次折叠后, 发送视图(archive)里是否出现 ctx_recall 指引
  B. 行为信号:   模型实际 pull(ctx_recall 调用) / MICRO_TURN 触发
  C. 结局对比:   治疗臂 vs v2 对照(任务级: patch/墙钟/verdict)
"""
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWE = os.path.join(os.path.dirname(REPO), "swe-eval")
ARM_TS = "2026-09-01T14:43"  # 治疗臂批跑启动(台账留痕)


def load_jsonl(path):
    try:
        return [json.loads(l) for l in open(path) if l.strip()]
    except (OSError, ValueError):
        return []


def main():
    print(f"=== A/B 欠拉修复 · 状态 (分臂: ts >= {ARM_TS}) ===\n")

    # ---- 批进度 ----
    runs = load_jsonl(os.path.join(SWE, "results/runs.jsonl"))
    ab = [r for r in runs if str(r.get("started_at", "")) >= ARM_TS]
    done = [(r.get("agent_exit_code"), r.get("instance_id", "")[-16:]) for r in ab]
    print(f"[进度] 治疗 run 完成: {len(done)}/10")
    for code, iid in done:
        print(f"    exit={code} …{iid}")

    # ---- 治疗臂会话 ----
    sess = [r for r in load_jsonl(os.path.join(REPO, "logs/diag/sessions.jsonl"))
            if str(r.get("ts", "")) >= ARM_TS]
    by_sid = {}
    for r in sess:
        by_sid.setdefault(r.get("session_key"), []).append(r)
    print(f"\n[会话] {len(sess)} 轮 / {len(by_sid)} 会话")

    # ---- 检查点 A: 折叠是否发生 + 指引是否入视图 ----
    drop_turns = []
    hint_seen = False
    for sid, recs in by_sid.items():
        for r in recs:
            ifc = r.get("ifc") or {}
            if (ifc.get("dropped_units") or 0) > 0:
                drop_turns.append((sid, r.get("turn"), ifc.get("dropped_units")))
        if not hint_seen:
            for f in glob.glob(os.path.join(REPO, f"logs/diag/archive/{sid}.jsonl")):
                for rec in load_jsonl(f):
                    if str(rec.get("ts", "")) >= ARM_TS:
                        pv = str(rec.get("payload", ""))
                        if "ctx_recall tool" in pv and "Context folded" in pv:
                            hint_seen = True
                            break
    print(f"\n[检查点A] 折叠轮: {len(drop_turns)} (首{drop_turns[:3]})")
    print(f"  指引入模型可见面: {'✅ 已确认' if hint_seen else '⏳ 尚未(等首次折叠后抓 archive)'}")

    # ---- 检查点 B: pull 行为 ----
    pulls = 0
    pull_sites = []
    for f in glob.glob(os.path.join(REPO, "logs/diag/ledger/*.jsonl")):
        for rec in load_jsonl(f):
            if str(rec.get("ts", "")) >= ARM_TS:
                for a in (rec.get("actions") or []):
                    if a.get("tool") == "ctx_recall":
                        pulls += 1
                        pull_sites.append((rec.get("ts", "")[:16], a.get("target", "")[:40]))
    mt = os.popen(f"grep -c MICRO_TURN {os.path.join(REPO, 'logs/anthropic_proxy.log')}").read().strip()
    print(f"\n[检查点B] ctx_recall 实际调用: {pulls} {pull_sites[:3]}")
    print(f"  MICRO_TURN 重派触发: {mt}")

    # ---- 检查点 C: 任务级对比骨架(数据齐后展开) ----
    print(f"\n[检查点C] 结局对比: 待治疗臂 10 任务完成后出对照表(对照=runs.jsonl v2 窗口)")

    # ---- 判读提示 ----
    print("\n[判读]")
    if not drop_turns:
        print("  ⏳ 折叠未发生——指引未登场, 此时 pull=0 不构成证据")
    elif not hint_seen:
        print("  ⚠️ 已折叠但未见指引——检查占位符分支/抓 archive 核对")
    elif pulls == 0:
        print("  ⏳ 指引已可见但无 pull——继续累计样本; 若多任务后仍 0, 指引力度不足需升级(提示→system-reminder)")
    else:
        print(f"  ✅ pull>0: 治疗方向成立, 继续累计至 10 任务出统计")


if __name__ == "__main__":
    main()
