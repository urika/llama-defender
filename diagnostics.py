#!/usr/bin/env python3
"""diagnostics.py — R13/R16 diagnostics recorder (stdlib only).

设计依据: docs/02-architecture-design/diagnostics-dataplane-design-20260819.md
  - D1: 流式携带通道 = SSE 注释行尾注(`: x-proxy-diag {...}`),规范保证所有
        解析器忽略;HTTP 头仅用于非流式 + 流式早期已知字段(Request-Id/注入标记)。
  - D2: Prompt-Processed-N 只在后端返回 timings 时才有,否则缺省不发假值(P1)。
  - D6: Feedback-Injected = 本请求代理向 prompt 注入的全部合成内容 kind;
        覆盖现有 6+ 处注入点,Phase 2 后增 negative_feedback/recitation。
  - D8: sessions.jsonl per-turn 深度记录,与 proxy_metrics.jsonl 经 request_id 关联。

线程模型: 每请求累积态放 _ps._diag_ctx(thread-local);进程级 timings 探测
        为模块全局;jsonl 写入经 _ps._diag_lock。
"""
import json
import hashlib
import os
import threading
from datetime import datetime

import proxy_state as _ps

DIAG_SCHEMA_VERSION = 1
JSONL_ROTATE_BYTES = 10 * 1024 * 1024  # 与 proxy_metrics.jsonl 同策略

# R9 实验前置①: 配置指纹——数据自带产生条件。信息域关键参数快照入每轮
# 记录,治疗分派变成可查询(config.keep_messages==12)而非依赖外部记忆;
# conf_hash 相等 = 同一信息域(队列识别)。缺参记 None(未注册本身是信息)。
# PD 两项(2026-09-01): 微轮重派改变模型可见面, 属信息域参数——下次重启生效。
_FINGERPRINT_KEYS = (
    ("truncate_strategy", "PROXY_CTX_TRUNCATE_STRATEGY"),
    ("keep_messages", "PROXY_CTX_KEEP_MESSAGES"),
    ("keep_head", "PROXY_CTX_KEEP_HEAD"),
    ("compress_profile", "PROXY_COMPRESSION_PROFILE"),
    ("compress_enabled", "PROXY_COMPRESS_ENABLED"),
    ("compress_mode", "PROXY_COMPRESS_MODE"),
    ("ctx_engine_enabled", "PROXY_CTX_ENGINE_ENABLED"),
    ("clear_enabled", "PROXY_CLEAR_ENABLED"),
    ("pd_enabled", "PROXY_PD_ENABLED"),
    ("pd_micro_turn", "PROXY_PD_MICRO_TURN_ENABLED"),
    ("hbe_enabled", "PROXY_HBE_ENABLED"),
    ("hbe_sample_every", "PROXY_HBE_SAMPLE_EVERY"),
    ("hbe_min_chars", "PROXY_HBE_MIN_CHARS"),
    ("hbe_completion_budget", "PROXY_HBE_MAX_TOKENS"),
)


def config_fingerprint():
    """信息域关键参数快照 + 10 位 hash（fail-open，异常 → (None, None)）。"""
    try:
        snap = {name: getattr(_ps, attr, None) for name, attr in _FINGERPRINT_KEYS}
        raw = json.dumps(snap, sort_keys=True, ensure_ascii=False, default=str)
        return snap, hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
    except Exception:
        return None, None

# 进程级后端 timings 能力探测(None=未知,True/False=已探测,见 D2)
_timings_state = {"supported": None}
_timings_probe_lock = threading.Lock()

# 诊断层异常可见性: fail-open 不抛异常,但每挂点前 N 次异常记 WARN(防止静默故障)
_EXCEPTION_LOG_LIMIT = 3
_exception_log_counts = {}
_exception_log_lock = threading.Lock()


# ============================================================================
# 纯函数(单元测试友好)
# ============================================================================

