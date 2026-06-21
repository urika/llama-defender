"""Auto-extracted admin_server module."""
import os, subprocess, time, threading
from datetime import datetime
import proxy_state as _ps
from backend_strategy import BackendStrategy
_strategy = BackendStrategy.create(_ps.IS_CLOUD)
from message_converter import _classify_content_for_ratio

def _log(msg, level="INFO"):
    pass

# --- _run ---
def _run(cmd, timeout=3):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=timeout).strip()
    except Exception:
        return ""
# --- _get_process_info ---
def _get_process_info(pattern, name, fallback_port=None):
    """Return dict with pid, rss_mb, cpu, elapsed for a process matching pattern."""
    # Try pgrep first
    pid = _run(f"pgrep -f '{pattern}' | head -1")
    # Fallback: detect by listening port (for proxy itself)
    # Use -sTCP:LISTEN to only match the listening process, not client connections
    if not pid and fallback_port:
        pid = _run(f"lsof -i :{fallback_port} -sTCP:LISTEN -t | head -1")
    if not pid:
        return {"running": False, "name": name}
    info = _run(f"ps -o pid=,rss=,pcpu=,etime= -p {pid}")
    parts = info.split()
    if len(parts) >= 4:
        rss_kb = int(parts[1])
        return {
            "running": True,
            "name": name,
            "pid": parts[0],
            "rss_mb": f"{rss_kb / 1024:.1f}",
            "cpu": parts[2],
            "elapsed": parts[3],
        }
    return {"running": False, "name": name}
