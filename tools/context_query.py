#!/usr/bin/env python3
"""context_query.py — 按 request / session / span 查询上下文管理单元引用。

数据源（全部只读主文件，.1 轮转件不含）:
  logs/proxy_metrics.jsonl        请求级 spans（含 context 单元引用）
  logs/diag/sessions.jsonl        per-turn 记录（record.trace.spans 含 context）
  logs/diag/manifest/<sid>.jsonl  被折叠/压缩单元索引行（reason/anchor/tool/handle）

用法:
  python3 tools/context_query.py request <request_id>
  python3 tools/context_query.py session <session_key> [--limit N] [--turn N]
  python3 tools/context_query.py span <span_id>
  python3 tools/context_query.py --json ...    # 机器可读

stdlib only，无第三方依赖。
"""
import argparse
import json
import os
import re
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_SCRIPT_DIR)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from trace_common import (  # noqa: E402
    TraceStore, iter_jsonl, sanitize_key)


# ---------------------------------------------------------------------------
# 纯数据访问
# ---------------------------------------------------------------------------

def manifest_rows(logs_dir, session_key):
    """某会话的 manifest 索引行（reason/anchor/tool/handle/size/head...）。"""
    path = os.path.join(logs_dir, "diag", "manifest",
                        sanitize_key(session_key) + ".jsonl")
    return list(iter_jsonl(path))


def _trace_spans(record):
    """从一条记录里取出 spans 列表（metrics 顶层 / pipeline.trace / diag.trace）。"""
    spans = record.get("spans")
    if isinstance(spans, list):
        return spans
    pipeline = record.get("pipeline") or {}
    trace = pipeline.get("trace") if isinstance(pipeline, dict) else None
    spans = (trace or {}).get("spans")
    if isinstance(spans, list):
        return spans
    trace = record.get("trace")
    spans = (trace or {}).get("spans")
    return spans if isinstance(spans, list) else []


def _context_spans(spans):
    """只保留携带 context 的 span（cache_aligner/content_compressor/...）。"""
    return [s for s in spans if isinstance(s, dict) and isinstance(s.get("context"), dict)]


def aggregate_context(spans):
    """聚合带 context 的 span → 紧凑摘要 + 单元引用明细。"""
    spans = _context_spans(spans)
    agg = {
        "stage_spans": [s.get("name") for s in spans],
        "compressed_units": 0,
        "dropped_units": 0,
        "saved_chars": 0,       # 压缩节省字符
        "dropped_chars": 0,     # 折叠/丢弃字符
        "pair_dropped": 0,
        "pair_integrity": True,
        "refs": [],
    }
    for s in spans:
        ctx = s.get("context") or {}
        agg["compressed_units"] += int(ctx.get("compressed_units") or 0)
        agg["dropped_units"] += int(ctx.get("dropped_units") or 0)
        agg["pair_dropped"] += int(ctx.get("pair_dropped") or 0)
        if not ctx.get("pair_integrity", True):
            agg["pair_integrity"] = False
        for u in ctx.get("changed_units") or []:
            action = u.get("action")
            before = int(u.get("before_chars") or 0)
            after = int(u.get("after_chars") or 0)
            if action == "compressed":
                agg["saved_chars"] += max(0, before - after)
            elif action == "dropped":
                agg["dropped_chars"] += before
            agg["refs"].append({
                "anchor": u.get("anchor"),
                "kind": u.get("kind"),
                "action": action,
                "before_chars": before,
                "after_chars": after,
                "stage": s.get("name"),
            })
    return agg


def _manifest_reasons(rows):
    reasons = {}
    for r in rows:
        k = r.get("reason") or "unknown"
        reasons[k] = reasons.get(k, 0) + 1
    return reasons