def compute_hit_ratio(processed, sent):
    """hit_ratio = 1 − processed/sent(上游设计 §10.2①)。任一缺失/非法 → None。"""
    if not isinstance(processed, (int, float)) or not isinstance(sent, (int, float)):
        return None
    if sent <= 0 or processed < 0:
        return None
    if processed > sent:  # 后端语义异常(实测保护),钳到 0 命中
        return 0.0
    return round(1.0 - processed / sent, 4)


def sse_tail_line(diag):
    """R13 流式通道: 序列化为 SSE 注释行(含结尾空行)。"""
    payload = json.dumps(diag, ensure_ascii=False, separators=(",", ":"))
    return ": x-proxy-diag " + payload + "\n\n"


def diag_headers(request_id, injections, prompt_processed_n=None):
    """R13 非流式头集合(值未知的字段不出现——时序诚实原则 P1)。"""
    headers = {}
    if request_id:
        headers["X-Proxy-Diag-Request-Id"] = request_id
    if injections:
        headers["X-Proxy-Feedback-Injected"] = ",".join(injections)
    if isinstance(prompt_processed_n, int) and prompt_processed_n >= 0:
        headers["X-Proxy-Prompt-Processed-N"] = str(prompt_processed_n)
    return headers


# ============================================================================
# per-request 累积(thread-local)
# ============================================================================

def begin_request(request_id, session_key, key_source="unknown"):
    """初始化本请求的诊断累积态(do_POST 入口调用)。"""
    if not _ps.PROXY_DIAG_ENABLED:
        return
    _ps._diag_ctx.request_id = request_id
    _ps._diag_ctx.session_key = session_key
    _ps._diag_ctx.key_source = key_source
    _ps._diag_ctx.injections = []
    _ps._diag_ctx.route_target = None
    _ps._diag_ctx.actual_model = None
    _ps._diag_ctx.prompt_processed_tokens = None
    _ps._diag_ctx.prompt_sent_tokens = None
    _ps._diag_ctx.generation_tokens = None
    _ps._diag_ctx.prompt_eval_ms = None
    _ps._diag_ctx.gen_ms = None
    _ps._diag_ctx.canonical_mismatch = False
    _ps._diag_ctx.backend_timings_note = None
    _ps._diag_ctx.ifc_view = None  # R9.1: 本轮发送视图摘要(capture 时填充)
    try:
        import trace_context
        trace = trace_context.current()
        _ps._diag_ctx.trace_id = trace.get("trace_id") if trace else None
        _ps._diag_ctx.root_span_id = trace.get("root_span_id") if trace else None
    except Exception:
        _ps._diag_ctx.trace_id = None
        _ps._diag_ctx.root_span_id = None


def record_injection(kind):
    """登记一处代理合成内容注入(管线各注入点调用,设计 D6)。"""
    if not _ps.PROXY_DIAG_ENABLED or not kind:
        return
    injections = getattr(_ps._diag_ctx, "injections", None)
    if injections is None:
        injections = []
        _ps._diag_ctx.injections = injections
    if kind not in injections:
        injections.append(kind)


def peek_injections():
    """读取(不清除)本请求注入 kind 列表。"""
    return list(getattr(_ps._diag_ctx, "injections", None) or [])


def set_route(route_target, actual_model):
    """记录路由归因(dispatch 时调用,与 R8 头同源)。"""
    if not _ps.PROXY_DIAG_ENABLED:
        return
    _ps._diag_ctx.route_target = route_target
    _ps._diag_ctx.actual_model = actual_model


def mark_canonical_mismatch():
    """台账前缀失配时置位(上游设计 §4.7.4 监控项)。"""
    if _ps.PROXY_DIAG_ENABLED:
        _ps._diag_ctx.canonical_mismatch = True


def probe_timings(timings_obj):
    """后端 timings 能力探测: 首个含 timings 的响应置 supported=True(D2)。"""
    if not _ps.PROXY_DIAG_TIMINGS_SOURCE or _ps.PROXY_DIAG_TIMINGS_SOURCE == "off":
        return False
    if not isinstance(timings_obj, dict):
        return False
    with _timings_probe_lock:
        if _timings_state["supported"] is None:
            _timings_state["supported"] = True
    return True


