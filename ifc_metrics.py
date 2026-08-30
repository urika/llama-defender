#!/usr/bin/env python3
"""ifc_metrics.py — IFC Tier-0 结构指标（stdlib only，纯函数 + 有界会话基线）。

R9.1（PRD v3.1 / IFC 设计 §4.1 Tier-0）：信息保真控制的零成本观测层。
全部指标由「相邻两轮发送视图差分」与「台账动作序列」派生——不触碰管线
stage、不需要 per-stage 钩子（模块化约束：pipeline.py 由并行工作占用中，
设计决策见 context-architecture-evolution-20260829.md §2.2）。

锚点差分法（v0 核心机制）：
  消息单元锚 = tool_use/tool_call id（客户端生成的稳定 ID，跨格式存在）
  或整消息指纹（unit_model.msg_hash）。据此区分两类损失：
  - 锚消失        → 单元被丢弃（fifo 截断 / epoch 折叠 / 客户端 compaction）
  - 锚保留但缩小  → 单元被就地压缩（ContentCompressor / 写入期压缩）
  Anthropic 与 OpenAI 两种消息格式均覆盖（capture_sent_view 的视图随路由
  可能是任一格式）。

口径（L2 派生指标，数据架构 §3.3；只写不读执行器）：
  retention        = 1 − (dropped_chars + shrunk_chars)/|V_{t−1}|（钳 [0,1]）
  rationale_ratio  = 1 − dropped_rationale_chars / rationale_chars(V_{t−1})（钳 [0,1]）
                     ——动机存活率：fifo 丢轮后「见效果不见原因」的直接量化
  action_div       = 归一化 bigram 熵（最近 N=20 次工具调用序列）——双端检测：
                     <0.15 循环坍缩（现有 loop 检测覆盖）/ >0.85 且重读压力高 → 游走
  reread_pressure  = 最近 W=10 个台账动作中命中既往 target_hash 的次数

已知口径噪声（诚实记录，离线分析可甄别）：客户端 compaction 与代理截断在
视图差分中同形（均为锚消失），区分依赖 canonical_mismatch 联判。

配置：PROXY_IFC_ENABLED 经 getattr(_ps,...,True) 读取（hbe_probe 同款模式）；
CONFIG_REGISTRY 正式注册待 pipeline/proxy_config 并行工作合并后补两行。
"""
import math
import threading

import unit_model as _um

# 会话基线上限（对齐 PROXY_DIAG_SESSION_MAX=64 的 FIFO 驱逐语义）
BASELINE_MAX_SESSIONS = 64
# 分类器版本(原始数据不可变原则:sessions.jsonl 只追加不改写;分类规则进化
# 靠版本号区分,离线可按 archive 原始载荷重放任意版本的分类):
#   1 = 初始锚点差分
#   2 = +system 单元排除 / view_reset 任务切换分类 / 探针伪迹口径(2026-08-30)
IFC_CLS_VERSION = 2
# 就地压缩的计入阈值（chars）：避免格式化噪声误报 compress_drop
SHRINK_MIN_CHARS = 128
# Tier-0 指标窗口
ACTION_DIV_WINDOW = 20
REREAD_WINDOW = 10


# ============================================================================
# 消息 → 单元锚点提取（两种协议格式统一）
# ============================================================================

