"""request_archive.py — S-17/BR-4 R 期录制面（ADR-014 组件①）。

逐请求档案 append-only 落盘（schema=`s17-archive/v1.2`，契约=
swe-eval `docs/s17-recording-surface-design-v0-20261005.md` §六；
消费方=`scripts/mirror_replay.py` 的校验/重建规则——本模块按其
fail-loud 不变量反推实现，任何字段/turn 序偏差都会让镜像重放拒收）。

**v1.2（2026-10-16，BR-4 tools 缺口闭合）**：snapshot 行新增可选
`tools`/`tool_choice` 字段（R 视图=客户端原 body 的工具定义面）。此前
v1/v1.1 档案不含 tools ⇒ mirror 重放请求无工具定义 ⇒ tool_use=0、行动
选择退化为叙述（云端切臂重放 12/12 实证）。兼容性：legacy 行（schema
`s17-archive/v1`，无 tools）对消费端合法（fail-soft 降级＋逐轮注记）；
writer 侧 tools 面任一变化（含从无到有）强制落 snapshot——delta 不携带
工具面，静默沿用旧工具集会造出假重放基线。默认关语义不变
（PROXY_REQUEST_ARCHIVE_ENABLED=false，生效=下次代理重启窗）。

红线（ADR-008 防滑坡）: 档案=被动文件——本模块只在挂点被旁路调用，
不改管线行为、不做运行时注入、无后台线程；写失败仅 WARN 不进请求
关键路径（fail-open，对齐 diagnostics.py 模式）。默认关
（PROXY_REQUEST_ARCHIVE_ENABLED=false，L-62 同款批界安全模式）。

分片（偏离设计件 §6.1 单分片命名的实作决定，理由）: mirror 的
validate_session 不分 view 追踪 turn 序——双 view 同轮 delta 交错在
同一文件里会被误判 "delta turn 断裂"。故按 view 分片:
  <DIR>/<session_key>.R.jsonl   — R  视图（进入管线的客户端原 body，
                                  改写前模型无关公平口径，裁决口径）
  <DIR>/<session_key>.Rp.jsonl  — R' 视图（代理改写定稿后 ctx.body，
                                  敏感性口径；Anthropic 格式可重放）
response 行双分片各写一份（mirror --view 过滤后各自有并排 diff 基线）。
mirror 用法: `--archive <DIR>/<key>.R.jsonl`（单文件分片即单会话）。

挂点（全部旁路，不改 log_request 本体/管线主链逻辑）:
  1. Handler._handle_messages 入口 → begin_request（R 视图暂存内存；
     每客户端请求恰一次——_handle_messages 全仓唯一调用点在 do_POST，
     微轮续流/H_BE 探针/admin 注入均不经此处）
  2. diagnostics.capture_sent_view 头部 → record_turn（local/cloud-openai
     路径；PROXY_DIAG_ENABLED 门内，录制面随诊断面启用）
  3. pipeline.py anthropic-protocol 云端 sent_view 块（zhipu 路径不走
     capture_sent_view，须独立旁路）→ record_turn
  4. Handler._handle_non_streaming_response 响应完形处 → record_response
  5. Handler do_POST 收尾（成功/异常两分支）→ finalize_request
     （流式轮落骨架 response 行 truncated:true——v1 不 materialize
     流式聚合体，不碰流式中继；基线缺失时 mirror 降级为 remote-only）

轮号与增量: 进程内 per-session 计数，双 view 同轮对齐；接管已有分片
（重启）时扫文件 max_turn 续号，且续写首行必为 snapshot（增量依赖的
内存前缀态随重启丢失）。前缀失配（客户端改写历史/epoch 折叠收缩）→
整轮 snapshot 兜底——mirror 重建（最近 snapshot + 依序 delta）对中途
snapshot 天然成立。meta/epoch_fold 标注行 v1 不落（折叠已表现为 R'
前缀失配 snapshot，标注无重建语义）。

驱逐: PROXY_REQUEST_ARCHIVE_MAX_MB 独立配额（与 R15 archive 不同族目录
logs/diag/archive-req/）；L-18 教训——mtime 在 PROXY_DIAG_ACTIVE_PROTECT_MIN
窗口内的分片不驱逐，全受保护时接受溢出不删证据。
"""