def timings_supported():
    """当前探测结果: None=未知(尚未收到带 timings 的响应)。"""
    if _ps.PROXY_DIAG_TIMINGS_SOURCE == "off":
        return False
    return _timings_state["supported"]


def reset_timings_probe():
    """SIGHUP 热重载时重置 timings 能力探测(后端可能切换 local↔cloud)。

    不重新探测——下一次带/不带 timings 的响应自然翻转状态;此处仅清除
    陈旧的真值,避免跨后端切换后 timings_supported() 语义失真(评审 P2)。
    """
    with _timings_probe_lock:
        _timings_state["supported"] = None


def warn_suppressed(site, exc):
    """记录一处被吞掉的诊断层异常(每挂点前 _EXCEPTION_LOG_LIMIT 次 WARN)。

    diagnostics 故障不得影响请求路径(fail-open),但也不能完全静默——
    前 N 次出现即 WARN,便于定位;之后自动降噪。
    """
    if exc is None:
        return
    with _exception_log_lock:
        n = _exception_log_counts.get(site, 0)
        if n >= _EXCEPTION_LOG_LIMIT:
            return
        _exception_log_counts[site] = n + 1
    try:
        from proxy_logging import log
        log(f"[diag:{site}] suppressed {type(exc).__name__}: {exc}", level="WARN")
    except Exception:
        pass


def set_prompt_tokens(processed_n=None, sent_n=None, generation_n=None,
                      prompt_eval_ms=None, gen_ms=None):
    """流式/非流式响应路径回填 token 事实(值存在才写)。

    注意: 同一请求会多次调用(先 timings 后 usage),None 参数不得覆盖
    已写入的值——否则 usage 回填会把 timings 阶段的 gen_ms 清掉。
    """
    if not _ps.PROXY_DIAG_ENABLED:
        return
    ctx = _ps._diag_ctx
    if isinstance(processed_n, (int, float)) and processed_n >= 0:
        ctx.prompt_processed_tokens = int(processed_n)
        if isinstance(prompt_eval_ms, (int, float)):
            ctx.prompt_eval_ms = prompt_eval_ms
    if isinstance(sent_n, (int, float)) and sent_n >= 0:
        ctx.prompt_sent_tokens = int(sent_n)
    if isinstance(generation_n, (int, float)) and generation_n >= 0:
        ctx.generation_tokens = int(generation_n)
        if isinstance(gen_ms, (int, float)):
            ctx.gen_ms = gen_ms


def build_diag_payload():
    """构建 SSE 尾注/头/落盘共用的诊断载荷(缺失字段不出现)。

    G-B: 恒含 session_key——metering 归因落会话无需自行实现 8 字符截断；
    fallback key 场景(无头客户端)亦可自省代理侧实际归并的 key。
    """
    ctx = _ps._diag_ctx
    diag = {}
    request_id = getattr(ctx, "request_id", None)
    if request_id:
        diag["request_id"] = request_id
    trace_id = getattr(ctx, "trace_id", None)
    root_span_id = getattr(ctx, "root_span_id", None)
    if trace_id:
        diag["trace_id"] = trace_id
    if root_span_id:
        diag["root_span_id"] = root_span_id
    session_key = getattr(ctx, "session_key", None)
    if session_key:
        diag["session_key"] = session_key
    processed = getattr(ctx, "prompt_processed_tokens", None)
    sent = getattr(ctx, "prompt_sent_tokens", None)
    if processed is not None:
        diag["prompt_processed_n"] = processed
    if sent is not None:
        diag["prompt_sent_n"] = sent
    ratio = compute_hit_ratio(processed, sent)
    if ratio is not None:
        diag["hit_ratio"] = ratio
    injections = peek_injections()
    if injections:
        diag["feedback_injected"] = injections
    # R13 契约: X-Proxy-Epoch-Count——上下文工程 Phase 1(引擎)开启且非零时出现
    if getattr(_ps, "PROXY_CTX_ENGINE_ENABLED", False) and session_key:
        try:
            import context_engine
            epoch_count = context_engine.ENGINE.epoch_count(session_key)
            if epoch_count:
                diag["epoch_count"] = epoch_count
        except Exception:
            pass
    return diag