# --- _get_system_memory ---
def _get_system_memory():
    out = _run("vm_stat")
    data = {}
    page_size = 16384
    for line in out.splitlines():
        if "Pages free:" in line:
            data["free_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages wired down:" in line:
            data["wired_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages active:" in line:
            data["active_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages inactive:" in line:
            data["inactive_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
        elif "Pages stored in compressor:" in line:
            data["compress_gb"] = int(line.split(":")[1].strip().rstrip(".")) * page_size / (1024**3)
    total = 48.0
    # macOS: Free is always tiny; Inactive is reclaimable cache.
    # Show meaningful metrics: true used (wired+active) vs available (free+inactive).
    true_used = data.get("wired_gb", 0) + data.get("active_gb", 0)
    available = data.get("free_gb", 0) + data.get("inactive_gb", 0)
    data["total_gb"] = total
    data["used_gb"] = true_used          # Wired + Active (truly in use)
    data["available_gb"] = available     # Free + Inactive (reclaimable)
    data["used_pct"] = f"{true_used/total*100:.1f}"
    return data
# --- _should_reject_for_memory ---
def _should_reject_for_memory(mem=None):
    """Return (rejected: bool, used_pct: float) based on memory pressure threshold."""
    try:
        if mem is None:
            mem = _get_system_memory()
        used_pct = float(mem.get("used_pct", 0))
        return used_pct > _ps.PROXY_MEMORY_REJECT_THRESHOLD, used_pct
    except Exception:
        return False, 0.0
# --- _cleanup_snapshots ---
def _cleanup_snapshots(snapshot_dir, max_files):
    """Keep only the most recent max_files snapshot pairs."""
    try:
        files = [
            (f, os.path.getmtime(os.path.join(snapshot_dir, f)))
            for f in os.listdir(snapshot_dir)
            if f.endswith(".json")
        ]
        files.sort(key=lambda x: x[1], reverse=True)
        for old_file, _ in files[max_files:]:
            try:
                os.remove(os.path.join(snapshot_dir, old_file))
            except OSError:
                pass
    except OSError:
        pass
# --- _write_request_snapshot ---
def _write_request_snapshot(request_id, before_body, after_body=None, error=None):
    """Write before/after request snapshots for debugging failures.

    Returns True if a snapshot was written.
    """
    if not _ps.PROXY_SNAPSHOT_ENABLED:
        return False
    try:
        snapshot_dir = os.path.join(_SCRIPT_DIR, "logs", "snapshots")
        os.makedirs(snapshot_dir, exist_ok=True)
        before_path = os.path.join(snapshot_dir, f"{request_id}_before.json")
        with open(before_path, "w", encoding="utf-8") as f:
            json.dump({"request_id": request_id, "body": before_body}, f,
                      ensure_ascii=False, indent=2)
        if after_body is not None or error is not None:
            after_path = os.path.join(snapshot_dir, f"{request_id}_after.json")
            payload = {"request_id": request_id}
            if after_body is not None:
                payload["body_after_pipeline"] = after_body
            if error is not None:
                payload["error"] = {"type": type(error).__name__, "message": str(error)[:500]}
            with open(after_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        _cleanup_snapshots(snapshot_dir, _ps.PROXY_SNAPSHOT_MAX_FILES)
        return True
    except Exception:
        return False
# --- _read_log_tail ---
def _read_log_tail(path, max_bytes=200000):
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", errors="ignore") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes), 0)
            return f.read()
    except OSError:
        return ""
# --- _record_request_for_concurrency ---
def _record_request_for_concurrency(duration_ms, status):
    """Append a sample to the latency/error sliding windows."""
    try:
        _ps._LATENCY_WINDOW.append(float(duration_ms))
        _ps._ERROR_WINDOW.append(0 if int(status) == 200 else 1)
    except Exception:
        pass
# --- _percentile ---
def _percentile(values, p):
    """Return the p-th percentile of a list of numbers (0 <= p <= 1)."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return s[f] + (s[c] - s[f]) * (k - f)
# --- _adjust_concurrency ---
def _adjust_concurrency():
    """Dynamically adjust backend semaphore size based on recent latency/error window.

    Returns a dict describing the decision (or None if disabled).
    """
    if not _ps.PROXY_DYNAMIC_CONCURRENT_ENABLED:
        return None
    try:
        latencies = list(_ps._LATENCY_WINDOW)
        errors = list(_ps._ERROR_WINDOW)
        if len(latencies) < 5:
            return None
        p95 = _percentile(latencies, 0.95)
        error_rate = sum(errors) / len(errors) if errors else 0.0
        current = _ps.PROXY_MAX_CONCURRENT
        new_max = current
        if p95 > _ps.PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS or error_rate > _ps.PROXY_DYNAMIC_CONCURRENT_ERROR_RATE:
            new_max = max(_ps.PROXY_DYNAMIC_CONCURRENT_MIN, current - 1)
        elif p95 < _ps.PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS / 2 and error_rate == 0.0:
            new_max = min(_ps.PROXY_DYNAMIC_CONCURRENT_MAX, current + 1)
        if new_max != current:
            _ps.PROXY_MAX_CONCURRENT = new_max
            _ps._llama_lock = threading.Semaphore(new_max)
            _log(f"[DYNAMIC_CONCURRENT] adjusted {current} -> {new_max} (p95={p95:.0f}ms, error_rate={error_rate:.2f})")
            return {"adjusted": True, "previous": current, "current": new_max, "p95": p95, "error_rate": error_rate}
        return {"adjusted": False, "current": current, "p95": p95, "error_rate": error_rate}
    except Exception:
        return None
# --- _get_log_stats ---
def _get_log_stats():
    """Count recent OOMs, forced cache clears, and requests from log tail.
    Requests get accurate timestamps from proxy logs [REQ_SUMMARY].
    OOM/CacheClear have no timestamp (backend logs don't include wall-clock time).
    For cloud backends, OOM/cache-clear metrics are not available."""
    backend_tail = _read_log_tail(_LOG_PATH, 200000) if not _strategy.oom_safety_enabled else ""
    proxy_log_path = os.environ.get("PROXY_LOG_PATH", "/tmp/anthropic_proxy.log")
    proxy_tail = _read_log_tail(proxy_log_path, 100000)

    # --- Extract request events from proxy logs ([HH:MM:SS] [REQ_SUMMARY] chars=X tools=Y) ---
    proxy_req_events = []
    for line in proxy_tail.splitlines()[-40:]:
        m = re.search(r'\[(\d{2}:\d{2}:\d{2})\].*\[REQ_SUMMARY\].*chars=(\d+).*tools=(\d+)', line)
        if m:
            proxy_req_events.append((m.group(1), m.group(2), m.group(3)))

    # --- Build recent events list ---
    events = []
    req_idx = 0
    if not _strategy.oom_safety_enabled:
        for line in backend_tail.splitlines()[-40:]:
            if "Insufficient Memory" in line:
                events.append(("—", "🔴 OOM", line.split(":")[-1].strip()[-80:]))
            elif "forced cache clear" in line:
                events.append(("—", "🟡 CacheClear", line.split(":")[-1].strip()[-80:]))
            elif "[REQUEST]" in line and "total_chars=" in line:
                m = re.search(r"total_chars=(\d+).*?tools=(\d+)", line)
                if m:
                    ts = proxy_req_events[req_idx][0] if req_idx < len(proxy_req_events) else "—"
                    events.append((ts, "📨 Request", f"{m.group(1)} chars, {m.group(2)} tools"))
                    req_idx += 1
    events = events[-12:]

    # --- Detailed lists for modal popup ---
    if _strategy.oom_safety_enabled:
        oom_details = []
        clear_details = []
    else:
        oom_details = [("—", line.split(":")[-1].strip()[-120:])
                       for line in backend_tail.splitlines() if "Insufficient Memory" in line]
        clear_details = [("—", line.split(":")[-1].strip()[-120:])
                         for line in backend_tail.splitlines() if "forced cache clear" in line]
    req_details = [(ts, f"{chars} chars, {tools} tools") for ts, chars, tools in proxy_req_events]

    return {
        "ooms": len(oom_details),
        "clears": len(clear_details),
        "requests": len(req_details),
        "last_events": events,
        "oom_details": oom_details[-20:],
        "clear_details": clear_details[-20:],
        "req_details": req_details[-20:],
    }
# --- _get_cache_stats ---
def _get_cache_stats():
    """Parse backend log for prefix cache HIT/MISS since current startup.
    Returns {"hit": N, "miss": N, "total": N, "rate_str": "X.X%", "since": "description"}.
    Cloud backends return zeros."""
    if _strategy.oom_safety_enabled:
        return {"hit": 0, "miss": 0, "total": 0, "rate_str": "N/A", "since": "N/A (cloud)"}
    try:
        with open(_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except (OSError, IOError):
        return {"hit": 0, "miss": 0, "total": 0, "rate_str": "N/A", "since": "log unavailable"}

    # Find the most recent startup (MemoryAwarePrefixCache initialized)
    start_idx = 0
    startup_line = ""
    for i, line in enumerate(lines):
        if "MemoryAwarePrefixCache initialized" in line:
            start_idx = i
            startup_line = line.strip()

    hit = miss = 0
    for line in lines[start_idx:]:
        if "cache_fetch" in line:
            if "HIT" in line:
                hit += 1
            elif "MISS" in line:
                miss += 1
    total = hit + miss
    rate = (hit / total * 100) if total > 0 else 0

    # Extract session label from startup line
    if startup_line:
        # e.g. "INFO:vllm_mlx.memory_cache:MemoryAwarePrefixCache initialized: max_memory=4096.0 MB"
        since = "last cache restart"
    else:
        since = "backend start"

    return {"hit": hit, "miss": miss, "total": total, "rate_str": f"{rate:.1f}%", "since": since}
# --- _get_traffic_stats ---
def _get_traffic_stats():
    """Read proxy_requests.jsonl and compute traffic metrics + anomaly detection."""
    try:
        with open(_ps._JSONL_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, IOError):
        return _empty_traffic_stats()

    if not lines:
        return _empty_traffic_stats()

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts_str = rec.get("start_time", "") or rec.get("timestamp", "") or rec.get("end_time", "")
            if ts_str:
                try:
                    rec["_ts"] = datetime.fromisoformat(ts_str)
                except ValueError:
                    continue
            else:
                continue
            records.append(rec)
        except json.JSONDecodeError:
            continue

    if not records:
        return _empty_traffic_stats()

    now = datetime.now()
    records_1h = [r for r in records if (now - r["_ts"]).total_seconds() <= 3600]
    records_10m = [r for r in records_1h if (now - r["_ts"]).total_seconds() <= 600]

    if not records_1h:
        return _empty_traffic_stats()

    def _stats(recs):
        if not recs:
            return {}
        durations = [r.get("duration_ms", 0) for r in recs]
        durations.sort()
        inputs = [r.get("input_chars", 0) for r in recs]
        outputs = [r.get("output_chars", 0) for r in recs]
        statuses = [r.get("status", 200) for r in recs]
        n = len(recs)
        return {
            "count": n,
            "avg_latency_ms": round(sum(durations) / n, 1) if n else 0,
            "p50_latency_ms": durations[n // 2] if n else 0,
            "p95_latency_ms": durations[int(n * 0.95)] if n else 0,
            "max_latency_ms": round(max(durations), 1) if durations else 0,
            "avg_input_chars": round(sum(inputs) / n, 0) if n else 0,
            "avg_output_chars": round(sum(outputs) / n, 0) if n else 0,
            "max_input_chars": max(inputs) if inputs else 0,
            "max_output_chars": max(outputs) if outputs else 0,
            "success_rate": round(sum(1 for s in statuses if s == 200) / n * 100, 1) if n else 100.0,
        }

    stats_1h = _stats(records_1h)
    stats_10m = _stats(records_10m)

    # --- Anomaly detection ---
    alerts = []
    # Duplicate requests: same input_chars within same second
    sec_to_inputs = {}
    for r in records_10m:
        sec_key = r["_ts"].strftime("%H:%M:%S")
        sec_to_inputs.setdefault(sec_key, []).append(r.get("input_chars", 0))
    for sec_key, inputs in sec_to_inputs.items():
        from collections import Counter
        c = Counter(inputs)
        for inp_chars, cnt in c.items():
            if cnt >= 2:
                alerts.append(("warn", f"重复请求: {sec_key} 内 {cnt} 个请求 input_chars={inp_chars:,}"))
    # Oversized requests
    for r in records_10m:
        inp = r.get("input_chars", 0)
        if inp > 100000:
            alerts.append(("warn", f"超大报文: {r['_ts'].strftime('%H:%M:%S')} input_chars={inp:,}"))
    # Slow requests
    for r in records_10m:
        dur = r.get("duration_ms", 0)
        if dur > 60000:
            et = r.get("end_time", "")
            et_short = datetime.fromisoformat(et).strftime('%H:%M:%S') if et else "?"
            alerts.append(("warn", f"超长耗时: {r['_ts'].strftime('%H:%M:%S')}→{et_short} {dur/1000:.1f}s"))
    # Very slow requests (critical)
    for r in records_10m:
        dur = r.get("duration_ms", 0)
        if dur > 120000:
            et = r.get("end_time", "")
            et_short = datetime.fromisoformat(et).strftime('%H:%M:%S') if et else "?"
            alerts.append(("critical", f"严重超时: {r['_ts'].strftime('%H:%M:%S')}→{et_short} {dur/1000:.1f}s"))

    # Latency distribution buckets for visualization
    all_durations = [r.get("duration_ms", 0) for r in records_1h]
    buckets = [
        ("<5s", 0), ("5-15s", 0), ("15-30s", 0),
        ("30-60s", 0), ("60-120s", 0), (">120s", 0),
    ]
    for d in all_durations:
        if d < 5000:
            buckets[0] = (buckets[0][0], buckets[0][1] + 1)
        elif d < 15000:
            buckets[1] = (buckets[1][0], buckets[1][1] + 1)
        elif d < 30000:
            buckets[2] = (buckets[2][0], buckets[2][1] + 1)
        elif d < 60000:
            buckets[3] = (buckets[3][0], buckets[3][1] + 1)
        elif d < 120000:
            buckets[4] = (buckets[4][0], buckets[4][1] + 1)
        else:
            buckets[5] = (buckets[5][0], buckets[5][1] + 1)

    return {
        "stats_1h": stats_1h,
        "stats_10m": stats_10m,
        "alerts": alerts,
        "latency_buckets": buckets,
        "last_record_time": records[-1]["_ts"].strftime("%H:%M:%S") if records else "—",
    }
# --- _empty_traffic_stats ---
def _empty_traffic_stats():
    return {
        "stats_1h": {},
        "stats_10m": {},
        "alerts": [],
        "latency_buckets": [("<5s", 0), ("5-15s", 0), ("15-30s", 0), ("30-60s", 0), ("60-120s", 0), (">120s", 0)],
        "last_record_time": "—",
    }
# --- _get_context_optimization_stats ---
def _get_context_optimization_stats():
    """Aggregate recent proxy_metrics.jsonl for context optimization dashboard.

    Returns dict with avg common_prefix_ratio, avg compression_ratio,
    loop/blocker counts, and the most recent blocker event.
    """
    try:
        with open(_ps._METRICS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, IOError):
        return _empty_context_optimization_stats()

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts_str = rec.get("ts", "")
            if ts_str:
                try:
                    rec["_ts"] = datetime.fromisoformat(ts_str)
                    records.append(rec)
                except ValueError:
                    pass
        except json.JSONDecodeError:
            continue

    if not records:
        return _empty_context_optimization_stats()

    now = datetime.now()
    recent = [r for r in records if (now - r["_ts"]).total_seconds() <= 600]
    if not recent:
        recent = records[-50:]

    ratios = [r.get("pipeline", {}).get("common_prefix_ratio", {}).get("ratio", 0) for r in recent]
    ratios = [r for r in ratios if isinstance(r, (int, float))]
    compressions = [r.get("compression_ratio", 1.0) for r in recent]
    compressions = [c for c in compressions if isinstance(c, (int, float))]

    loop_count = 0
    blocker_count = 0
    recent_blocker = None
    for r in recent:
        pipeline = r.get("pipeline", {})
        if pipeline.get("loop_detect", {}).get("max_run", 0) >= _ps.PROXY_LOOP_THRESHOLD:
            loop_count += 1
        blocker = pipeline.get("blocker_detect", {})
        if blocker.get("triggered"):
            blocker_count += 1
            recent_blocker = {
                "ts": r.get("ts", ""),
                "tool": blocker.get("tool_name", "?"),
                "error": blocker.get("error_type", "?"),
                "run": blocker.get("run_length", 0),
            }

    return {
        "avg_common_prefix_ratio": round(sum(ratios) / len(ratios), 3) if ratios else 0.0,
        "avg_compression_ratio": round(sum(compressions) / len(compressions), 3) if compressions else 1.0,
        "loop_triggered_10m": loop_count,
        "blocker_triggered_10m": blocker_count,
        "recent_blocker": recent_blocker,
        "max_concurrent": _ps.PROXY_MAX_CONCURRENT,
        "dynamic_concurrent_enabled": _ps.PROXY_DYNAMIC_CONCURRENT_ENABLED,
    }
# --- _empty_context_optimization_stats ---
def _empty_context_optimization_stats():
    return {
        "avg_common_prefix_ratio": 0.0,
        "avg_compression_ratio": 1.0,
        "loop_triggered_10m": 0,
        "blocker_triggered_10m": 0,
        "recent_blocker": None,
        "max_concurrent": _ps.PROXY_MAX_CONCURRENT,
        "dynamic_concurrent_enabled": _ps.PROXY_DYNAMIC_CONCURRENT_ENABLED,
    }
# --- _get_session_trace ---
def _get_session_trace():
    """Parse /tmp/anthropic_request_body.json and build an HTML snippet showing
    the semantic message timeline (roles, tool calls, text previews, errors).
    Returns (html_str, tools_list) where tools_list is [(msg_idx, name, params), ...]
    for modal popup display."""
    try:
        with open("/tmp/anthropic_request_body.json", "r", encoding="utf-8") as f:
            body = json.load(f)
    except (OSError, json.JSONDecodeError):
        return '<div class="evt">No active request body</div>', [], []

    try:
        mtime = os.path.getmtime("/tmp/anthropic_request_body.json")
        saved_at = datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
    except OSError:
        saved_at = None

    msgs = body.get("messages", [])
    model = body.get("model", "unknown")
    max_tokens = body.get("max_tokens", "?")
    total_chars = len(json.dumps(msgs, ensure_ascii=False)) if msgs else 0

    # Count roles and tool actions
    user_count = sum(1 for m in msgs if m.get("role") == "user")
    assistant_count = sum(1 for m in msgs if m.get("role") == "assistant")
    tool_uses = 0
    tool_results = 0
    errors = 0
    # Collect detailed tool use and error info for modal
    tools_detail = []
    errors_detail = []
    for idx, m in enumerate(msgs):
        content = m.get("content", [])
        if isinstance(content, list):
            for c in content:
                if c.get("type") == "tool_use":
                    tool_uses += 1
                    name = c.get("name", "?")
                    inp = c.get("input", {})
                    params = ", ".join(f"{k}={v!r}" for k, v in list(inp.items())[:4])
                    if len(inp) > 4:
                        params += ", ..."
                    tools_detail.append((saved_at or "—", f"Msg {idx}: {name}({params})"))
                elif c.get("type") == "tool_result":
                    tool_results += 1
                    tr = c.get("content", "")
                    err_text = ""
                    if isinstance(tr, str) and "tool_use_error" in tr:
                        errors += 1
                        err_text = tr[:120]
                    elif isinstance(tr, list) and tr:
                        t = tr[0].get("text", "")
                        if "tool_use_error" in str(t):
                            errors += 1
                            err_text = str(t)[:120]
                    if err_text:
                        err_summary = err_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        errors_detail.append((saved_at or "—", f"Msg {idx}: {err_summary}"))

    # Build timeline HTML (last 8 messages)
    timeline = []
    ts_html = f'<span class="evt-ts">{saved_at}</span> ' if saved_at else ''
    for idx, m in enumerate(msgs):
        if idx < len(msgs) - 8:
            continue
        role = m.get("role", "?")
        content = m.get("content", [])
        prefix = f"Msg {idx}"
        line = ""
        if isinstance(content, list):
            texts = []
            tools = []
            has_error = False
            for c in content:
                ctype = c.get("type", "")
                if ctype == "text":
                    t = c.get("text", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    texts.append(t[:60] + ("..." if len(t) > 60 else ""))
                elif ctype == "tool_use":
                    name = c.get("name", "?")
                    inp = c.get("input", {})
                    # Show a key param preview
                    preview = ""
                    if isinstance(inp, dict):
                        for k in ("command", "file_path", "subject", "description", "old_string"):
                            if k in inp:
                                v = str(inp[k])[:40]
                                preview = f" {k}={v}"
                                break
                    tools.append(f"{name}{preview}")
                elif ctype == "tool_result":
                    tr = c.get("content", "")
                    if isinstance(tr, str) and "tool_use_error" in tr:
                        has_error = True
                    elif isinstance(tr, list) and tr:
                        t = tr[0].get("text", "")
                        if "tool_use_error" in str(t):
                            has_error = True
            parts = []
            if texts:
                parts.append(texts[0])
            if tools:
                parts.append(" | ".join(tools))
            if has_error:
                parts.append("❌ ERROR")
            line = " | ".join(parts) if parts else "[empty]"
        else:
            t = str(content).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:60]
            line = t + ("..." if len(str(content)) > 60 else "")

        role_color = "#3498db" if role == "user" else ("#2ecc71" if role == "assistant" else "#888")
        timeline.append(
            f'<div class="evt">{ts_html}<span style="color:{role_color};font-weight:600;">{prefix} ({role})</span> {line}</div>'
        )

    if not timeline:
        timeline.append('<div class="evt">No messages</div>')

    summary = (
        f'<div class="row"><span class="label">Messages</span>'
        f'<span class="value">{len(msgs)}</span></div>'
        f'<div class="row"><span class="label">Model</span>'
        f'<span class="value">{model}</span></div>'
        f'<div class="row"><span class="label">Max Tokens</span>'
        f'<span class="value">{max_tokens}</span></div>'
        f'<div class="row"><span class="label">Total Chars</span>'
        f'<span class="value">{total_chars:,}</span></div>'
        f'<div class="row"><span class="label">User / Assistant</span>'
        f'<span class="value">{user_count} / {assistant_count}</span></div>'
        f'<div class="row"><span class="label">Tool Uses</span>'
        f'<span class="value clickable" onclick="showModal(\'tools\', \'🔧 Tool Calls Detail\')">{tool_uses}</span></div>'
        f'<div class="row"><span class="label">Errors</span>'
        f'<span class="value clickable" style="color:{"#e74c3c" if errors else "#2ecc71"}" onclick="showModal(' + "'errors', '❌ Errors Detail')" + f'">{errors}</span></div>'
    )
    if saved_at:
        summary += f'<div class="row"><span class="label">Captured At</span><span class="value">{saved_at}</span></div>'

    return summary + "\n".join(timeline), tools_detail, errors_detail
# --- _build_status_html ---
def _build_status_html():
    backend_info = _get_process_info("rapid-mlx|llama-server", "Backend")
    proxy_info = _get_process_info("anthropic_proxy.py", "Proxy", fallback_port=4000)
    mem = _get_system_memory()
    log = _get_log_stats()
    traffic = _get_traffic_stats()
    session_trace, tools_detail, errors_detail = _get_session_trace()
    cache_stats = _get_cache_stats()
    ctx_opt = _get_context_optimization_stats()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    backend_color = "#2ecc71" if backend_info.get("running") else "#e74c3c"
    cache_rate_color = "#888"
    if cache_stats["total"] > 0:
        rate = cache_stats["hit"] / cache_stats["total"] * 100
        cache_rate_color = "#2ecc71" if rate >= 50 else "#f39c12" if rate >= 20 else "#e74c3c"
    proxy_color = "#2ecc71" if proxy_info.get("running") else "#e74c3c"
    mem_used_pct = float(mem.get("used_pct", 0))
    mem_warn = mem_used_pct > _ps.PROXY_MEMORY_REJECT_THRESHOLD
    mem_alert = mem_used_pct > 75
    mem_color = "#e74c3c" if mem_warn or mem_alert else "#2ecc71"

    # --- Traffic Stats card ---
    s1h = traffic.get("stats_1h", {})
    s10m = traffic.get("stats_10m", {})
    qps = round(s10m.get("count", 0) / 600, 3) if s10m.get("count") else 0
    traffic_card = f"""<div class="card">
    <h2>📊 Traffic Stats</h2>
    <div class="row"><span class="label">Requests (1h / 10m)</span><span class="value">{s1h.get("count", 0)} / {s10m.get("count", 0)}</span></div>
    <div class="row"><span class="label">Avg Latency</span><span class="value">{s1h.get("avg_latency_ms", 0)/1000:.1f}s</span></div>
    <div class="row"><span class="label">P95 Latency</span><span class="value">{s1h.get("p95_latency_ms", 0)/1000:.1f}s</span></div>
    <div class="row"><span class="label">Max Latency</span><span class="value">{s1h.get("max_latency_ms", 0)/1000:.1f}s</span></div>
    <div class="row"><span class="label">Avg In / Out</span><span class="value">{s1h.get("avg_input_chars", 0):.0f} / {s1h.get("avg_output_chars", 0):.0f} chars</span></div>
    <div class="row"><span class="label">Max In / Out</span><span class="value">{s1h.get("max_input_chars", 0):,.0f} / {s1h.get("max_output_chars", 0):,.0f}</span></div>
    <div class="row"><span class="label">Success Rate</span><span class="value" style="color:{"#2ecc71" if s1h.get("success_rate", 100) >= 95 else "#f39c12" if s1h.get("success_rate", 100) >= 80 else "#e74c3c"}">{s1h.get("success_rate", 100):.1f}%</span></div>
    <div class="row"><span class="label">Est. QPS (10m)</span><span class="value">{qps:.3f}</span></div>
    <div class="row"><span class="label">Last Record</span><span class="value">{traffic.get("last_record_time", "—")}</span></div>
  </div>"""

    # --- Context Optimization card (Phase 3) ---
    recent_blocker_html = ""
    rb = ctx_opt.get("recent_blocker")
    if rb:
        recent_blocker_html = (
            f'<div class="row"><span class="label">Recent Blocker</span>'
            f'<span class="value" style="color:#f39c12">{rb.get("tool", "?")} / {rb.get("error", "?")} (run={rb.get("run", 0)})</span></div>'
        )
    ctx_opt_card = f"""<div class="card">
    <h2>🧠 Context Optimization</h2>
    <div class="row"><span class="label">Avg Prefix Ratio</span><span class="value">{ctx_opt.get("avg_common_prefix_ratio", 0):.1%}</span></div>
    <div class="row"><span class="label">Avg Compression</span><span class="value">{ctx_opt.get("avg_compression_ratio", 1.0):.2f}x</span></div>
    <div class="row"><span class="label">Loop Triggered (10m)</span><span class="value">{ctx_opt.get("loop_triggered_10m", 0)}</span></div>
    <div class="row"><span class="label">Blocker Triggered (10m)</span><span class="value">{ctx_opt.get("blocker_triggered_10m", 0)}</span></div>
    {recent_blocker_html}
    <div class="row"><span class="label">Max Concurrent</span><span class="value">{ctx_opt.get("max_concurrent", _ps.PROXY_MAX_CONCURRENT)}{" (dynamic)" if ctx_opt.get("dynamic_concurrent_enabled") else ""}</span></div>
  </div>"""

    # --- Alerts card ---
    alerts = traffic.get("alerts", [])
    if alerts:
        alerts_html = ""
        for severity, msg in alerts:
            color = "#e74c3c" if severity == "critical" else "#f39c12"
            icon = "🔴" if severity == "critical" else "⚠️"
            alerts_html += f'<div class="evt"><span style="color:{color};font-weight:600;">{icon} {msg}</span></div>'
    else:
        alerts_html = '<div class="evt" style="color:#2ecc71;">✅ No anomalies detected (last 10m)</div>'

    # Cloud-backend status card (no PID/memory/uptime)
    if _strategy.oom_safety_enabled:
        backend_card = f"""<div class="card">
    <h2>Backend</h2>
    <div class="row"><span class="label">Type</span><span class="value">Cloud API ({BACKEND_TYPE})</span></div>
    <div class="row"><span class="label">Endpoint</span><span class="value">{LLAMA_BASE}</span></div>
    <div class="row"><span class="label">Model</span><span class="value">{MODEL_NAME}</span></div>
    <div class="row"><span class="label">API Key</span><span class="value">{LLAMA_API_KEY[:8]}****</span></div>
  </div>"""
    else:
        backend_card = f"""<div class="card">
    <h2>Backend</h2>
    <div class="row"><span class="label">Status</span><span class="value"><span class="status-dot" style="background:{backend_color}"></span>{"Running" if backend_info.get("running") else "Stopped"}</span></div>
    <div class="row"><span class="label">Name</span><span class="value">{backend_info.get("name", "N/A")}</span></div>
    <div class="row"><span class="label">PID</span><span class="value">{backend_info.get("pid", "N/A")}</span></div>
    <div class="row"><span class="label">Memory</span><span class="value">{backend_info.get("rss_mb", "N/A")} MB</span></div>
    <div class="row"><span class="label">CPU</span><span class="value">{backend_info.get("cpu", "N/A")}%</span></div>
    <div class="row"><span class="label">Uptime</span><span class="value">{backend_info.get("elapsed", "N/A")}</span></div>
  </div>"""

    # Conditional log-stat rows (avoid backslashes inside f-strings)
    oom_row = ""
    cache_row = ""
    if not _strategy.oom_safety_enabled:
        oom_row = '<div class="row"><span class="label">OOM Crashes</span><span class="value oom clickable" onclick="showModal(' + "'oom', '🔴 OOM Crashes Detail')" + f'">{log["ooms"]}</span></div>'
        cache_row = '<div class="row"><span class="label">Forced Cache Clear</span><span class="value clear clickable" onclick="showModal(' + "'clear', '🟡 Forced Cache Clear Detail')" + f'">{log["clears"]}</span></div>'

    events_html = ""
    for ts, evt_type, evt_msg in log["last_events"]:
        ts_display = f'<span class="evt-ts">{ts}</span>' if ts != "—" else '<span class="evt-ts" style="color:#666">—</span>'
        events_html += f'<div class="evt">{ts_display} <span class="evt-tag">{evt_type}</span> {evt_msg}</div>'
    if not events_html:
        events_html = '<div class="evt">No recent events</div>'

    # JSON data for modal popups
    import json as _json
    modal_data = _json.dumps({
        "oom": log.get("oom_details", []),
        "clear": log.get("clear_details", []),
        "request": log.get("req_details", []),
        "tools": tools_detail,
        "errors": errors_detail,
    })
    # Escape </script> inside <script> to prevent premature tag closure
    # when eventData contains nested HTML/JS (e.g. Write tool content).
    modal_data = modal_data.replace("</script>", "<\\/script>")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<!-- auto-refresh disabled when modal is open -->
<title>Local LLM Stack Status</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .ts {{ color: #888; font-size: 12px; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
  .card {{ background: #16213e; border-radius: 10px; padding: 16px; }}
  .card h2 {{ font-size: 14px; margin: 0 0 12px 0; color: #a0a0c0; text-transform: uppercase; letter-spacing: 1px; }}
  .row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #2a2a4a; font-size: 13px; }}
  .row:last-child {{ border-bottom: none; }}
  .label {{ color: #888; }}
  .value {{ font-weight: 600; }}
  .status-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
  .mem-bar {{ height: 10px; background: #2a2a4a; border-radius: 5px; margin-top: 8px; overflow: hidden; }}
  .mem-fill {{ height: 100%; border-radius: 5px; transition: width 0.5s; }}
  .evt {{ font-size: 12px; padding: 4px 0; border-bottom: 1px solid #2a2a4a; color: #ccc; }}
  .evt:last-child {{ border-bottom: none; }}
  .evt-tag {{ display: inline-block; min-width: 80px; font-weight: 600; font-size: 11px; }}
  .evt-ts {{ display: inline-block; min-width: 60px; font-family: monospace; font-size: 11px; color: #888; margin-right: 4px; }}
  .oom {{ color: #e74c3c; }}
  .clear {{ color: #f39c12; }}
  .req {{ color: #3498db; }}
  .clickable {{ cursor: pointer; text-decoration: underline; }}
  .clickable:hover {{ opacity: 0.8; }}
  .footer {{ margin-top: 20px; font-size: 11px; color: #666; text-align: center; }}
  .modal {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.75); z-index: 100; justify-content: center; align-items: center; }}
  .modal-content {{ background: #16213e; border-radius: 10px; padding: 20px; max-width: 800px; width: 90%; max-height: 80vh; overflow-y: auto; border: 1px solid #2a2a4a; }}
  .close-btn {{ float: right; font-size: 24px; cursor: pointer; color: #888; line-height: 1; }}
  .close-btn:hover {{ color: #fff; }}
  .modal-row {{ padding: 8px 0; border-bottom: 1px solid #2a2a4a; font-size: 12px; color: #ccc; display: flex; gap: 12px; }}
  .modal-row:last-child {{ border-bottom: none; }}
  .modal-time {{ color: #888; font-family: monospace; min-width: 60px; flex-shrink: 0; }}
  .modal-msg {{ word-break: break-word; }}
</style>
</head>
<body>
<h1>🖥️ Local LLM Stack Status</h1>
<div class="ts">Updated: {now} &nbsp;•&nbsp; Auto-refresh every 5s</div>

<div class="grid">
  {backend_card}

  <div class="card">
    <h2>Proxy</h2>
    <div class="row"><span class="label">Status</span><span class="value"><span class="status-dot" style="background:{proxy_color}"></span>{"Running" if proxy_info.get("running") else "Stopped"}</span></div>
    <div class="row"><span class="label">Name</span><span class="value">{proxy_info.get("name", "N/A")}</span></div>
    <div class="row"><span class="label">PID</span><span class="value">{proxy_info.get("pid", "N/A")}</span></div>
    <div class="row"><span class="label">Memory</span><span class="value">{proxy_info.get("rss_mb", "N/A")} MB</span></div>
    <div class="row"><span class="label">Listen</span><span class="value">127.0.0.1:4000</span></div>
    <div class="row"><span class="label">Backend</span><span class="value">{LLAMA_BASE}</span></div>
  </div>

  <div class="card">
    <h2>System Memory</h2>
    <div class="row"><span class="label">Total</span><span class="value">{mem.get("total_gb", 48):.0f} GB</span></div>
    <div class="row"><span class="label">Used (Wired+Active)</span><span class="value" style="color:{mem_color}">{mem.get("used_gb", 0):.1f} GB ({mem.get("used_pct", "0")}%)</span></div>
    <div class="row"><span class="label">Available</span><span class="value">{mem.get("available_gb", 0):.1f} GB (Free+Inactive)</span></div>
    <div class="row"><span class="label">Wired</span><span class="value">{mem.get("wired_gb", 0):.1f} GB</span></div>
    <div class="row"><span class="label">Active</span><span class="value">{mem.get("active_gb", 0):.1f} GB</span></div>
    <div class="row"><span class="label">Inactive</span><span class="value">{mem.get("inactive_gb", 0):.1f} GB</span></div>
    <div class="row"><span class="label">Compressed</span><span class="value">{mem.get("compress_gb", 0):.1f} GB</span></div>
    <div class="mem-bar"><div class="mem-fill" style="width:{mem.get("used_pct", 0)}%;background:{mem_color}"></div></div>
  </div>

  <div class="card">
    <h2>Log Stats (recent tail)</h2>
    {oom_row}
    {cache_row}
    <div class="row"><span class="label">Requests</span><span class="value req clickable" onclick="showModal('request', '📨 Requests Detail')">{log["requests"]}</span></div>
    <div class="row"><span class="label">Prefix Cache</span><span class="value" style="color:{cache_rate_color}" title="统计范围: {cache_stats['since']} (跨session累计请查看 /status 页面历史)">{cache_stats["hit"]}/{cache_stats["total"]} ({cache_stats["rate_str"]})</span></div>
    <div class="row"><span class="label">Config</span><span class="value">CLEAR={'on' if PROXY_CLEAR_ENABLED else 'off'}, LIMIT={'on' if PROXY_CTX_LIMIT_ENABLED else 'off'}, MAX_CONCURRENT={_ps.PROXY_MAX_CONCURRENT}</span></div>
    <div class="row"><span class="label">Model</span><span class="value">{MODEL_NAME}</span></div>
    {'<div class="row"><span class="label">Memory Alert</span><span class="value" style="color:#e74c3c">⚠️ Used ' + str(mem_used_pct) + '% (reject threshold ' + str(_ps.PROXY_MEMORY_REJECT_THRESHOLD) + '%)</span></div>' if mem_warn else ''}
  </div>

  {traffic_card}

  {ctx_opt_card}

  <div class="card" style="grid-column: 1 / -1;">
    <h2>🚨 Alerts (last 10m)</h2>
    {alerts_html}
  </div>

  <div class="card" style="grid-column: 1 / -1;">
    <h2>Session Trace</h2>
    {session_trace}
  </div>

  <div class="card" style="grid-column: 1 / -1;">
    <h2>Recent Events</h2>
    {events_html}
  </div>
</div>

<div class="footer">Open http://127.0.0.1:4000/status in your browser</div>

<!-- Modal -->
<div id="modal" class="modal" onclick="closeModal(event)">
  <div class="modal-content" onclick="event.stopPropagation()">
    <span class="close-btn" onclick="closeModal()">&times;</span>
    <h3 id="modal-title" style="margin-top:0;color:#a0a0c0;font-size:14px;text-transform:uppercase;letter-spacing:1px;">Detail</h3>
    <div id="modal-body"></div>
  </div>
</div>

<script>
var eventData = {modal_data};
function showModal(type, title) {{
  document.getElementById('modal-title').innerText = title;
  var body = document.getElementById('modal-body');
  body.innerHTML = '';
  var items = eventData[type] || [];
  if (items.length === 0) {{
    body.innerHTML = '<div class="modal-row">No events found</div>';
  }} else {{
    items.forEach(function(item) {{
      var row = document.createElement('div');
      row.className = 'modal-row';
      var ts = item[0] || '—';
      var msg = item[1] || '';
      row.innerHTML = '<span class="modal-time">' + ts + '</span><span class="modal-msg">' + msg + '</span>';
      body.appendChild(row);
    }});
  }}
  document.getElementById('modal').style.display = 'flex';
}}
function closeModal(e) {{
  if (!e || e.target.id === 'modal') {{
    document.getElementById('modal').style.display = 'none';
  }}
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') closeModal();
}});
setInterval(function() {{
  if (document.getElementById('modal').style.display !== 'flex') {{
    location.reload();
  }}
}}, 5000);
</script>
</body>
</html>"""
    return html
# --- _finalize_metrics ---
def _finalize_metrics(mc):
    pipeline = mc.get("pipeline", {})
    quality_flags = []
    trunc = pipeline.get("truncate", {})
    if trunc.get("triggered"):
        dropped = trunc.get("dropped", 0)
        kept = trunc.get("kept", 0)
        if kept + dropped > 0 and dropped / (dropped + kept) > 0.7:
            quality_flags.append("high_drop_ratio")
        if trunc.get("compression") in ("rules", "folded") and dropped >= 10:
            quality_flags.append("llm_compress_failed")
        est_after = trunc.get("est_tokens_after", 0)
        budget = trunc.get("budget", 0)
        if budget > 0 and est_after > budget * 1.1:
            quality_flags.append("budget_overflow")
    loop = pipeline.get("loop_detect", {})
    if loop.get("max_run", 0) >= _ps.PROXY_LOOP_THRESHOLD:
        quality_flags.append("loop_injected")
    blocker = pipeline.get("blocker_detect", {})
    if blocker.get("triggered"):
        quality_flags.append("blocker_injected")
    mc["quality_flags"] = quality_flags

    # Phase 3: dynamic token estimation
    input_chars = mc.get("input_chars", 0)
    token_ratio = _ps.PROXY_CTX_TOKEN_RATIO
    try:
        # Reconstruct a minimal message list for ratio detection. The original
        # body is no longer available here, so we fall back to classifying the
        # input_chars text as a single English block for ratio selection.
        content_type = _classify_content_for_ratio("x" * min(input_chars, 1000))
        ratio_map = {
            "chinese": _ps.PROXY_TOKEN_RATIO_CHINESE,
            "english": _ps.PROXY_TOKEN_RATIO_ENGLISH,
            "code": _ps.PROXY_TOKEN_RATIO_CODE,
        }
        token_ratio = ratio_map.get(content_type, _ps.PROXY_CTX_TOKEN_RATIO)
    except Exception:
        token_ratio = _ps.PROXY_CTX_TOKEN_RATIO
    input_est = int(input_chars / max(token_ratio, 0.1))
    est_after = trunc.get("est_tokens_after", input_est) if trunc.get("triggered") else input_est
    if input_est > 0:
        mc["compression_ratio"] = round(est_after / input_est, 2)
    else:
        mc["compression_ratio"] = 1.0
    mc["token_ratio"] = round(token_ratio, 2)
    mc["est_input_tokens"] = input_est
    output_chars = mc.get("output_chars", 0)
    mc["est_output_tokens"] = int(output_chars / max(token_ratio, 0.1))

    # Phase 3: schema v1 — guarantee a fixed set of keys
    mc["schema_version"] = "v1"
    for field in _ps._METRICS_V1_FIELDS:
        mc.setdefault(field, None)
    mc["dynamic_concurrent"] = {
        "enabled": _ps.PROXY_DYNAMIC_CONCURRENT_ENABLED,
        "current": _ps.PROXY_MAX_CONCURRENT,
        "min": _ps.PROXY_DYNAMIC_CONCURRENT_MIN,
        "max": _ps.PROXY_DYNAMIC_CONCURRENT_MAX,
    }
# --- _mc_put ---
def _mc_put(step_key, data):
    mc = getattr(_ps._metrics_ctx, 'mc', None)
    if mc and _ps.PROXY_METRICS_ENABLED:
        mc["pipeline"][step_key] = data

__all__ = [
    "_run",
    "_get_process_info",
    "_get_system_memory",
    "_should_reject_for_memory",
    "_cleanup_snapshots",
    "_write_request_snapshot",
    "_read_log_tail",
    "_record_request_for_concurrency",
    "_percentile",
    "_adjust_concurrency",
    "_get_log_stats",
    "_get_cache_stats",
    "_get_traffic_stats",
    "_empty_traffic_stats",
    "_get_context_optimization_stats",
    "_empty_context_optimization_stats",
    "_get_session_trace",
    "_build_status_html",
    "_finalize_metrics",
    "_mc_put",
]
