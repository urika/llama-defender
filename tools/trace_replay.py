#!/usr/bin/env python3
"""trace_replay.py — B2 sent_view 离线投影 + A/B diff（Phase B，代理侧自用）。

把 archive（模型实际所见）+ diag/sessions（延迟/命中率）+ ledger（动作轨迹）
投影为每轮时间线 / 动作列表 / 注入标记；支持两会话（或同会话两时段）diff
——修复前后对比（click-06 复跑验证）、TS-4 压缩副作用、epoch 轮分析。

用法:
  python3 tools/trace_replay.py timeline <key> [--turns N]
  python3 tools/trace_replay.py actions <key> [--top-dup N | --from-turn N]
  python3 tools/trace_replay.py diff <keyA> <keyB> [--turns-a N] [--turns-b N]
  python3 tools/trace_replay.py html <key> [-o out.html]

数据源主文件；ledger 缺失时 actions 回退解析 archive payload（best-effort）。
"""
import argparse
import json
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from trace_common import (  # noqa: E402
    TraceStore, fmt_ms, percentile, replay_ledger_actions)


# ============================================================================
# 投影
# ============================================================================

def build_timeline(store, key, turns=None, from_turn=None):
    """时间线行: archive 轮次为主干，join diag（延迟/命中率）与 ledger（动作数）。"""
    archive = store.archive_turns(key)
    diag_idx = {d.get("turn"): d for d in store.diag_by_session().get(key) or []}
    ledger_counts = {}
    for delta in store.ledger_deltas(key):
        t = delta.get("turn") or 0
        ledger_counts[t] = ledger_counts.get(t, 0) + len(delta.get("actions") or [])
    rows = []
    for rec in sorted(archive, key=lambda r: r.get("turn") or 0):
        t = rec.get("turn")
        if from_turn is not None and (t or 0) < from_turn:
            continue
        d = diag_idx.get(t) or {}
        rows.append({
            "turn": t,
            "ts": rec.get("ts"),
            "model": rec.get("model"),
            "route_target": rec.get("route_target"),
            "messages": rec.get("messages"),
            "chars": rec.get("chars"),
            "injections": rec.get("injections") or [],
            "payload_truncated": bool(rec.get("payload_truncated")),
            "ttft_ms": d.get("ttft_ms"),
            "duration_ms": d.get("duration_ms"),
            "hit_ratio": d.get("hit_ratio"),
            "ledger_actions": ledger_counts.get(t),
        })
    if turns:
        rows = rows[-turns:]
    return rows


def extract_actions_from_archive(store, key):
    """ledger 缺失时的兜底: 解析 sent_view payload 提取 tool_use 块（best-effort）。

    返回 (actions, truncation_skipped)——truncation_skipped 为因 payload 截断
    无法解析的轮数。target 取首个字符串参数（无 ledger 归一化，dup 仅供参考）。
    """
    actions = []
    skipped = 0
    for rec in store.archive_turns(key):
        if rec.get("payload_truncated"):
            skipped += 1
            continue
        try:
            body = json.loads(rec.get("payload") or "null")
        except (json.JSONDecodeError, ValueError):
            skipped += 1
            continue
        messages = (body or {}).get("messages") or []
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    args = block.get("input") if isinstance(block.get("input"), dict) else {}
                    target = ""
                    for k in ("command", "query", "file_path", "path", "url", "pattern"):
                        v = args.get(k)
                        if isinstance(v, str) and v.strip():
                            target = v.strip()[:80]
                            break
                    actions.append({
                        "turn": rec.get("turn"),
                        "tool": block.get("name") or "",
                        "target": target,
                        "result_chars": None,
                        "source": "archive-fallback",
                    })
    return actions, skipped