# ============================================================================
# R15 sent_view 捕获(管线 dispatch 前调用)
# ============================================================================

def capture_sent_view(ctx):
    """把实际发给后端的最终 payload 落入档案(design D7 sent_view)。

    在 BackendDispatcher 发请求前调用;payload 必须是完整 openai_body 拷贝
    (dispatch 路径可能改写 model 字段——调用点在改写之后即可)。
    """
    if not _ps.PROXY_DIAG_ENABLED:
        return
    # R9.1 IFC Tier-0: 视图摘要先于 archive 开关计算(差分基线不依赖档案)。
    # 格式双态: local 路径为 OpenAI 格式, cloud anthropic 协议路径为 Anthropic
    # 格式——ifc_metrics.unit_anchors 两种均覆盖。
    openai_body = getattr(ctx, "openai_body", None)
    if getattr(_ps, "PROXY_IFC_ENABLED", True) and isinstance(openai_body, dict):
        try:
            import ifc_metrics
            _ps._diag_ctx.ifc_view = ifc_metrics.view_summary(
                openai_body.get("messages") or [])
        except Exception as _e:
            warn_suppressed("ifc_view", _e)
    if not _ps.PROXY_DIAG_ARCHIVE_ENABLED:
        return
    session_key = getattr(ctx, "session_id", "") or getattr(_ps._diag_ctx, "session_key", "")
    if not session_key:
        return
    if not isinstance(openai_body, dict):
        return
    turn = _ps._SESSION_REQUEST_COUNT.get(session_key, 0) or 1
    import session_ledger
    session_ledger.ARCHIVE.append_turn(
        session_key, turn,
        payload=openai_body,
        injections=peek_injections(),
        meta={
            "model": openai_body.get("model"),
            "route_target": getattr(ctx, "_route_target", None),
            "messages": len(openai_body.get("messages", []) or []),
        },
    )


# ============================================================================
# R16 jsonl 落盘 + R7 lifecycle events
# ============================================================================

def _ensure_diag_dir():
    try:
        os.makedirs(_ps._DIAG_DIR, exist_ok=True)
        os.chmod(_ps._DIAG_DIR, 0o700)
    except OSError:
        pass


