#!/usr/bin/env python3
"""unit_model.py — 消息原子化统一词汇表（stdlib only，叶子模块）。

来源: docs/02-architecture-design/context-architecture-evolution-20260829.md
模块化 review 调整 A。对齐前 _msg_hash/_extract_handle/_result_text 在
context_engine / session_ledger / pipeline 三处漂移——_extract_handle 两份
返回形态不兼容(裸字符串 vs 结构化 dict),是 D_ledger 对账、manifest 行、
ctx_recall 检索共享句柄词汇表的正确性阻断项。本模块即唯一规范:

  - Handle(TypedDict) {"type","value"}: 以 session_ledger 版结构化形态为规范
    (agent_go R14 契约消费的形态),键集合并 context_engine 版(command/pattern)。
  - msg_hash: 全量消息指纹(sort_keys md5)。对齐前两模块实现同口径,此处为唯一实现。
  - msg_text_hash: role+纯文本块短指纹(原 pipeline MessageHashDebug 内联实现,
    语义逐字节保留,名字显式化)。
  - text_str: text 字段防御规范化(原 content_compressor._text_str,list 形态实测存在)。
  - iter_blocks / result_text / result_chars: content block 原子化遍历与取文。

IFC/PDC 数据架构定位: 本模块是 Handle 词汇表的唯一源头,后续 unit_id 分配器
(数据架构 §3.1 Unit 实体)在此扩展。禁止 import 本仓库其他模块(保持叶子)。
"""
import hashlib
import json
import re

# 句柄参数键(优先级序): "重新取得该内容的充分信息"(Manus 可恢复原则, 上游设计 §4.3)
# = ledger 版四键 + ctx_engine 版扩展键(command/pattern),对齐后单一来源。
HANDLE_KEYS = ("url", "file_path", "path", "query", "command", "pattern")
_HANDLE_TYPE_BY_KEY = {
    "url": "url", "file_path": "path", "path": "path",
    "query": "query", "command": "command", "pattern": "pattern",
}
# 搜索类工具子串(句柄 type=tool 兜底; session_ledger.normalize_target 共用)
SEARCH_TOOLS = ("search", "websearch", "web_fetch", "webfetch", "query")
_HANDLE_VALUE_MAX = 300


class Handle(dict):
    """统一句柄对象: {"type": 重取方式, "value": 重取充分信息(≤300 chars)}。

    继承 dict 而非 TypedDict 运行时类: 保持与既有台账 JSON 完全同构
    (dict 字面量构造、json 序列化、R14 端点输出零差异)。
    """


def handle_value(handle):
    """Handle → 展示串(None 安全,返回 "")。折叠面板行/压缩标记行用。"""
    if not handle:
        return ""
    v = handle.get("value")
    return v if isinstance(v, str) else ""


def handle_key(handle):
    """Handle → join/去重键 "type:value"。D_ledger 对账与拉后即弃 join 用。"""
    if not handle:
        return ""
    return "%s:%s" % (handle.get("type", ""), handle.get("value", ""))


def extract_handle(tool_name, args):
    """(tool, args) → Handle dict | None。

    规范 = ledger 版结构化形态; 键优先级 HANDLE_KEYS; 未命中且为搜索类
    工具 → {"type": "tool", "value": 工具名} 兜底。
    """
    tool_l = (tool_name or "").lower()
    args = args if isinstance(args, dict) else {}
    for key in HANDLE_KEYS:
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return {"type": _HANDLE_TYPE_BY_KEY.get(key, "query"),
                    "value": v.strip()[:_HANDLE_VALUE_MAX]}
    if any(t in tool_l for t in SEARCH_TOOLS):
        return {"type": "tool", "value": tool_name or ""}
    return None


