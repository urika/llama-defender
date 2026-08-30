#!/usr/bin/env python3
"""H_BE shadow 探针（belief-entropy shadow probe，2026-08-29）。

只测不动：成功的本地响应完成后，对同一会话上下文追加一次双探针锚定提问
（MMPO 式："当前任务进度？还缺什么信息？"），用 top_logprobs 截断分布估计
信念熵 H_BE，落盘 logs/diag/hbe.jsonl。用途：离线验证「熵曲线 vs 任务成败」
的区分度，为未来实时压缩决策控制提供信号标定数据（设计见 agent_go
docs/design/offline-policy-metrics-analysis.md §5）。

设计约束：
- 永不改变任何路由/截断/压缩决策；任何失败静默跳过（fail-open）。
- 仅本地路径（route_target=local/local_forced）触发——上下文不出本机。
- 搭 prefix cache 便车：探针紧随主请求、同引擎同前缀，增量成本为秒级。
- 绝不增加用户可感延迟：引擎并发锁等待有界（PROXY_HBE_LOCK_WAIT_S），
  拿不到就跳过；全局同时在飞探针 ≤1。
- 熵口径：H = -Σ p·log2(p)，p 取自 top_logprobs（默认 20）截断分布，
  实测质量覆盖 92-100%；跨轮比较只需口径一致的相对值。

配置（proxy_config.CONFIG_REGISTRY，均 reloadable，默认全关）：
    PROXY_HBE_ENABLED / MIN_CHARS / SAMPLE_EVERY / TOP_LOGPROBS /
    MAX_TOKENS / LOCK_WAIT_S / TIMEOUT_S
"""

import json
import math
import os
import threading
import time
import urllib.request
from datetime import datetime

import proxy_state as _ps
from proxy_logging import log

SCHEMA_VERSION = 2  # v2(2026-08-30): answer_preview 200→2048, +h_max_token_idx/
                    # answer_truncated/completion_budget；MAX_TOKENS 默认 48→160
                    # （48 时代 97.6% 答案被预算截断, "还缺什么"半句系统性丢失）

# 双探针锚定问题（MMPO §2(c)：把不可观测的信念不确定性转为可观测的响应不确定性）
ANCHOR_QUESTION = (
    "请用两句话如实回答（不要调用任何工具）："
    "(1) 当前任务的进度状态是什么？"
    "(2) 为了继续推进，还缺少哪些关键信息？"
)

_HBE_PATH = None  # 惰性解析（_ps._DIAG_DIR 在 import 时未必就绪）
_HBE_ROTATE_BYTES = 10 * 1024 * 1024  # 10MB 单备份轮转，仿 sessions.jsonl
_probe_in_flight = threading.Lock()   # 全局同时在飞探针 ≤1
_arm_logged = False                   # 每进程一次：hook 到达 + 门槛状态
_skip_log_counts = {}                 # 各门槛跳过原因限频日志（每原因前 3 次）
_SKIP_LOG_LIMIT = 3


def _log_skip(reason, detail=""):
    """门槛跳过的限频日志——shadow 模式的静默性不应以不可诊断为代价。"""
    n = _skip_log_counts.get(reason, 0)
    if n >= _SKIP_LOG_LIMIT:
        return
    _skip_log_counts[reason] = n + 1
    log(f"  -> [hbe] skip: {reason}{(' ' + detail) if detail else ''}")


def _hbe_path():
    global _HBE_PATH
    if _HBE_PATH is None:
        _HBE_PATH = os.path.join(_ps._DIAG_DIR, "hbe.jsonl")
    return _HBE_PATH


def _write_record(record):
    """append 一条记录到 hbe.jsonl（10MB 轮转）。写失败静默——同 archive 模式。"""
    try:
        path = _hbe_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _ps._diag_lock:
            if os.path.isfile(path) and os.path.getsize(path) > _HBE_ROTATE_BYTES:
                try:
                    os.replace(path, path + ".1")
                except OSError:
                    pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _truncated_entropy_bits(top_logprobs):
    """top-k 截断分布的香农熵（bits）与质量覆盖率。返回 (H, coverage)。"""
    ps = []
    for cand in top_logprobs or []:
        lp = cand.get("logprob")
        if isinstance(lp, (int, float)):
            ps.append(math.exp(lp))
    if not ps:
        return None, 0.0
    h = -sum(p * math.log2(p) for p in ps if p > 0)
    return h, sum(ps)


