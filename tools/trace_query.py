#!/usr/bin/env python3
"""trace_query.py — B1 跨流统一查询 CLI（Phase B，代理侧自用，不进集成契约）。

按 session/turn/status/request_id join proxy_requests + proxy_metrics +
diag/sessions + diag/ledger，输出人类表格（默认）或 JSON（--json）。

用法:
  python3 tools/trace_query.py sessions [--limit N]
  python3 tools/trace_query.py show <session_key> [--turns N]
  python3 tools/trace_query.py request <request_id>
  python3 tools/trace_query.py failures [--status 500] [--since "08-20 15:00"] [--session KEY]
  python3 tools/trace_query.py last [--hours N] [--session KEY]

数据源主文件（.1 轮转件不含）；--logs-dir 可覆盖 logs 目录。
"""
import argparse
import json
import sys
from collections import Counter

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from trace_common import (  # noqa: E402
    TraceStore, iter_jsonl, parse_ts, fmt_ms, replay_ledger_actions)


# ============================================================================
# 子命令实现
# ============================================================================

def cmd_sessions(store, args):
    """跨流会话清单（diag 优先，补 ledger/archive 档案会话——含已驱逐）。"""
    overview = store.sessions_overview()
    diag = store.diag_by_session()
    rows = []
    for key, item in overview.items():
        diag_rows = diag.get(key) or []
        ok = sum(1 for r in diag_rows if (r.get("_status") is not None))
        rows.append({
            "session_key": key,
            "sources": sorted(item["sources"]),
            "diag_turns": diag_rows[-1].get("turn") if diag_rows else None,
            "turns": item.get("turns"),
            "last_ts": item.get("last_ts"),
        })
    rows.sort(key=lambda r: r.get("last_ts") or "", reverse=True)
    rows = rows[:args.limit]
    if args.json:
        return rows
    if not rows:
        print("(无会话记录)")
        return
    print("%-18s %-6s %-28s %s" % ("SESSION", "TURNS", "LAST_TS", "SOURCES"))
    for r in rows:
        print("%-18s %-6s %-28s %s" % (
            r["session_key"],
            r.get("diag_turns") if r.get("diag_turns") is not None else (r.get("turns") or "-"),
            (r.get("last_ts") or "-")[:19].replace("T", " "),
            ",".join(r["sources"])))


def _join_turn_rows(store, key):
    """diag/sessions 轮记录 + metrics(request_id) + ledger 该轮 action 数 → 行列表。"""
    diag_rows = store.diag_by_session().get(key) or []
    metrics_idx = store.metrics_by_request()
    # ledger: turn → (新增 action 数, mismatch)
    ledger_turns = {}
    for delta in store.ledger_deltas(key):
        turn = delta.get("turn") or 0
        item = ledger_turns.setdefault(turn, {"actions": 0, "mismatch": False})
        item["actions"] += len(delta.get("actions") or [])
        item["mismatch"] = item["mismatch"] or bool(delta.get("mismatch"))
    rows = []
    for d in diag_rows:
        rid = d.get("request_id")
        m = metrics_idx.get(rid) or {}
        led = ledger_turns.get(d.get("turn")) or {}
        rows.append({
            "turn": d.get("turn"),
            "ts": d.get("ts"),
            "request_id": rid,
            "status": m.get("status"),
            "ttft_ms": d.get("ttft_ms") if isinstance(d.get("ttft_ms"), (int, float)) else m.get("ttft_ms"),
            "duration_ms": d.get("duration_ms") if isinstance(d.get("duration_ms"), (int, float)) else m.get("duration_ms"),
            "hit_ratio": d.get("hit_ratio"),
            "feedback_injected": d.get("feedback_injected") or [],
            "route_target": d.get("route_target"),
            "actual_model": d.get("actual_model"),
            "error_type": m.get("error_type"),
            "ledger_actions": led.get("actions"),
            "canonical_mismatch": bool(d.get("canonical_mismatch")),
        })
    return rows