def build_actions(store, key, from_turn=None):
    """动作轨迹: ledger 重放优先；缺失回退 archive 提取。返回 (actions, meta)。"""
    deltas = store.ledger_deltas(key)
    if deltas:
        actions, materials, mismatch, dropped, dup_queries = replay_ledger_actions(deltas)
        for a in actions:
            a["source"] = "ledger"
        meta = {"source": "ledger", "materials": materials,
                "canonical_mismatch_count": mismatch, "aggregated_dropped": dropped}
    else:
        actions, skipped = extract_actions_from_archive(store, key)
        groups = {}
        for a in actions:
            groups.setdefault((a["tool"], a["target"]), []).append(a)
        dup_queries = []
        for (t, tg), members in groups.items():
            if len(members) > 1:
                dup_queries.append({
                    "tool": t, "target": tg, "count": len(members),
                    "first_turn": members[0].get("turn"),
                    "last_turn": members[-1].get("turn"),
                })
        dup_queries.sort(key=lambda d: -d["count"])
        meta = {"source": "archive-fallback", "truncated_turns_skipped": skipped,
                "materials": [], "canonical_mismatch_count": None,
                "aggregated_dropped": None}
    if from_turn is not None:
        actions = [a for a in actions if (a.get("turn") or 0) >= from_turn]
        dup_queries = [d for d in dup_queries
                       if any((a.get("turn") or 0) >= from_turn for a in actions
                              if a.get("tool") == d["tool"] and a.get("target") == d["target"])]
    return actions, meta, dup_queries


def _stats_block(rows):
    """时间线行 → 汇总统计（diff/摘要共用口径）。"""
    durs = [r.get("duration_ms") for r in rows if isinstance(r.get("duration_ms"), (int, float))]
    ttfts = [r.get("ttft_ms") for r in rows if isinstance(r.get("ttft_ms"), (int, float))]
    hits = [r.get("hit_ratio") for r in rows if isinstance(r.get("hit_ratio"), (int, float))]
    inj = Counter()
    for r in rows:
        for k in r.get("injections") or []:
            inj[k] += 1
    return {
        "turns": len(rows),
        "span": [rows[0].get("ts"), rows[-1].get("ts")] if rows else None,
        "duration_p50_ms": percentile(durs, 0.5),
        "duration_p90_ms": percentile(durs, 0.9),
        "ttft_p50_ms": percentile(ttfts, 0.5),
        "ttft_p90_ms": percentile(ttfts, 0.9),
        "hit_ratio_p50": percentile(hits, 0.5),
        "injection_histogram": dict(inj),
        "truncated_payload_turns": sum(1 for r in rows if r.get("payload_truncated")),
    }


# ============================================================================
# 子命令
# ============================================================================

def cmd_timeline(store, args):
    rows = build_timeline(store, args.session_key, turns=args.turns)
    if args.json:
        return rows
    if not rows:
        print("(会话 %s 无 archive 记录)" % args.session_key)
        return
    print("== timeline %s | %d turns ==" % (args.session_key, len(rows)))
    print("%-4s %-8s %-11s %-8s %-8s %-6s %-9s %s" % (
        "TURN", "TS", "MODEL", "TTFT", "DUR", "HIT", "CHARS", "INJECT"))
    for r in rows:
        inj = ",".join(r["injections"])[:30] if r["injections"] else "-"
        model = (r.get("model") or "-").split("/")[-1][:11]
        print("%-4s %-8s %-11s %-8s %-8s %-6s %-9s %s%s" % (
            r.get("turn"), (r.get("ts") or "")[11:19], model,
            fmt_ms(r.get("ttft_ms")), fmt_ms(r.get("duration_ms")),
            ("%.2f" % r["hit_ratio"]) if isinstance(r.get("hit_ratio"), (int, float)) else "-",
            r.get("chars") or "-", inj,
            " !trunc" if r.get("payload_truncated") else ""))


def cmd_actions(store, args):
    actions, meta, dup_queries = build_actions(store, args.session_key, from_turn=args.from_turn)
    if args.json:
        return {"meta": meta, "actions": actions, "dup_queries": dup_queries}
    if not actions:
        print("(会话 %s 无动作记录——ledger 与 archive 均空)" % args.session_key)
        return
    print("== actions %s | %d actions | source=%s ==" % (
        args.session_key, len(actions), meta.get("source")))
    if meta.get("canonical_mismatch_count") is not None:
        print("   mismatch=%s dropped=%s materials=%d" % (
            meta.get("canonical_mismatch_count"), meta.get("aggregated_dropped"),
            len(meta.get("materials") or [])))
    print("%-4s %-14s %-8s %-40s %s" % ("TURN", "TOOL", "DUP", "TARGET", "RESULT"))
    for a in actions:
        print("%-4s %-14s %-8s %-40s %s" % (
            a.get("turn"), (a.get("tool") or "")[:14],
            "%d/%d" % (a.get("dup", 1), a.get("last_dup_turn", a.get("turn", 0))) if a.get("dup", 1) > 1 else "-",
            (a.get("target") or "")[:40],
            fmt_chars(a.get("result_chars"))))
    if dup_queries:
        print("-- top dup --")
        for d in dup_queries[:args.top_dup]:
            print("   x%-3d %-14s %s (turn %s-%s)" % (
                d["count"], d["tool"], (d.get("target") or "")[:50],
                d.get("first_turn"), d.get("last_turn")))