import json
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime

import proxy_state as _ps
from unit_model import msg_hash

SCHEMA = "s17-archive/v1.2"   # v1.2: snapshot 行可带 tools/tool_choice（legacy v1 行无此字段=合法）
VIEW_R = "R"
VIEW_RP = "R'"          # R-prime；分片文件名用 Rp（引号不进文件名）
_SHARD_SUFFIX = {VIEW_R: ".R.jsonl", VIEW_RP: ".Rp.jsonl"}

# 入口暂存上界（并发在途 ≤ PROXY_MAX_CONCURRENT，64 已冗余；溢出丢最旧
# = 该请求不进档案，fail-open 不阻塞）
MAX_PENDING = 64
# 会话态 LRU 上界（超限逐出最旧会话 → 其后一轮冷启动 snapshot 重建）
MAX_SESSIONS = 128
_QUOTA_CHECK_INTERVAL_S = 60.0

_pending_lock = threading.Lock()
_pending = OrderedDict()        # request_id → {body, stream, ts}
_recorded = OrderedDict()       # request_id → {session, turn, stream, responded}
_sessions = OrderedDict()       # session_key → {turn, views: {view: {turn, hashes}}}
# 初值=已到期(本机 monotonic 为进程相对时钟, 0 不是"很久以前")
_quota_checked_at = time.monotonic() - _QUOTA_CHECK_INTERVAL_S


# ---------------------------------------------------------------------------
# 落盘原语
# ---------------------------------------------------------------------------

def _archive_dir():
    d = getattr(_ps, "PROXY_REQUEST_ARCHIVE_DIR", "") or os.path.join(
        _ps._SCRIPT_DIR, "logs", "diag", "archive-req")
    if not os.path.isabs(d):
        d = os.path.join(_ps._SCRIPT_DIR, d)
    return d


def _safe_name(session_key):
    return str(session_key).replace(os.sep, "_").replace("/", "_").replace("\x00", "")[:80]


def _shard_path(session_key, view):
    return os.path.join(_archive_dir(), _safe_name(session_key) + _SHARD_SUFFIX[view])


