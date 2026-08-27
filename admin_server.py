"""Admin server: status page, system monitoring, metrics, and observability.

Functions for building the HTML status page at /status, collecting system
memory stats, parsing backend/proxy logs, and managing request snapshots.
All functions are stateless — they read from proxy_state and file system.
"""
import json
import os, re, subprocess, time, threading
from datetime import datetime, timedelta
import proxy_state as _ps
import model_registry
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
# --- _current_active_profile ---
def _current_active_profile():
    """Return the basename of the configs/active.conf symlink target, or 'default'."""
    try:
        if os.path.islink(_ps._ACTIVE_CONF_PATH):
            target = os.readlink(_ps._ACTIVE_CONF_PATH)
            return os.path.splitext(os.path.basename(target))[0]
    except OSError:
        pass
    return "default"


# --- _parse_conf_value ---
def _parse_conf_value(path, key, default=""):
    """Read a single KEY=\"value\" line from a bash-sourcable config file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    value = line[len(key) + 1:]
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    return value
    except OSError:
        pass
    return default


# --- _parse_memory_gb ---
def _parse_memory_gb(memory_str):
    """Extract a representative GB number from strings like '~14-18 GB' or '32GB'.

    Returns None when the string does not contain a numeric GB estimate.
    """
    if not memory_str:
        return None
    # Look for ranges like 14-18 or single numbers followed by optional GB/gb
    numbers = re.findall(r"(\d+(?:\.\d+)?)", memory_str)
    if not numbers:
        return None
    try:
        vals = [float(n) for n in numbers]
    except ValueError:
        return None
    # Use the largest number as the conservative estimate for ranges.
    return max(vals)


# --- _build_profiles_json ---
def _build_profiles_json():
    """Return a list of all available profiles from configs/*.conf."""
    configs_dir = os.path.join(_ps._SCRIPT_DIR, "configs")
    active = _current_active_profile()
    profiles = []
    try:
        entries = sorted(os.listdir(configs_dir))
    except OSError:
        return profiles

    for name in entries:
        if not name.endswith(".conf") or name == "active.conf" or name == "secret.local.conf":
            continue
        path = os.path.join(configs_dir, name)
        if not os.path.isfile(path):
            continue
        profile_name = os.path.splitext(name)[0]
        desc = _parse_conf_value(path, "CONFIG_DESC", "")
        memory_str = _parse_conf_value(path, "CONFIG_MEMORY", "")
        memory_gb = _parse_memory_gb(memory_str)
        profiles.append({
            "name": profile_name,
            "desc": desc,
            "memory_gb": memory_gb,
            "active": profile_name == active,
        })
    return profiles
# --- _elapsed_to_seconds ---
def _elapsed_to_seconds(elapsed_str):
    """Convert ps etime like '02:15' or '3-02:15:30' to seconds."""
    if not elapsed_str:
        return 0
    elapsed_str = str(elapsed_str).strip()
    days = 0
    if "-" in elapsed_str:
        days_part, elapsed_str = elapsed_str.split("-", 1)
        try:
            days = int(days_part)
        except ValueError:
            days = 0
    parts = elapsed_str.split(":")
    try:
        if len(parts) == 3:
            # dd-hh:mm:ss or hh:mm:ss
            h, m, s = map(int, parts)
        elif len(parts) == 2:
            # mm:ss
            h = 0
            m, s = map(int, parts)
        else:
            # ss
            h = m = 0
            s = int(parts[0])
    except ValueError:
        return 0
    return days * 86400 + h * 3600 + m * 60 + s
# --- _probe_backend_model_name / _probe_backend_ready ---
def _probe_backend_models():
    """Probe the backend /v1/models endpoint and return the parsed response dict.

    Returns an empty dict on any failure.
    """
    import urllib.request
    import urllib.error

    base = _ps.LLAMA_BASE
    if not base:
        return {}
    url = base.rstrip("/") + "/models"
    headers = {}
    if _ps.IS_CLOUD and _ps.LLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {_ps.LLAMA_API_KEY}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status != 200:
                return {}
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def _probe_backend_model_name():
    """Return the model name reported by the backend, or None if unavailable."""
    data = _probe_backend_models()
    models = data.get("data") if isinstance(data.get("data"), list) else data.get("models") if isinstance(data.get("models"), list) else []
    if not models:
        return None
    ids = [m.get("id") or m.get("model") for m in models if isinstance(m, dict)]
    ids = [i for i in ids if i]
    expected = getattr(_ps, "MODEL_NAME", None)
    # 后端可能列出多个模型(dflash 列出全部本地 MLX 模型):优先精确匹配
    # 配置 MODEL_NAME,其次家族兼容,避免探到错误的模型触发 model_drift。
    if expected:
        for mid in ids:
            if mid == expected:
                return mid
        for mid in ids:
            if mid and _model_slugs_compatible(mid, expected):
                return mid
    return ids[0] if ids else None


def _probe_backend_ready():
    """Probe whether the real backend is loaded and ready to accept inference."""
    data = _probe_backend_models()
    models = data.get("data") if isinstance(data.get("data"), list) else data.get("models") if isinstance(data.get("models"), list) else []
    return len(models) > 0


def _model_slugs_compatible(actual, expected):
    """Return True if actual/expected model names refer to the same model family.

    Different orgs or quant suffixes (e.g. mlx-community vs unsloth, -4bit vs
    -UD-MLX-4bit) should not trigger a false-positive drift alert.  We compare
    the alphanumeric tokens from the basename; if they share enough tokens we
    consider them compatible.
    """
    if not actual or not expected:
        return True
    actual_short = actual.split("/")[-1]
    expected_short = expected.split("/")[-1]
    if actual_short == expected_short:
        return True

    def _tokens(s):
        return set(re.findall(r"[A-Za-z0-9\.]+", s))

    a_tokens = _tokens(actual_short)
    b_tokens = _tokens(expected_short)
    if not a_tokens or not b_tokens:
        return False
    intersection = a_tokens & b_tokens
    min_len = min(len(a_tokens), len(b_tokens))
    # Share at least 2 tokens, or at least half of the smaller token set.
    return len(intersection) >= max(2, min_len // 2)
# --- _build_route_policies_json ---
def _build_route_policies_json():
    """R9: sanitized routing policies + model catalog for agent_go.

    NEVER returns key material — provider keys surface as `key_set` bool only.
    `catalog_hash` lets agent_go detect drift between its own provider config
    (direct-connect path) and this proxy's catalog (via-proxy path).
    """
    def _key_set(key_env):
        if not key_env:
            return False
        return bool(_ps._env_lookup(key_env, ""))

    providers = {}
    for pname in model_registry.list_providers():
        p = model_registry.get_provider(pname) or {}
        # Dispatch-effective key: anthropic-protocol providers authenticate
        # with anthropic_key_env (may differ from the archived openai key_env).
        if p.get("protocol") == "anthropic":
            eff_key_env = p.get("anthropic_key_env") or p.get("key_env", "")
        else:
            eff_key_env = p.get("key_env", "")
        providers[pname] = {
            "base_url": p.get("base_url", ""),
            "base_url_env": p.get("base_url_env", ""),
            "protocol": p.get("protocol", "openai"),
            "anthropic_base_url": p.get("anthropic_base_url", ""),
            "anthropic_compatible": bool(p.get("anthropic_compatible", False)),
            "key_env": eff_key_env,
            "key_set": _key_set(eff_key_env),
            "concurrent": p.get("concurrent"),
            "concurrent_env": p.get("concurrent_env", ""),
        }

    models = {}
    for mname in model_registry.list_models():
        m = model_registry.get_model(mname) or {}
        caps = m.get("capabilities") or {}
        prov = model_registry.get_provider(m.get("provider", "")) or {}
        models[mname] = {
            "provider": m.get("provider", ""),
            "tier": m.get("tier", ""),
            "price": m.get("price"),
            "thinking": caps.get("thinking"),
            "json_compliance": caps.get("json"),
            "context_tokens": caps.get("context_tokens"),
            "vision": bool(caps.get("vision", False)),
            "direct_capable": bool(prov.get("anthropic_compatible", False)),
        }

    defaults_raw = model_registry._catalog().get("defaults", {})
    defaults = {
        "cloud_model": defaults_raw.get("cloud_model", ""),
        "daily_budget": defaults_raw.get("daily_budget"),
    }
    if "per_provider_budget" in defaults_raw:
        defaults["per_provider_budget"] = defaults_raw["per_provider_budget"]

    return {
        "api_version": _ps.PROXY_STATUS_API_VERSION,
        "catalog_hash": model_registry.catalog_hash(),
        "catalog_source": "file" if model_registry.is_loaded_from_file() else "synthesized",
        "catalog_error": model_registry.last_error(),
        "route_enabled": _ps.PROXY_ROUTE_ENABLED,
        "threshold_chars": _ps.PROXY_ROUTE_THRESHOLD_CHARS,
        "cloud_model": _ps.PROXY_CLOUD_MODEL,
        "cloud_key_set": _key_set("PROXY_CLOUD_API_KEY"),
        "providers": providers,
        "models": models,
        # Contract field (需求稿 §R9): alias → {route_bias, behavior, cloud_model, ...}
        "preferences": _ps.MODEL_ROUTE_PREFERENCES,
        "defaults": defaults,
    }


# --- _build_status_json ---
def _build_status_json():
    """Build the structured JSON status payload for agent_go."""
    backend_info = _get_process_info("rapid-mlx|llama-server|dflash", "Backend")
    proxy_info = _get_process_info("anthropic_proxy.py", "Proxy", fallback_port=4000)

    proxy_alive = proxy_info.get("running", False)
    backend_alive = backend_info.get("running", False)

    proxy_pid = proxy_info.get("pid")
    backend_pid = backend_info.get("pid")

    proxy_uptime = _elapsed_to_seconds(proxy_info.get("elapsed", ""))
    backend_uptime = _elapsed_to_seconds(backend_info.get("elapsed", ""))

    active_profile = _current_active_profile()

    backend_model_name = None
    ready = False
    state = "down"

    if not proxy_alive:
        state = "proxy_down"
    elif _ps.IS_CLOUD:
        backend_alive = True  # cloud has no local PID
        backend_model_name = _probe_backend_model_name() or _ps.MODEL_NAME
        ready = _probe_backend_ready()
        state = "healthy" if ready else "backend_down"
    else:
        if not backend_alive:
            state = "backend_down"
        else:
            backend_model_name = _probe_backend_model_name()
            ready = _probe_backend_ready()
            if not ready:
                state = "starting"
            else:
                expected = _ps.MODEL_NAME
                # Drift detection: tolerate org/quant suffix differences
                if backend_model_name and expected and not _model_slugs_compatible(backend_model_name, expected):
                    state = "model_drift"
                else:
                    state = "healthy"

    return {
        "api_version": _ps.PROXY_STATUS_API_VERSION,
        "proxy": {
            "pid": int(proxy_pid) if proxy_pid else None,
            "uptime_sec": proxy_uptime,
            "alive": proxy_alive,
        },
        "backend": {
            "pid": int(backend_pid) if backend_pid else None,
            "uptime_sec": backend_uptime,
            "alive": backend_alive,
            "model_name": backend_model_name or _ps.MODEL_NAME,
            "backend_type": _ps.BACKEND_TYPE or ("rapid-mlx" if not _ps.IS_CLOUD else "cloud"),
            "base_url": _ps.LLAMA_BASE,
            # R13-R16: 运行时后端名（启动轴 LLAMA_BACKEND，manage.sh source conf 注入）
            "name": getattr(_ps, "PROXY_BACKEND_NAME", "unknown"),
        },
        "active_profile": active_profile,
        "state": state,
        "ready": ready,
        # R11: routing config summary — complements /api/route/policies (R9:
        # full catalog there, current-state digest here).
        "route_config": {
            "route_enabled": _ps.PROXY_ROUTE_ENABLED,
            "cloud_model": _ps.PROXY_CLOUD_MODEL,
            "cloud_key_set": bool(_ps.PROXY_CLOUD_API_KEY),
            "cloud_concurrent": _ps.PROXY_ROUTE_CLOUD_CONCURRENT,
        },
        # R16: context-engineering config digest（仿 R11 route_config 先例）—
        # bench manifest 口径标注的机读数据源（上游设计 §9 P0-1）。
        # epoch_S / window_K 在上下文工程 Phase 1 落地前为 None。
        # G-E: 补压缩「有效状态」——mode 是配置值,enabled=false 时 mode 无意义,
        # bench 口径须以有效行为标注(臂间误标会让 A/B 结论不可信)。
        "ctx_config": {
            "diag_enabled": bool(getattr(_ps, "PROXY_DIAG_ENABLED", False)),
            "engine_enabled": bool(getattr(_ps, "PROXY_CTX_ENGINE_ENABLED", False)),
            "compression_mode": getattr(_ps, "PROXY_COMPRESS_MODE", None),
            "compress_enabled": bool(getattr(_ps, "PROXY_COMPRESS_ENABLED", False)),
            "compression_profile": getattr(_ps, "PROXY_COMPRESSION_PROFILE", None),
            "bm25_enabled": bool(getattr(_ps, "PROXY_BM25_ENABLED", False)),
            "feedback_injection_enabled": False,  # 合成负反馈 Phase 2 落地后接线
            "epoch_S": _ctx_engine_values()[0],
            "window_K": _ctx_engine_values()[1],
        },
    }


def _ctx_engine_values():
    """epoch_S / window_K 生效值（引擎关 = None 保持原语义；S/K auto 推导）。"""
    if not getattr(_ps, "PROXY_CTX_ENGINE_ENABLED", False):
        return None, None
    try:
        import context_engine
        return (context_engine.effective_trigger_tokens(),
                context_engine.effective_window_k())
    except Exception:
        return None, None
# --- _build_watchdog_json ---
def _build_watchdog_json():
    """Return structured watchdog status by reading logs/watchdog_state.json."""
    default = {
        "enabled": False,
        "running": False,
        "pid": None,
        "last_restart_at": "",
        "restart_count_1h": 0,
        "last_failure_reason": "",
    }
    try:
        with open(_ps._WATCHDOG_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return default

    pid = state.get("pid")
    running = False
    if pid:
        try:
            os.kill(int(pid), 0)
            running = True
        except (OSError, ValueError):
            pass

    return {
        "enabled": bool(state.get("enabled", False)),
        "running": running,
        "pid": pid,
        "last_restart_at": state.get("last_restart_at", ""),
        "restart_count_1h": int(state.get("restart_count_1h", 0)),
        "last_failure_reason": state.get("last_failure_reason", ""),
    }


def _build_queue_json():
    """请求优先级队列状态（GET /api/queue）。

    队列未启用（PROXY_QUEUE_ENABLED=false，默认）时返回 enabled=false + 静态字段，
    不触发队列管理器的惰性构建。
    """
    if not getattr(_ps, "PROXY_QUEUE_ENABLED", False):
        return {
            "enabled": False,
            "workers": _ps.PROXY_MAX_CONCURRENT,
            "waiting": 0,
            "by_bucket": {"interactive": 0, "standard": 0, "large": 0},
            "oldest_wait_ms": 0,
        }
    try:
        stats = _ps.get_queue_manager().stats()
    except Exception as e:
        return {"enabled": True, "error": f"queue_unavailable: {e}"}
    return {"enabled": True, **stats}
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
        if pipeline.get("loop_detect", {}).get("level", 0) >= 1:
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
def _get_compression_stats():
    """Aggregate当日 CompressionResult 统计 (design §4.7).

    从 proxy_metrics.jsonl 解析 pipeline.content_compressor / context_truncator /
    oom_safety 的 compression 段, 聚合:
      - strategy_counts: 各 strategy 出现次数
      - avg_compression_ratio: 平均压缩比
      - skipped_reason_counts: 各 skipped_reason 出现次数
      - truncated_total: 实际截断请求数
      - protected_pair_avg: 平均 protected_indices 长度

    当 metrics 文件不存在或为空时返回 _empty_compression_stats.
    """
    try:
        with open(_ps._METRICS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, IOError):
        return _empty_compression_stats()

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
        return _empty_compression_stats()

    now = datetime.now()
    today_records = [r for r in records if (now - r["_ts"]).total_seconds() <= 86400]
    recent_records = [r for r in records if (now - r["_ts"]).total_seconds() <= 600]

    def _collect(recs):
        strategy_counts = {}
        skipped_reason_counts = {}
        ratios = []
        truncated_total = 0
        protected_pair_lens = []
        for r in recs:
            p = r.get("pipeline", {})
            for stage_key in ("content_compressor", "context_truncator", "oom_safety"):
                comp = p.get(stage_key, {}).get("compression", {})
                if not comp:
                    continue
                s = comp.get("strategy")
                if s:
                    strategy_counts[s] = strategy_counts.get(s, 0) + 1
                sr = comp.get("skipped_reason")
                if sr:
                    skipped_reason_counts[sr] = skipped_reason_counts.get(sr, 0) + 1
                ratio = comp.get("ratio")
                if isinstance(ratio, (int, float)):
                    ratios.append(ratio)
                if comp.get("truncated"):
                    truncated_total += 1
                pn = comp.get("protected_n", 0)
                if isinstance(pn, (int, float)):
                    protected_pair_lens.append(pn)
        return {
            "strategy_counts": strategy_counts,
            "avg_compression_ratio": round(sum(ratios) / len(ratios), 3) if ratios else 0.0,
            "skipped_reason_counts": skipped_reason_counts,
            "truncated_total": truncated_total,
            "protected_pair_avg": round(sum(protected_pair_lens) / len(protected_pair_lens), 1) if protected_pair_lens else 0.0,
        }

    return {
        "today": _collect(today_records),
        "last_10m": _collect(recent_records),
    }


def _empty_compression_stats():
    return {
        "today": {
            "strategy_counts": {},
            "avg_compression_ratio": 0.0,
            "skipped_reason_counts": {},
            "truncated_total": 0,
            "protected_pair_avg": 0.0,
        },
        "last_10m": {
            "strategy_counts": {},
            "avg_compression_ratio": 0.0,
            "skipped_reason_counts": {},
            "truncated_total": 0,
            "protected_pair_avg": 0.0,
        },
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
    # Track per-session latest target / request count from recent metrics so that
    # sessions which are local-by-default (not in _SESSION_ROUTE_MAP) still show up.
    recent_cutoff = (datetime.now() - timedelta(minutes=10)).isoformat()
    metrics_session_targets = {}
    metrics_session_counts = {}
    metrics_session_ts = {}
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
                    sid = rec.get("session_id", "")
                    # Only consider recent records for active session display
                    if sid and ts and ts >= recent_cutoff:
                        if ts >= metrics_session_ts.get(sid, ""):
                            metrics_session_targets[sid] = rt
                            metrics_session_ts[sid] = ts
                        metrics_session_counts[sid] = metrics_session_counts.get(sid, 0) + 1
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
        seen_active = set()
        for sid, target in sorted(_ps._SESSION_ROUTE_MAP.items()):
            seen_active.add(sid)
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
        # Include sessions seen in metrics that are not in the live route map
        # (e.g., local-by-default sessions never got a _SESSION_ROUTE_MAP entry).
        for sid, target in metrics_session_targets.items():
            if sid and sid not in seen_active:
                active_sessions.append({
                    "session_id": sid,
                    "target": target,
                    "source": "",
                    "failures": _ps._cloud_fail_count.get(sid, 0),
                    "requests": metrics_session_counts.get(sid, 0),
                    "cooldown_remaining": 0,
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

    # Recompute session summary from the merged active_sessions list so that
    # metrics-derived local-by-default sessions are also counted.
    session_cloud = sum(1 for s in active_sessions if s.get("target") == "cloud")
    session_local = sum(1 for s in active_sessions if s.get("target") in ("local", "local_forced"))
    session_total = len(active_sessions)

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
        ts_short = _fmt_ts(ts)
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

# M-3: Simple LRU cache for session metrics — cleared when file mtime changes.
_SESSION_METRICS_CACHE = {}  # session_id -> list[dict]
_SESSION_METRICS_CACHE_MTIME = 0.0
_SESSION_METRICS_CACHE_MAXSIZE = 16


def _load_session_metrics(session_id: str, max_lines: int = 200000):
    """Load all metrics rows for a given session_id from proxy_metrics.jsonl.

    Returns rows sorted by timestamp (oldest first). Downstream callers rely
    on this ordering (e.g. rows[0] is the session start, rows[-1] the end).
    Uses a simple LRU cache keyed by session_id; the cache is invalidated
    when the file mtime changes.
    """
    metrics_path = os.path.join(_ps._SCRIPT_DIR, "logs", "proxy_metrics.jsonl")
    global _SESSION_METRICS_CACHE, _SESSION_METRICS_CACHE_MTIME
    try:
        current_mtime = os.path.getmtime(metrics_path)
    except OSError:
        return []
    if current_mtime != _SESSION_METRICS_CACHE_MTIME:
        _SESSION_METRICS_CACHE.clear()
        _SESSION_METRICS_CACHE_MTIME = current_mtime
    # Return cached result if present
    cached = _SESSION_METRICS_CACHE.get(session_id)
    if cached is not None:
        return cached
    rows = []
    try:
        with open(metrics_path, "r", encoding="utf-8") as f:
            from collections import deque
            for line in deque(f, maxlen=max_lines):
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
    # Cache with LRU eviction (pop oldest when at maxsize)
    if len(_SESSION_METRICS_CACHE) >= _SESSION_METRICS_CACHE_MAXSIZE:
        _SESSION_METRICS_CACHE.pop(next(iter(_SESSION_METRICS_CACHE)), None)
    _SESSION_METRICS_CACHE[session_id] = rows
    return rows


def _load_recent_session_ids(max_lines: int = 5000, n: int = 12):
    """Return the most meaningful session_ids from proxy_metrics.jsonl.

    Filters out auto-generated noise sessions (req_* prefix, count < 2).
    Ranks by a combined score of recency × activity so high-activity sessions
    are visible even when many one-shot requests flood the metrics log.

    Returns a list of dicts with keys:
      session_id, count, last_ts, score, model, models_count, client_type
    ordered by score descending.
    """
    metrics_path = os.path.join(_ps._SCRIPT_DIR, "logs", "proxy_metrics.jsonl")
    counts = {}
    last_ts = {}
    models = {}  # sid -> set of model names
    client_types = {}  # sid -> {type: count}
    try:
        with open(metrics_path, "r", encoding="utf-8") as f:
            from collections import deque
            for line in deque(f, maxlen=max_lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = rec.get("session_id")
                if not sid:
                    continue
                counts[sid] = counts.get(sid, 0) + 1
                ts = rec.get("ts", "")
                if ts and ts > last_ts.get(sid, ""):
                    last_ts[sid] = ts
                # Track model used
                bd = rec.get("pipeline", {}).get("backend_dispatcher", {})
                target = bd.get("route_target", "")
                if target == "cloud":
                    m = bd.get("route_cloud_model", "") or _ps.PROXY_CLOUD_MODEL
                elif target in ("local", "local_forced"):
                    m = _ps.MODEL_NAME
                else:
                    m = ""
                if m:
                    models.setdefault(sid, set()).add(m)
                # Track client type
                ct = rec.get("client_type", "")
                if ct:
                    client_types.setdefault(sid, {})
                    client_types[sid][ct] = client_types[sid].get(ct, 0) + 1
    except FileNotFoundError:
        pass

    # Compute score: recency (0-1) × sqrt(activity) to surface busy sessions
    all_ts = [t for t in last_ts.values() if t]
    max_ts = max(all_ts) if all_ts else ""
    sessions = []
    for sid in counts:
        ts = last_ts.get(sid, "")
        cnt = counts[sid]
        # Include all sessions regardless of req_* prefix.
        # req_* = auto-generated ID when Client doesn't send X-Claude-Code-Session-Id;
        # these are still valid sessions (e.g. opencode client requests).
        # Recency score: 1.0 for most recent, decays linearly to 0.0 for oldest
        if max_ts and ts:
            try:
                from datetime import datetime, timedelta
                t = datetime.fromisoformat(ts)
                t0 = datetime.fromisoformat(max_ts)
                age_hours = (t0 - t).total_seconds() / 3600
                recency = max(0.0, 1.0 - age_hours / 72)  # decay over 72h
            except Exception:
                recency = 0.0
        else:
            recency = 0.0
        score = recency * (cnt ** 0.5)
        session_models = models.get(sid, set())
        first_model = next(iter(sorted(session_models))) if session_models else ""
        ct_counts = client_types.get(sid, {})
        client_type = max(ct_counts, key=ct_counts.get) if ct_counts else "unknown"
        sessions.append({
            "session_id": sid, "count": cnt, "last_ts": ts, "score": round(score, 2),
            "model": first_model,
            "models_count": len(session_models),
            "client_type": client_type,
            "route_type": "",
        })

    # Sort by most recent timestamp first
    sessions.sort(key=lambda s: s["last_ts"], reverse=True)
    # If no meaningful sessions found, include most recent (up to n)
    if not sessions:
        for sid in sorted(counts, key=lambda s: last_ts.get(s, ""), reverse=True):
            ts = last_ts.get(sid, "")
            ct_counts = client_types.get(sid, {})
            client_type = max(ct_counts, key=ct_counts.get) if ct_counts else "unknown"
            sessions.append({
                "session_id": sid, "count": counts[sid], "last_ts": ts, "score": 0.0,
                "client_type": client_type,
            })
            if len(sessions) >= n:
                break
    return sessions[:n]


def _session_percentile(vals, p):
    if not vals:
        return 0
    s = sorted(vals)
    idx = int(len(s) * p)
    return s[min(idx, len(s) - 1)]


def _fallback_client_type_from_log(session_id: str) -> str:
    """Scan anthropic_proxy.log for the first User-Agent of a session.

    Metrics written before client_type was captured do not include the field.
    This fallback allows historical sessions to still show a meaningful client
    type label after the feature is deployed.
    """
    log_path = os.path.join(_ps._SCRIPT_DIR, "logs", "anthropic_proxy.log")
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            needle = f"[sess={session_id}]"
            for line in f:
                if needle not in line:
                    continue
                if "User-Agent" in line:
                    m = re.search(r"'User-Agent': '([^']+)'", line)
                    if m:
                        return _ps._detect_client_type(m.group(1))
    except (OSError, FileNotFoundError):
        pass
    return "unknown"


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
    models_seen = set()
    first_model = ""
    client_types = {}

    for idx, r in enumerate(rows, start=1):
        ts = r.get("ts", "")
        bd = r.get("pipeline", {}).get("backend_dispatcher", {})
        target = bd.get("route_target")
        if target is None:
            # Fallback: smart_router sets target before BackendDispatcher
            target = r.get("pipeline", {}).get("smart_router", {}).get("target")
        if target is None:
            # Last resort: infer from session route map if this is the active session
            target = _ps._SESSION_ROUTE_MAP.get(session_id, "local")
        reason = bd.get("route_reason", "") or r.get("pipeline", {}).get("smart_router", {}).get("reason", "") or ""
        stage = r.get("pipeline", {}).get("smart_router", {}).get("stage", "")
        if not stage:
            stage = r.get("pipeline", {}).get("lifecycle_stage", {}).get("stage", "unknown")
        disp = bd.get("dispatch_latency_ms")
        dur = r.get("duration_ms") or 0
        in_chars = r.get("input_chars") or 0
        out_chars = r.get("output_chars") or 0
        status = r.get("status", 200)
        # Track model used (route_cloud_model for cloud, MODEL_NAME for local)
        if target == "cloud":
            model_name = bd.get("route_cloud_model", "") or _ps.PROXY_CLOUD_MODEL
        else:
            model_name = _ps.MODEL_NAME
        if model_name:
            if not first_model:
                first_model = model_name
            models_seen.add(model_name)

        # Track client type from metrics (newer records) or fall back later
        ct = r.get("client_type", "")
        if ct:
            client_types[ct] = client_types.get(ct, 0) + 1

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

        request_count = r.get("pipeline", {}).get("lifecycle_stage", {}).get("request_count", idx)
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
            "request_count": request_count,
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

    # Determine session type (routing) and client type
    if cloud_count > 0 and local_count > 0:
        session_type = "mixed"
    elif cloud_count > 0:
        session_type = "cloud"
    else:
        session_type = "local"
    if client_types:
        client_type = max(client_types, key=client_types.get)
    else:
        client_type = _fallback_client_type_from_log(session_id)
    force_source = _ps._SESSION_ROUTE_FORCE_SOURCE.get(session_id, "")
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
        "force_source": force_source,
        "session_type": session_type,
        "client_type": client_type,
        "client_types": sorted(client_types.keys()) if client_types else [],
        "models": sorted(models_seen) if models_seen else [],
        "first_model": first_model,
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


def _fmt_ts(ts: str) -> str:
    """Format ISO timestamp as MM-DD HH:MM:SS for compact display."""
    if len(ts) < 19:
        return ts or "—"
    return ts[5:10] + " " + ts[11:19]


def _svg_line_chart(values, rows=None, width=800, height=120, color="#3498db", fill=True):
    """Render a simple SVG line chart with points.

    values is a list of numeric y values (x is evenly spaced).
    rows is an optional list of dicts for per-point tooltips.
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
        title = f"#{i + 1}: {_fmt_ms(v)}"
        if rows and i < len(rows):
            r = rows[i]
            ts = r.get("ts", "")[11:19] if r.get("ts") else ""
            title = f"#{i + 1} {ts}&#10;Duration: {_fmt_ms(v)}&#10;Target: {r.get('target', '')}&#10;Status: {r.get('status', '')}"
        circles += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{color}"><title>{title}</title></circle>'
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
    """Render input_chars line with local/cloud colored points and tooltips."""
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
        ts = r.get("ts", "")[11:19] if r.get("ts") else ""
        title = (
            f"#{i + 1} {ts}&#10;"
            f"Input chars: {r['input_chars']:,}&#10;"
            f"Target: {r.get('target', '')}&#10;"
            f"Reason: {r.get('reason', '')}&#10;"
            f"Duration: {_fmt_ms(r.get('duration_ms', 0))}&#10;"
            f"Status: {r.get('status', '')}"
        )
        circles += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"><title>{title}</title></circle>'
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
            ts_short = _fmt_ts(sw["ts"])
            swap_icon = "☁️" if sw["to"] == "cloud" else "🖥️"
            reason_text = sw["reason"].replace("_", " ").replace("(", " (").replace(">", " > ") if sw["reason"] else "—"
            switch_items += (
                f'<div style="font-size:0.85em;padding:5px 0;border-bottom:1px solid #2a2a4a">'
                f'<b>{ts_short}</b> #{sw["index"]}: {_target_badge(sw["from"])} {swap_icon} {_target_badge(sw["to"])}'
                f'<div style="color:#a0a0c0;font-size:0.9em;margin-top:2px;padding-left:12px">'
                f'⬅ 原因: <code style="color:#f0c674">{reason_text}</code></div></div>'
            )
        switch_html = (
            f'<div class="card"><h2>🔄 路由切换 ({len(switches)} 次)</h2>{switch_items}</div>'
        )
    else:
        switch_html = '<div class="card"><h2>🔄 路由切换</h2><div style="color:#888">无切换，全程同一目标</div></div>'

    # Timeline rows
    rows_html = ""
    for r in timeline:
        ts_short = _fmt_ts(r["ts"])
        dur_bar_width = min(100, max(1, r["duration_ms"] / max(data["p99_duration_ms"], 1) * 100))
        dur_bar = (
            f'<div style="width:80px;background:#2a2a4a;height:6px;border-radius:3px;overflow:hidden">'
            f'<div style="width:{dur_bar_width:.0f}%;background:#3498db;height:100%"></div></div>'
        )
        # Compute loop threshold for this point in the session (short/long/very_long tier)
        req_count = r.get("request_count", 1)
        if req_count <= _ps.PROXY_LOOP_SESSION_SHORT_BOUND:
            loop_threshold = _ps.PROXY_LOOP_THRESHOLD
        elif req_count <= _ps.PROXY_LOOP_SESSION_LONG_BOUND:
            loop_threshold = _ps.PROXY_LOOP_THRESHOLD_LONG
        else:
            loop_threshold = _ps.PROXY_LOOP_THRESHOLD_VERY_LONG
        flags = []
        if r["fallback"]:
            flags.append('<span style="color:#e74c3c">fallback</span>')
        if r["emergency"]:
            flags.append('<span style="color:#e74c3c">emergency</span>')
        if r["blocker"]:
            flags.append('<span style="color:#e67e22">blocker</span>')
        if r["loop_max_run"] >= loop_threshold:
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
    dur_svg = _svg_line_chart(dur_values, rows=timeline, color="#9b59b6")

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
  <div class="card"><h2>客户端</h2>
    <div class="row"><span>Type</span><span style="font-weight:bold">{data['client_type']}</span></div>
    <div class="row"><span>Route</span><span>{data['session_type']}</span></div>
    <div class="row"><span>Model</span><span style="font-size:0.85em">{data['first_model'] or '—'}</span></div>
    {'<div class="row"><span>Models</span><span style="font-size:0.8em;color:#888">' + ', '.join(data['models']) + '</span></div>' if len(data.get('models',[])) > 1 else ''}
  </div>
  <div class="card"><h2>路由分布</h2>
    <div class="row"><span>Local</span><span style="color:#27ae60;font-weight:bold">{data['local_count']}</span></div>
    <div class="row"><span>Cloud</span><span style="color:#3498db;font-weight:bold">{data['cloud_count']}</span></div>
    <div class="row"><span>Unknown</span><span style="color:#888">{data['unknown_count']}</span></div>
    <div class="row"><span>Errors</span><span style="color:#e74c3c;font-weight:bold">{data['error_count']}</span></div>
    {'<div class="row"><span>Route source</span><span style="color:#e67e22">{}</span></div>'.format(data['force_source']) if data.get('force_source') else ''}
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
        saved_dt = datetime.fromtimestamp(mtime)
        saved_at = saved_dt.strftime("%Y-%m-%d %H:%M:%S")
        saved_time = saved_dt.strftime("%H:%M:%S")
        age_seconds = (datetime.now() - saved_dt).total_seconds()
        stale = age_seconds > 300  # older than 5 minutes considered stale
        very_stale = age_seconds > 3600  # older than 1 hour
    except OSError:
        saved_at = saved_time = None
        stale = very_stale = False

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
    ts_html = f'<span class="evt-ts">{saved_time}</span> ' if saved_time else ''
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
        if very_stale:
            stale_badge = '<span style="color:#e74c3c;font-weight:bold;margin-left:6px">● 已过期 (>1h)</span>'
        elif stale:
            stale_badge = '<span style="color:#f39c12;font-weight:bold;margin-left:6px">● 非实时 (>5min)</span>'
        else:
            stale_badge = '<span style="color:#2ecc71;margin-left:6px">● 实时</span>'
        summary += f'<div class="row"><span class="label">Captured At</span><span class="value">{saved_at}{stale_badge}</span></div>'

    return summary + "\n".join(timeline), tools_detail, errors_detail
# --- _build_status_html ---
def _build_status_html():
    backend_info = _get_process_info("rapid-mlx|llama-server|dflash", "Backend")
    proxy_info = _get_process_info("anthropic_proxy.py", "Proxy", fallback_port=4000)
    mem = _get_system_memory()
    log = _get_log_stats()
    traffic = _get_traffic_stats()
    session_trace, tools_detail, errors_detail = _get_session_trace()
    recent_sessions = _load_recent_session_ids()
    if recent_sessions:
        rs_rows = "".join(
            f'<div class="row">'
            f'<span><a href="/session?sid={s["session_id"]}" title="{s["session_id"]}">'
            f'{s["session_id"][:16]}{"…" if len(s["session_id"]) > 16 else ""}</a>'
            f'<span style="color:#888;font-size:0.85em;margin-left:4px">{s["count"]} req'
            + (f' · <span style="font-size:0.8em">{s["client_type"]}</span>' if s.get("client_type") and s["client_type"] != "unknown" else '')
            + (f' · <span style="font-size:0.8em">{s["model"][:20]}</span>' if s.get("model") else '')
            + (f' · +{s["models_count"]-1} more' if s.get("models_count",0) > 1 else '')
            + f'</span></span>'
            f'<span style="color:#888">{_fmt_ts(s["last_ts"])}</span></div>'
            for s in recent_sessions
        )
        recent_sessions_card = f'<div class="card" style="grid-column: 1 / -1;"><h2>📁 Recent Sessions</h2>{rs_rows}</div>'
    else:
        recent_sessions_card = ""
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

    # --- Compression (TS-3) card ---
    comp_stats = _get_compression_stats()
    today_comp = comp_stats.get("today", {})
    last10m_comp = comp_stats.get("last_10m", {})
    strategy_counts = today_comp.get("strategy_counts", {})
    strategy_str = ", ".join(f"{k}={v}" for k, v in sorted(strategy_counts.items()))
    skipped_reasons = last10m_comp.get("skipped_reason_counts", {})
    skipped_str = ", ".join(f"{k}={v}" for k, v in sorted(skipped_reasons.items()))
    comp_card = f"""<div class="card">
    <h3>Compression (TS-3)</h3>
    <div class="row"><span class="label">Today / Strategies</span>
      <span class="value">{strategy_str if strategy_str else "—"}</span></div>
    <div class="row"><span class="label">Avg Ratio</span>
      <span class="value">{today_comp.get("avg_compression_ratio", 0.0):.2f}</span></div>
    <div class="row"><span class="label">Skipped Reasons (10m)</span>
      <span class="value">{skipped_str if skipped_str else "—"}</span></div>
    <div class="row"><span class="label">Avg Protected Pairs</span>
      <span class="value">{today_comp.get("protected_pair_avg", 0.0):.1f}</span></div>
    <div class="row"><span class="label">Truncated (today)</span>
      <span class="value">{today_comp.get("truncated_total", 0)}</span></div>
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
        ts_display = _fmt_ts(last_ts)
        ts_span = f'<span style="color:#888">  {ts_display}</span>' if last_ts else ""
        last_reason_html = (
            f'<div style="margin-top:6px;padding:6px 8px;border-left:3px solid {rc};'
            f'background:rgba(0,0,0,0.2);font-size:0.85em">'
            f'<b>Last Decision:</b> <span style="color:{rc}">{last_target}</span>'
            f' — <code>{route["last_route_reason"]}</code>'
            f'{ts_span}'
            f'</div>'
        )

    # Session tier distribution
    tiers_html = ""
    tier_counts = {"short": 0, "long": 0, "very_long": 0}
    for cnt in _ps._SESSION_REQUEST_COUNT.values():
        if cnt <= _ps.PROXY_LOOP_SESSION_SHORT_BOUND:
            tier_counts["short"] += 1
        elif cnt <= _ps.PROXY_LOOP_SESSION_LONG_BOUND:
            tier_counts["long"] += 1
        else:
            tier_counts["very_long"] += 1
    if any(tier_counts.values()):
        tier_badges = "".join(
            f'<span style="margin:0 2px;padding:1px 6px;border-radius:3px;font-size:0.85em;'
            f'background:{c};color:#fff">{k}: {v}</span>'
            for k, v, c in [
                ("short", tier_counts["short"], "#27ae60"),
                ("long", tier_counts["long"], "#f39c12"),
                ("very_long", tier_counts["very_long"], "#e74c3c"),
            ] if v > 0
        )
        tiers_html = f'<div class="row"><span class="label">Session Tiers</span><span class="value">{tier_badges}</span></div>'

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
    {tiers_html}
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
    if _ps.IS_CLOUD:
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
    <div class="row"><span class="label">Type</span><span class="value">{getattr(_ps, "PROXY_BACKEND_NAME", "unknown")} ({_ps.BACKEND_TYPE})</span></div>
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
  .chart-container {{ width: 100%; height: 200px; margin-top: 8px; }}
  .chart-row {{ display: flex; gap: 16px; flex-wrap: wrap; }}
  .chart-row .chart-box {{ flex: 1; min-width: 280px; }}
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

  {comp_card}

  <div class="card" style="grid-column: 1 / -1;">
    <h2>📈 Trends (24h)</h2>
    <div class="chart-row">
      <div class="chart-box"><canvas id="chartRequests"></canvas></div>
      <div class="chart-box"><canvas id="chartLatency"></canvas></div>
    </div>
    <div class="chart-row">
      <div class="chart-box"><canvas id="chartSuccess"></canvas></div>
      <div class="chart-box"><canvas id="chartQuality"></canvas></div>
    </div>
    <div class="chart-row">
      <div class="chart-box"><canvas id="chartChars"></canvas></div>
      <div class="chart-box"><canvas id="chartMemory"></canvas></div>
    </div>
  </div>

  <div class="card" style="grid-column: 1 / -1;">
    <h2>🚨 Alerts (last 10m)</h2>
    {alerts_html}
  </div>

  <div class="card" style="grid-column: 1 / -1;">
    <h2>Session Trace</h2>
    {session_trace}
  </div>

  {recent_sessions_card}

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

// --- Trend charts ---
function loadTrends() {{
  fetch('/metrics/history')
    .then(r => r.json())
    .then(data => {{
      var buckets = data.buckets || [];
      if (buckets.length < 2) return;
      var labels = buckets.map(b => b.ts.slice(5, 16));
      var colors = ['rgba(46,204,113,0.7)', 'rgba(231,76,60,0.7)', 'rgba(243,156,18,0.7)', 'rgba(52,152,219,0.7)'];
      var borderColors = ['rgba(46,204,113,1)', 'rgba(231,76,60,1)', 'rgba(243,156,18,1)', 'rgba(52,152,219,1)'];

      // Requests/hour
      new Chart(document.getElementById('chartRequests'), {{
        type: 'bar',
        data: {{
          labels: labels,
          datasets: [
            {{ label: '200 OK', data: buckets.map(b => b.status_200), backgroundColor: colors[0], borderColor: borderColors[0], borderWidth: 1 }},
            {{ label: '500', data: buckets.map(b => b.status_500), backgroundColor: colors[1], borderColor: borderColors[1], borderWidth: 1 }},
            {{ label: '503', data: buckets.map(b => b.status_503), backgroundColor: colors[2], borderColor: borderColors[2], borderWidth: 1 }},
          ]
        }},
        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ labels: {{ color: '#aaa', boxWidth: 12 }} }} }}, scales: {{ x: {{ ticks: {{ color: '#888', maxTicksLimit: 12 }} }}, y: {{ beginAtZero: true, ticks: {{ color: '#888' }} }} }} }}
      }});

      // Latency P50/P95
      new Chart(document.getElementById('chartLatency'), {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [
            {{ label: 'P50 (ms)', data: buckets.map(b => b.latency_p50_ms), borderColor: borderColors[0], backgroundColor: colors[0], fill: false, tension: 0.3 }},
            {{ label: 'P95 (ms)', data: buckets.map(b => b.latency_p95_ms), borderColor: borderColors[1], backgroundColor: colors[1], fill: false, tension: 0.3 }},
          ]
        }},
        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ labels: {{ color: '#aaa', boxWidth: 12 }} }} }}, scales: {{ x: {{ ticks: {{ color: '#888', maxTicksLimit: 12 }} }}, y: {{ beginAtZero: true, ticks: {{ color: '#888' }} }} }} }}
      }});

      // Success rate
      new Chart(document.getElementById('chartSuccess'), {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [{{ label: 'Success Rate (%)', data: buckets.map(b => b.success_rate), borderColor: borderColors[0], backgroundColor: colors[0], fill: true, tension: 0.3 }}]
        }},
        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ labels: {{ color: '#aaa', boxWidth: 12 }} }} }}, scales: {{ x: {{ ticks: {{ color: '#888', maxTicksLimit: 12 }} }}, y: {{ min: 50, max: 100, ticks: {{ color: '#888' }} }} }} }}
      }});

      // Quality flags
      new Chart(document.getElementById('chartQuality'), {{
        type: 'bar',
        data: {{
          labels: labels,
          datasets: [
            {{ label: 'Loop', data: buckets.map(b => b.loop_injected), backgroundColor: colors[1], borderColor: borderColors[1], borderWidth: 1 }},
            {{ label: 'Blocker', data: buckets.map(b => b.blocker_injected), backgroundColor: colors[2], borderColor: borderColors[2], borderWidth: 1 }},
            {{ label: 'High Drop', data: buckets.map(b => b.high_drop_ratio), backgroundColor: colors[3], borderColor: borderColors[3], borderWidth: 1 }},
          ]
        }},
        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ labels: {{ color: '#aaa', boxWidth: 12 }} }} }}, scales: {{ x: {{ ticks: {{ color: '#888', maxTicksLimit: 12 }} }}, y: {{ beginAtZero: true, ticks: {{ color: '#888' }} }} }} }}
      }});

      // Input/Output chars
      new Chart(document.getElementById('chartChars'), {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [
            {{ label: 'Avg Input (chars)', data: buckets.map(b => b.avg_input_chars), borderColor: borderColors[0], backgroundColor: colors[0], fill: false, tension: 0.3, yAxisID: 'y' }},
            {{ label: 'Avg Output (chars)', data: buckets.map(b => b.avg_output_chars), borderColor: borderColors[1], backgroundColor: colors[1], fill: false, tension: 0.3, yAxisID: 'y' }},
          ]
        }},
        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ labels: {{ color: '#aaa', boxWidth: 12 }} }} }}, scales: {{ x: {{ ticks: {{ color: '#888', maxTicksLimit: 12 }} }}, y: {{ beginAtZero: true, ticks: {{ color: '#888' }} }} }} }}
      }});

      // Memory rejection + truncation
      new Chart(document.getElementById('chartMemory'), {{
        type: 'bar',
        data: {{
          labels: labels,
          datasets: [
            {{ label: 'Truncation', data: buckets.map(b => b.truncation_triggered), backgroundColor: colors[2], borderColor: borderColors[2], borderWidth: 1 }},
            {{ label: 'Memory Reject', data: buckets.map(b => b.memory_rejected), backgroundColor: colors[1], borderColor: borderColors[1], borderWidth: 1 }},
          ]
        }},
        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ labels: {{ color: '#aaa', boxWidth: 12 }} }} }}, scales: {{ x: {{ ticks: {{ color: '#888', maxTicksLimit: 12 }} }}, y: {{ beginAtZero: true, ticks: {{ color: '#888' }} }} }} }}
      }});
    }})
    .catch(function(err) {{ console.error('Trend chart error:', err); }});
}}

</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script>
loadTrends();
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
    if loop.get("level", 0) >= 1:
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

# --- _build_session_metrics_json ---
def _build_session_metrics_json(session_key, records):
    """R16: GET /api/session/<key>/metrics 聚合体（设计 §4.4）。

    records 来自 diagnostics.read_session_metrics(sessions.jsonl 的 per-turn
    深度记录)。latency_by_kind 的 epoch/非 epoch 分档是上下文工程 Phase 1
    验收门禁 2（非 epoch 轮 P90 <15s / epoch 轮 <60s）的出数前提——is_epoch_turn
    为 null 的记录归 normal_turn 桶。
    """
    hit_ratios = [r.get("hit_ratio") for r in records
                  if isinstance(r.get("hit_ratio"), (int, float))]
    ttfts = [r.get("ttft_ms") for r in records
             if isinstance(r.get("ttft_ms"), (int, float))]
    durs_normal, durs_epoch = [], []
    injection_counts = {}
    for r in records:
        bucket = durs_epoch if r.get("is_epoch_turn") else durs_normal
        d = r.get("duration_ms")
        if isinstance(d, (int, float)) and d > 0:
            bucket.append(d)
        for k in r.get("feedback_injected") or []:
            injection_counts[k] = injection_counts.get(k, 0) + 1
    series = [{
        "turn": r.get("turn"),
        "hit_ratio": r.get("hit_ratio"),
        "ttft_ms": r.get("ttft_ms"),
        "duration_ms": r.get("duration_ms"),
        "prompt_processed_tokens": r.get("prompt_processed_tokens"),
        "is_epoch_turn": r.get("is_epoch_turn"),
        "feedback_injected": r.get("feedback_injected"),
        "canonical_mismatch": r.get("canonical_mismatch"),
    } for r in records[-200:]]
    return {
        "session_key": session_key,
        "turns": len(records),
        "hit_ratio_p50": round(_percentile(hit_ratios, 0.5), 4) if hit_ratios else None,
        "hit_ratio_p90": round(_percentile(hit_ratios, 0.9), 4) if hit_ratios else None,
        "ttft_p50_ms": round(_percentile(ttfts, 0.5), 1) if ttfts else None,
        "ttft_p90_ms": round(_percentile(ttfts, 0.9), 1) if ttfts else None,
        "latency_by_kind": {
            "normal_turn": {
                "count": len(durs_normal),
                "p90_ms": round(_percentile(durs_normal, 0.9), 1) if durs_normal else None,
            },
            "epoch_turn": {
                "count": len(durs_epoch),
                "p90_ms": round(_percentile(durs_epoch, 0.9), 1) if durs_epoch else None,
            },
        },
        "epoch_count": (records[-1].get("epoch_count") if records else None),
        "injection_counts": injection_counts,
        "canonical_mismatch_count": sum(1 for r in records if r.get("canonical_mismatch")),
        "series": series,
    }

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
    "_build_status_json",
    "_build_watchdog_json",
    "_build_queue_json",
    "_build_profiles_json",
    "_build_route_policies_json",
    "_current_active_profile",
    "_parse_conf_value",
    "_parse_memory_gb",
    "_load_session_metrics",
    "_load_recent_session_ids",
    "_fallback_client_type_from_log",
    "_analyze_session",
    "_build_session_html",
    "_finalize_metrics",
    "_mc_put",
    "_build_session_metrics_json",
]
