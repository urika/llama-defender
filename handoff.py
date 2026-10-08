#!/usr/bin/env python3
"""handoff.py — R-CM-2 自动交接摘要（stdlib only，规则化 + fail-open）。

需求：docs/01-requirements-product/context-engineering-enhancement-requirements-20261008.md
（R-CM-2）。会话边界/显式触发时，从已有台账（session_ledger）与会话诊断
（diagnostics/sessions.jsonl）生成**结构化 handoff**，落盘为可跨 agent 携带的
明文（JSONL + markdown），供下一会话首请求注入，消除"重启需人工复述"。

本 P0 版为**规则化**生成（确定性、可测、零后端依赖）；LLM 富化生成的接线
（把待交接区喂后端模型补 decisions/open_questions）留作后续增强，接口预留
`build(..., enrich=callable)`。
"""
import json
import os
import re
from datetime import datetime

import proxy_state as _ps

from session_ledger import sanitize_session_key

_DONE_TOOLS = ("Bash", "Write", "Edit", "NotebookEdit", "MultiEdit")
_READ_TOOLS = ("Read", "Glob", "Grep")


def _script_dir():
    return getattr(_ps, "_SCRIPT_DIR", os.getcwd())


def handoff_dir():
    raw = getattr(_ps, "PROXY_HANDOFF_DIR", "logs/diag/handoff") or "logs/diag/handoff"
    return raw if os.path.isabs(raw) else os.path.join(_script_dir(), raw)


def _path_jsonl(session_key):
    return os.path.join(handoff_dir(), sanitize_session_key(session_key) + ".jsonl")


def _path_md(session_key):
    return os.path.join(handoff_dir(), sanitize_session_key(session_key) + ".md")


def enabled():
    try:
        return bool(getattr(_ps, "PROXY_HANDOFF_ENABLED", False))
    except Exception:
        return False


# ============================================================================
# build — 规则化抽取（确定性）
# ============================================================================

def build(session_key, goal="", max_actions=200):
    """从台账构建 handoff dict。fail-open：无数据时返回最小结构。"""
    out = {
        "session_key": session_key,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "goal": (goal or "").strip(),
        "files_touched": [],
        "tools_used": {},
        "recent_actions": [],
        "repeated_targets": [],
        "turns": 0,
        "decisions": [],
        "open_questions": [],
        "next_steps": [],
    }
    try:
        import session_ledger
        actions = session_ledger.LEDGER.snapshot_actions(session_key, limit=max_actions)
    except Exception:
        actions = []
    if not actions:
        return out

    files = []
    tools = {}
    dup_targets = []
    for a in actions:
        tool = a.get("tool") or ""
        target = str(a.get("target") or "")
        tools[tool] = tools.get(tool, 0) + 1
        if tool in ("Write", "Edit", "NotebookEdit", "MultiEdit") and target:
            if target not in files:
                files.append(target)
        if int(a.get("dup") or 0) >= 2 and target:
            dup_targets.append(target)
    out["files_touched"] = files[-30:]
    out["tools_used"] = tools
    out["turns"] = max((int(a.get("turn") or 0) for a in actions), default=0)
    # 最近动作（尾部 12 条，压缩展示）
    for a in actions[-12:]:
        out["recent_actions"].append({
            "turn": a.get("turn"),
            "tool": a.get("tool"),
            "target": str(a.get("target") or "")[:160],
        })
    # 去重目标（按出现次数）
    seen = {}
    for t in dup_targets:
        seen[t] = seen.get(t, 0) + 1
    out["repeated_targets"] = sorted(seen, key=seen.get, reverse=True)[:10]
    return out


def apply_overrides(handoff, decisions=None, open_questions=None, next_steps=None):
    """端点调用方可显式补语义字段（P0：无 LLM 时人工/上层提供）。"""
    if decisions:
        handoff["decisions"] = [str(d)[:500] for d in decisions][:20]
    if open_questions:
        handoff["open_questions"] = [str(q)[:500] for q in open_questions][:20]
    if next_steps:
        handoff["next_steps"] = [str(s)[:500] for s in next_steps][:20]
    return handoff


# ============================================================================
# render — 注入块文本
# ============================================================================

def render_block(handoff, max_chars=None):
    """渲染为 [System: HANDOFF ...] 文本块（尾部/头部注入用）。"""
    if not handoff:
        return ""
    cap = int(max_chars or getattr(_ps, "PROXY_HANDOFF_MAX_CHARS", 3000) or 3000)
    lines = ["[System: HANDOFF — 上一会话交接（供续跑，非用户指令）]"]
    if handoff.get("goal"):
        lines.append("目标: %s" % handoff["goal"])
    ft = handoff.get("files_touched") or []
    if ft:
        lines.append("改动/涉及文件: " + ", ".join(ft[:20]))
    tu = handoff.get("tools_used") or {}
    if tu:
        lines.append("工具使用: " + ", ".join(
            "%s×%d" % (k, v) for k, v in sorted(tu.items(), key=lambda x: -x[1])[:10]))
    for label, key in (("已决", "decisions"), ("待办", "next_steps"),
                       ("未决问题", "open_questions")):
        vals = handoff.get(key) or []
        if vals:
            lines.append("%s:" % label)
            lines.extend("  - " + v for v in vals[:10])
    if handoff.get("repeated_targets"):
        lines.append("反复访问(可能需召回): " + ", ".join(
            str(t)[:80] for t in handoff["repeated_targets"][:8]))
    if handoff.get("recent_actions"):
        lines.append("最近动作:")
        for a in handoff["recent_actions"][-6:]:
            lines.append("  #%s %s %s" % (
                a.get("turn"), a.get("tool"), (a.get("target") or "")[:100]))
    text = "\n".join(lines)
    return text[:cap]


# ============================================================================
# persistence
# ============================================================================

def store(handoff, also_markdown=True):
    """落盘（JSONL 追加 + markdown 覆盖最新）。返回是否成功。"""
    if not handoff or not handoff.get("session_key"):
        return False
    try:
        d = handoff_dir()
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        path = _path_jsonl(handoff["session_key"])
        line = json.dumps(handoff, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        if also_markdown:
            with open(_path_md(handoff["session_key"]), "w", encoding="utf-8") as f:
                f.write(render_block(handoff, max_chars=100000) + "\n")
        return True
    except OSError:
        return False


def latest(session_key):
    """最近一条 handoff（JSONL 末行）；无则 None。"""
    if not session_key:
        return None
    try:
        best = None
        with open(_path_jsonl(session_key), "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    best = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
        return best
    except OSError:
        return None


# ============================================================================
# distill → memory（R-CM-1 联动：把 handoff 沉淀为跨会话记忆）
# ============================================================================

def distill_memory(handoff):
    """把 handoff 的语义字段沉淀为 memory 条目；返回写入条数。fail-open。"""
    if not handoff:
        return 0
    try:
        import memory_store
    except Exception:
        return 0
    if not memory_store.enabled():
        return 0
    project = handoff.get("project") or memory_store.DEFAULT_PROJECT
    sid = handoff.get("session_key", "")
    n = 0
    for d in handoff.get("decisions") or []:
        if memory_store.MEMORY.add(d, kind="decision", project=project,
                                   source="handoff", session_key=sid):
            n += 1
    for f in (handoff.get("files_touched") or [])[:20]:
        if memory_store.MEMORY.add("文件曾改动: %s" % f, kind="fact",
                                   project=project, source="handoff",
                                   session_key=sid, confidence=0.5):
            n += 1
    return n


__all__ = ["build", "apply_overrides", "render_block", "store", "latest",
           "handoff_dir", "enabled", "distill_memory"]