def cmd_show(store, args):
    """会话全景: 每轮 join 行（时间线 + 注入 + 台账动作计数）。"""
    rows = _join_turn_rows(store, args.session_key)
    if args.turns:
        rows = rows[-args.turns:]
    if args.json:
        return rows
    if not rows:
        print("(会话 %s 无 diag/sessions 记录——检查 key 或 --logs-dir)" % args.session_key)
        return
    print("== session %s | %d turns ==" % (args.session_key, len(rows)))
    print("%-4s %-14s %-5s %-8s %-8s %-7s %-18s %-4s %s" % (
        "TURN", "TS", "ST", "TTFT", "DUR", "HIT", "INJECT", "ACT", "MODEL/ERR"))
    for r in rows:
        inj = ",".join(r["feedback_injected"])[:18] if r["feedback_injected"] else "-"
        st = r.get("status") if r.get("status") is not None else "?"
        tail = r.get("actual_model") or ""
        if r.get("error_type"):
            tail += " !" + str(r["error_type"])
        if r.get("canonical_mismatch"):
            tail += " !mismatch"
        print("%-4s %-14s %-5s %-8s %-8s %-7s %-18s %-4s %s" % (
            r.get("turn"), (r.get("ts") or "")[11:19], st,
            fmt_ms(r.get("ttft_ms")), fmt_ms(r.get("duration_ms")),
            ("%.2f" % r["hit_ratio"]) if isinstance(r.get("hit_ratio"), (int, float)) else "-",
            inj, r.get("ledger_actions") if r.get("ledger_actions") is not None else "-",
            tail))


def cmd_request(store, args):
    """单请求详情: requests + metrics(阶段分解) + diag + ledger 对应轮。"""
    rid = args.request_id
    req = None
    for rec in iter_jsonl(store.requests_path):
        if rec.get("request_id") == rid:
            req = rec
            break
    met = store.metrics_by_request().get(rid)
    diag = None
    session_key = None
    for key, rows in store.diag_by_session().items():
        for r in rows:
            if r.get("request_id") == rid:
                diag = r
                session_key = key
                break
        if diag:
            break
    led_delta = None
    if diag is not None:
        for delta in store.ledger_deltas(session_key):
            if delta.get("turn") == diag.get("turn"):
                led_delta = delta
                break
    out = {"request": req, "metrics": met, "diag": diag, "ledger_delta": led_delta}
    if args.json:
        return out
    if req:
        print("== request %s ==" % rid)
        print("  session=%s model=%s status=%s dur=%s in=%s out=%s" % (
            req.get("session_id"), req.get("model"), req.get("status"),
            fmt_ms(req.get("duration_ms")), req.get("input_chars"), req.get("output_chars")))
    else:
        print("== request %s（requests.jsonl 无记录——可能已轮转）==" % rid)
    if met:
        pl = met.get("pipeline") or {}
        print("  pipeline 阶段（>10ms）:")
        stages = []
        for name, val in pl.items():
            ms = None
            if isinstance(val, dict):
                ms = val.get("elapsed_ms") or val.get("pipeline_total_ms")
            if isinstance(ms, (int, float)) and ms > 10:
                stages.append((ms, name))
        for ms, name in sorted(stages, reverse=True)[:12]:
            print("    %-24s %s" % (name, fmt_ms(ms)))
        if met.get("error"):
            print("  error: %s: %s" % (met.get("error_type"), str(met.get("error"))[:160]))
    if diag:
        print("  diag: turn=%s route=%s hit_ratio=%s inj=%s" % (
            diag.get("turn"), diag.get("route_target"), diag.get("hit_ratio"),
            ",".join(diag.get("feedback_injected") or []) or "-"))
    if led_delta:
        tools = [a.get("tool") for a in led_delta.get("actions") or []]
        print("  ledger: turn=%s actions=%s mismatch=%s" % (
            led_delta.get("turn"), tools or "-", led_delta.get("mismatch")))
    if not (req or met or diag):
        print("(全流均无此 request_id)")