def _append_line(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.chmod(os.path.dirname(path), 0o700)
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _base_row(request_id, session_key, view, kind, turn):
    return {
        "schema": SCHEMA,
        "request_id": request_id,
        "session_key": session_key,
        "ts": _now_iso(),
        "view": view,
        "kind": kind,
        "turn": turn,
    }


def _scan_max_turn(path):
    """接管已有分片: 扫 snapshot/delta 行的最大 turn（validator 的 max_turn 口径）。"""
    max_turn = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # 半行（进程中断残留）不阻断接管
                if rec.get("kind") in ("snapshot", "delta"):
                    try:
                        max_turn = max(max_turn, int(rec.get("turn") or 0))
                    except (TypeError, ValueError):
                        pass
    except OSError:
        pass
    return max_turn


# ---------------------------------------------------------------------------
# 配额（独立核算 + L-18 活跃保护）
# ---------------------------------------------------------------------------

def _enforce_quota():
    global _quota_checked_at
    now = time.monotonic()
    if now - _quota_checked_at < _QUOTA_CHECK_INTERVAL_S:
        return
    _quota_checked_at = now
    cap_mb = int(getattr(_ps, "PROXY_REQUEST_ARCHIVE_MAX_MB", 2048) or 0)
    if cap_mb <= 0:
        return
    d = _archive_dir()
    try:
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        return
    # 以会话为驱逐单位（.R/.Rp 成对）——拆散会让存活分片在重启接管后
    # 产出"首 snapshot 非 turn=1"的 mirror 拒收档案
    groups = {}  # session_stem → {path, size, mtime}
    for n in names:
        stem, suffix = None, None
        for v, sfx in _SHARD_SUFFIX.items():
            if n.endswith(sfx):
                stem, suffix = n[: -len(sfx)], sfx
                break
        if stem is None:
            continue
        p = os.path.join(d, n)
        try:
            st_ = os.stat(p)
        except OSError:
            continue
        g = groups.setdefault(stem, {"paths": [], "size": 0, "mtime": st_.st_mtime})
        g["paths"].append(p)
        g["size"] += st_.st_size
        g["mtime"] = min(g["mtime"], st_.st_mtime)
    total = sum(g["size"] for g in groups.values())
    if total <= cap_mb * 1024 * 1024:
        return
    protect_s = int(getattr(_ps, "PROXY_DIAG_ACTIVE_PROTECT_MIN", 360) or 0) * 60
    oldest_first = sorted(groups.items(), key=lambda kv: kv[1]["mtime"])
    for stem, g in oldest_first:
        if total <= cap_mb * 1024 * 1024:
            break
        if protect_s and (time.time() - g["mtime"]) < protect_s:
            continue  # L-18: 活跃保护窗内不驱逐
        removed = 0
        try:
            for p in g["paths"]:
                os.remove(p)
        except OSError:
            continue
        total -= g["size"]
        _archive_warn(f"quota evicted session {stem} "
                      f"({cap_mb}MB cap, active-protect {protect_s // 60}min)")
    # 全受保护仍超限: 接受溢出（不删证据）——对齐 R15 驱逐器语义


def _archive_warn(msg):
    try:
        from proxy_logging import log  # deferred: proxy_logging 顶层 import proxy_state
        log(f"[request_archive] {msg}", level="WARN")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 增量计算
# ---------------------------------------------------------------------------

def _view_fields(body):
    """(model, system, messages, tools, tool_choice)——v1.2 快照载荷；
    body 非法返回 None。tools 非 list 或为空 → None（未带工具面，合法）；
    tool_choice 原样透传（客户端未带为 None，不落行）。"""
    if not isinstance(body, dict):
        return None
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return None
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        tools = None
    return body.get("model"), body.get("system"), msgs, tools, body.get("tool_choice")


def _tools_fp(tools, tool_choice):
    """工具面指纹——v1.2 delta 判定的附加等值条件（tools 变化 → 强制 snapshot）。

    delta 行不携带 tools：若消息是严格后缀扩展而工具集已变，落 delta 会让
    重建静默沿用旧工具面（假基线）。指纹经 msg_hash（sort_keys＋cache_control
    归一化）——key 顺序不稳定不误判。无工具面（None）与"上轮也无"等值，
    两轮都无工具时 delta 语义不变。"""
    if not tools:
        return None
    return msg_hash({"tools": tools, "tool_choice": tool_choice})


def _delta_or_none(prev_hashes, msgs):
    """新消息数组相对旧视图的增量; 非严格后缀扩展（改写/收缩/重排）返回 None。"""
    if len(msgs) < len(prev_hashes):
        return None
    for i, h in enumerate(prev_hashes):
        if msg_hash(msgs[i]) != h:
            return None
    return msgs[len(prev_hashes):]


# ---------------------------------------------------------------------------
# 挂点 API（全部 fail-open，由调用侧 try/except 或本侧兜底）
# ---------------------------------------------------------------------------

def begin_request(request_id, body, is_stream=False):
    """挂点 1: Handler._handle_messages 入口——暂存 R 视图（改写前原 body）。

    每客户端请求恰一次; 内部重派（微轮续流等）不经 _handle_messages 入口,
    天然不计。暂存溢出丢最旧（该请求不进档案）。
    """
    if not getattr(_ps, "PROXY_REQUEST_ARCHIVE_ENABLED", False):
        return
    if not request_id or not isinstance(body, dict):
        return
    with _pending_lock:
        _pending[request_id] = {
            "body": body,
            "stream": bool(is_stream),
            "ts": time.monotonic(),
        }
        while len(_pending) > MAX_PENDING:
            _pending.popitem(last=False)


def record_turn(request_id, session_key, rp_view):
    """挂点 2/3: sent_view 捕获处——R/R' 双视图落 snapshot 或 delta 行。

    同 request_id 只记首派（微轮重派/同请求二次 dispatch 到此即被跳过）;
    request_id 不在暂存（内部路径/暂存被逐）→ 整轮跳过（fail-open,
    不得为补一行而虚构轮号——turn 断裂会让 mirror 拒收整个分片）。
    """
    if not getattr(_ps, "PROXY_REQUEST_ARCHIVE_ENABLED", False):
        return
    if not request_id or not session_key:
        return
    with _pending_lock:
        entry = _pending.pop(request_id, None)
        if entry is None:
            return
        if request_id in _recorded:  # 首派已记（双挂点/重派兜底）
            return
        st = _sessions.get(session_key)
        if st is None:
            # 冷启动/接管: 扫双分片续号（本轮 client turn = 扫描 max + 1）
            r_turn = _scan_max_turn(_shard_path(session_key, VIEW_R))
            rp_turn = _scan_max_turn(_shard_path(session_key, VIEW_RP))
            st = {"turn": max(r_turn, rp_turn),
                  "views": {VIEW_R: {"turn": r_turn, "hashes": None, "tools_fp": None},
                            VIEW_RP: {"turn": rp_turn, "hashes": None, "tools_fp": None}}}
            while len(_sessions) >= MAX_SESSIONS:
                _sessions.popitem(last=False)
        st["turn"] += 1
        turn = st["turn"]
        _sessions[session_key] = st
        _recorded[request_id] = {
            "session": session_key, "turn": turn,
            "stream": entry.get("stream", False), "responded": False,
        }
        while len(_recorded) > MAX_PENDING:
            _recorded.popitem(last=False)
        # 视图行写盘同锁完成: 同会话并发轮的 turn/前缀态变更必须串行,
        # 否则重复 turn 行会让 mirror 校验器拒收分片
        _enforce_quota()
        for view, body in ((VIEW_R, entry.get("body")), (VIEW_RP, rp_view)):
            try:
                _write_view_row(session_key, view, turn, request_id, body, st)
            except Exception as e:
                _archive_warn(f"write {view} turn {turn} failed: {e}")


def _write_view_row(session_key, view, turn, request_id, body, st):
    fields = _view_fields(body)
    vst = st["views"][view]
    path = _shard_path(session_key, view)
    if fields is None:
        if vst["turn"] == turn:  # 该视图本轮已写过（双挂点兜底）
            return
        raise ValueError("view body not a dict with messages")
    model, system, msgs, tools, tool_choice = fields
    hashes = [msg_hash(m) for m in msgs]
    tools_fp = _tools_fp(tools, tool_choice)
    delta = None
    if (vst["hashes"] is not None and vst["turn"] == turn - 1
            and tools_fp == vst.get("tools_fp")):
        # v1.2: 工具面指纹不一致（含从无到有）即放弃 delta——tools 只在
        # snapshot 行上，落 delta 会静默沿用旧工具集
        delta = _delta_or_none(vst["hashes"], msgs)
    if delta is not None and len(delta) == 0:
        # 视图与上轮完全一致（同轮重写场景）——delta 空数组会让重建出
        # 错序 payload, 落 snapshot 保持基线明确
        delta = None
    if delta is not None:
        row = _base_row(request_id, session_key, view, "delta", turn)
        row["messages_delta"] = delta
    else:
        row = _base_row(request_id, session_key, view, "snapshot", turn)
        row["model"] = model
        row["system"] = system
        row["messages"] = msgs
        if tools:
            row["tools"] = tools
        if tool_choice is not None:
            row["tool_choice"] = tool_choice
    _append_line(path, row)
    vst["turn"] = turn
    vst["hashes"] = hashes
    vst["tools_fp"] = tools_fp


def build_stream_body(model, total_text, tool_calls_buffer, stop_reason,
                      input_tokens=0, output_tokens=0):
    """v1.1: 由流式尾部既有聚合变量成形 Anthropic body（挂点 4 的流式形态）。

    中继本身就有全程聚合（total_text/tool_calls_buffer/stop_reason/usage，
    anthropic_proxy._handle_streaming_response 尾部）——直接取用即可，无需
    tee 字节流或解析自己发的 SSE。块序=发射序（text 单块在前，tool_use 按
    索引序）；thinking 的 reasoning_content 在中继里本就并入文本增量，无
    独立块可漏。参数串解析失败保 raw（mirror 只消费 name 序列，兼容）。
    """
    content = []
    if total_text:
        content.append({"type": "text", "text": total_text})
    for idx in sorted(tool_calls_buffer or {}):
        entry = tool_calls_buffer.get(idx) or {}
        fn = entry.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except ValueError:
                args = {"_unparsed": True, "_raw": raw_args[:2000]}
        else:
            args = raw_args if raw_args is not None else {}
        content.append({"type": "tool_use", "id": entry.get("id") or "",
                        "name": fn.get("name") or "", "input": args})
    return {"model": model, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": int(input_tokens or 0),
                      "output_tokens": int(output_tokens or 0)},
            "content": content}


