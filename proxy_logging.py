"""Auto-extracted proxy_logging module."""
import os
import json
import threading
from datetime import datetime
import proxy_state as _ps

LOG_SCHEMA_VERSION = "v1"

# --- A1: proxy_requests.jsonl 10MB 轮转(与 proxy_metrics.jsonl 同策略) ---
JSONL_ROTATE_BYTES = 10 * 1024 * 1024

# --- A2: 主日志(anthropic_proxy.log)按大小轮转 ---
# copytruncate 语义: 备份 = 复制原文件,原文件原地截断——shell 持有的 O_APPEND fd
# (manage.sh wrapper `exec >>`)与按路径 open("a") 并存时均安全,无双写分裂。
_LOG_ROTATE_BYTES = int(os.environ.get("PROXY_LOG_ROTATE_MB", "50")) * 1024 * 1024
_LOG_ROTATE_KEEP = int(os.environ.get("PROXY_LOG_ROTATE_KEEP", "3"))
_LOG_ROTATE_CHECK_INTERVAL = 128  # 每 N 次写入检查一次大小(避免每行 stat)
_log_rotate_lock = threading.Lock()
_log_write_counter = {"n": 0}

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
# --- _maybe_rotate_jsonl ---
def _maybe_rotate_jsonl(path, lock, rotate_bytes=None):
    """JSONL 10MB 轮转: 移至 <path>.1(旧 .1 删除),与 log_metrics 原策略一致。

    A1 起供 log_request/log_metrics 共用;rotate_bytes 参数供测试注入小阈值。
    注意: 调用方必须已持有 lock(不可重入,函数内不再获取)。
    """
    import shutil
    threshold = rotate_bytes if rotate_bytes is not None else JSONL_ROTATE_BYTES
    try:
        try:
            if os.path.getsize(path) < threshold:
                return
            bak = path + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            shutil.move(path, bak)
        except OSError:
            pass
    except OSError:
        pass
# --- _maybe_rotate_main_log ---
def _maybe_rotate_main_log(log_path):
    """A2: 主日志按大小 copytruncate 轮转(默认 50MB × 3 份备份)。

    每 _LOG_ROTATE_CHECK_INTERVAL 次写入选一次大小;超限则移链 .1->.2->.3
    (删最老)并把当前文件复制为 .1 后原地截断。append-mode fd 不受截断影响。
    """
    _log_write_counter["n"] += 1
    if _log_write_counter["n"] % _LOG_ROTATE_CHECK_INTERVAL != 0:
        return
    import shutil
    try:
        if os.path.getsize(log_path) < _LOG_ROTATE_BYTES:
            return
    except OSError:
        return
    with _log_rotate_lock:
        try:
            if os.path.getsize(log_path) < _LOG_ROTATE_BYTES:
                return  # double-check: 另一线程已轮转
            for i in range(_LOG_ROTATE_KEEP, 1, -1):
                src, dst = f"{log_path}.{i - 1}", f"{log_path}.{i}"
                if os.path.exists(src):
                    if os.path.exists(dst):
                        os.remove(dst)
                    shutil.move(src, dst)
            shutil.copyfile(log_path, f"{log_path}.1")
            with open(log_path, "r+b") as f:
                f.truncate(0)  # 原地截断: O_APPEND fd(含 wrapper 重定向)继续写
        except OSError:
            pass
# --- log_request ---
def log_request(model: str, input_chars: int, output_chars: int,
                status: int, duration_ms: float, start_time: str = "",
                session_id: str = "", request_id: str = ""):
    """Append one JSON Lines record to proxy_requests.jsonl (thread-safe).

    A1(2026-08-20): 补 session_id/request_id 关联字段(此前按会话归因全部断裂)
    + 10MB 轮转;两字段恒出现(未知为空串)以保持 schema 稳定。
    """
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
        "session_id": session_id,
        "request_id": request_id,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with _ps._jsonl_lock:
            _maybe_rotate_jsonl(_ps._JSONL_PATH, _ps._jsonl_lock)
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
            _maybe_rotate_jsonl(_ps._METRICS_PATH, _ps._metrics_lock)
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
        _maybe_rotate_main_log(log_path)
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
        _maybe_rotate_main_log(log_path)
        with open(log_path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass

__all__ = [
    "_next_jsonl_token",
    "_ensure_jsonl_dir",
    "_maybe_rotate_jsonl",
    "_maybe_rotate_main_log",
    "log_request",
    "log_metrics",
    "_mask_sensitive",
    "log",
    "log_structured",
]