def _text_chars_of_content(content):
    """消息 content（str 或 block list）里的文本字符量。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                n += len(b["text"])
            elif isinstance(b, str):
                n += len(b)
        return n
    return 0


def _content_head(content, limit=120):
    """content 的头部摘录（manifest 可检索体；丢弃的动机文本恰恰是模型
    最想按关键词找回的，纯锚索引搜不到内容词）。"""
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        parts = []
        n = 0
        for b in content:
            t = b.get("text") if isinstance(b, dict) else (b if isinstance(b, str) else None)
            t = _um.text_str(t) if t is not None else ""
            if t:
                parts.append(t)
                n += len(t)
                if n >= limit:
                    break
        return "".join(parts)[:limit]
    return ""


def unit_anchors(msg):
    """一条消息 → 单元字典列表（锚点 + 分类，manifest 与视图差分共用词汇）。

    每个单元: {anchor, role, kind(tool_use|tool_result|text), tool, handle, size_chars}
    - Anthropic: content block 的 tool_use（含 name/input）与 tool_result（tool_use_id）
    - OpenAI:    assistant.tool_calls[].id 与 role=="tool" 的 tool_call_id
    - 无工具消息: 整条为一个 text 单元（锚 = 消息指纹）
    """
    role = msg.get("role") or ""
    content = msg.get("content")
    units = []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "tool_use":
                args = b.get("input") if isinstance(b.get("input"), dict) else {}
                units.append({
                    "anchor": "u:%s" % (b.get("id") or _um.msg_hash(b)),
                    "role": role, "kind": "tool_use", "tool": b.get("name", ""),
                    "handle": _um.extract_handle(b.get("name", ""), args),
                    "size_chars": len(_um.text_str(b.get("input", ""))),
                })
            elif btype == "tool_result":
                units.append({
                    "anchor": "r:%s" % (b.get("tool_use_id") or _um.msg_hash(b)),
                    "role": role, "kind": "tool_result", "tool": "",
                    "handle": None,
                    "size_chars": _um.result_chars(b),
                    "head": _um.result_text(b, 120),
                })
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name", "") if isinstance(fn, dict) else ""
            args = fn.get("arguments", "") if isinstance(fn, dict) else ""
            try:
                import json as _json
                args_d = _json.loads(args) if isinstance(args, str) and args.strip() else {}
            except (ValueError, TypeError):
                args_d = {}
            units.append({
                "anchor": "u:%s" % (tc.get("id") or _um.msg_hash(tc)),
                "role": role, "kind": "tool_use", "tool": name,
                "handle": _um.extract_handle(name, args_d if isinstance(args_d, dict) else {}),
                "size_chars": len(_um.text_str(args)),
            })
    if msg.get("role") == "tool":
        tcid = msg.get("tool_call_id")
        units.append({
            "anchor": "r:%s" % (tcid or _um.msg_hash(msg)),
            "role": "tool", "kind": "tool_result", "tool": "",
            "handle": None,
            "size_chars": _text_chars_of_content(content),
        })
    if not units:
        units.append({
            "anchor": "h:%s" % _um.msg_hash(msg),
            "role": role, "kind": "text", "tool": "",
            "handle": None,
            "size_chars": _text_chars_of_content(content) + len(_um.text_str(role)),
            "head": _content_head(content),
        })
    return units


def view_summary(messages):
    """发送视图 → 差分基线摘要（锚→单元，含 rationale 标记与工具名尾序）。"""
    units = {}
    tool_names = []
    rationale_chars = 0
    total_chars = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if (msg.get("role") or "") == "system":
            # system 是每请求重注入的运行时上下文(env/日期/提醒),非会话记忆——
            # 不进差分,否则客户端 system 微调即误报 unit_drop(2026-08-30 实测:
            # 92 个 ile 误报轮的成分之一)
            continue
        for u in unit_anchors(msg):
            if u["anchor"] in units:
                # 同锚罕见（重发/复制）；保守取首见，规模累加以免低估损失
                units[u["anchor"]]["size_chars"] += u["size_chars"]
                continue
            entry = dict(u)
            # rationale = 非工具结果单元的文本量（动机面：用户诉求 + 助手论证）
            entry["is_rationale"] = u["kind"] != "tool_result"
            units[u["anchor"]] = entry
            if u["kind"] == "tool_use":
                tool_names.append(u["tool"])
            if entry["is_rationale"]:
                rationale_chars += u["size_chars"]
            total_chars += u["size_chars"]
    return {
        "n_msgs": len(messages or []),
        "total_chars": total_chars,
        "rationale_chars": rationale_chars,
        "units": units,
        "tool_names": tool_names[-32:],
    }


def diff_views(prev, cur):
    """相邻两轮视图差分 → 损失事实（不推断 ILE，纯事实层）。"""
    if not prev or not cur:
        return None
    prev_units = prev["units"]
    cur_units = cur["units"]
    dropped = [u for a, u in prev_units.items() if a not in cur_units]
    shrunk_chars = 0
    shrunk_units = 0
    for a, pu in prev_units.items():
        cu = cur_units.get(a)
        if cu is not None and pu["size_chars"] - cu["size_chars"] >= SHRINK_MIN_CHARS:
            shrunk_units += 1
            shrunk_chars += pu["size_chars"] - cu["size_chars"]
    added = [a for a in cur_units if a not in prev_units]
    dropped_chars = sum(u["size_chars"] for u in dropped)
    dropped_rationale = sum(u["size_chars"] for u in dropped if u.get("is_rationale"))
    # 视图连续性判定: 真实截断/压缩/compaction 都是「保尾丢头」;任务切换或
    # 客户端整段重写则连上一视图尾部单元一起消失(swe-eval 同键下「命名请求→
    # 正式任务」实测)。尾部不存活 → view_reset,不算信息损失事件。
    tail_keys = list(prev_units.keys())[-2:]
    view_reset = any(k not in cur_units for k in tail_keys) if tail_keys else False
    return {
        "dropped_units": len(dropped),
        "dropped_chars": dropped_chars,
        "dropped_rationale_chars": dropped_rationale,
        "shrunk_units": shrunk_units,
        "shrunk_chars": shrunk_chars,
        "added_units": len(added),
        "prev_total_chars": prev["total_chars"],
        "prev_rationale_chars": prev["rationale_chars"],
        "view_reset": view_reset,
    }


# ============================================================================
# L2 纯函数指标
# ============================================================================

def retention(diff):
    """视图连续性: 1 − (丢弃+收缩)/|V_{t−1}|，无基线 → None。"""
    if not diff or diff["prev_total_chars"] <= 0:
        return None
    lost = diff["dropped_chars"] + diff["shrunk_chars"]
    return round(max(0.0, 1.0 - lost / diff["prev_total_chars"]), 4)


def rationale_ratio(diff):
    """动机存活率: 1 − dropped_rationale/prev_rationale，钳 [0,1]。"""
    if not diff or diff["prev_rationale_chars"] <= 0:
        return None
    r = 1.0 - diff["dropped_rationale_chars"] / diff["prev_rationale_chars"]
    return round(max(0.0, min(1.0, r)), 4)


def action_diversity(tool_names, window=ACTION_DIV_WINDOW):
    """归一化 bigram 熵 ∈ [0,1]：低 → 病态重复；高 → 动作发散。样本不足 → None。"""
    seq = [t for t in (tool_names or []) if t][-window:]
    if len(seq) < 4:
        return None
    bigrams = [(seq[i], seq[i + 1]) for i in range(len(seq) - 1)]
    if len(bigrams) < 2:
        return None
    counts = {}
    for bg in bigrams:
        counts[bg] = counts.get(bg, 0) + 1
    total = len(bigrams)
    h = -sum((c / total) * math.log2(c / total) for c in counts.values())
    return round(h / math.log2(total), 4)


def reread_pressure(actions, window=REREAD_WINDOW):
    """最近 W 个台账动作中重复 target_hash 的次数（首现不计）。"""
    tail = [a for a in (actions or []) if isinstance(a, dict)][-window:]
    seen = set()
    dup = 0
    for a in tail:
        th = a.get("target_hash")
        if not th:
            continue
        if th in seen:
            dup += 1
        else:
            seen.add(th)
    return dup


def infer_ile_kinds(diff):
    """损失事实 → ILE 类别推断(v0 视图差分口径;view_reset 不算损失)。"""
    if not diff or diff.get("view_reset"):
        return []
    kinds = []
    if diff["dropped_units"] > 0:
        kinds.append("unit_drop")
    if diff["shrunk_chars"] > 0:
        kinds.append("compress_drop")
    return kinds


def build_ifc_section(prev, cur, actions, manifest_lines=None):
    """组装 per-turn `ifc` 记录段（diagnostics.finalize_request 调用）。

    prev/cur: view_summary 输出（prev=None 表示该会话首见/基线缺失）。
    actions:  台账动作快照（session_ledger.LEDGER.snapshot_actions）。
    manifest_lines: manifest 当前行数（覆盖率观测：有推断损失但行数为 0 → 缺口）。
    """
    section = {
        "cls_version": IFC_CLS_VERSION,
        "n_msgs": cur["n_msgs"] if cur else None,
        "retention": None,
        "rationale_ratio": None,
        "reread_pressure": reread_pressure(actions),
        "action_div": None,
        "ile": False,
        "ile_kinds": [],
        "dropped_units": 0,
        "dropped_chars": 0,
        "shrunk_chars": 0,
    }
    if cur:
        section["action_div"] = action_diversity(cur.get("tool_names"))
    if prev and cur:
        diff = diff_views(prev, cur)
        kinds = infer_ile_kinds(diff)
        # 原始事实层: 丢弃单元明细(锚/类别/角色/规模,截 16 条)——分类规则
        # 进化时无需回放 archive 即可重分类;事实与分类同记,重放可校验
        section["dropped_detail"] = [
            {"anchor": u["anchor"], "kind": u["kind"], "role": u["role"],
             "size_chars": u["size_chars"]}
            for a, u in prev["units"].items() if a not in cur["units"]
        ][:16]
        section.update({
            "retention": retention(diff),
            "rationale_ratio": rationale_ratio(diff),
            "ile": bool(kinds),
            "ile_kinds": kinds,
            "dropped_units": diff["dropped_units"],
            "dropped_chars": diff["dropped_chars"],
            "shrunk_chars": diff["shrunk_chars"],
            "view_reset": diff.get("view_reset", False),
        })
    if manifest_lines is not None:
        section["manifest_lines"] = manifest_lines
    return section


# ============================================================================
# 会话基线存储（有界，FIFO 驱逐——台账同款语义）
# ============================================================================

class ViewBaselineStore(object):
    """session_key → 上一轮 view_summary（进程内；重启后首轮 retention=None）。"""

    def __init__(self, max_sessions=BASELINE_MAX_SESSIONS):
        self._lock = threading.Lock()
        self._order = []
        self._store = {}
        self._max = max_sessions

    def get(self, session_key):
        with self._lock:
            return self._store.get(session_key)

    def update(self, session_key, summary):
        if not session_key or not summary:
            return
        with self._lock:
            if session_key not in self._store:
                self._order.append(session_key)
            self._store[session_key] = summary
            while len(self._order) > self._max:
                oldest = self._order.pop(0)
                self._store.pop(oldest, None)

    def clear(self):
        with self._lock:
            self._order.clear()
            self._store.clear()


BASELINE = ViewBaselineStore()


__all__ = [
    "unit_anchors", "view_summary", "diff_views", "infer_ile_kinds",
    "retention", "rationale_ratio", "action_diversity", "reread_pressure",
    "build_ifc_section", "ViewBaselineStore", "BASELINE",
    "ACTION_DIV_WINDOW", "REREAD_WINDOW", "SHRINK_MIN_CHARS",
    "IFC_CLS_VERSION",
]
