"""Lightweight request tracing for the proxy (stdlib only).

Trace IDs identify one logical request chain. Request IDs identify one proxy
HTTP request. Spans identify individual processing operations.
"""
import os
import re
import threading
import time


_TRACE_HEX_BYTES = 8
_trace_local = threading.local()


def _new_id(prefix):
    return prefix + os.urandom(_TRACE_HEX_BYTES).hex()


def parse_traceparent(value):
    """Return a W3C trace-id when a valid traceparent header is supplied."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split("-")
    if len(parts) != 4:
        return None
    trace_id, parent_id, flags = parts[1], parts[2], parts[3]
    if (len(trace_id) != 32 or len(parent_id) != 16 or len(flags) != 2 or
            trace_id == "0" * 32 or parent_id == "0" * 16):
        return None
    try:
        int(trace_id + parent_id + flags, 16)
    except ValueError:
        return None
    return "tr_" + trace_id


def begin(request_id, trace_id=None, traceparent=None):
    """Create the request root span and store it in thread-local state."""
    inherited = parse_traceparent(traceparent)
    if inherited:
        trace_id = inherited
    elif (not isinstance(trace_id, str) or
          not re.fullmatch(r"tr_[0-9a-f]{16,64}", trace_id)):
        trace_id = None
    trace_id = trace_id or _new_id("tr_")
    root_span_id = _new_id("sp_")
    state = {
        "trace_id": trace_id,
        "request_id": request_id or "",
        "root_span_id": root_span_id,
        "spans": [],
    }
    _trace_local.state = state
    start = time.monotonic()
    state["root_start"] = start
    return state


def current():
    return getattr(_trace_local, "state", None)


def start_span(name, parent_span_id=None, kind="internal", attributes=None):
    """Start a span; returns a mutable token for finish_span()."""
    state = current()
    if state is None:
        return None
    parent = parent_span_id or state["root_span_id"]
    return {
        "trace_id": state["trace_id"],
        "span_id": _new_id("sp_"),
        "parent_span_id": parent,
        "name": name,
        "kind": kind,
        "start_monotonic": time.monotonic(),
        "attributes": dict(attributes or {}),
    }


def finish_span(span, status="ok", error=None, attributes=None, context=None):
    """Finish and append a span, omitting implementation-only timing state.

    context holds bounded unit references/stats (never message bodies) so a
    span can answer "which context units were dropped/compressed here" while
    payloads stay in archive/manifest/orig.
    """
    if not span:
        return None
    ended = time.monotonic()
    record = {
        "trace_id": span["trace_id"],
        "span_id": span["span_id"],
        "parent_span_id": span["parent_span_id"],
        "name": span["name"],
        "kind": span["kind"],
        "status": status,
        "duration_ms": round((ended - span["start_monotonic"]) * 1000, 1),
    }
    if span.get("attributes"):
        record["attributes"] = span["attributes"]
    if attributes:
        record.setdefault("attributes", {}).update(attributes)
    if context:
        record["context"] = context
    if error:
        record["error_type"] = type(error).__name__
        record["error"] = str(error)[:500]
    state = current()
    if state is not None:
        state["spans"].append(record)
    return record


_SHRINK_MIN_CHARS = 128  # 与 ifc_metrics.SHRINK_MIN_CHARS 对齐(避免格式化噪声)


def context_delta(before_units, after_units, limit=20):
    """Summarize unit changes between two anchor→unit maps.

    Input maps come from ifc_metrics.unit_anchors (keys are u:/r:/h: anchors;
    entries carry kind + size_chars). Output is a compact, bounded trace
    context containing only unit references and character deltas — never the
    message bodies themselves.

    Dropped-pair detection: when u:X and r:X are both dropped together the
    tool pair is treated as an atomic drop (pair_integrity stays True); when
    only one side is dropped while the counterpart survives, pair_integrity
    becomes False.
    """
    before = before_units or {}
    after = after_units or {}
    after_keys = set(after.keys())

    dropped_set = {a for a in before if a not in after}
    dropped = []
    pair_dropped = 0
    integrity = True
    for anchor in dropped_set:
        entry = before[anchor]
        prefix, sep, suffix = anchor.partition(":")
        if sep and prefix in ("u", "r"):
            other = ("r:" if prefix == "u" else "u:") + suffix
            if other in after_keys:
                integrity = False  # 单边删除 = 工具对破坏
            elif other in dropped_set and prefix == "u":
                pair_dropped += 1  # 每对只从 u: 侧计一次
        dropped.append({
            "anchor": anchor,
            "kind": entry.get("kind") or "",
            "action": "dropped",
            "before_chars": int(entry.get("size_chars") or 0),
            "after_chars": 0,
            "recoverable": prefix in ("u", "r"),
        })

    compressed = []
    for anchor, entry in before.items():
        cur = after.get(anchor)
        if cur is None:
            continue
        before_chars = int(entry.get("size_chars") or 0)
        after_chars = int(cur.get("size_chars") or 0)
        if before_chars - after_chars >= _SHRINK_MIN_CHARS:
            compressed.append({
                "anchor": anchor,
                "kind": entry.get("kind") or "",
                "action": "compressed",
                "before_chars": before_chars,
                "after_chars": after_chars,
            })

    changed = compressed + dropped
    return {
        "before_units": len(before),
        "after_units": len(after),
        "dropped_units": len(dropped),
        "compressed_units": len(compressed),
        "pair_dropped": pair_dropped,
        "pair_integrity": integrity,
        "changed_total": len(changed),
        "changed_units": changed[:limit],
        "changed_truncated": len(changed) > limit,
    }


def finish_request(status="ok", error=None):
    """Finish the root operation and return a serializable trace summary."""
    state = current()
    if state is None:
        return None
    ended = time.monotonic()
    result = {
        "trace_id": state["trace_id"],
        "request_id": state["request_id"],
        "root_span_id": state["root_span_id"],
        "span_count": len(state["spans"]),
        "spans": list(state["spans"]),
        "status": status,
        "duration_ms": round((ended - state["root_start"]) * 1000, 1),
    }
    if error:
        result["error_type"] = type(error).__name__
        result["error"] = str(error)[:500]
    return result


def clear():
    try:
        del _trace_local.state
    except AttributeError:
        pass


__all__ = [
    "parse_traceparent", "begin", "current", "start_span", "finish_span",
    "finish_request", "clear", "context_delta",
]
