"""Admin server: status page, system monitoring, metrics, and observability.

Functions for building the HTML status page at /status, collecting system
memory stats, parsing backend/proxy logs, and managing request snapshots.
All functions are stateless — they read from proxy_state and file system.
"""
import json
import os, re, subprocess, time, threading
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
# _get_system_memory() moved to proxy_state.py so pipeline.py can use it without
# creating a circular import.  Keep this module-level alias for backward compat.
_get_system_memory = _ps._get_system_memory
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
        snapshot_dir = os.path.join(_ps._SCRIPT_DIR, "logs", "snapshots")
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
    backend_tail = _read_log_tail(_ps._LOG_PATH, 200000) if not _strategy.oom_safety_enabled else ""
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
        with open(_ps._LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
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
def _get_route_stats():
    """Gather intelligent routing statistics for the /status page.

    Returns a dict with three layers of routing information:
      1. Configuration snapshot — enabled, threshold, profile, cloud model/endpoint.
      2. Aggregate counters from proxy_metrics.jsonl — local/cloud/fallback totals,
         last route reason, recent fallback details.
      3. Real-time session state from proxy_state — session map, cooldown map,
         per-session request counts, active session detail.
    """
    route_enabled = _ps.PROXY_ROUTE_ENABLED
    # --- Aggregate counters from metrics JSONL ---
    local_count = 0
    cloud_count = 0
    fallback_count = 0
    last_route_reason = ""
    last_route_target = ""
    last_route_timestamp = ""
    recent_fallbacks = []  # last 5 fallback events
    try:
        metrics_file = os.path.join(_ps._SCRIPT_DIR, "logs", "proxy_metrics.jsonl")
        with open(metrics_file, "r") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    p = rec.get("pipeline", {})
                    bd = p.get("backend_dispatcher", {})
                    rt = bd.get("route_target", "local")
                    if rt == "cloud":
                        cloud_count += 1
                    else:
                        local_count += 1
                    reason = bd.get("route_reason", "")
                    ts = rec.get("timestamp", rec.get("ts", ""))
                    if ts:
                        last_route_timestamp = ts
                    if reason:
                        last_route_reason = reason
                        last_route_target = rt
                    if bd.get("route_fallback"):
                        fallback_count += 1
                        if len(recent_fallbacks) < 5:
                            recent_fallbacks.append({
                                "timestamp": ts,
                                "reason": reason,
                                "cloud_model": bd.get("route_cloud_model", ""),
                                "target": rt,
                            })
                except (json.JSONDecodeError, KeyError):
                    pass
    except OSError:
        pass

    # --- Real-time session state from proxy_state ---
    with _ps._state_lock:
        daily_date = getattr(_ps, '_route_daily_date', '')
        daily_cost = getattr(_ps, '_route_daily_cost', 0.0)
        today = time.strftime("%Y-%m-%d")
        if daily_date != today:
            daily_cost = 0.0

        # Real-time session counts
        session_cloud = sum(1 for t in _ps._SESSION_ROUTE_MAP.values() if t == "cloud")
        session_local = sum(1 for t in _ps._SESSION_ROUTE_MAP.values()
                            if t in ("local", "local_forced"))
        session_total = len(_ps._SESSION_ROUTE_MAP)

        # Active sessions detail — enrich with request count, cooldown, and reason
        active_sessions = []
        for sid, target in sorted(_ps._SESSION_ROUTE_MAP.items()):
            cd_start = _ps._cloud_cooldown_start.get(sid, 0)
            cooldown_remaining = 0
            if cd_start > 0:
                elapsed = getattr(time, 'monotonic', time.time)() - cd_start
                cooldown_total = getattr(_ps, 'PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS', 1800)
                cooldown_remaining = max(0, int(cooldown_total - elapsed))
            active_sessions.append({
                "session_id": sid,
                "target": target,
                "source": _ps._SESSION_ROUTE_FORCE_SOURCE.get(sid, ""),
                "failures": _ps._cloud_fail_count.get(sid, 0),
                "requests": _ps._SESSION_REQUEST_COUNT.get(sid, 0),
                "cooldown_remaining": cooldown_remaining,
            })

        # Sessions in cool-down
        cooldown_sessions = [
            {
                "session_id": sid,
                "remaining_s": cooldown_remaining,
            }
            for sid, cd_start in _ps._cloud_cooldown_start.items()
            if cd_start > 0
            for elapsed in [getattr(time, 'monotonic', time.time)() - cd_start]
            for cooldown_total in [getattr(_ps, 'PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS', 1800)]
            for cooldown_remaining in [max(0, int(cooldown_total - elapsed))]
            if cooldown_remaining > 0
        ]

    total = local_count + cloud_count
    cloud_pct = (cloud_count / total * 100) if total > 0 else 0.0

    # Budget tiered alert state
    budget_used_pct = 0.0
    budget_alert_level = ""
    if _ps.PROXY_ROUTE_DAILY_BUDGET > 0 and daily_cost >= 0:
        budget_used_pct = daily_cost / _ps.PROXY_ROUTE_DAILY_BUDGET * 100
        budget_alert_level = _ps._get_budget_alert_level(budget_used_pct)

    return {
        # Configuration snapshot
        "route_enabled": route_enabled,
        "threshold": _ps.PROXY_ROUTE_THRESHOLD_CHARS,
        "memory_pct": _ps.PROXY_ROUTE_MEMORY_PCT,
        "profile": _ps.PROXY_ROUTE_PROFILE or "custom",
        "cloud_model": _ps.PROXY_CLOUD_MODEL,
        "cloud_base_url": _ps.PROXY_CLOUD_BASE_URL,
        "fallback_enabled": _ps.PROXY_ROUTE_FALLBACK_ENABLED,
        "max_cloud_fails": _ps.PROXY_ROUTE_MAX_CLOUD_FAILS,
        "cloud_concurrent": _ps.PROXY_ROUTE_CLOUD_CONCURRENT,
        "cloud_api_key_configured": bool(_ps.PROXY_CLOUD_API_KEY),
        # Aggregate counters
        "local_count": local_count,
        "cloud_count": cloud_count,
        "cloud_pct": cloud_pct,
        "fallback_count": fallback_count,
        "last_route_reason": last_route_reason,
        "last_route_target": last_route_target,
        "last_route_timestamp": last_route_timestamp,
        "recent_fallbacks": recent_fallbacks,
        # Cost tracking
        "daily_cost": daily_cost,
        "daily_budget": _ps.PROXY_ROUTE_DAILY_BUDGET,
        "daily_budget_hard_stop": _ps.PROXY_ROUTE_DAILY_BUDGET_HARD_STOP,
        "budget_used_pct": budget_used_pct,
        "budget_alert_level": budget_alert_level,
        "budget_alert_tiers": _ps._parse_budget_alert_tiers(),
        # Phase 3+ (建议3): per-backend segmented latency (last 100 samples each)
        "local_latency": _latency_summary(_ps._LATENCY_BY_TARGET.get("local")),
        "cloud_latency": _latency_summary(_ps._LATENCY_BY_TARGET.get("cloud")),
        # Real-time session state
        "session_cloud": session_cloud,
        "session_local": session_local,
        "session_total": session_total,
        "active_sessions": active_sessions,
        "cooldown_sessions": cooldown_sessions,
    }


def _latency_summary(deq):
    """Compute avg/p95/max/count from a deque of latency samples (ms).

    None and non-finite values are skipped.  Returns dict of 0s when empty.
    """
    if not deq:
        return {"count": 0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    try:
        vals = [float(v) for v in list(deq) if v is not None]
    except (TypeError, ValueError):
        return {"count": 0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    if not vals:
        return {"count": 0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    return {
        "count": len(vals),
        "avg_ms": round(sum(vals) / len(vals), 1),
        "p50_ms": round(_percentile(vals, 0.50), 1),
        "p95_ms": round(_percentile(vals, 0.95), 1),
        "max_ms": round(max(vals), 1),
    }


def _build_active_sessions_table(sessions: list) -> str:
    """Build an HTML table showing active session routing state.

    Columns: Session ID | Target (colored badge) | Source | Reqs | Fails | Cooldown
    Sessions in cool-down are highlighted with a warning row style.
    """
    if not sessions:
        return ""
    rows = ""
    for s in sessions:
        target = s["target"]
        # Color-code target: cloud=blue, local=green, local_forced=orange
        if target == "cloud":
            color = "#3498db"
        elif target == "local_forced":
            color = "#e67e22"
        else:
            color = "#27ae60"
        badge = f'<span style="color:{color};font-weight:bold">{target}</span>'
        cd = s.get("cooldown_remaining", 0)
        cd_html = f'<span style="color:#e74c3c">{cd}s</span>' if cd > 0 else "—"
        row_style = ' style="background:rgba(231,76,60,0.1)"' if cd > 0 else ""
        sid = s["session_id"]
        rows += (
            f'<tr{row_style}>'
            f'<td><a href="/session?sid={sid}" title="查看完整会话分析">{sid[:12]}…</a></td>'
            f'<td>{badge}</td>'
            f'<td>{s.get("source") or "—"}</td>'
            f'<td>{s.get("requests", 0)}</td>'
            f'<td>{s.get("failures", 0)}</td>'
            f'<td>{cd_html}</td>'
            f'</tr>'
        )
    return (
        '<table style="width:100%;margin-top:8px;font-size:0.85em;border-collapse:collapse">'
        '<tr style="border-bottom:1px solid #444">'
        '<th>Session</th><th>Target</th><th>Source</th><th>Reqs</th><th>Fails</th><th>Cooldown</th>'
        '</tr>'
        f'{rows}</table>'
    )


def _build_route_reason_legend() -> str:
    """Return a small inline explanation of common route_reason codes for the /status page."""
    reasons = [
        ("under_threshold", "上下文 < 阈值，本地处理"),
        ("chars_exceed_threshold", "上下文超阈值，路由到云端"),
        ("session_already_cloud", "会话已锁定云端"),
        ("session_force_local", "会话强制本地"),
        ("cloud_cooldown_active", "云端冷却中，走本地"),
        ("cloud_no_api_key", "未配置云端 API Key，回退本地"),
        ("cloud_fallback", "云端请求失败，回退本地"),
    ]
    items = "".join(
        f'<div style="font-size:0.8em;color:#888"><code>{code}</code> — {desc}</div>'
        for code, desc in reasons
    )
    return f'<div style="margin-top:6px;padding:6px;border:1px solid #333;border-radius:4px">{items}</div>'


def _build_recent_fallbacks_table(fallbacks: list) -> str:
    """Build a compact table of recent fallback events (last 5)."""
    if not fallbacks:
        return ""
    rows = ""
    for fb in fallbacks:
        ts = fb.get("timestamp", "")
        ts_short = ts[11:19] if len(ts) >= 19 else ts
        rows += (
            f'<tr><td>{ts_short}</td>'
            f'<td>{fb.get("reason", "—")}</td>'
            f'<td>{fb.get("cloud_model", "—")}</td></tr>'
        )
    return (
        '<table style="width:100%;margin-top:6px;font-size:0.82em;border-collapse:collapse">'
        '<tr style="border-bottom:1px solid #444"><th>Time</th><th>Reason</th><th>Cloud Model</th></tr>'
        f'{rows}</table>'
    )


def _format_latency(lat: dict) -> str:
    """Render a per-backend latency summary as a compact inline string.

    Shows avg / p95 / max and the sample count.  Highlighted red when p95
    exceeds the dynamic-concurrency threshold, amber when above 2× avg.
    """
    count = lat.get("count", 0)
    if not count:
        return '<span style="color:#888">No samples yet</span>'
    avg = lat.get("avg_ms", 0.0)
    p95 = lat.get("p95_ms", 0.0)
    mx = lat.get("max_ms", 0.0)
    threshold = _ps.PROXY_DYNAMIC_CONCURRENT_LATENCY_P95_MS
    if p95 > threshold:
        color = "#e74c3c"
    elif p95 > avg * 2 and p95 > 1000:
        color = "#f39c12"
    else:
        color = "#27ae60"
    return (
        f'<span style="color:{color}">'
        f'avg {avg:.0f}ms / p95 {p95:.0f}ms / max {mx:.0f}ms'
        f'</span> <span style="color:#888">({count})</span>'
    )


# ---------------------------------------------------------------------------
# Session-level analysis (timeline / switches / performance)
# ---------------------------------------------------------------------------


def _load_session_metrics(session_id: str, max_lines: int = 200000):
    """Load all metrics rows for a given session_id from proxy_metrics.jsonl.

    Returns rows sorted by timestamp (oldest first).
    """
    metrics_path = os.path.join(_ps._SCRIPT_DIR, "logs", "proxy_metrics.jsonl")
    rows = []
    try:
        with open(metrics_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("session_id") == session_id:
                    rows.append(rec)
    except FileNotFoundError:
        pass
    rows.sort(key=lambda r: r.get("ts") or "")
    return rows


def _session_percentile(vals, p):
    if not vals:
        return 0
    s = sorted(vals)
    idx = int(len(s) * p)
    return s[min(idx, len(s) - 1)]


def _analyze_session(session_id: str) -> dict:
    """Analyze a single session from metrics logs.

    Returns a dict with:
      - session_id, total, time_range, duration_seconds
      - counts: local/cloud/unknown/errors
      - performance: avg/p95/p99 duration, max input_chars, total output_chars
      - switches: list of target transitions
      - timeline: per-request detail rows
      - cost: estimated cloud cost if pricing is available
    """
    rows = _load_session_metrics(session_id)
    if not rows:
        return {"session_id": session_id, "total": 0, "timeline": []}

    timeline = []
    prev_target = None
    switches = []
    durations = []
    input_chars_all = []
    output_chars_total = 0
    local_count = cloud_count = unknown_count = error_count = 0
    cloud_cost = 0.0

    for idx, r in enumerate(rows, start=1):
        ts = r.get("ts", "")
        bd = r.get("pipeline", {}).get("backend_dispatcher", {})
        target = bd.get("route_target")
        if target is None:
            # Fallback: infer from session route map if this is the active session
            target = _ps._SESSION_ROUTE_MAP.get(session_id, "unknown")
        reason = bd.get("route_reason", "") or ""
        stage = r.get("pipeline", {}).get("smart_router", {}).get("stage", "")
        if not stage:
            stage = r.get("pipeline", {}).get("lifecycle_stage", {}).get("stage", "unknown")
        disp = bd.get("dispatch_latency_ms")
        dur = r.get("duration_ms") or 0
        in_chars = r.get("input_chars") or 0
        out_chars = r.get("output_chars") or 0
        status = r.get("status", 200)

        if target == "cloud":
            cloud_count += 1
        elif target in ("local", "local_forced"):
            local_count += 1
        else:
            unknown_count += 1

        if status not in (200, None):
            error_count += 1

        durations.append(dur)
        input_chars_all.append(in_chars)
        output_chars_total += out_chars

        # Switch detection
        if prev_target is not None and target != prev_target:
            switches.append({
                "index": idx,
                "ts": ts,
                "from": prev_target,
                "to": target,
                "reason": reason,
            })
        prev_target = target

        # Cost estimation (best-effort)
        if target == "cloud" and not bd.get("route_fallback"):
            in_tok = r.get("est_input_tokens") or int(in_chars / max(_ps.PROXY_CTX_TOKEN_RATIO, 0.1))
            out_tok = r.get("est_output_tokens") or int(out_chars / max(_ps.PROXY_CTX_TOKEN_RATIO, 0.1))
            cloud_cost += (in_tok * _ps.PROXY_CLOUD_PRICE_INPUT + out_tok * _ps.PROXY_CLOUD_PRICE_OUTPUT) / 1_000_000

        timeline.append({
            "index": idx,
            "ts": ts,
            "target": target,
            "reason": reason,
            "stage": stage,
            "input_chars": in_chars,
            "input_msgs": r.get("input_msgs") or 0,
            "input_tools": r.get("input_tools") or 0,
            "output_chars": out_chars,
            "duration_ms": dur,
            "dispatch_latency_ms": disp,
            "status": status,
            "fallback": bool(bd.get("route_fallback")),
            "emergency": bool(bd.get("emergency_fallback")),
            "loop_max_run": r.get("pipeline", {}).get("loop_detect", {}).get("max_run", 0),
            "blocker": bool(r.get("pipeline", {}).get("blocker_detect", {}).get("triggered")),
            "truncate": bool(r.get("pipeline", {}).get("truncate", {}).get("triggered")),
        })

    total = len(rows)
    start_ts = rows[0].get("ts", "")
    end_ts = rows[-1].get("ts", "")
    duration_seconds = 0
    if start_ts and end_ts:
        try:
            t0 = datetime.fromisoformat(start_ts)
            t1 = datetime.fromisoformat(end_ts)
            duration_seconds = (t1 - t0).total_seconds()
        except Exception:
            pass

    return {
        "session_id": session_id,
        "total": total,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration_seconds": duration_seconds,
        "local_count": local_count,
        "cloud_count": cloud_count,
        "unknown_count": unknown_count,
        "error_count": error_count,
        "avg_duration_ms": sum(durations) / total if total else 0,
        "p95_duration_ms": _session_percentile(durations, 0.95),
        "p99_duration_ms": _session_percentile(durations, 0.99),
        "max_input_chars": max(input_chars_all) if input_chars_all else 0,
        "total_output_chars": output_chars_total,
        "switches": switches,
        "timeline": timeline,
        "cloud_cost": cloud_cost,
    }


def _target_badge(target: str) -> str:
    if target == "cloud":
        color = "#3498db"
    elif target in ("local_forced",):
        color = "#e67e22"
    elif target == "local":
        color = "#27ae60"
    else:
        color = "#888"
    return f'<span style="color:{color};font-weight:bold">{target}</span>'


def _fmt_ms(ms):
    if ms is None or ms == 0:
        return "—"
    if ms >= 1000:
        return f"{ms/1000:.2f}s"
    return f"{ms:.0f}ms"


def _svg_line_chart(values, width=800, height=120, color="#3498db", fill=True):
    """Render a simple SVG line chart with points.

    values is a list of numeric y values (x is evenly spaced).
    """
    if not values:
        return "<div style='color:#888'>无数据</div>"
    n = len(values)
    max_v = max(values) or 1
    min_v = min(values)
    rng = max_v - min_v or 1
    pad = 4
    pts = []
    for i, v in enumerate(values):
        x = pad + (width - 2 * pad) * i / max(n - 1, 1)
        y = height - pad - (v - min_v) / rng * (height - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    path_d = "M" + " L".join(pts)
    circles = ""
    for i, v in enumerate(values):
        x = pad + (width - 2 * pad) * i / max(n - 1, 1)
        y = height - pad - (v - min_v) / rng * (height - 2 * pad)
        circles += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{color}" />'
    area = ""
    if fill:
        area_d = f"M{pad:.1f},{height-pad:.1f} L{path_d.split('L',1)[1] if 'L' in path_d else ''} L{width-pad:.1f},{height-pad:.1f} Z"
        # Simpler area: just the line path + bottom corners
        area = f'<path d="{path_d} L{width-pad:.1f},{height-pad:.1f} L{pad:.1f},{height-pad:.1f} Z" fill="{color}" opacity="0.15" />'
    return (
        f'<svg width="100%" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="background:rgba(255,255,255,0.03);border-radius:4px">'
        f'{area}<path d="{path_d}" fill="none" stroke="{color}" stroke-width="2" />{circles}</svg>'
    )


def _svg_dual_chart(rows, width=800, height=120):
    """Render input_chars line with local/cloud colored points."""
    if not rows:
        return "<div style='color:#888'>无数据</div>"
    values = [r["input_chars"] for r in rows]
    n = len(values)
    max_v = max(values) or 1
    min_v = min(values)
    rng = max_v - min_v or 1
    pad = 4
    # Main line in neutral color
    pts = []
    for i, v in enumerate(values):
        x = pad + (width - 2 * pad) * i / max(n - 1, 1)
        y = height - pad - (v - min_v) / rng * (height - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    path_d = "M" + " L".join(pts)
    circles = ""
    for i, r in enumerate(rows):
        x = pad + (width - 2 * pad) * i / max(n - 1, 1)
        y = height - pad - (r["input_chars"] - min_v) / rng * (height - 2 * pad)
        color = "#3498db" if r["target"] == "cloud" else "#27ae60"
        circles += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}" />'
    return (
        f'<svg width="100%" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="background:rgba(255,255,255,0.03);border-radius:4px">'
        f'<path d="{path_d}" fill="none" stroke="#888" stroke-width="1.5" />{circles}</svg>'
    )


def _build_session_html(session_id: str) -> str:
    """Build an HTML page showing a single session timeline and performance."""
    data = _analyze_session(session_id)
    if data["total"] == 0:
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Session {session_id}</title></head>
        <body style="background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px">
        <h2>未找到会话数据</h2><p>session_id = <code>{session_id}</code></p>
        <p><a href="/status" style="color:#3498db">← 返回 /status</a></p></body></html>"""

    total = data["total"]
    timeline = data["timeline"]
    switches = data["switches"]

    # Summary cards
    duration_str = f"{data['duration_seconds']:.0f}s" if data["duration_seconds"] < 120 else f"{data['duration_seconds']/60:.1f}min"
    switch_html = ""
    if switches:
        switch_items = ""
        for sw in switches:
            ts_short = sw["ts"][11:19] if len(sw["ts"]) >= 19 else sw["ts"]
            switch_items += (
                f'<div style="font-size:0.85em;padding:4px 0;border-bottom:1px solid #2a2a4a">'
                f'<b>{ts_short}</b> #{sw["index"]}: {_target_badge(sw["from"])} → {_target_badge(sw["to"])}'
                f'<span style="color:#888;margin-left:8px">{sw["reason"]}</span></div>'
            )
        switch_html = (
            f'<div class="card"><h2>🔄 路由切换 ({len(switches)} 次)</h2>{switch_items}</div>'
        )
    else:
        switch_html = '<div class="card"><h2>🔄 路由切换</h2><div style="color:#888">无切换，全程同一目标</div></div>'

    # Timeline rows
    rows_html = ""
    for r in timeline:
        ts_short = r["ts"][11:19] if len(r["ts"]) >= 19 else r["ts"]
        dur_bar_width = min(100, max(1, r["duration_ms"] / max(data["p99_duration_ms"], 1) * 100))
        dur_bar = (
            f'<div style="width:80px;background:#2a2a4a;height:6px;border-radius:3px;overflow:hidden">'
            f'<div style="width:{dur_bar_width:.0f}%;background:#3498db;height:100%"></div></div>'
        )
        flags = []
        if r["fallback"]:
            flags.append('<span style="color:#e74c3c">fallback</span>')
        if r["emergency"]:
            flags.append('<span style="color:#e74c3c">emergency</span>')
        if r["blocker"]:
            flags.append('<span style="color:#e67e22">blocker</span>')
        if r["loop_max_run"] >= _ps.PROXY_LOOP_THRESHOLD:
            flags.append(f'<span style="color:#f39c12">loop({r["loop_max_run"]})</span>')
        if r["truncate"]:
            flags.append('<span style="color:#f39c12">truncate</span>')
        flag_html = ", ".join(flags) if flags else "—"
        status_color = "#e74c3c" if r["status"] not in (200, None) else "#27ae60"
        rows_html += (
            f'<tr>'
            f'<td>#{r["index"]}</td>'
            f'<td>{ts_short}</td>'
            f'<td>{_target_badge(r["target"])}</td>'
            f'<td style="font-size:0.85em;color:#888">{r["reason"][:40]}</td>'
            f'<td>{r["stage"]}</td>'
            f'<td>{r["input_chars"]:,}</td>'
            f'<td>{r["output_chars"]:,}</td>'
            f'<td>{_fmt_ms(r["duration_ms"])} {dur_bar}</td>'
            f'<td>{_fmt_ms(r["dispatch_latency_ms"])}</td>'
            f'<td><span style="color:{status_color}">{r["status"]}</span></td>'
            f'<td style="font-size:0.8em">{flag_html}</td>'
            f'</tr>'
        )

    timeline_table = (
        '<table style="width:100%;font-size:0.85em;border-collapse:collapse">'
        '<tr style="border-bottom:1px solid #444;text-align:left">'
        '<th>#</th><th>Time</th><th>Target</th><th>Reason</th><th>Stage</th>'
        '<th>In chars</th><th>Out chars</th><th>Duration</th><th>Dispatch</th><th>Status</th><th>Flags</th>'
        '</tr>'
        f'{rows_html}</table>'
    )

    # Charts
    dur_values = [r["duration_ms"] for r in timeline]
    chars_svg = _svg_dual_chart(timeline)
    dur_svg = _svg_line_chart(dur_values, color="#9b59b6")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Session {session_id}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .subtitle {{ color: #888; font-size: 13px; margin-bottom: 16px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; margin-bottom: 16px; }}
  .card {{ background: #16213e; border-radius: 10px; padding: 14px; }}
  .card h2 {{ font-size: 13px; margin: 0 0 10px 0; color: #a0a0c0; text-transform: uppercase; letter-spacing: 1px; }}
  .big {{ font-size: 22px; font-weight: 700; }}
  .muted {{ color: #888; font-size: 12px; }}
  .row {{ display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #2a2a4a; font-size: 13px; }}
  .row:last-child {{ border-bottom: none; }}
  a {{ color: #3498db; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  td, th {{ padding: 6px 8px; }}
  tr:nth-child(even) {{ background: rgba(255,255,255,0.03); }}
</style>
</head>
<body>
<h1>📊 Session Analysis</h1>
<div class="subtitle">{session_id} &nbsp;•&nbsp; {data['start_ts']} → {data['end_ts']} &nbsp;•&nbsp; 时长 {duration_str}</div>

<div class="grid">
  <div class="card"><h2>总请求数</h2><div class="big">{total}</div></div>
  <div class="card"><h2>路由分布</h2>
    <div class="row"><span>Local</span><span style="color:#27ae60;font-weight:bold">{data['local_count']}</span></div>
    <div class="row"><span>Cloud</span><span style="color:#3498db;font-weight:bold">{data['cloud_count']}</span></div>
    <div class="row"><span>Unknown</span><span style="color:#888">{data['unknown_count']}</span></div>
    <div class="row"><span>Errors</span><span style="color:#e74c3c;font-weight:bold">{data['error_count']}</span></div>
  </div>
  <div class="card"><h2>延迟</h2>
    <div class="row"><span>Avg</span><span>{_fmt_ms(data['avg_duration_ms'])}</span></div>
    <div class="row"><span>P95</span><span>{_fmt_ms(data['p95_duration_ms'])}</span></div>
    <div class="row"><span>P99</span><span>{_fmt_ms(data['p99_duration_ms'])}</span></div>
  </div>
  <div class="card"><h2>上下文</h2>
    <div class="row"><span>Peak chars</span><span>{data['max_input_chars']:,}</span></div>
    <div class="row"><span>Total out</span><span>{data['total_output_chars']:,}</span></div>
    <div class="row"><span>Cloud cost</span><span>¥{data['cloud_cost']:.4f}</span></div>
  </div>
  {switch_html}
</div>

<div class="card" style="margin-bottom:16px">
  <h2>📈 输入字符变化（绿=Local，蓝=Cloud）</h2>
  {chars_svg}
</div>

<div class="card" style="margin-bottom:16px">
  <h2>⏱️ 请求耗时变化</h2>
  {dur_svg}
</div>

<div class="card">
  <h2>🕒 请求时间线</h2>
  {timeline_table}
</div>

<div style="margin-top:16px"><a href="/status">← 返回 /status</a></div>
</body>
</html>"""
    return html


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
    route = _get_route_stats()
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

    # --- Route card ---
    route_status_icon = "✅" if route["route_enabled"] else "❌"
    route_status_text = "Enabled" if route["route_enabled"] else "Disabled"
    route_target_text = "Cloud" if route["cloud_count"] > route["local_count"] else "Local"
    # Cloud API key indicator — visual health check for prerequisite cloud configs
    api_key_ok = route.get("cloud_api_key_configured", False)
    if not _ps.PROXY_ROUTE_ENABLED:
        api_key_badge = '<span style="color:#888">—（路由已禁用）</span>'
    elif api_key_ok:
        api_key_badge = '<span style="color:#27ae60">✅ 已配置</span>'
    else:
        api_key_badge = '<span style="color:#e74c3c">❌ 未配置（路由会回退本地）</span>'
    # Tiered budget alert badge and progress bar
    budget_pct = route.get("budget_used_pct", 0.0)
    alert_level = route.get("budget_alert_level", "")
    if route["daily_budget"] > 0 and budget_pct > 0:
        if alert_level == "critical":
            budget_color = "#e74c3c"
            budget_badge = "🔴 预算耗尽"
        elif alert_level == "danger":
            budget_color = "#e67e22"
            budget_badge = "🟠 高使用率"
        elif alert_level == "warning":
            budget_color = "#f39c12"
            budget_badge = "🟡 注意"
        else:
            budget_color = "#27ae60"
            budget_badge = ""
        route_budget_warn = f' <span style="color:{budget_color}">({budget_pct:.0f}% used)</span>'
    else:
        budget_color = "#27ae60"
        budget_badge = ""
        route_budget_warn = ""

    budget_bar_width = min(100.0, max(0.0, budget_pct))
    budget_bar = (
        f'<div style="margin-top:4px;display:flex;height:18px;border-radius:3px;overflow:hidden;'
        f'font-size:0.75em;background:rgba(255,255,255,0.1)">'
        f'<div style="width:{budget_bar_width:.1f}%;background:{budget_color};color:#fff;'
        f'text-align:center;line-height:18px">¥{route["daily_cost"]:.2f}</div>'
        f'</div>'
    )
    hard_stop_badge = (
        '<span style="color:#e74c3c;margin-left:6px">🛑 硬停止已启用</span>'
        if route.get("daily_budget_hard_stop") else
        '<span style="color:#888;margin-left:6px">（硬停止关闭）</span>'
    )

    # Cloud ratio bar — visual indicator of local/cloud split
    total_reqs = route["local_count"] + route["cloud_count"]
    if total_reqs > 0:
        local_bar_pct = route["local_count"] / total_reqs * 100
        cloud_bar_pct = route["cloud_count"] / total_reqs * 100
        route_bar = (
            f'<div style="margin-top:4px;display:flex;height:18px;border-radius:3px;overflow:hidden;font-size:0.75em">'
            f'<div style="width:{local_bar_pct:.1f}%;background:#27ae60;color:#fff;text-align:center;line-height:18px">Local {route["local_count"]}</div>'
            f'<div style="width:{cloud_bar_pct:.1f}%;background:#3498db;color:#fff;text-align:center;line-height:18px">Cloud {route["cloud_count"]}</div>'
            f'</div>'
        )
    else:
        route_bar = '<div style="margin-top:4px;color:#888;font-size:0.85em">No requests yet</div>'

    # Last route decision — highlight the most recent routing decision
    last_reason_html = ""
    if route.get("last_route_reason"):
        last_target = route.get("last_route_target", "local")
        rc = "#27ae60" if last_target != "cloud" else "#3498db"
        last_ts = route.get("last_route_timestamp", "")
        ts_display = last_ts[11:19] if len(last_ts) >= 19 else ""
        ts_span = f'<span style="color:#888">  {ts_display}</span>' if ts_display else ""
        last_reason_html = (
            f'<div style="margin-top:6px;padding:6px 8px;border-left:3px solid {rc};'
            f'background:rgba(0,0,0,0.2);font-size:0.85em">'
            f'<b>Last Decision:</b> <span style="color:{rc}">{last_target}</span>'
            f' — <code>{route["last_route_reason"]}</code>'
            f'{ts_span}'
            f'</div>'
        )

    # Cooldown sessions summary
    cooldown_html = ""
    cooldown_sessions = route.get("cooldown_sessions", [])
    if cooldown_sessions:
        cooldown_html = (
            f'<div style="margin-top:6px;padding:6px;background:rgba(231,76,60,0.15);border-radius:4px;font-size:0.85em">'
            f'⚠️ <b>{len(cooldown_sessions)}</b> session(s) in cloud cooldown: '
            + ", ".join(
                f'<code>{cs["session_id"][:8]}…</code> ({cs["remaining_s"]}s)'
                for cs in cooldown_sessions
            )
            + '</div>'
        )

    # Recent fallbacks table
    fallbacks_html = ""
    if route.get("recent_fallbacks"):
        fallbacks_html = (
            '<div style="margin-top:6px"><b style="font-size:0.85em">Recent Fallbacks (last 5):</b>'
            + _build_recent_fallbacks_table(route["recent_fallbacks"])
            + '</div>'
        )

    route_card = f"""<div class="card" style="grid-column: 1 / -1;">
    <h2>🔀 Intelligent Routing</h2>
    <div class="row"><span class="label">Status</span><span class="value">{route_status_icon} {route_status_text}</span></div>
    <div class="row"><span class="label">API Key</span><span class="value">{api_key_badge}</span></div>
    <div class="row"><span class="label">Cloud Model</span><span class="value">{route["cloud_model"]}</span></div>
    <div class="row"><span class="label">Cloud Endpoint</span><span class="value" style="font-size:0.8em">{route["cloud_base_url"]}</span></div>
    <div class="row"><span class="label">Threshold</span><span class="value">{route["threshold"]:,} chars</span></div>
    <div class="row"><span class="label">Memory Trigger</span><span class="value">{route["memory_pct"]}% used</span></div>
    <div class="row"><span class="label">Profile</span><span class="value">{route["profile"]}</span></div>
    <div class="row"><span class="label">Fallback</span><span class="value">{"✅ enabled (max " + str(route["max_cloud_fails"]) + " fails)" if route["fallback_enabled"] else "❌ disabled"}</span></div>
    <div class="row"><span class="label">Cloud Concurrent</span><span class="value">{route["cloud_concurrent"]}</span></div>
    {route_bar}
    <div class="row" style="margin-top:6px"><span class="label">Cloud Ratio</span><span class="value">{route["cloud_pct"]:.1f}%</span></div>
    <div class="row"><span class="label">Fallbacks</span><span class="value">{route["fallback_count"]}</span></div>
    {last_reason_html}
    {fallbacks_html}
    {cooldown_html}
    <div class="row" style="margin-top:6px"><span class="label">Latency (Local)</span><span class="value">{_format_latency(route.get("local_latency", {}))}</span></div>
    <div class="row"><span class="label">Latency (Cloud)</span><span class="value">{_format_latency(route.get("cloud_latency", {}))}</span></div>
    <div class="row" style="margin-top:6px"><span class="label">Active Sessions</span><span class="value">{route.get("session_cloud", "?")} cloud / {route.get("session_local", "?")} local / {route.get("session_total", 0)} total</span></div>
    <div class="row"><span class="label">Daily Cost</span><span class="value">¥{route["daily_cost"]:.2f} / ¥{route["daily_budget"]:.0f}{route_budget_warn}</span></div>
    {budget_bar}
    <div class="row" style="margin-top:4px"><span class="label">Hard Stop</span><span class="value">{hard_stop_badge}</span></div>
    {budget_badge and f'<div style="margin-top:6px;padding:6px 8px;border-radius:4px;background:rgba(231,76,60,0.15);font-size:0.85em">{budget_badge}</div>' or ''}
    {_build_active_sessions_table(route.get("active_sessions", [])) if route.get("active_sessions") else ""}
    {_build_route_reason_legend()}
  </div>"""

    # --- Alerts card ---
    alerts = list(traffic.get("alerts", []))
    # Inject budget tiered alert into the global alert stream
    if route.get("budget_alert_level") == "critical":
        alerts.insert(
            0,
            (
                "critical",
                f"Daily cloud budget exceeded: ¥{route['daily_cost']:.2f} / ¥{route['daily_budget']:.0f} "
                f"({route['budget_used_pct']:.0f}%). Cloud routing is hard-stopped.",
            ),
        )
    elif route.get("budget_alert_level") == "danger":
        alerts.insert(
            0,
            (
                "warning",
                f"Daily cloud budget high: ¥{route['daily_cost']:.2f} / ¥{route['daily_budget']:.0f} "
                f"({route['budget_used_pct']:.0f}%).",
            ),
        )
    elif route.get("budget_alert_level") == "warning":
        alerts.insert(
            0,
            (
                "warning",
                f"Daily cloud budget over 50%: ¥{route['daily_cost']:.2f} / ¥{route['daily_budget']:.0f} "
                f"({route['budget_used_pct']:.0f}%).",
            ),
        )
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
    <div class="row"><span class="label">Type</span><span class="value">Cloud API ({_ps.BACKEND_TYPE})</span></div>
    <div class="row"><span class="label">Endpoint</span><span class="value">{_ps.LLAMA_BASE}</span></div>
    <div class="row"><span class="label">Model</span><span class="value">{_ps.MODEL_NAME}</span></div>
    <div class="row"><span class="label">API Key</span><span class="value">{_ps.LLAMA_API_KEY[:8]}****</span></div>
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
    <div class="row"><span class="label">Backend</span><span class="value">{_ps.LLAMA_BASE}</span></div>
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
    <div class="row"><span class="label">Config</span><span class="value">CLEAR={'on' if _ps.PROXY_CLEAR_ENABLED else 'off'}, LIMIT={'on' if _ps.PROXY_CTX_LIMIT_ENABLED else 'off'}, MAX_CONCURRENT={_ps.PROXY_MAX_CONCURRENT}</span></div>
    <div class="row"><span class="label">Model</span><span class="value">{_ps.MODEL_NAME}</span></div>
    {'<div class="row"><span class="label">Memory Alert</span><span class="value" style="color:#e74c3c">⚠️ Used ' + str(mem_used_pct) + '% (reject threshold ' + str(_ps.PROXY_MEMORY_REJECT_THRESHOLD) + '%)</span></div>' if mem_warn else ''}
  </div>

  {traffic_card}

  {route_card}

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
    "_load_session_metrics",
    "_analyze_session",
    "_build_session_html",
    "_finalize_metrics",
    "_mc_put",
]