def fmt_chars(value):
    if not isinstance(value, (int, float)):
        return "-"
    return "%.1fK" % (value / 1024.0) if value >= 1024 else "%d" % value


def cmd_diff(store, args):
    """A/B 对比: 时间线统计 + 动作统计 + 注入直方图并排 + 关键差异。"""
    rows_a = build_timeline(store, args.key_a, turns=args.turns_a, from_turn=args.from_turn_a)
    rows_b = build_timeline(store, args.key_b, turns=args.turns_b, from_turn=args.from_turn_b)
    acts_a, meta_a, dup_a = build_actions(store, args.key_a, from_turn=args.from_turn_a)
    acts_b, meta_b, dup_b = build_actions(store, args.key_b, from_turn=args.from_turn_b)
    side_a = {"timeline": _stats_block(rows_a), "actions_total": len(acts_a),
              "dup_groups": len(dup_a), "top_dup": dup_a[:5],
              "materials": len(meta_a.get("materials") or [])}
    side_b = {"timeline": _stats_block(rows_b), "actions_total": len(acts_b),
              "dup_groups": len(dup_b), "top_dup": dup_b[:5],
              "materials": len(meta_b.get("materials") or [])}
    if args.json:
        return {"a": {"key": args.key_a, **side_a}, "b": {"key": args.key_b, **side_b}}
    print("== diff A=%s vs B=%s ==" % (args.key_a, args.key_b))
    hdr = "%-24s %-22s %-22s" % ("METRIC", "A", "B")
    print(hdr)
    ta, tb = side_a["timeline"], side_b["timeline"]
    fields = [
        ("turns", lambda s: s["turns"]),
        ("duration_p50", lambda s: fmt_ms(s["duration_p50_ms"])),
        ("duration_p90", lambda s: fmt_ms(s["duration_p90_ms"])),
        ("ttft_p50", lambda s: fmt_ms(s["ttft_p50_ms"])),
        ("ttft_p90", lambda s: fmt_ms(s["ttft_p90_ms"])),
        ("hit_ratio_p50", lambda s: ("%.3f" % s["hit_ratio_p50"]) if s["hit_ratio_p50"] is not None else "-"),
    ]
    for name, fn in fields:
        print("%-24s %-22s %-22s" % (name, fn(ta), fn(tb)))
    print("%-24s %-22s %-22s" % ("actions_total", side_a["actions_total"], side_b["actions_total"]))
    print("%-24s %-22s %-22s" % ("dup_groups", side_a["dup_groups"], side_b["dup_groups"]))
    print("%-24s %-22s %-22s" % ("materials", side_a["materials"], side_b["materials"]))
    print("%-24s %-22s %-22s" % ("truncated_payload_turns",
                                 ta["truncated_payload_turns"], tb["truncated_payload_turns"]))
    for label, t in (("A", ta), ("B", tb)):
        if t["injection_histogram"]:
            print("injections[%s]: %s" % (label, dict(sorted(
                t["injection_histogram"].items(), key=lambda kv: -kv[1]))))
    for label, dup in (("A", side_a["top_dup"]), ("B", side_b["top_dup"])):
        for d in dup:
            print("top_dup[%s]: x%d %s %s (turn %s-%s)" % (
                label, d["count"], d["tool"], (d.get("target") or "")[:40],
                d.get("first_turn"), d.get("last_turn")))


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>trace replay — {key}</title>
<style>
body{{font-family:-apple-system,'PingFang SC',monospace;margin:24px;color:#222;background:#fafafa}}
h1{{font-size:18px}} h2{{font-size:15px;margin-top:28px}}
table{{border-collapse:collapse;font-size:12px;background:#fff}}
th,td{{border:1px solid #ddd;padding:4px 8px;text-align:left}}
th{{background:#f0f0f0}}
.bad{{color:#c0392b;font-weight:600}} .inj{{color:#8e44ad}}
.cards{{display:flex;gap:12px;flex-wrap:wrap}}
.card{{background:#fff;border:1px solid #ddd;border-radius:6px;padding:10px 16px}}
.card .v{{font-size:20px;font-weight:700}} .card .k{{font-size:11px;color:#888}}
</style></head><body>
<h1>trace replay — {key}</h1>
<div class="cards">{cards}</div>
<h2>时间线（{n_turns} 轮）</h2>
{timeline}
<h2>动作轨迹（{n_actions} · 来源 {source}）</h2>
{actions}
<h2>重复动作 Top</h2>
{dups}
</body></html>
"""


def _html_table(headers, rows):
    out = ["<table><tr>" + "".join("<th>%s</th>" % h for h in headers) + "</tr>"]
    for r in rows:
        out.append("<tr>" + "".join("<td>%s</td>" % c for c in r) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def cmd_html(store, args):
    key = args.session_key
    rows = build_timeline(store, key)
    actions, meta, dup_queries = build_actions(store, key)
    stats = _stats_block(rows)
    cards = "".join(
        '<div class="card"><div class="v">%s</div><div class="k">%s</div></div>' % (v, k)
        for k, v in [
            (stats["turns"], "轮次"), (len(actions), "动作"),
            (fmt_ms(stats["duration_p50_ms"]), "P50 时延"),
            (fmt_ms(stats["duration_p90_ms"]), "P90 时延"),
            ("%.3f" % stats["hit_ratio_p50"] if stats["hit_ratio_p50"] is not None else "-",
             "命中率 P50"),
            (sum(stats["injection_histogram"].values()), "注入次数"),
        ])
    tl_rows = [[
        r.get("turn"), (r.get("ts") or "")[11:19], (r.get("model") or "-").split("/")[-1][:20],
        fmt_ms(r.get("ttft_ms")), fmt_ms(r.get("duration_ms")),
        "%.2f" % r["hit_ratio"] if isinstance(r.get("hit_ratio"), (int, float)) else "-",
        r.get("chars") or "-",
        '<span class="inj">%s</span>' % ",".join(r["injections"]) if r.get("injections") else "-",
        '<span class="bad">trunc</span>' if r.get("payload_truncated") else "",
    ] for r in rows]
    act_rows = [[
        a.get("turn"), a.get("tool"), a.get("dup", 1) if a.get("dup", 1) > 1 else "-",
        (a.get("target") or "")[:80], fmt_chars(a.get("result_chars")),
    ] for a in actions]
    dup_rows = [[d["count"], d["tool"], (d.get("target") or "")[:80],
                 "%s-%s" % (d.get("first_turn"), d.get("last_turn"))]
                for d in dup_queries[:15]]
    html = _HTML_TEMPLATE.format(
        key=key, cards=cards, n_turns=len(rows), n_actions=len(actions),
        source=meta.get("source"),
        timeline=_html_table(
            ["turn", "ts", "model", "ttft", "dur", "hit", "chars", "inject", ""], tl_rows),
        actions=_html_table(["turn", "tool", "dup", "target", "result"], act_rows),
        dups=_html_table(["count", "tool", "target", "turns"], dup_rows) or "(无重复)",
    )
    out = args.output or ("trace_%s_%s.html" % (key, datetime.now().strftime("%Y%m%d-%H%M%S")))
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("written: %s (turns=%d actions=%d)" % (out, len(rows), len(actions)))
    return out


# ============================================================================
# CLI
# ============================================================================

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="sent_view 离线投影: 时间线/动作轨迹/注入标记 + A/B diff")
    parser.add_argument("--logs-dir", default=None)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("timeline", help="每轮时间线(archive+diag join)")
    p.add_argument("session_key")
    p.add_argument("--turns", type=int, default=0)

    p = sub.add_parser("actions", help="动作轨迹(ledger 优先,archive 兜底)")
    p.add_argument("session_key")
    p.add_argument("--top-dup", type=int, default=10)
    p.add_argument("--from-turn", type=int, default=None)

    p = sub.add_parser("diff", help="A/B 对比(两会话或同会话两时段)")
    p.add_argument("key_a")
    p.add_argument("key_b")
    p.add_argument("--turns-a", type=int, default=0)
    p.add_argument("--turns-b", type=int, default=0)
    p.add_argument("--from-turn-a", type=int, default=None)
    p.add_argument("--from-turn-b", type=int, default=None)

    p = sub.add_parser("html", help="自包含 HTML 投影")
    p.add_argument("session_key")
    p.add_argument("-o", "--output", default=None)

    args = parser.parse_args(argv)
    store = TraceStore(args.logs_dir)
    handlers = {
        "timeline": cmd_timeline, "actions": cmd_actions,
        "diff": cmd_diff, "html": cmd_html,
    }
    result = handlers[args.cmd](store, args)
    if args.json and result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