def log_session_diag(record):
    """append 一条 per-turn 记录到 sessions.jsonl(10MB 轮转,仿 log_metrics)。"""
    _ensure_diag_dir()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with _ps._diag_lock:
            try:
                if os.path.getsize(_ps._DIAG_SESSIONS_PATH) > JSONL_ROTATE_BYTES:
                    import shutil
                    base = _ps._DIAG_SESSIONS_PATH
                    bak = base + ".1"
                    if os.path.exists(bak):
                        os.remove(bak)
                    shutil.move(base, bak)
            except OSError:
                pass
            with open(_ps._DIAG_SESSIONS_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


def log_lifecycle_event(event, **detail):
    """激活 lifecycle_events.jsonl 死路径(集成契约 R7 顺带兑现)。"""
    entry = {"ts": datetime.now().isoformat(), "event": event}
    entry.update(detail)
    try:
        os.makedirs(os.path.dirname(_ps._LIFECYCLE_EVENTS_PATH), exist_ok=True)
        with _ps._diag_lock:
            with open(_ps._LIFECYCLE_EVENTS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def finalize_request(mc):
    """请求成功完成时落盘 R16 记录(与 log_metrics 同点调用)。

    mc: 现有 metrics dict(取 session_id/ttft_ms/duration_ms/status);诊断
        专属字段来自 _diag_ctx。返回记录 dict(测试用),未启用返回 None。
    """
    if not _ps.PROXY_DIAG_ENABLED:
        return None
    ctx = _ps._diag_ctx
    session_key = getattr(ctx, "session_key", None) or (mc or {}).get("session_id") or ""
    request_id = getattr(ctx, "request_id", None)
    if not session_key or not request_id:
        return None
    turn = _ps._SESSION_REQUEST_COUNT.get(session_key, 0)
    processed = getattr(ctx, "prompt_processed_tokens", None)
    sent = getattr(ctx, "prompt_sent_tokens", None)
    record = {
        "schema_version": DIAG_SCHEMA_VERSION,
        "ts": datetime.now().isoformat(),
        "request_id": request_id,
        "trace_id": getattr(ctx, "trace_id", None),
        "root_span_id": getattr(ctx, "root_span_id", None),
        "session_key": session_key,
        "key_source": getattr(ctx, "key_source", "unknown"),
        "turn": turn,
        "route_target": getattr(ctx, "route_target", None),
        "actual_model": getattr(ctx, "actual_model", None),
        "backend": {
            "type": _ps.BACKEND_TYPE or ("cloud" if _ps.IS_CLOUD else "local"),
            "name": _ps.PROXY_BACKEND_NAME,
            "timings_supported": timings_supported(),
        },
        "prompt_sent_tokens": sent,
        "prompt_processed_tokens": processed,
        "hit_ratio": compute_hit_ratio(processed, sent),
        "generation_tokens": getattr(ctx, "generation_tokens", None),
        "ttft_ms": (mc or {}).get("ttft_ms"),
        "duration_ms": (mc or {}).get("duration_ms"),
        "prompt_eval_ms": getattr(ctx, "prompt_eval_ms", None),
        "gen_ms": getattr(ctx, "gen_ms", None),
        # Phase 1(上下文工程)落地前为 null——字段名先定死,消费方可先行编码
        "epoch_count": None,
        "epoch_triggered": None,
        "is_epoch_turn": None,
        "feedback_injected": peek_injections(),
        "prefix_ratio": ((mc or {}).get("pipeline", {})
                         .get("common_prefix_ratio", {}) or {}).get("ratio"),
        "canonical_mismatch": bool(getattr(ctx, "canonical_mismatch", False)),
        "compression": {
            "mode": ((mc or {}).get("pipeline", {}).get("truncate", {}) or {}).get("strategy"),
            "ratio": (mc or {}).get("compression_ratio"),
        },
        "trace": None,
        # ① 配置指纹: 数据自带产生条件(队列识别/实验归因)
        "config": None,
        "conf_hash": None,
    }
    snap, chash = config_fingerprint()
    if snap:
        record["config"] = snap
        record["conf_hash"] = chash
    # R8 上下文工程引擎开启时回填 epoch 事实(与 X-Proxy-Epoch-Count 头同源)
    if getattr(_ps, "PROXY_CTX_ENGINE_ENABLED", False) and session_key:
        try:
            import context_engine
            record["epoch_count"] = context_engine.ENGINE.epoch_count(session_key)
            record["is_epoch_turn"] = context_engine.ENGINE.is_epoch_turn(
                session_key, record["turn"])
            record["epoch_triggered"] = record["is_epoch_turn"]
        except Exception:
            pass
    # R9.1 IFC Tier-0: 视图差分 + 台账动作 → per-turn ifc 段(只写不读执行器,
    # 数据架构 §3.3 L2 层)。首见会话/基线缺失时 retention 为 null(口径诚实)。
    if getattr(_ps, "PROXY_IFC_ENABLED", True):
        try:
            import ifc_metrics
            import memory_stores
            import session_ledger
            prev = ifc_metrics.BASELINE.get(session_key)
            cur = getattr(ctx, "ifc_view", None)
            actions = session_ledger.LEDGER.snapshot_actions(session_key, 40)
            record["ifc"] = ifc_metrics.build_ifc_section(
                prev, cur, actions,
                manifest_lines=memory_stores.MANIFEST.count(session_key))
            ifc_metrics.BASELINE.update(session_key, cur)
        except Exception as _e:
            warn_suppressed("ifc_finalize", _e)
    try:
        import trace_context
        record["trace"] = trace_context.finish_request(
            status="ok" if (mc or {}).get("status", 200) == 200 else "error")
    except Exception as _e:
        warn_suppressed("trace_finalize", _e)
    log_session_diag(record)
    if record["canonical_mismatch"]:
        log_lifecycle_event("canonical_mismatch",
                            session_key=session_key, turn=turn, request_id=request_id)
    return record


def read_session_metrics(session_key, max_lines=50000):
    """从 sessions.jsonl 读取某会话的 per-turn 记录(R16 聚合端点数据源)。

    jsonl 是 source of truth(重启后仍可查);倒序读取提升长文件效率。
    """
    records = []
    try:
        with open(_ps._DIAG_SESSIONS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return records
    for line in reversed(lines[-max_lines:]):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("session_key") == session_key:
            records.append(rec)
    records.reverse()
    return records


def read_session_hbe(session_key, max_lines=50000):
    """从 hbe.jsonl 读取某会话的 H_BE shadow 探针记录（R17 端点数据源）。

    与 read_session_metrics 同模式：jsonl 是 source of truth，倒序读取，
    坏行跳过。文件不存在（探针未启用时代）→ 空列表，由端点层决定 404 语义。
    """
    records = []
    path = os.path.join(_ps._DIAG_DIR, "hbe.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return records
    for line in reversed(lines[-max_lines:]):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("session_key") == session_key:
            records.append(rec)
    records.reverse()
    return records


def build_session_signals(session_key, records=None, hbe_records=None):
    """R17: 聚合某会话最新信号为 SignalSnapshot（契约 v1，集成契约 §3.3 冻结版）。

    records/hbe_records 可注入（单测/调用方已持有数据时免重读）；缺省内部
    读取。字段口径（全 Optional fail-open，信号缺失 → null）：
    - IFC 五指标取 sessions.jsonl 末轮 `ifc` 段；无记录时 reread_pressure 按
      契约默认 0、ile False、ile_kinds []，其余 null。
    - h_be = 末条成功探针 h_mean_bits；h_be_trend = 末两条成功探针之差
      （bits/turn，负值=熵降）；成功探针不足两条 → null。
    - d_ledger 恒 null：对账仅探针轮产生、未入 per-turn 记录（口径诚实）。
    - cognitive_load 契约默认 0.0（预留位，非测量值）。
    """
    if records is None:
        records = read_session_metrics(session_key)
    if hbe_records is None:
        hbe_records = read_session_hbe(session_key)
    last = records[-1] if records else {}
    ifc = last.get("ifc") or {}
    ok_hbe = [r for r in hbe_records
              if r.get("result") == "ok"
              and isinstance(r.get("h_mean_bits"), (int, float))]
    h_be = ok_hbe[-1].get("h_mean_bits") if ok_hbe else None
    trend = None
    if len(ok_hbe) >= 2:
        trend = round(ok_hbe[-1]["h_mean_bits"] - ok_hbe[-2]["h_mean_bits"], 4)
    import signal_types
    return signal_types.build_signal_snapshot(
        h_be=h_be,
        h_be_trend=trend,
        d_ledger=None,
        retention=ifc.get("retention"),
        rationale_ratio=ifc.get("rationale_ratio"),
        ile=bool(ifc.get("ile", False)),
        ile_kinds=list(ifc.get("ile_kinds") or []),
        view_reset=bool(ifc.get("view_reset", False)),
        reread_pressure=int(ifc.get("reread_pressure") or 0),
        action_diversity=ifc.get("action_div"),
        cognitive_load=0.0,
        config_fingerprint=str(last.get("conf_hash") or ""),
        session_key=session_key,
        turn=int(last.get("turn") or 0),
    )


__all__ = [
    "DIAG_SCHEMA_VERSION",
    "compute_hit_ratio", "sse_tail_line", "diag_headers",
    "begin_request", "record_injection", "peek_injections", "set_route",
    "mark_canonical_mismatch", "probe_timings", "timings_supported",
    "reset_timings_probe", "warn_suppressed",
    "set_prompt_tokens", "build_diag_payload", "capture_sent_view",
    "log_session_diag", "log_lifecycle_event", "finalize_request",
    "read_session_metrics", "read_session_hbe", "build_session_signals",
]