def record_response(request_id, resp_body):
    """挂点 4: 非流式响应完形处——完整 Anthropic body 落 response 行（双分片）。

    OpenAI-mode 响应（形状不同）由调用侧跳过; 超长文本块截断并标 truncated。
    """
    if not getattr(_ps, "PROXY_REQUEST_ARCHIVE_ENABLED", False):
        return
    with _pending_lock:
        rec = _recorded.get(request_id)
        if rec is None or rec["responded"]:
            return
        rec["responded"] = True
        session_key, turn = rec["session"], rec["turn"]
    body = _truncate_response(resp_body)
    if body is None:
        return
    _enforce_quota()
    for view in (VIEW_R, VIEW_RP):
        try:
            row = _base_row(request_id, session_key, view, "response", turn)
            row["stop_reason"] = body.get("stop_reason")
            row["usage"] = body.get("usage") or {}
            row["response"] = body
            _append_line(_shard_path(session_key, view), row)
        except Exception as e:
            _archive_warn(f"response {view} turn {turn} failed: {e}")


def _truncate_response(body, max_chars=None):
    """保形截断: 文本块按比例截, 仍超限则退化为形状骨架（truncated:true）。"""
    if not isinstance(body, dict):
        return None
    if max_chars is None:
        max_chars = int(getattr(_ps, "PROXY_REQUEST_ARCHIVE_RESPONSE_MAX_CHARS", 65536))
    try:
        if len(json.dumps(body, ensure_ascii=False)) <= max_chars:
            return body
    except (TypeError, ValueError):
        return {"truncated": True, "content": [], "stop_reason": body.get("stop_reason"),
                "note": "unserializable-response"}
    import copy
    slim = copy.deepcopy(body)
    blocks = slim.get("content") or []
    per = max(256, max_chars // max(1, len(blocks) or 1))
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text" and len(b.get("text", "")) > per:
            b["text"] = b["text"][:per]
    try:
        if len(json.dumps(slim, ensure_ascii=False)) <= max_chars * 2:
            slim["truncated"] = True
            return slim
    except (TypeError, ValueError):
        pass
    return {"truncated": True, "content": [], "stop_reason": slim.get("stop_reason"),
            "note": "oversize-response"}


def finalize_request(request_id, status=200):
    """挂点 5: do_POST 收尾——流式轮落骨架 response 行; 清理全部暂存态。

    流式 v1 不 materialize 聚合体: 骨架标 truncated:true（schema 允许）,
    mirror 对无基线轮自动降级 remote-only。异常请求（status!=200）不落
    response 行（validator 允许缺行）。
    """
    if not getattr(_ps, "PROXY_REQUEST_ARCHIVE_ENABLED", False):
        with _pending_lock:
            _pending.pop(request_id, None)
            _recorded.pop(request_id, None)
        return
    with _pending_lock:
        _pending.pop(request_id, None)
        rec = _recorded.pop(request_id, None)
    if rec is None or rec["responded"] or status != 200:
        return
    session_key, turn = rec["session"], rec["turn"]
    skeleton = {
        "truncated": True,
        "content": [],
        "stop_reason": None,
        "note": ("stream-not-materialized-v1" if rec.get("stream")
                 else "nonstream-response-not-captured"),
    }
    for view in (VIEW_R, VIEW_RP):
        try:
            row = _base_row(request_id, session_key, view, "response", turn)
            row["stop_reason"] = None
            row["usage"] = {}
            row["response"] = skeleton
            _append_line(_shard_path(session_key, view), row)
        except Exception as e:
            _archive_warn(f"skeleton {view} turn {turn} failed: {e}")


def stats():
    """只读画像: 会话数/暂存量（/api 或调试用, 零副作用）。"""
    with _pending_lock:
        return {
            "sessions": len(_sessions),
            "pending": len(_pending),
            "recorded_inflight": len(_recorded),
            "dir": _archive_dir(),
        }