def cmd_failures(store, args):
    """失败请求清单（requests.jsonl 为主，join metrics 错误类型 + diag 会话）。"""
    since = parse_ts(args.since) if args.since else None
    metrics_idx = store.metrics_by_request()
    wanted = {int(s) for s in args.status} if args.status else None
    rows = []
    for rec in iter_jsonl(store.requests_path):
        status = rec.get("status")
        if status == 200:
            continue
        if wanted and status not in wanted:
            continue
        if args.session and rec.get("session_id") != args.session:
            continue
        ts = parse_ts(rec.get("start_time"))
        if since and ts and ts < since:
            continue
        rid = rec.get("request_id")
        met = metrics_idx.get(rid) or {}
        rows.append({
            "start_time": rec.get("start_time"),
            "session_id": rec.get("session_id"),
            "request_id": rid,
            "status": status,
            "duration_ms": rec.get("duration_ms"),
            "input_chars": rec.get("input_chars"),
            "error_type": met.get("error_type"),
            "model": rec.get("model"),
        })
    rows.sort(key=lambda r: r.get("start_time") or "")
    if args.limit:
        rows = rows[-args.limit:]
    if args.json:
        return rows
    if not rows:
        print("(无匹配失败请求)")
        return
    by_status = Counter(r["status"] for r in rows)
    print("== failures %s ==" % dict(by_status))
    print("%-19s %-14s %-5s %-9s %-9s %s" % (
        "START", "SESSION", "ST", "DUR", "IN_CHARS", "ERROR"))
    for r in rows:
        print("%-19s %-14s %-5s %-9s %-9s %s" % (
            (r.get("start_time") or "")[:19].replace("T", " "),
            r.get("session_id") or "-",
            r.get("status"), fmt_ms(r.get("duration_ms")),
            r.get("input_chars"), r.get("error_type") or "-"))


def cmd_last(store, args):
    """最近请求流（requests.jsonl，A1 起可按会话归因）。"""
    since = None
    if args.hours:
        from datetime import timedelta
        from datetime import datetime
        since = datetime.now() - timedelta(hours=args.hours)
    rows = []
    for rec in iter_jsonl(store.requests_path):
        if args.session and rec.get("session_id") != args.session:
            continue
        ts = parse_ts(rec.get("start_time"))
        if since and ts and ts < since:
            continue
        rows.append(rec)
    rows = rows[-args.limit:]
    if args.json:
        return rows
    if not rows:
        print("(无匹配请求)")
        return
    ok = sum(1 for r in rows if r.get("status") == 200)
    print("== last %d requests | %d ok / %d fail ==" % (len(rows), ok, len(rows) - ok))
    print("%-19s %-14s %-5s %-9s %-9s %s" % (
        "START", "SESSION", "ST", "DUR", "IN_CHARS", "MODEL"))
    for r in rows:
        print("%-19s %-14s %-5s %-9s %-9s %s" % (
            (r.get("start_time") or "")[:19].replace("T", " "),
            r.get("session_id") or "-", r.get("status"),
            fmt_ms(r.get("duration_ms")), r.get("input_chars"),
            r.get("model") or "-"))


# ============================================================================
# CLI
# ============================================================================

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="跨流轨迹查询: requests/metrics/diag/ledger 按会话·轮次·请求关联")
    parser.add_argument("--logs-dir", default=None, help="覆盖 logs 目录(默认仓库 logs/)")
    parser.add_argument("--json", action="store_true", help="输出 JSON(机器可读)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sessions", help="跨流会话清单(含档案会话)")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("show", help="会话每轮 join 全景")
    p.add_argument("session_key")
    p.add_argument("--turns", type=int, default=0, help="仅展示最近 N 轮")

    p = sub.add_parser("request", help="单请求详情(阶段分解/错误/台账)")
    p.add_argument("request_id")

    p = sub.add_parser("failures", help="失败请求清单(500/499/...)")
    p.add_argument("--status", nargs="*", default=None, help="过滤状态码,如 500 504")
    p.add_argument("--since", default=None, help='起始时间 "MM-DD HH:MM" 或 ISO')
    p.add_argument("--session", default=None)
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("last", help="最近请求流")
    p.add_argument("--hours", type=float, default=0, help="最近 N 小时")
    p.add_argument("--session", default=None)
    p.add_argument("--limit", type=int, default=30)

    args = parser.parse_args(argv)
    store = TraceStore(args.logs_dir)
    handlers = {
        "sessions": cmd_sessions, "show": cmd_show, "request": cmd_request,
        "failures": cmd_failures, "last": cmd_last,
    }
    result = handlers[args.cmd](store, args)
    if args.json and result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
