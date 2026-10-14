#!/usr/bin/env python3
"""Jev shadow harness：规则判断点 → Jev MCP 离线比对（旁路观测，批期安全）。

用途
----
把管线里**规则触发**的判断点（当前接 loop_l1）现场重放给 Jev 决策模型，
落盘「规则结论 vs Jev 结论」供级联校准（φ_accept/ξ_abstain 覆盖-准确率曲线）。

设计约束（级联文档 §3「shadow 先行」+ 批期红线）
------------------------------------------------
- **只读日志，不碰运行路径**：只消费 logs/diag/{sessions,archive}/*.jsonl，
  绝不写代理状态、绝不改 pipeline、不打 :4000/:8081。
- **离线批处理**：从台账抽出 loop_l1 触发点 → 从 archive 取该轮 sent_view
  → 构造 Jev `choice` 问句 → 调 Jev → 落盘 logs/diag/jev_shadow/。
- **fail-open**：任何一步失败只记 error 字段，不抛出、不阻塞。

数据流
------
sessions.jsonl(找 loop_l1 触发 turn)
  → archive/<sid>.jsonl(取该 turn 的 payload.messages 尾部工具序列)
  → 构造 state + choice 问句
  → Jev decide
  → jev_shadow.jsonl(rule_verdict / jev_choice / band / raw)

依赖
----
- tools/jev_mcp_server.py 的 _call_jev / _validate_request（同目录复用）。
- 密钥读取顺序与 MCP server 一致：env CMD_API_KEY → ~/.zshrc 回落。
- 纯标准库。

用法
----
  # 抽最近 N 个 loop_l1 触发点重放（N=0 → 只统计不调用）
  python3 tools/jev_shadow.py --tail 20

  # 只统计规则触发点分布，不打 Jev
  python3 tools/jev_shadow.py --tail 0

  # 自定义双阈值分带（默认按臂取：cloud φ=0.80 / local φ=0.50，ξ=0.40）
  python3 tools/jev_shadow.py --tail 20 --phi 0.80 --xi 0.40

  # 分臂聚合摘要（云臂 + 本地对照臂分别统计）
  python3 tools/jev_shadow.py --summary

输出
----
logs/diag/jev_shadow.jsonl（append-only，每行一个比对样本）
聚合摘要打 stdout；--summary 读回历史落盘。
"""

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import jev_mcp_server as jms  # noqa: E402  复用 API 契约

_REPO = os.path.dirname(_HERE)
SESS_LOG = os.path.join(_REPO, "logs", "diag", "sessions.jsonl")
ARCHIVE_DIR = os.path.join(_REPO, "logs", "diag", "archive")
OUT_PATH = os.path.join(_REPO, "logs", "diag", "jev_shadow.jsonl")

# 级联双阈值默认（arXiv:2502.09054 思想；具体值由本 harness 落盘数据校准）
DEFAULT_PHI_ACCEPT = 0.80
DEFAULT_XI_ABSTAIN = 0.40

# 分臂阈值（2026-10-15 效用裁决：**行动阈值宁严勿松，双臂同严 0.80**）。
# 依据：① 本地 RSI-Jev 概率系统性平坦（p_prog 中位 0.49/max 0.62，effort
# low/medium/high/auto 四档 max 仅 0.56-0.65，全部摸不到 0.80——平坦是模型
# 固有特性）；② 0.50 上的「确信」是刻度假象非校准证据，错判成本（放走真循环/
# 错推翻干预）≫ 灰区升级成本（9B 本地免费）。
# 0.50 仅为离线**测量对齐**值（band 一致率 84% 扫描峰值），留档不进行动路径；
# 下调资格 = holdout 上证明该阈值 band precision 达标（n≥100 + 参考标签）。
# 纪律：切臂必须同时核对阈值——分臂常量保留，防将来接本地臂时漏配。
ARM_PHI = {"cloud": DEFAULT_PHI_ACCEPT, "local": DEFAULT_PHI_ACCEPT}
ARM_XI = {"cloud": DEFAULT_XI_ABSTAIN, "local": DEFAULT_XI_ABSTAIN}
LOCAL_COMPARE_PATH = os.path.join(_REPO, "logs", "diag",
                                  "jev_shadow_local_compare.jsonl")