def maybe_schedule(ctx, base_url, api_key, lock):
    """成功本地响应后调用：满足采样条件则起 daemon 线程异步探针。

    所有条件判断同步完成（微秒级）；探针本体在后台线程，绝不阻塞响应路径。
    本函数自身也 fail-open——调用方无需 try。
    """
    try:
        global _arm_logged
        if not _arm_logged:
            _arm_logged = True
            log(f"  -> [hbe] shadow hook reached (enabled={getattr(_ps, 'PROXY_HBE_ENABLED', False)}, "
                f"min_chars={getattr(_ps, 'PROXY_HBE_MIN_CHARS', '?')}, "
                f"sample_every={getattr(_ps, 'PROXY_HBE_SAMPLE_EVERY', '?')})")
        if not getattr(_ps, "PROXY_HBE_ENABLED", False):
            return
        body = getattr(ctx, "openai_body", None)
        if not isinstance(body, dict):
            _log_skip("no_openai_body")
            return
        messages = body.get("messages") or []
        if not messages:
            _log_skip("no_messages")
            return
        # 采样门槛：payload 规模 + 会话轮次 cadence
        payload_chars = len(json.dumps(messages, ensure_ascii=False))
        if payload_chars < _ps.PROXY_HBE_MIN_CHARS:
            _log_skip("small_payload", f"chars={payload_chars}")
            return
        session_key = getattr(ctx, "session_id", "") or ""
        turn = _ps._SESSION_REQUEST_COUNT.get(session_key, 0) if session_key else 0
        every = max(1, _ps.PROXY_HBE_SAMPLE_EVERY)
        if not session_key or turn < 1 or turn % every != 0:
            _log_skip("cadence", f"session={bool(session_key)} turn={turn} every={every}")
            return
        if not _probe_in_flight.acquire(blocking=False):
            _log_skip("in_flight")
            return
        # 快照 payload（线程晚些执行，与管线后续可能的改写隔离）
        snapshot = json.loads(json.dumps(
            {"model": body.get("model") or _ps.MODEL_NAME, "messages": messages},
            ensure_ascii=False))
        request_id = getattr(_ps._diag_ctx, "request_id", "") or ""
        t = threading.Thread(
            target=_run_probe,
            args=(snapshot, base_url, api_key, lock, session_key, turn,
                  request_id, payload_chars,
                  getattr(ctx, "_route_local_model", "") or ""),
            daemon=True, name="hbe-shadow-probe")
        t.start()
    except Exception as e:  # fail-open：调度失败只留日志
        log(f"  -> [hbe] schedule failed ({e}) — skipped", level="WARN")
        try:
            if _probe_in_flight.locked():
                _probe_in_flight.release()
        except Exception:
            pass


def _run_probe(payload, base_url, api_key, lock, session_key, turn,
               request_id, payload_chars, local_model):
    """后台线程：有界等锁 → 直连后端探针调用 → 熵计算 → 落盘。"""
    record = {
        "schema_version": SCHEMA_VERSION,
        "event": "hbe_shadow",
        "ts": datetime.now().isoformat(),
        "session_key": session_key,
        "turn": turn,
        "request_id": request_id,
        "model": payload["model"],
        "local_model": local_model,
        "payload_chars": payload_chars,
        "top_logprobs": _ps.PROXY_HBE_TOP_LOGPROBS,
        # 回显生成预算：答案长度分布变化会使 H 均值基线漂移，记录自带预算值
        # 让「48-token 时代 / 160-token 时代」自描述，跨时代对比可校准
        "completion_budget": _ps.PROXY_HBE_MAX_TOKENS,
    }
    try:
        # 有界等锁：拿不到就跳过——shadow 探测绝不延迟用户请求
        if not lock.acquire(timeout=_ps.PROXY_HBE_LOCK_WAIT_S):
            record["result"] = "skipped_lock"
            _write_record(record)
            return
        try:
            probe_body = {
                "model": payload["model"],
                "messages": payload["messages"] + [
                    {"role": "user", "content": ANCHOR_QUESTION}],
                "temperature": 0.0,
                "max_tokens": _ps.PROXY_HBE_MAX_TOKENS,
                "logprobs": True,
                "top_logprobs": _ps.PROXY_HBE_TOP_LOGPROBS,
                "stream": False,
            }
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(probe_body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"},
                method="POST")
            t0 = time.monotonic()
            resp = urllib.request.urlopen(req, timeout=_ps.PROXY_HBE_TIMEOUT_S)
            data = json.loads(resp.read().decode("utf-8"))
            record["probe_latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        finally:
            lock.release()

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        record["answer_preview"] = (msg.get("content") or "")[:2048]
        record["finish_reason"] = choice.get("finish_reason")
        # 预算截断标记：length = 答案没说完（48 时代 97.6%），分析时须区分
        record["answer_truncated"] = choice.get("finish_reason") == "length"
        usage = data.get("usage") or {}
        record["prompt_tokens"] = usage.get("prompt_tokens")
        record["completion_tokens"] = usage.get("completion_tokens")

        tokens = ((choice.get("logprobs") or {}).get("content")) or []
        if not tokens:
            record["result"] = "no_logprobs"
            _write_record(record)
            return
        hs, covers = [], []
        for tok in tokens:
            h, cov = _truncated_entropy_bits(tok.get("top_logprobs"))
            if h is not None:
                hs.append(h)
                covers.append(cov)
        if not hs:
            record["result"] = "no_logprobs"
            _write_record(record)
            return
        h_max = max(hs)
        record.update({
            "result": "ok",
            "n_tokens": len(hs),
            "h_mean_bits": round(sum(hs) / len(hs), 4),
            "h_max_bits": round(h_max, 4),
            # 熵峰 token 位置——「哪一步开始犹豫」；多个同峰取首个
            "h_max_token_idx": hs.index(h_max),
            "coverage_mean": round(sum(covers) / len(covers), 4),
        })
        _write_record(record)
        log(f"  -> [hbe] shadow probe turn={turn} "
            f"H_mean={record['h_mean_bits']}bit coverage={record['coverage_mean']} "
            f"latency={record['probe_latency_ms']}ms")
    except Exception as e:  # fail-open：探针失败不影响任何主流程
        record["result"] = "error"
        record["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        _write_record(record)
        log(f"  -> [hbe] probe failed ({record['error']}) — skipped", level="WARN")
    finally:
        try:
            _probe_in_flight.release()
        except Exception:
            pass
