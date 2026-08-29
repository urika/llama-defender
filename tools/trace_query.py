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
    TraceStore, iter_jsonl, parse_ts, fmt_ms, replay_ledger_actions, spearman)


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
    """diag/sessions 轮记录 + metrics(request_id) + ledger 该轮 action 数
    + ifc Tier-0 段 + hbe 影子探针(按 turn join, result==ok) → 行列表。"""
    diag_rows = store.diag_by_session().get(key) or []
    metrics_idx = store.metrics_by_request()
    hbe_by_turn = {}
    for h in store.hbe_by_session().get(key) or []:
        if h.get("result") == "ok" and isinstance(h.get("h_mean_bits"), (int, float)):
            hbe_by_turn[h.get("turn")] = h
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
        ifc = d.get("ifc") or {}
        hbe = hbe_by_turn.get(d.get("turn")) or {}
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
            # IFC 信息面(R9.1 Tier-0 + R9.2 影子探针 join)
            "ifc_ile": bool(ifc.get("ile")),
            "ifc_kinds": ifc.get("ile_kinds") or [],
            "retention": ifc.get("retention"),
            "rationale_ratio": ifc.get("rationale_ratio"),
            "reread_pressure": ifc.get("reread_pressure"),
            "action_div": ifc.get("action_div"),
            "manifest_lines": ifc.get("manifest_lines"),
            "h_be": hbe.get("h_mean_bits"),
            "hbe_coverage": hbe.get("coverage_mean"),
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


def _failure_flagged(row):
    """失败代理标记(v0 口径): 循环/重读/截断摘要等注入干预, 或 5xx/error。"""
    if row.get("feedback_injected"):
        return True
    if row.get("error_type"):
        return True
    st = str(row.get("status") or "")
    return st.startswith("5")


def _ifc_validity_summary(rows):
    """Phase 2 效度脚手架: H_BE×损失相关 + ILE→失败前瞻列联。"""
    both = [r for r in rows
            if isinstance(r.get("h_be"), (int, float))
            and isinstance(r.get("retention"), (int, float))]
    rho = spearman([r["h_be"] for r in both],
                   [1.0 - r["retention"] for r in both]) if len(both) >= 3 else None
    rho_ra = None
    ra = [r for r in rows
          if isinstance(r.get("h_be"), (int, float))
          and isinstance(r.get("rationale_ratio"), (int, float))]
    if len(ra) >= 3:
        rho_ra = spearman([r["h_be"] for r in ra],
                          [1.0 - r["rationale_ratio"] for r in ra])
    # 前瞻列联: 上一轮 ILE 后本轮失败率 vs 基线失败率
    base_fail = sum(1 for r in rows if _failure_flagged(r))
    n = len(rows)
    after_fail = after_n = 0
    for prev, cur in zip(rows, rows[1:]):
        if prev.get("ifc_ile"):
            after_n += 1
            if _failure_flagged(cur):
                after_fail += 1
    return {
        "turns": n,
        "hbe_samples": len(both),
        "spearman_hbe_vs_loss": rho,
        "spearman_hbe_vs_rationale_loss": rho_ra,
        "failure_rate_baseline": round(base_fail / n, 4) if n else None,
        "failure_rate_after_ile": round(after_fail / after_n, 4) if after_n else None,
        "ile_turns": sum(1 for r in rows if r.get("ifc_ile")),
        "ile_after_n": after_n,
    }


def cmd_ifc(store, args):
    """IFC 信息面: per-turn Tier-0 × H_BE join + Phase 2 效度相关脚手架。"""
    if args.session:
        keys = [args.session]
    else:
        keys = sorted(store.diag_by_session().keys())[-args.limit:]
    out = []
    for key in keys:
        rows = _join_turn_rows(store, key)
        if not rows:
            continue
        if args.json:
            out.append({"session_key": key,
                        "summary": _ifc_validity_summary(rows),
                        "turns": rows})
            continue
        print("== session %s | IFC 信息面 ==" % key)
        print("%-4s %-3s %-8s %-9s %-6s %-5s %-8s %-6s %-4s %s" % (
            "TURN", "ILE", "RETAIN", "RATIONALE", "REREAD", "ADIV", "H_BE", "MANIF", "ST", "KINDS/INJ"))
        for r in rows:
            def _f(v, fmt="%.3f"):
                return fmt % v if isinstance(v, (int, float)) else "-"
            print("%-4s %-3s %-8s %-9s %-6s %-5s %-8s %-6s %-4s %s" % (
                r.get("turn"), "Y" if r.get("ifc_ile") else "-",
                _f(r.get("retention")), _f(r.get("rationale_ratio")),
                r.get("reread_pressure") if r.get("reread_pressure") is not None else "-",
                _f(r.get("action_div"), "%.2f"),
                _f(r.get("h_be"), "%.2f"),
                r.get("manifest_lines") if r.get("manifest_lines") is not None else "-",
                r.get("status") if r.get("status") is not None else "?",
                ",".join((r.get("ifc_kinds") or []) + (r.get("feedback_injected") or []))[:32] or "-"))
        print("  效度: %s" % json.dumps(_ifc_validity_summary(rows), ensure_ascii=False))
    if args.json:
        return out


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

    p = sub.add_parser("ifc", help="IFC 信息面: Tier-0×H_BE join + 效度相关(Phase 2)")
    p.add_argument("session_key", nargs="?", default=None, help="单会话; 缺省=最近 N 会话")
    p.add_argument("--limit", type=int, default=5, help="缺省模式下的会话数")

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
        "failures": cmd_failures, "last": cmd_last, "ifc": cmd_ifc,
    }
    result = handlers[args.cmd](store, args)
    if args.json and result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