def query_request(request_id, logs_dir):
    """按 request_id 查询：metrics + diag + 上下文 spans + 对应 turn 的 manifest。"""
    store = TraceStore(logs_dir)
    metrics = store.metrics_by_request().get(request_id)
    diag = None
    session_key = None
    for key, rows in store.diag_by_session().items():
        for r in rows:
            if r.get("request_id") == request_id:
                diag = r
                session_key = key
                break
        if diag:
            break
    spans = _trace_spans(metrics) if metrics else []
    if not spans and diag is not None:
        spans = _trace_spans(diag)
    turn = diag.get("turn") if diag is not None else None
    manifest = []
    if session_key:
        manifest = [m for m in manifest_rows(logs_dir, session_key)
                    if turn is None or m.get("turn") == turn]
    return {
        "request_id": request_id,
        "session_key": session_key,
        "turn": turn,
        "metrics": {
            "status": metrics.get("status") if metrics else None,
            "trace_id": metrics.get("trace_id") if metrics else None,
            "root_span_id": metrics.get("root_span_id") if metrics else None,
            "compression_ratio": metrics.get("compression_ratio") if metrics else None,
        },
        "diag": {
            "ts": diag.get("ts") if diag else None,
            "hit_ratio": diag.get("hit_ratio") if diag else None,
            "feedback_injected": (diag or {}).get("feedback_injected") or [],
            "ifc": (diag or {}).get("ifc"),
        },
        "context": aggregate_context(spans),
        "manifest_reasons": _manifest_reasons(manifest),
        "manifest_sample": [{
            "reason": m.get("reason"), "anchor": m.get("anchor"),
            "kind": m.get("kind"), "tool": m.get("tool") or "",
            "handle": m.get("handle"), "size_chars": m.get("size_chars"),
            "turn": m.get("turn"),
        } for m in manifest[:20]],
        "manifest_count": len(manifest),
    }


def query_session(session_key, logs_dir, limit=None, turn=None):
    """按会话查询：每轮上下文摘要（diag trace spans 聚合）+ manifest 分轮统计。"""
    store = TraceStore(logs_dir)
    rows = store.diag_by_session().get(session_key) or []
    if turn is not None:
        rows = [r for r in rows if r.get("turn") == turn]
    if limit:
        rows = rows[-limit:]
    manifest = manifest_rows(logs_dir, session_key)
    per_turn = []
    for r in rows:
        t = r.get("turn")
        m_rows = [m for m in manifest if m.get("turn") == t]
        per_turn.append({
            "turn": t,
            "ts": r.get("ts"),
            "request_id": r.get("request_id"),
            "trace_id": (r.get("trace") or {}).get("trace_id")
                        or (r.get("trace_id")),
            "context": aggregate_context(_trace_spans(r)),
            "ifc": r.get("ifc"),
            "manifest_reasons": _manifest_reasons(m_rows),
            "manifest_count": len(m_rows),
            "manifest_sample": [{
                "reason": m.get("reason"), "anchor": m.get("anchor"),
                "kind": m.get("kind"), "tool": m.get("tool") or "",
                "handle": m.get("handle"), "size_chars": m.get("size_chars"),
            } for m in m_rows[:10]],
        })
    return {
        "session_key": session_key,
        "turns": len(per_turn),
        "per_turn": per_turn,
    }


def query_span(span_id, logs_dir):
    """按 span_id 定位：跨 metrics / diag 查找该 span 及其请求/会话上下文。"""
    store = TraceStore(logs_dir)
    # metrics 顶层 spans
    for rid, metrics in store.metrics_by_request().items():
        for s in _trace_spans(metrics):
            if isinstance(s, dict) and s.get("span_id") == span_id:
                return {
                    "span_id": span_id,
                    "found_in": "metrics",
                    "request_id": rid,
                    "session_key": metrics.get("session_id"),
                    "span": s,
                }
    # diag trace spans
    for key, rows in store.diag_by_session().items():
        for r in rows:
            for s in _trace_spans(r):
                if isinstance(s, dict) and s.get("span_id") == span_id:
                    return {
                        "span_id": span_id,
                        "found_in": "diag",
                        "request_id": r.get("request_id"),
                        "session_key": key,
                        "turn": r.get("turn"),
                        "span": s,
                    }
    return {"span_id": span_id, "found_in": None}


# ---------------------------------------------------------------------------
# 人类可读输出
# ---------------------------------------------------------------------------

def _fmt_ref(ref):
    return ("  %-24s %-6s %-10s %s -> %s chars" % (
        ref.get("anchor") or "-", ref.get("kind") or "-", ref.get("action") or "-",
        ref.get("before_chars"), ref.get("after_chars")))


