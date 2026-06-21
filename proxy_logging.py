"""Auto-extracted proxy_logging module."""
import os
import json
from datetime import datetime
import proxy_state as _ps

LOG_SCHEMA_VERSION = "v1"

# --- _next_jsonl_token ---
def _next_jsonl_token():
    """Generate a unique request token for correlating request log entries."""
    _ps._jsonl_counter += 1  # modify proxy_state directly
    return f"req_{_ps._jsonl_counter}_{os.urandom(4).hex()}"
# --- _ensure_jsonl_dir ---
def _ensure_jsonl_dir():
    """Create logs/ directory if it doesn't exist."""
    try:
        os.makedirs(_ps._LOG_DIR, exist_ok=True)
        os.chmod(_ps._LOG_DIR, 0o700)
    except OSError:
        pass
# --- log_request ---
def log_request(model: str, input_chars: int, output_chars: int,
                status: int, duration_ms: float, start_time: str = ""):
    """Append one JSON Lines record to proxy_requests.jsonl (thread-safe)."""
    _ensure_jsonl_dir()
    now_iso = datetime.now().isoformat()
    record = {
        "start_time": start_time or now_iso,
        "end_time": now_iso,
        "method": "POST",
        "path": "/v1/messages",
        "model": model,
        "input_chars": input_chars,
        "output_chars": output_chars,
        "status": status,
        "duration_ms": round(duration_ms, 1),
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with _ps._jsonl_lock:
            with open(_ps._JSONL_PATH, "a") as f:
                f.write(line)
    except OSError:
        pass
# --- log_metrics ---
def log_metrics(metrics: dict):
    _ensure_jsonl_dir()
    line = json.dumps(metrics, ensure_ascii=False) + "\n"
    try:
        with _ps._metrics_lock:
            with open(_ps._METRICS_PATH, "a") as f:
                f.write(line)
    except OSError:
        pass
# --- _mask_sensitive ---
def _mask_sensitive(headers_dict):
    if not isinstance(headers_dict, dict):
        return headers_dict
    masked = {}
    for k, v in headers_dict.items():
        kl = k.lower()
        if kl in ("authorization", "x-api-key") and isinstance(v, str):
            if len(v) > 12:
                masked[k] = v[:8] + "****" + v[-4:]
            else:
                masked[k] = v[:4] + "****"
        else:
            masked[k] = v
    return masked
# --- log ---
def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    sess = getattr(_ps._log_ctx, 'session_id', None)
    if sess:
        line = f"[{ts}] [{level}] [sess={sess}] {msg}"
    else:
        line = f"[{ts}] [{level}] {msg}"
    print(line)
    log_path = os.environ.get("PROXY_LOG_PATH", "/tmp/anthropic_proxy.log")
    try:
        with open(log_path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
# --- _log ---
def _log(msg, level="INFO"):
    log(msg, level)
# --- log_structured ---
def log_structured(event, **kwargs):
    ts = datetime.now().strftime("%H:%M:%S")
    sess = getattr(_ps._log_ctx, 'session_id', None)
    entry = {"schema": LOG_SCHEMA_VERSION, "ts": ts, "event": event}
    if sess:
        entry["session_id"] = sess
    entry.update(kwargs)
    line = json.dumps(entry, ensure_ascii=False)
    print(line)
    log_path = os.environ.get("PROXY_LOG_PATH", "/tmp/anthropic_proxy.log")
    try:
        with open(log_path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass

__all__ = [
    "_next_jsonl_token",
    "_ensure_jsonl_dir",
    "log_request",
    "log_metrics",
    "_mask_sensitive",
    "log",
    "log_structured",
]
