#!/usr/bin/env python3
"""ab_compare.py — A/B(欠拉修复) 四维对照表一键生成。

对照设计:
  对照臂 = exp-2 v2 批窗口(2026-08-30T20:09 起, 旧代码无指引)
  治疗臂 = ab-underpull-fix 窗口(2026-09-01T17:00 起, 258d414+b7a22fa+49574a2+589d778 全链)
  配对键 = instance_id(同任务跨臂对比)

四维: ①pull(ctx_recall 调用数) ②reread_pressure 轮均 ③墙钟分钟 ④exit/patch
用法: python3 tools/ab_compare.py
"""
import json
import os
import statistics
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWE = os.path.join(os.path.dirname(REPO), "swe-eval")
RUNS = os.path.join(SWE, "results", "runs.jsonl")
PICKS = os.path.join(SWE, "results", "ab-treatment-picks.json")

# 窗口边界(与 experiments.jsonl 台账一致)
CTRL_WIN = "2026-08-30T20:00"   # exp-2 v2 批(对照臂)
TRT_WIN = "2026-09-01T17:00"    # ab 治疗臂重启后窗口(修复后代码)


def load_jsonl(path):
    try:
        return [json.loads(l) for l in open(path) if l.strip()]
    except (OSError, ValueError):
        return []


def patch_size(r):
    pp = r.get("patch_path")
    return os.path.getsize(pp) if pp and os.path.exists(pp) else 0


def main():
    runs = load_jsonl(RUNS)
    picks = json.load(open(PICKS)).get("tasks", [])
    pick_ids = [t["instance_id"] for t in picks]

    ctrl = [r for r in runs if r.get("target") == "llama-defender-38"
            and CTRL_WIN <= str(r.get("started_at", "")) < "2026-09-01T14:00"
            and r.get("instance_id") in pick_ids]
    trt = [r for r in runs if r.get("target") == "llama-defender-38"
           and str(r.get("started_at", "")) >= TRT_WIN
           and r.get("instance_id") in pick_ids]
    trt.sort(key=lambda r: r.get("started_at", ""))

    # 墙钟: 批内相邻 started_at 差(近似, 同批顺序执行)
    def wall_minutes(batch):
        out = {}
        ordered = sorted(batch, key=lambda r: r.get("started_at", ""))
        for i, r in enumerate(ordered):
            t0 = datetime.fromisoformat(r["started_at"])
            t1 = (datetime.fromisoformat(ordered[i + 1]["started_at"])
                  if i + 1 < len(ordered) else None)
            out[id(r)] = round((t1 - t0).total_seconds() / 60) if t1 else None
        return out

    ctrl_wall = wall_minutes(ctrl)
    trt_wall = wall_minutes(trt)

    # pull 计数: 治疗窗口内各会话的 ctx_recall 调用(ledger)
    pull_by_sid = {}
    sess_dir = os.path.join(REPO, "logs", "diag", "ledger")
    if os.path.isdir(sess_dir):
        for fn in os.listdir(sess_dir):
            sid_file = fn[:-6]
            for rec in load_jsonl(os.path.join(sess_dir, fn)):
                if str(rec.get("ts", "")) >= TRT_WIN:
                    for a in (rec.get("actions") or []):
                        if a.get("tool") == "ctx_recall":
                            pull_by_sid[sid_file] = pull_by_sid.get(sid_file, 0) + 1

    # reread: sessions.jsonl 治疗窗口逐会话
    sess = load_jsonl(os.path.join(REPO, "logs", "diag", "sessions.jsonl"))
    reread_by_sid = {}
    for r in sess:
        if str(r.get("ts", "")) >= TRT_WIN and r.get("session_key"):
            reread_by_sid.setdefault(r["session_key"], []).append(
                (r.get("ifc") or {}).get("reread_pressure", 0) or 0)

    def fmt_run(r, wall):
        code = r.get("agent_exit_code")
        sz = patch_size(r)
        return f"exit={code} patch={sz}B wall≈{wall}min"

    print("=== A/B 欠拉修复 · 任务配对对照 ===")
    print(f"{'任务…':>18} | {'对照臂(v2)':>28} | {'治疗臂(修复后)':>28} | pulls")
    for iid in pick_ids:
        c_runs = [r for r in ctrl if r.get("instance_id") == iid]
        t_runs = [r for r in trt if r.get("instance_id") == iid]
        c_desc = " / ".join(fmt_run(r, ctrl_wall.get(id(r), "?")) for r in c_runs) or "无记录"
        t_desc = " / ".join(fmt_run(r, trt_wall.get(id(r), "?")) for r in t_runs) or "待跑"
        pulls = sum(pull_by_sid.get(str(r.get("session_id", ""))[:8], 0)
                    for r in t_runs)
        print(f"…{iid[-16:]:>16} | {c_desc:>28} | {t_desc:>28} | {pulls}")

    print("\n=== 汇总 ===")
    n_ctrl_ok = sum(1 for r in ctrl if r.get("agent_exit_code") == 0)
    n_trt_ok = sum(1 for r in trt if r.get("agent_exit_code") == 0)
    n_trt = len(trt)
    total_pulls = sum(pull_by_sid.values())
    print(f"对照臂: {len(ctrl)} run, exit=0 {n_ctrl_ok}")
    print(f"治疗臂: {n_trt}/10 run, exit=0 {n_trt_ok}")
    print(f"pull 总数: {total_pulls} (对照臂基线=0)")
    if n_trt < 10:
        print(f"\n(样本未齐: {n_trt}/10, 跑完后重出此表)")


if __name__ == "__main__":
    main()