def phi_for(arm):
    """按臂取 accept 阈值；未知臂回落云默认（向后兼容无 arm 字段的历史行）。"""
    return ARM_PHI.get(arm, DEFAULT_PHI_ACCEPT)

# 送进 Jev 的工具序列尾部长度（省 token，聚焦最近行为）
TAIL_TOOL_EVENTS = 14
TAIL_TEXT_CHARS = 400


def _load_api_key():
    """env 优先，回落 ~/.zshrc 单源（与 jev_quota.load_key 语义一致，但不强依赖）。"""
    key = os.environ.get("CMD_API_KEY", "").strip()
    if key:
        return key
    try:
        import jev_quota  # type: ignore
        return (jev_quota.load_key() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 触发点抽取

def scan_loop_triggers(sessions_path=SESS_LOG, tail=0):
    """扫描 sessions.jsonl 里 feedback_injected 含 loop_l1 的轮次。

    返回按时间升序的触发点列表（tail>0 时只保留最后 tail 个）。
    每项：{sid, key_source, turn, ts, request_id, loop:{...}, prefix_ratio}
    """
    hits = []
    if not os.path.exists(sessions_path):
        return hits
    with open(sessions_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            fb = d.get("feedback_injected") or []
            if "loop_l1" not in fb:
                continue
            hits.append({
                "sid": d.get("session_key", ""),
                "key_source": d.get("key_source", ""),
                "turn": d.get("turn"),
                "ts": d.get("ts", ""),
                "request_id": d.get("request_id", ""),
                "route_target": d.get("route_target"),
                "actual_model": d.get("actual_model"),
                "prefix_ratio": d.get("prefix_ratio"),
            })
    hits.sort(key=lambda x: (x.get("ts") or "", x.get("turn") or 0))
    if tail and tail > 0:
        hits = hits[-tail:]
    return hits


def _find_archive(sid):
    """按 sid 定位 archive 文件（含 key_source 变体：header/fallback 同名）。"""
    if not sid:
        return None
    p = os.path.join(ARCHIVE_DIR, sid + ".jsonl")
    return p if os.path.exists(p) else None


def load_turn_view(sid, turn):
    """从 archive/<sid>.jsonl 取指定 turn 的 payload（模型实际所见）。

    返回 (payload_dict | None, note)。
    """
    path = _find_archive(sid)
    if not path:
        return None, "archive_missing"
    want = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("turn") == turn:
                    want = rec
                    break  # 每 turn 一条记录，命中即停（文件可达几十 MB）
                if isinstance(rec.get("turn"), int) and rec["turn"] > (turn or 0):
                    break  # turn 升序，越界即停
    except OSError as exc:
        return None, "archive_read_error:%s" % exc
    if not want:
        return None, "turn_not_in_archive"
    if want.get("payload_truncated"):
        return None, "payload_truncated"
    payload = want.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None, "payload_bad_json"
    if not isinstance(payload, dict):
        return None, "payload_not_dict"
    return payload, "ok"


# ---------------------------------------------------------------- 状态构造

def _iter_tool_events(messages):
    """从 messages 抽工具事件序列：[{role, tool, input_snip, result_snip}]。

    只保留有辨识度的字段（file_path/command 的前缀），避免整包送出去。
    """
    events = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "tool_use":
                inp = b.get("input") or {}
                snip = ""
                if isinstance(inp, dict):
                    for k in ("file_path", "command", "pattern", "path", "url"):
                        if k in inp and isinstance(inp[k], str):
                            snip = "%s=%s" % (k, inp[k][:120])
                            break
                    if not snip:
                        snip = json.dumps(inp, ensure_ascii=False)[:120]
                events.append({"role": role, "tool": b.get("name", "?"),
                               "input": snip, "result": ""})
            elif btype == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(
                        x.get("text", "") for x in c if isinstance(x, dict)
                    )
                if not isinstance(c, str):
                    c = str(c)
                if events and events[-1].get("role") == "assistant":
                    events[-1]["result"] = c[:200]
                else:
                    events.append({"role": role, "tool": "(result)",
                                   "input": "", "result": c[:200]})
    return events


def _recent_text(messages, chars=TAIL_TEXT_CHARS):
    """取最近几条 assistant 文本片段（判断意图/是否自述卡住）。"""
    out = []
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append(b.get("text", ""))
        t = " ".join(texts).strip()
        if t:
            out.append(t[:200])
        if sum(len(x) for x in out) >= chars:
            break
    return " | ".join(reversed(out))[:chars]


def build_state(payload):
    """把 sent_view payload 压成 Jev state（字符串，聚焦最近工具行为）。"""
    messages = payload.get("messages") or []
    events = _iter_tool_events(messages)[-TAIL_TOOL_EVENTS:]
    lines = []
    for e in events:
        if e.get("tool") == "(result)":
            continue
        line = "[%s] %s %s" % (e.get("role"), e.get("tool"), e.get("input"))
        if e.get("result"):
            line += " -> %s" % e["result"][:120]
        lines.append(line)
    recent = _recent_text(messages)
    state = (
        "Agent coding session. Recent tool-action sequence (chronological):\n"
        + "\n".join(lines)
        + ("\n\nRecent assistant reasoning snippets: " + recent if recent else "")
    )
    return state[:6000]


# ---------------------------------------------------------------- Jev 问句

LOOP_QUESTIONS = {
    "loop_assessment": {
        "type": "choice",
        "instructions": (
            "Based on the recent tool-action sequence and reasoning snippets, "
            "is the agent genuinely stuck in a non-productive loop, or is it "
            "making forward progress with legitimately distinct steps?"
        ),
        "criteria": {
            "stuck_loop": (
                "The agent is repeating the same or near-identical action "
                "(same tool + same target/args + same outcome) with no sign of "
                "progress or strategy change."
            ),
            "progressing": (
                "Same tool appears repeatedly but each call is a distinct, "
                "purposeful step (different files/args/commands) advancing a "
                "coherent debugging or editing task."
            ),
            "blocked_retry": (
                "The agent is retrying after failures/errors, but shows "
                "adjustment between attempts (not identical retries)."
            ),
            "insufficient_evidence": (
                "The visible sequence does not contain enough distinct signals "
                "to judge looping vs progress."
            ),
        },
    }
}


def band_for(probs, phi, xi):
    """双阈值三带 v2（按被选中类的置信度定向，修复「确信否定」误归低置信档）。

    - p(stuck_loop) ≥ φ        → accept_rule_verdict   （Jev 确认规则干预正当）
    - p(其他类) ≥ φ 且最大      → jev_contradicts_rule （Jev 确信规则是假阳性——校准黄金样本）
    - 其余                     → gray_zone_escalate    （真灰区，应升级 Tier 2）
    """
    p_stuck = float(probs.get("stuck_loop", 0.0))
    p_prog = float(probs.get("progressing", 0.0))
    p_blocked = float(probs.get("blocked_retry", 0.0))
    p_insuf = float(probs.get("insufficient_evidence", 0.0))
    if p_stuck >= phi:
        return "accept_rule_verdict"
    if max(p_prog, p_blocked, p_insuf) >= phi:
        return "jev_contradicts_rule"
    return "gray_zone_escalate"


# ---------------------------------------------------------------- 主流程

def run(tail, phi, xi, dry_run, key):
    triggers = scan_loop_triggers(tail=tail)
    print("loop_l1 触发点总数(全量): %d; 本次取尾: %d"
          % (len(scan_loop_triggers()), len(triggers)))
    if not triggers:
        print("无触发点可重放。")
        return []

    results = []
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    out_fh = open(OUT_PATH, "a", encoding="utf-8")
    ok = err = 0
    try:
        for t in triggers:
            payload, note = load_turn_view(t["sid"], t["turn"])
            rec = {
                "ts": t["ts"], "sid": t["sid"], "turn": t["turn"],
                "request_id": t["request_id"], "rule_verdict": "loop_l1",
                "arm": "cloud", "route_target": t["route_target"],
                "phi": phi, "xi": xi, "view_note": note,
            }
            if payload is None:
                rec["error"] = note
                out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                err += 1
                results.append(rec)
                continue
            state = build_state(payload)
            rec["state_chars"] = len(state)
            if dry_run:
                rec["dry_run"] = True
                out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                ok += 1
                results.append(rec)
                continue
            if not key:
                rec["error"] = "no_api_key"
                out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                err += 1
                results.append(rec)
                break
            try:
                ans = jms._call_jev({"state": state, "questions": LOOP_QUESTIONS}, key)
                loop_ans = (ans.get("answers") or {}).get("loop_assessment") or {}
                probs = loop_ans.get("probabilities") or {}
                p_stuck = float(probs.get("stuck_loop", 0.0))
                rec.update({
                    "jev_choice": loop_ans.get("choice"),
                    "jev_confidence": loop_ans.get("confidence"),
                    "p_stuck_loop": round(p_stuck, 4),
                    "p_progressing": round(float(probs.get("progressing", 0.0)), 4),
                    "p_blocked_retry": round(float(probs.get("blocked_retry", 0.0)), 4),
                    "p_insufficient": round(float(probs.get("insufficient_evidence", 0.0)), 4),
                    "band": band_for(probs, phi, xi),
                    "usage": ans.get("usage"),
                })
                ok += 1
            except Exception as exc:  # noqa: BLE001  fail-open
                rec["error"] = str(exc)[:300]
                err += 1
            out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            results.append(rec)
            time.sleep(0.15)  # 轻微限速，避免打爆额度窗口
    finally:
        out_fh.close()

    _print_summary(results)
    print("\n落盘: %s  (ok=%d err=%d)" % (OUT_PATH, ok, err))
    return results


def _print_summary(results):
    # 从落盘概率重算分带（不信任历史存储标签，兼容 v1 分带残留）
    judged = []
    for r in results:
        if r.get("p_stuck_loop") is None:
            continue
        r = dict(r)
        r["band"] = band_for(
            {"stuck_loop": r.get("p_stuck_loop", 0.0),
             "progressing": r.get("p_progressing", 0.0),
             "blocked_retry": r.get("p_blocked_retry", 0.0),
             "insufficient_evidence": r.get("p_insufficient", 0.0)},
            r.get("phi") or phi_for(r.get("arm") or "cloud"),
            r.get("xi") or ARM_XI.get(r.get("arm") or "cloud",
                                      DEFAULT_XI_ABSTAIN),
        )
        judged.append(r)
    if not judged:
        return
    # (sid, turn) 去重：append-only 落盘可能含重放残留
    seen = set()
    dedup = []
    for r in judged:
        k = (r.get("sid"), r.get("turn"))
        if k in seen:
            continue
        seen.add(k)
        dedup.append(r)
    judged = dedup
    from collections import Counter
    bands = Counter(r["band"] for r in judged)
    choices = Counter(r.get("jev_choice") for r in judged)
    total = len(judged)
    print("\n== Jev 重放摘要（去重后 n=%d）==" % total)
    print("Jev choice 分布:", dict(choices))
    print("三带分布:", dict(bands))
    accept = bands.get("accept_rule_verdict", 0)
    contra = bands.get("jev_contradicts_rule", 0)
    print("Jev 确认规则正当: %.1f%%（%d）" % (100.0 * accept / total, accept))
    print("Jev 确信规则假阳性: %.1f%%（%d）" % (100.0 * contra / total, contra))
    print("真灰区(应升级 Tier2): %.1f%%（%d）"
          % (100.0 * bands.get("gray_zone_escalate", 0) / total,
             bands.get("gray_zone_escalate", 0)))


def _load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def _local_compare_rows():
    """从对照文件抽本地臂行，转成 _print_summary 可用的 schema。

    对照文件 schema: {sid, turn, cloud:{...}, local:{choice, p_stuck, ...}}
    转换后按 ARM_PHI["local"] 重算分带（不信任落盘 band 标签）。
    """
    rows = []
    for rec in _load_jsonl(LOCAL_COMPARE_PATH):
        loc = rec.get("local")
        if not loc or rec.get("error"):
            continue
        rows.append({
            "sid": rec.get("sid"), "turn": rec.get("turn"), "arm": "local",
            "jev_choice": loc.get("choice"),
            "p_stuck_loop": loc.get("p_stuck"),
            "p_progressing": loc.get("p_progressing"),
            "p_blocked_retry": loc.get("p_blocked_retry"),
            "p_insufficient": loc.get("p_insufficient", 0.0),
            "phi": ARM_PHI["local"], "xi": ARM_XI["local"],
        })
    return rows


def summary():
    rows = _load_jsonl(OUT_PATH)
    if not rows:
        print("无历史落盘: %s" % OUT_PATH)
        return
    print("历史样本总数: %d" % len(rows))
    # 主文件按臂分组（历史行无 arm 字段 → 归 cloud）
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        groups[r.get("arm") or "cloud"].append(r)
    for arm in sorted(groups):
        print("\n-- 臂: %s（n=%d，φ=%.2f）--"
              % (arm, len(groups[arm]), phi_for(arm)))
        _print_summary(groups[arm])
    # 本地对照臂（独立文件，schema 不同）
    local_rows = _local_compare_rows()
    if local_rows:
        print("\n-- 臂: local 对照文件（n=%d，φ=%.2f，%s）--"
              % (len(local_rows), ARM_PHI["local"],
                 os.path.basename(LOCAL_COMPARE_PATH)))
        _print_summary(local_rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Jev shadow harness (rule judgment → Jev)")
    ap.add_argument("--tail", type=int, default=20,
                    help="重放最近 N 个 loop_l1 触发点；0=只统计不调用")
    ap.add_argument("--phi", type=float, default=None,
                    help="accept 阈值 φ_accept（默认按臂取 ARM_PHI；"
                         "显式指定则覆盖本次运行）")
    ap.add_argument("--xi", type=float, default=DEFAULT_XI_ABSTAIN,
                    help="abstain 阈值 ξ_abstain")
    ap.add_argument("--dry-run", action="store_true",
                    help="只构造 state，不调用 Jev")
    ap.add_argument("--summary", action="store_true",
                    help="聚合历史落盘")
    args = ap.parse_args(argv)

    if args.summary:
        summary()
        return 0

    if args.tail <= 0 and not args.dry_run:
        # 纯统计模式：只扫触发点，不读 archive、不调 API（批期最安全姿势）
        all_hits = scan_loop_triggers(tail=0)
        from collections import Counter
        by_day = Counter((h.get("ts") or "")[:10] for h in all_hits)
        by_sid = Counter(h["sid"] for h in all_hits)
        print("loop_l1 触发点总数: %d" % len(all_hits))
        print("按天:", dict(sorted(by_day.items())))
        print("按会话 top5:", by_sid.most_common(5))
        return 0

    key = "" if args.dry_run else _load_api_key()
    if not args.dry_run and not key:
        print("缺 CMD_API_KEY（env 或 ~/.zshrc），无法调用 Jev。"
              "可先用 --dry-run 或 --tail 0。", file=sys.stderr)
        return 2

    # run() 是云臂：显式 --phi 覆盖，否则取该臂默认（phi=None 传入会崩）
    phi = args.phi if args.phi is not None else phi_for("cloud")
    run(args.tail, phi, args.xi, args.dry_run, key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