def print_request(q):
    print("== request %s ==" % q["request_id"])
    m = q["metrics"]
    d = q["diag"]
    print("  session=%s turn=%s status=%s trace=%s ratio=%s" % (
        q["session_key"] or "-", q["turn"] if q["turn"] is not None else "-",
        m.get("status") if m else "-", m.get("trace_id") or "-",
        m.get("compression_ratio") if m else "-"))
    if d:
        print("  diag: ts=%s hit_ratio=%s inj=%s" % (
            (d.get("ts") or "-")[11:19], d.get("hit_ratio"),
            ",".join(d.get("feedback_injected") or []) or "-"))
    ctx = q["context"]
    print("  context: compressed=%s dropped=%s saved=%s chars dropped=%s chars "
          "pair_integrity=%s" % (
        ctx["compressed_units"], ctx["dropped_units"], ctx["saved_chars"],
        ctx["dropped_chars"], ctx["pair_integrity"]))
    if q["manifest_count"]:
        print("  manifest(%d): %s" % (q["manifest_count"], q["manifest_reasons"]))
        for s in q["manifest_sample"]:
            print("    [%s] %s kind=%s tool=%s handle=%s size=%s" % (
                s.get("reason"), s.get("anchor"), s.get("kind"),
                s.get("tool") or "-", s.get("handle") or "-", s.get("size_chars")))
    print("  changed_units:")
    for ref in ctx["refs"]:
        print(_fmt_ref(ref))
    if not ctx["refs"] and not q["manifest_count"]:
        print("  (该请求无上下文单元变化记录)")


def print_session(q):
    print("== session %s | %d turns ==" % (q["session_key"], q["turns"]))
    if not q["per_turn"]:
        print("  (无 per-turn 记录——检查 diag/sessions.jsonl)")
        return
    for t in q["per_turn"]:
        ctx = t["context"]
        reasons = ",".join("%s:%d" % (k, v) for k, v in t["manifest_reasons"].items()) or "-"
        ifc = t.get("ifc") or {}
        print("  turn=%s %s rid=%s compressed=%s dropped=%s saved=%s manifest=[%s] "
              "retention=%s ile=%s" % (
            t["turn"], (t.get("ts") or "-")[11:19], t.get("request_id") or "-",
            ctx["compressed_units"], ctx["dropped_units"], ctx["saved_chars"],
            reasons, ifc.get("retention"), ",".join(ifc.get("ile_kinds") or []) or "-"))
        for ref in ctx["refs"][:10]:
            print(_fmt_ref(ref))
        if ctx["refs"] and len(ctx["refs"]) > 10:
            print("    ... 其余 %d 条 (--json 取全量)" % (len(ctx["refs"]) - 10))


def print_span(q):
    if q["found_in"] is None:
        print("(span %s 未找到)" % q["span_id"])
        return
    print("== span %s (from %s) ==" % (q["span_id"], q["found_in"]))
    print("  session=%s turn=%s request=%s" % (
        q.get("session_key") or "-", q.get("turn"), q.get("request_id") or "-"))
    s = q["span"]
    print("  name=%s kind=%s status=%s duration_ms=%s" % (
        s.get("name"), s.get("kind"), s.get("status"), s.get("duration_ms")))
    if s.get("context"):
        print("  context: %s" % json.dumps(s["context"], ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="按 request/session/span 查询上下文管理(压缩/折叠)单元引用")
    parser.add_argument("--logs-dir", default=None, help="覆盖 logs 目录(默认仓库 logs/)")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("request", help="单请求的上下文单元引用")
    p.add_argument("request_id")

    p = sub.add_parser("session", help="会话的逐轮上下文摘要")
    p.add_argument("session_key")
    p.add_argument("--limit", type=int, default=None, help="仅最近 N 轮")
    p.add_argument("--turn", type=int, default=None, help="仅指定轮次")

    p = sub.add_parser("span", help="按 span_id 定位并展示其 context")
    p.add_argument("span_id")

    args = parser.parse_args(argv)
    logs_dir = args.logs_dir
    if args.cmd == "request":
        result = query_request(args.request_id, logs_dir)
    elif args.cmd == "session":
        result = query_session(args.session_key, logs_dir,
                               limit=args.limit, turn=args.turn)
    else:
        result = query_span(args.span_id, logs_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "request":
        print_request(result)
    elif args.cmd == "session":
        print_session(result)
    else:
        print_span(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