def text_str(value):
    """text 字段防御规范化: str 原样; list 逐项 str 后连接; 其余 str()。

    2026-08-17 实测部分消息 content 块的 text 字段为 list 形态,直接 join 会
    TypeError→500(原 content_compressor._text_str 的防御语义,逐字节保留)。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(x) for x in value)
    return str(value)


def _strip_cache_control(obj):
    """递归摘除 cache_control（L-12，2026-09-06）：它是传输层缓存断点提示
    （Claude Code 每轮把 ephemeral 断点前移到新末条并从旧末条摘除），非消息
    内容——计入 hash 会让 tail 指纹每轮失配，mismatch WARN 系统性误报。"""
    if isinstance(obj, dict):
        return {k: _strip_cache_control(v) for k, v in obj.items()
                if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip_cache_control(v) for v in obj]
    return obj


def msg_hash(msg):
    """全量消息指纹(前缀 diff 用)——sort_keys 保证 key 顺序不稳定不误判。
    cache_control 归一化剔除（L-12：断点轮换不改变消息语义）。"""
    try:
        raw = json.dumps(_strip_cache_control(msg), sort_keys=True,
                         ensure_ascii=False)
    except (TypeError, ValueError):
        raw = repr(msg)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def msg_text_hash(msg, length=8):
    """role + 纯文本块的短指纹(调试展示用; hash 值与原内联实现逐字节一致)。"""
    content = msg.get("content", "")
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(text_str(b.get("text", "")))
        content = "".join(parts)
    elif not isinstance(content, str):
        content = str(content)
    return hashlib.md5(
        (msg.get("role", "") + ":" + content).encode("utf-8")).hexdigest()[:length]


def iter_blocks(msg):
    """遍历一条消息的 content block(list 形态); string content 不产出 block。"""
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def result_chars(block):
    """tool_result 内容规模(字符)。"""
    content = block.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(json.dumps(b, ensure_ascii=False)) for b in content
                   if isinstance(b, (dict, str)))
    return 0


def result_text(block, limit=None):
    """tool_result 文本(str 原样; list 取 text 块 join; limit 截断,None 不截)。"""
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        text = "\n".join(parts)
    else:
        text = ""
    return text if limit is None else text[:limit]


__all__ = [
    "HANDLE_KEYS", "SEARCH_TOOLS", "Handle",
    "handle_value", "handle_key", "extract_handle",
    "text_str", "msg_hash", "msg_text_hash",
    "iter_blocks", "result_chars", "result_text",
]


# ============================================================================
# 关键实体提取(PDC 语义补全, 2026-09-01): 指称性内容(路径/命令/ID/数字/
# URL)是寻址词汇表——压缩可丢描述, 不可丢指称。确定性正则, 无 LLM。
# ============================================================================

_ENTITY_PATTERNS = (
    # 注意: 第二分支需 (?<![\w\-./]) 防从长词中间起配(否则 400 个 A 的
    # 填充 + 路径粘连会整段吞为一个"实体", 2026-09-01 单测实测)
    ("path", re.compile(r"(?:/[\w.\-]+){2,}|(?<![\w\-./])[\w\-]+(?:/[\w\-]+)+\.[a-z]{1,5}\b")),
    ("url", re.compile(r"https?://\S{4,120}")),
    ("id", re.compile(r"\b(?:id|ID|uuid|key|token|session)['\": =]+[\w\-]{4,40}")),
    ("hash", re.compile(r"\b[0-9a-f]{8,64}\b")),
    ("num", re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|kb|mb|gb|%|rows?|lines?)\b")),
    ("code", re.compile(r"\b(?:ERR|ERROR|E)\d{3,}\b")),
)


def extract_key_entities(text, max_items=12, max_chars=300):
    """提取指称性实体(路径/URL/ID/哈希/带单位数字/错误码), 去重保序。

    语义: 这些是压缩/截断后必须存活的"寻址词汇"——模型引用与
    ctx_recall 检索都依赖它们。返回带类别前缀的字符串列表。
    """
    if not text or not isinstance(text, str):
        return []
    out, seen = [], set()
    for kind, pat in _ENTITY_PATTERNS:
        for m in pat.finditer(text):
            v = m.group(0).strip()
            k = v.lower()
            if k not in seen:
                seen.add(k)
                out.append(f"{v}")
            if len(out) >= max_items:
                return out
    return out


def entity_line(entities):
    """实体列表 → 追加到压缩产物尾部的实体行(超长截断)。"""
    if not entities:
        return ""
    line = "[关键实体] " + " | ".join(entities)
    if len(line) > 300:
        line = line[:297] + "..."
    return line
