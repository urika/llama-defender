# 代理层 Prefix Cache 稳定化设计文档

> **状态**: v1 设计  
> **作者**: opencode  
> **日期**: 2026-06-06  
> **关联**: R3.2 (前缀稳定化), commit `6060552` (prefix cache analysis), `08925bb` (HEAD=6), `a6952e6` (static placeholder)  
> **版本**: v1.0

---

## 0. 文档目的

在 **代理层 (proxy layer)** 实现 prefix cache 稳定化,使得发送给后端的 prompt 在跨请求时保持**结构 + 内容的双重稳定**,从而最大化后端 prefix cache (vLLM APC / SGLang RadixAttention / rapid-mlx) 的命中率和减少 forced cache clear。

**与 R3.2 的关系**: 本文档是 R3.2 的**升级版**。R3.2 当前的 4 项实现(日期占位、Thinking 清除、Cleared 合并、固定占位文本)是 4 个**ad-hoc 规则**,本文档将其重构为**可配置、可扩展、可度量**的**规则引擎 + 块哈希**体系。

---

## 1. 背景与现状

### 1.1 当前 R3.2 实现的局限性

| 当前实现 | 行号 | 局限 |
|---------|------|------|
| 日期占位符 (`re.sub`) | L3127 | 仅日期,无其他动态字段 |
| 固定占位 `[Context folded: ...]` | L3071 | 嵌入 rounds 截断,作用域窄 |
| Thinking strip | L1530-1594 | 仅基于计数 keep_recent,无缓存 |
| Cleared 压缩 | L1616-1737 | 启发式合并,无 hash 决策 |
| **缺**: 工具列表稳定化 | ❌ | 工具变化直接破坏 prefix |
| **缺**: prefix 哈希埋点 | ❌ | 无命中率统计,无法量化 |
| **缺**: 跨请求 cache 复用 | ❌ | 仅 ad-hoc 文本占位 |
| **缺**: 可配置规则系统 | ❌ | 改一处要改 4 处代码 |

### 1.2 当前代理层稳定化效果 (commit `08925bb` 数据)

| 配置 | 共同前缀比例 | prefix cache 命中率 | TTFT |
|------|------------|-------------------|------|
| `rounds` 策略 (理想) | 90-99% | 90-99% | 1-5s |
| **实际跑 `fifo`** (DEF-102) | 24% | 0% | 90s |
| HEAD=6 占位消息 (commit `08925bb`) | 数据未量化 | 待测 | 待测 |

**关键观察**: 当前实际**未在生产**跑 rounds 策略,效果未知。需要**标准化、可度量**的稳定化层。

### 1.3 行业方案参考

| 方案 | 层 | 核心机制 | 来源 |
|------|------|---------|------|
| **vLLM APC** | 后端 | Block-based hierarchical hash: `hash(parent_hash + block_tokens + extra_hashes)` | [vLLM 设计文档](https://docs.vllm.ai/en/latest/design/prefix_caching.html) |
| **SGLang RadixAttention** | 后端 | Radix tree,LRU eviction,5x faster than vLLM | LMSYS 2024 |
| **Anthropic cache_control** | API | 显式 breakpoint,4 个上限,5min/1h TTL | Anthropic API |
| **OpenAI auto cache** | API | 自动,>1024 tokens,50% 折扣 | OpenAI API |
| **我们 R3.2** | 代理 | 4 个 ad-hoc 规则 | 当前实现 |

### 1.4 关键洞察: **vLLM 块哈希原理** (核心算法)

来自 vLLM 官方设计文档(2026-01-19)的核心算法:

```
                    Block 1                  Block 2                  Block 3
         [A gentle breeze stirred] [the leaves as children] [laughed in the distance]
Block 1: |<--- block tokens ---->|
Block 2: |<------- prefix ------>| |<--- block tokens --->|
Block 3: |<------------------ prefix -------------------->| |<--- block tokens ---->|

block_hash = hash(parent_hash, block_tokens_tuple, extra_hashes)
```

**3 个关键设计点**:
1. **分层哈希** (parent + current) — 避免全局重算
2. **块粒度** (block_size = 16 tokens 默认) — 平衡命中率与缓存粒度
3. **extra_hashes** (LoRA IDs, 多模态 hashes, cache_salt) — 隔离多租户

**直接借鉴到代理层**: 我们不能用 tokens(因为还没进 tokenizer),但可以用 **JSON 序列化后的字符串块** 作为稳定化单位。

---

## 2. 设计目标

### 2.1 核心目标

| ID | 目标 | 度量 |
|----|------|------|
| **G1** | 跨请求 prompt 共同前缀 ≥ 80% | 代理层 log prefix_hash 命中率 |
| **G2** | 后端 prefix cache 命中率 ≥ 90% | backend 端 cache_fetch HIT ratio |
| **G3** | TTFT 从 90s 降至 1-5s (含 cache miss 兜底) | Prometheus 监控 |
| **G4** | 规则可配置,改 1 行生效,无需重写代码 | 配置驱动,JSON/YAML |

### 2.2 非目标 (Out of Scope)

- ❌ **不实现完整 KV cache 引擎** — 那是 vLLM/SGLang 的活
- ❌ **不修改后端 prefix cache 算法** — 黑盒处理
- ❌ **不引入第三方 tokenizer 依赖** — 保持 zero-dep
- ❌ **不持久化 prefix cache** — 重启即重建,符合"代理层临时优化"定位

---

## 3. 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Claude Code (Anthropic SDK)                    │
└────────────────────────────┬────────────────────────────────────┘
                             │ POST /v1/messages
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              anthropic_proxy.py :4000                              │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Layer 1-3: 请求处理 (现有)                                   │  │
│  │   parse → clear → loop detect                               │  │
│  └─────────────────────┬────────────────────────────────────────┘  │
│  ┌─────────────────────▼────────────────────────────────────────┐  │
│  │  ★ Layer 3.5: Prefix Stabilization (NEW)                       │  │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐              │  │
│  │  │ Block       │  │ Rule       │  │ Hash       │              │  │
│  │  │ Builder     │→ │ Engine     │→ │ Computer   │              │  │
│  │  │ (canonical  │  │ (5 内置    │  │ (hierarch- │              │  │
│  │  │  blocks)    │  │  rules)    │  │  ical)     │              │  │
│  │  └────────────┘  └────────────┘  └─────┬──────┘              │  │
│  │  ┌─────────────────────────────────────▼──────────────┐     │  │
│  │  │ Prefix Cache Stats (NEW)                              │     │  │
│  │  │ - hash hit / miss 计数器                              │     │  │
│  │  │ - 写入 logs/proxy_metrics.jsonl                       │     │  │
│  │  └────────────────────────────────────────────────────────┘     │  │
│  └─────────────────────┬────────────────────────────────────────┘  │
│  ┌─────────────────────▼────────────────────────────────────────┐  │
│  │ Layer 4-7: 截断 + 转换 + 转发 (现有)                          │  │
│  └─────────────────────┬────────────────────────────────────────┘  │
│                        │ POST /chat/completions                  │
└────────────────────────┼────────────────────────────────────────┘
                         ▼
              ┌──────────────────────┐
              │ rapid-mlx / vLLM     │
              │ (prefix cache 自动) │
              └──────────────────────┘
```

**关键定位**: Layer 3.5 (Prefix Stabilization) **位于** Layer 3 (循环检测) 之后、Layer 4 (截断) 之前。**先稳定化,再截断**(截断后内容已确定,稳定化无意义)。

---

## 4. 核心设计: 分层块哈希 (Hierarchical Block Hash)

### 4.1 为什么是"块"?

参考 vLLM 经验:
- **太细** (1 token/block) → 哈希开销爆炸,缓存利用率低
- **太粗** (whole prompt) → 一字之差即全 miss
- **适中** (16 chars/block) → 平衡

**代理层选择**: `block_size=16` 字符 (char) 作为最小单位。原因:
- 与 token 数大致对应 (中英文混排约 1 char ≈ 1 token)
- JSON 序列化后可预测切分
- 不依赖 tokenizer (zero-dep)

### 4.2 块哈希算法 (借鉴 vLLM)

```python
# llama_defender/prefix_cache/hash.py

import hashlib
import json

def compute_block_hash(
    parent_hash: str | None,
    block_content: str,
    extra_hashes: dict[str, str] = None,
    algo: str = "sha256",  # 默认 sha256 抗碰撞
) -> str:
    """借鉴 vLLM 的 hierarchical block hash。
    
    block_hash = hash(parent_hash + block_content + extra_hashes)
    """
    h = hashlib.new(algo)
    if parent_hash:
        h.update(parent_hash.encode("utf-8"))
    h.update(b"|")
    h.update(block_content.encode("utf-8"))
    h.update(b"|")
    if extra_hashes:
        # 使用 cbor 序列化保证顺序无关、跨语言一致
        h.update(json.dumps(extra_hashes, sort_keys=True).encode("utf-8"))
    return h.hexdigest()
```

### 4.3 消息 → 块的规范化 (Block Builder)

```python
# llama_defender/prefix_cache/block_builder.py

@dataclass
class CanonicalBlock:
    """一个规范化后的块,可被哈希。"""
    block_id: str          # 稳定 ID (e.g. "msg:0:user:body:0:16")
    parent_hash: str | None
    block_type: str        # system | user_text | user_tool_result | assistant_text | assistant_tool_use | tools_schema
    content_canonical: str  # 规范化后的内容 (确定性)
    raw_chars: int          # 原始字符数 (用于 metrics)
    extra_hashes: dict      # image_hash / cache_salt 等

class BlockBuilder:
    """将 Anthropic Messages + Tools 拆分为确定性块。"""
    
    BLOCK_SIZE = 16  # chars
    
    def build(self, body: dict) -> list[CanonicalBlock]:
        blocks = []
        parent = None
        
        # 1. system prompt (永远是第一个)
        for sys_msg in body.get("system", []):
            for block in self._split_text(sys_msg["text"]):
                parent = self._make_block(parent, "system", block, blocks)
        
        # 2. tools (按 name 排序后,确保稳定)
        if body.get("tools"):
            tools_canonical = self._canonicalize_tools(body["tools"])
            parent = self._make_block(parent, "tools_schema", tools_canonical, blocks)
        
        # 3. messages 序列
        for msg_idx, msg in enumerate(body.get("messages", [])):
            parent = self._process_message(parent, msg_idx, msg, blocks)
        
        return blocks
    
    def _canonicalize_tools(self, tools: list) -> str:
        """工具列表稳定化:按 name 排序,移除不稳定字段。"""
        stable_tools = []
        for t in tools:
            stable_tools.append({
                "name": t["name"],
                "description": t["description"],
                "parameters": t.get("input_schema", {}),  # 移除 examples 等
            })
        # 按 name 排序 (关键!)
        stable_tools.sort(key=lambda x: x["name"])
        return json.dumps(stable_tools, sort_keys=True, ensure_ascii=False)
    
    def _process_message(self, parent, msg_idx, msg, blocks) -> str:
        role = msg["role"]
        content = msg.get("content", "")
        
        if role == "user":
            if isinstance(content, str):
                # 纯文本
                for block in self._split_text(content):
                    parent = self._make_block(parent, f"user_text", block, blocks)
            else:
                # 复合 content (含 tool_result)
                for part in content:
                    if part["type"] == "text":
                        for block in self._split_text(part["text"]):
                            parent = self._make_block(parent, "user_text", block, blocks)
                    elif part["type"] == "tool_result":
                        parent = self._process_tool_result(parent, msg_idx, part, blocks)
        
        elif role == "assistant":
            # 包含 text + tool_use 的复合 content
            for part in content:
                if part["type"] == "text":
                    for block in self._split_text(part["text"]):
                        parent = self._make_block(parent, "assistant_text", block, blocks)
                elif part["type"] == "tool_use":
                    parent = self._process_tool_use(parent, part, blocks)
        
        return parent
    
    def _make_block(self, parent, block_type, content, blocks) -> str:
        h = compute_block_hash(parent, content)
        blocks.append(CanonicalBlock(
            block_id=block_type,
            parent_hash=parent,
            block_type=block_type,
            content_canonical=content,
            raw_chars=len(content),
        ))
        return h
    
    def _split_text(self, text: str) -> list[str]:
        """按 BLOCK_SIZE 切分长文本,保留完整语义。"""
        if len(text) <= self.BLOCK_SIZE:
            return [text]
        return [text[i:i+self.BLOCK_SIZE] for i in range(0, len(text), self.BLOCK_SIZE)]
```

**关键设计**:
- `system` 是绝对的 prefix anchor,跨请求 100% 稳定
- `tools_schema` 按 name 排序,跨请求稳定(即便 Claude Code 添加新工具,旧的依然稳定)
- 文本按 16 char 切块,既保留语义又便于 hash 复用
- `parent_hash` 形成 linked list,任何块内容变化导致该块及其后续块 hash 全部变化(预期行为)

---

## 5. 规则引擎: 5 个内置稳定化规则

### 5.1 规则接口

```python
# llama_defender/prefix_cache/rules.py

from abc import ABC, abstractmethod

class StabilizationRule(ABC):
    """稳定化规则:对 block content 应用变换,使其跨请求更稳定。"""
    
    name: str  # 规则名 (用于 metrics + 配置)
    priority: int  # 应用顺序 (数字越小越早)
    enabled: bool = True
    
    @abstractmethod
    def apply(self, content: str, context: dict) -> tuple[str, dict]:
        """返回 (新内容, 应用统计)。
        
        context 包含: session_id, request_id, original_date, current_block_type 等
        """
        ...
```

### 5.2 内置规则 1: DatePlaceholderRule (L3127 升级版)

**当前问题**: 硬编码只匹配 `Today's date is \d{4}/\d{2}/\d{2}.`,无法处理其他日期格式或多日期。

**升级方案**:

```python
# llama_defender/prefix_cache/rules/date_placeholder.py

import re
from datetime import datetime

class DatePlaceholderRule(StabilizationRule):
    """将所有日期字符串替换为固定占位符。"""
    
    name = "date_placeholder"
    priority = 10
    
    # 默认日期模式列表
    DEFAULT_PATTERNS = [
        r"\b\d{4}-\d{2}-\d{2}\b",              # ISO 8601: 2026-06-06
        r"\b\d{4}/\d{2}/\d{2}\b",              # Slash: 2026/06/06
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",        # US: 6/6/26
        r"\bJanuary|February|March|April|May|June|July|August|September|October|November|December\b \d{1,2},? \d{4}\b",
        # 中文日期:
        r"\d{4}年\d{1,2}月\d{1,2}日",
    ]
    
    def __init__(self, patterns: list[str] = None, placeholder: str = "<DATE>"):
        self.patterns = [re.compile(p) for p in (patterns or self.DEFAULT_PATTERNS)]
        self.placeholder = placeholder
        self.compiled = self.patterns
    
    def apply(self, content: str, context: dict) -> tuple[str, dict]:
        stats = {"dates_replaced": 0}
        for pattern in self.compiled:
            matches = pattern.findall(content)
            if matches:
                stats["dates_replaced"] += len(matches)
                content = pattern.sub(self.placeholder, content)
        return content, stats
```

**对比 vLLM `cache_salt`**: 我们不是隔离,而是**归一化**。两者互补,代理层归一化 → 后端按归一化后的 prompt 哈希 → cache 命中。

### 5.3 内置规则 2: DynamicVarRule (新增)

**动机**: Claude Code 的 system prompt 包含各种动态变量 (`$CLAUDE_PROJECT_DIR`, `$WORKSPACE`, 时间戳等),需归一化。

```python
# llama_defender/prefix_cache/rules/dynamic_var.py

import re
import os

class DynamicVarRule(StabilizationRule):
    """归一化环境变量、路径变量、时间戳等动态值。"""
    
    name = "dynamic_var"
    priority = 20  # 在 DatePlaceholder 之后
    
    DEFAULT_VARS = {
        r"\$CLAUDE_PROJECT_DIR": "<PROJECT_DIR>",
        r"\$CLAUDE_WORKSPACE": "<WORKSPACE>",
        r"\$HOME": "<HOME>",
        r"\$USER": "<USER>",
        r"\d{10,}": "<UNIX_TS>",  # Unix timestamp (>= 10 digits)
    }
    
    def __init__(self, var_map: dict = None):
        self.var_map = var_map or self.DEFAULT_VARS
        self.compiled = [(re.compile(p), v) for p, v in self.var_map.items()]
    
    def apply(self, content: str, context: dict) -> tuple[str, dict]:
        stats = {"vars_replaced": 0}
        for pattern, replacement in self.compiled:
            matches = pattern.findall(content)
            if matches:
                stats["vars_replaced"] += len(matches)
                content = pattern.sub(replacement, content)
        return content, stats
```

### 5.4 内置规则 3: ThinkingStripRule (L1530 升级版)

**当前问题**: 简单 `keep_recent=3`,但 thinking 内容会破坏 prefix 哈希。

**升级方案**: 在规则引擎中,**只 strip thinking content** (其他规则不动),保持其他稳定。

```python
# llama_defender/prefix_cache/rules/thinking_strip.py

import re

THINK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)
THINK_PATTERN_XML = re.compile(r"<thinking>.*?</thinking>", re.DOTALL)

class ThinkingStripRule(StabilizationRule):
    """从 assistant 消息中清除 thinking 内容(只保留最近 N 条)。"""
    
    name = "thinking_strip"
    priority = 30
    
    def __init__(self, keep_recent: int = 3):
        self.keep_recent = keep_recent
    
    def apply(self, content: str, context: dict) -> tuple[str, dict]:
        # 单条消息级别,thinking 在 assistant content 中
        content = THINK_PATTERN.sub("[thinking stripped]", content)
        content = THINK_PATTERN_XML.sub("[thinking stripped]", content)
        return content, {"thinking_stripped": 1}
    
    def apply_message_level(self, messages: list, context: dict) -> tuple[list, dict]:
        """消息级别:扫描所有 assistant 消息,strip 旧的 thinking。"""
        # ... 复用现有 strip_old_thinking_blocks 逻辑
        ...
```

### 5.5 内置规则 4: ClearedCompressRule (L1616 升级版)

**当前问题**: 启发式合并 `[cleared: ...]` 消息,无 cache 决策。

**升级方案**: 在规则引擎中,**保证 cleared 消息的内容确定性**。

```python
# llama_defender/prefix_cache/rules/cleared_compress.py

class ClearedCompressRule(StabilizationRule):
    """将 [cleared: original_len chars] 归一化。"""
    
    name = "cleared_compress"
    priority = 40
    
    def apply(self, content: str, context: dict) -> tuple[str, dict]:
        # 关键:不保留 original_len (每次清除长度不同)
        # 改为固定字符串,确保 cleared messages 跨请求一致
        cleared_pattern = re.compile(r"\[cleared[^\]]*\]")
        normalized = cleared_pattern.sub("[cleared]", content)
        return normalized, {"cleared_normalized": 1}
```

**对比当前实现**: 当前 L694 的 `[cleared file=src/main.py: 12543 chars. Preview: import os...]` 中:
- `12543 chars` 每次都不同 (原文件大小变化)
- `Preview: import os...` 内容变化

升级后:**所有 cleared 消息统一为 `[cleared]`**,**跨请求 100% 稳定**。

### 5.6 内置规则 5: ToolSchemaStabilizeRule (新增)

**动机**: 工具定义 schema 中 `description` 字段被 Claude Code 频繁修改(每次会话可能加新工具)。

```python
# llama_defender/prefix_cache/rules/tool_schema_stabilize.py

class ToolSchemaStabilizeRule(StabilizationRule):
    """稳定化 tools 数组:
    1. 按 name 排序
    2. 移除 description 中的不稳定字段
    3. 规范化 input_schema 字段顺序
    """
    
    name = "tool_schema_stabilize"
    priority = 5  # 最先执行
    
    def apply(self, content: str, context: dict) -> tuple[str, dict]:
        # 由 BlockBuilder 在 tools 阶段直接调用,不在 content 级别
        # 此处作为占位
        return content, {}
```

实际工作在 BlockBuilder 的 `_canonicalize_tools()` 中完成(见 §4.3)。

### 5.7 规则配置文件

```yaml
# configs/prefix_stabilization.yaml
prefix_stabilization:
  enabled: true
  block_size: 16
  hash_algo: sha256  # 或 sha256_cbor / xxhash
  
  rules:
    - name: date_placeholder
      enabled: true
      priority: 10
      params:
        patterns:
          - "\\b\\d{4}-\\d{2}-\\d{2}\\b"
          - "\\b\\d{4}/\\d{2}/\\d{2}\\b"
          - "\\d{4}年\\d{1,2}月\\d{1,2}日"
        placeholder: "<DATE>"
    
    - name: dynamic_var
      enabled: true
      priority: 20
      params:
        var_map:
          "\\$CLAUDE_PROJECT_DIR": "<PROJECT_DIR>"
          "\\d{10,}": "<UNIX_TS>"
    
    - name: thinking_strip
      enabled: true
      priority: 30
      params:
        keep_recent: 3
    
    - name: cleared_compress
      enabled: true
      priority: 40
      params: {}
    
    - name: tool_schema_stabilize
      enabled: true
      priority: 5
      params: {}
```

---

## 6. Cache Key 计算与命中追踪

### 6.1 Cache Key = 块链哈希 (Block Chain Hash)

借鉴 vLLM `block_hash = hash(parent_hash + block_tokens)` 思路:

```python
# llama_defender/prefix_cache/cache_key.py

def compute_cache_key(blocks: list[CanonicalBlock]) -> str:
    """缓存键 = 所有块的最终 hash 链 + extra 字段。
    
    注意:不是简单拼接,而是递归:
    key[n] = hash(key[n-1] + block[n])
    """
    if not blocks:
        return ""
    key = blocks[0].parent_hash or ""
    for block in blocks:
        key = compute_block_hash(
            parent_hash=key,
            block_content=block.content_canonical,
            extra_hashes=block.extra_hashes,
        )
    return key
```

**示例**:
```
请求 1:
  block[0] = system "You are helpful"        → hash0
  block[1] = tools_schema (12 个工具)        → hash1 = hash(hash0 + tools)
  block[2] = user_text "What is 2+2?"        → hash2 = hash(hash1 + text)
  最终 cache_key = hash2

请求 2 (同一 system + tools,不同问题):
  block[0] = system "You are helpful"        → hash0  (同!)
  block[1] = tools_schema (12 个工具)        → hash1  (同!)
  block[2] = user_text "What is 3+3?"        → hash2' (不同)
  
→ 共同前缀 hash0 + hash1 完全一致
→ 后端 prefix cache 命中 2 个块!
```

### 6.2 命中率埋点 (Metrics)

```python
# llama_defender/prefix_cache/stats.py

@dataclass
class PrefixCacheStats:
    request_id: str
    total_blocks: int
    stable_blocks: int  # 跨请求不变的块数
    common_prefix_chars: int
    common_prefix_ratio: float  # = stable_blocks / total_blocks
    cache_key: str  # 本请求的最终 hash
    parent_cache_key: str | None  # 上一请求的 hash(用于对比)
    rule_stats: dict[str, dict]  # 规则应用统计
    duration_ms: float
    
    def quality_flags(self) -> list[str]:
        flags = []
        if self.common_prefix_ratio < 0.5:
            flags.append("low_prefix_reuse")
        if self.common_prefix_ratio > 0.9:
            flags.append("high_prefix_reuse")
        if not self.parent_cache_key:
            flags.append("first_request_in_session")
        return flags
```

**输出到 logs/proxy_metrics.jsonl**:
```json
{
  "ts": "2026-06-06T15:00:00",
  "session_id": "abc12345",
  "prefix_cache": {
    "total_blocks": 156,
    "stable_blocks": 142,
    "common_prefix_ratio": 0.91,
    "cache_key": "sha256:abc123...",
    "parent_cache_key": "sha256:def456...",
    "rule_stats": {
      "date_placeholder": {"dates_replaced": 3},
      "dynamic_var": {"vars_replaced": 7},
      "cleared_compress": {"cleared_normalized": 12}
    },
    "duration_ms": 1.2
  },
  "quality_flags": ["high_prefix_reuse"]
}
```

**新的 metrics 字段**: 替换当前**失效的** `re_read_rate=2862%` (DEF-003),用 `common_prefix_ratio` 真正反映 prefix 复用度。

---

## 7. 与后端 Prefix Cache 的协同

### 7.1 双层优化模型

```
[代理层] (本设计)
  输入: Claude Code 的 messages
  操作: 应用 5 个稳定化规则 → 计算块哈希 → 记录命中率
  输出: 稳定化后的 messages (送入后端)
  
       ↓
       
[后端层] (vLLM / SGLang / rapid-mlx)
  输入: 稳定化后的 messages
  操作: tokenizer → 块哈希(基于 token) → prefix cache 查找
  输出: KV cache 复用,TTFT 降低
```

**关键**: 代理层和后端的哈希**不必一致**。代理层用 char-block(16 chars),后端用 token-block(16 tokens)。两者的**共同前缀长度**会**正相关**:
- 代理层 common_prefix_ratio = 0.9
- 后端 prefix cache 命中率 ≈ 0.85-0.9 (略低,因为 token 切分不同)

### 7.2 vLLM/SGLang 场景下的额外收益

当后端是 vLLM (开启 APC) 或 SGLang (开启 RadixAttention):
- **代理层稳定化** → 后端收到的 prompt 更稳定
- **后端 prefix cache** → KV cache 复用最大化
- **双层协同**: 代理层把"漂移"消除,后端把"匹配"加速

**实测预测** (待验证):
- 代理层 common_prefix_ratio = 0.9 → 后端 cache hit 率 = 85-90%
- TTFT: cache hit 1-3s, cache miss 90s (兜底)

### 7.3 rapid-mlx 场景下的当前问题

来自 `docs/rapid-mlx-cache-analysis.md`:
- v0.6.30: MoE `ArraysCache` non-trimmable,prefix cache LCP 找到后被强制跳过
- v0.6.71: 修复 MoE non-trimmable,prefix cache 90-99% 命中

**我们的设计假设**: **v0.6.71 已升级**。若 v0.6.30:
- 代理层稳定化收益 = 0 (后端根本 miss)
- 升级到 v0.6.71 后,**代理层 + 后端双重优化生效**

---

## 8. 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_PREFIX_STABILIZATION_ENABLED` | `true` | 总开关 |
| `PROXY_PREFIX_BLOCK_SIZE` | `16` | 块大小 (chars) |
| `PROXY_PREFIX_HASH_ALGO` | `sha256` | 哈希算法 |
| `PROXY_PREFIX_CACHE_SALT` | `""` | 多租户隔离 salt |
| `PROXY_PREFIX_LOG_INTERVAL` | `100` | 每 N 个请求输出一次 stats 摘要 |
| `PROXY_PREFIX_STATS_PATH` | `logs/prefix_cache_stats.jsonl` | 详细 stats 输出 |

**规则独立开关** (按 YAML 配置):
- `date_placeholder.enabled`
- `dynamic_var.enabled`
- `thinking_strip.enabled`
- `cleared_compress.enabled`
- `tool_schema_stabilize.enabled`

---

## 9. 实施计划 (4 个 Phase)

### Phase 1: 核心数据结构 (Week 1-2)

**目标**: 建立 `llama_defender/prefix_cache/` 模块骨架

| 任务 | 工时 | 验收 |
|------|------|------|
| `block_builder.py` 实现 | 3 天 | 单元测试覆盖 system/tools/messages 拆解 |
| `hash.py` 实现 | 1 天 | 与 vLLM sha256 / sha256_cbor 兼容 |
| `cache_key.py` 实现 | 1 天 | 验证分层 hash 正确性 |
| 单元测试 | 2 天 | 100+ cases |

**关键决策**: **不立即**迁移 anthropic_proxy.py 的 L1530/L1616/L3071/L3127,而是**并行运行**(老逻辑保留,新逻辑作为可选)。

### Phase 2: 规则引擎 (Week 3-4)

**目标**: 实现 5 个内置规则

| 任务 | 工时 | 验收 |
|------|------|------|
| `DatePlaceholderRule` | 0.5 天 | 单元测试 5+ 模式 |
| `DynamicVarRule` | 0.5 天 | 单元测试 4+ 变量 |
| `ThinkingStripRule` | 1 天 | 复用 L1530 逻辑,加埋点 |
| `ClearedCompressRule` | 0.5 天 | 单元测试 cleared 模式 |
| `ToolSchemaStabilizeRule` | 1 天 | 集成到 BlockBuilder |
| YAML 配置加载 | 1 天 | 单元测试 config 解析 |

### Phase 3: Stats + 集成 (Week 5-6)

**目标**: 埋点 + 在代理层主流程集成

| 任务 | 工时 | 验收 |
|------|------|------|
| `stats.py` 实现 | 1 天 | 输出到 proxy_metrics.jsonl |
| 与 `proxy_metrics.jsonl` 集成 | 1 天 | 验证 DEF-003 修复 |
| 与 anthropic_proxy.py 集成 | 2 天 | 灰度开关 (A/B 测试) |
| 与 R3.2 老逻辑对比 | 1 天 | 验证 common_prefix_ratio 提升 |

### Phase 4: 上线 + 监控 (Week 7-8)

**目标**: 替换 R3.2 老逻辑

| 任务 | 工时 | 验收 |
|------|------|------|
| 老逻辑 (L1530/L1616/L3071/L3127) 标记 DEPRECATED | 0.5 天 | 文档 + 警告日志 |
| 默认启用新规则 | 0.5 天 | 配置默认 true |
| Prometheus 指标 | 1 天 | 暴露 `common_prefix_ratio` |
| 文档更新 | 0.5 天 | PRD/AGENTS.md |

---

## 10. 测试计划

### 10.1 单元测试 (目标 ≥ 80% 覆盖率)

```python
# test/unit/test_prefix_cache.py

class TestDatePlaceholderRule:
    def test_iso_date_replaced(self):
        rule = DatePlaceholderRule()
        content, stats = rule.apply("Today is 2026-06-06", {})
        assert "<DATE>" in content
        assert stats["dates_replaced"] == 1
    
    def test_chinese_date_replaced(self):
        rule = DatePlaceholderRule()
        content, stats = rule.apply("今天是2026年6月6日", {})
        assert "<DATE>" in content
    
    def test_unix_timestamp_replaced(self):
        rule = DatePlaceholderRule()
        content, stats = rule.apply("ts=1717654321", {})
        # timestamp 由 DynamicVarRule 处理,date rule 不动
        assert "1717654321" in content

class TestHierarchicalBlockHash:
    def test_parent_hash_propagation(self):
        h1 = compute_block_hash(None, "A gentle breeze stirred")
        h2 = compute_block_hash(h1, "the leaves as children")
        h3 = compute_block_hash(h2, "laughed in the distance")
        # 改变第 2 块,后续 hash 全部变化
        h2_alt = compute_block_hash(h1, "the leaves as adults")
        h3_alt = compute_block_hash(h2_alt, "laughed in the distance")
        assert h3 != h3_alt
    
    def test_cache_salt_isolation(self):
        h1 = compute_block_hash(None, "data", extra_hashes={"cache_salt": "user1"})
        h2 = compute_block_hash(None, "data", extra_hashes={"cache_salt": "user2"})
        assert h1 != h2  # 多租户隔离

class TestBlockBuilder:
    def test_system_always_first(self):
        body = {"system": [{"text": "You are helpful"}], "messages": [...]}
        blocks = BlockBuilder().build(body)
        assert blocks[0].block_type == "system"
    
    def test_tools_sorted_by_name(self):
        body = {"tools": [{"name": "Read"}, {"name": "Bash"}]}
        blocks = BlockBuilder().build(body)
        # 即便输入顺序乱,canonical 后的 tools_schema 总是 sorted
    
    def test_message_split_preserves_blocks(self):
        long_text = "x" * 100  # 100 chars
        blocks = BlockBuilder().build({"messages": [{"role": "user", "content": long_text}]})
        # 应被切分为 ceil(100/16) = 7 个块

class TestCacheKey:
    def test_identical_prompts_same_key(self):
        body = {...}
        blocks1 = BlockBuilder().build(body)
        blocks2 = BlockBuilder().build(body)
        assert compute_cache_key(blocks1) == compute_cache_key(blocks2)
    
    def test_different_user_query_different_key(self):
        body1 = {... "messages": [{"role": "user", "content": "Q1"}]}
        body2 = {... "messages": [{"role": "user", "content": "Q2"}]}
        assert compute_cache_key(blocks1) != compute_cache_key(blocks2)
        # 但前 N 个块(系统+工具)相同
        assert blocks1[0] == blocks2[0]
```

### 10.2 集成测试 (目标: 真实 Claude Code 会话)

```python
# test/integration/test_prefix_stabilization.py

def test_real_claude_code_session_stability():
    """运行 10 轮 Claude Code 会话,验证 prefix reuse 趋势。"""
    proxy = start_test_proxy()
    common_ratios = []
    
    for i in range(10):
        response = send_claude_code_request(proxy, ...)
        stats = get_stats_from_log(proxy, i)
        common_ratios.append(stats.common_prefix_ratio)
    
    # 期望: 后续请求的 ratio 应 > 0.7 (相比当前 24%)
    assert common_ratios[-1] > 0.7
    assert statistics.mean(common_ratios) > 0.5

def test_tool_schema_change_should_not_break_existing_cache():
    """新增工具不应破坏已有工具的 prefix cache。"""
    body1 = {"tools": [tool_a, tool_b]}
    body2 = {"tools": [tool_a, tool_b, tool_c_new]}  # 新增 tool_c
    
    blocks1 = BlockBuilder().build(body1)
    blocks2 = BlockBuilder().build(body2)
    
    # tool_a 和 tool_b 块应该完全相同
    assert blocks1[0] == blocks2[0]
    assert blocks1[1] == blocks2[1]
```

### 10.3 A/B 测试 (目标: 修复 DEF-102 / DEF-003)

| 组 | 配置 | 预期结果 |
|---|------|---------|
| A (对照) | 现行 R3.2 (L1530/L1616/L3071/L3127) | 24% 共同前缀,re_read_rate=2862% |
| B (新) | 规则引擎 + 分层块哈希 | 70-90% 共同前缀,re_read_rate ≤ 100% |

---

## 11. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 规则改动破坏 prompt 语义 | 中 | 高 | 灰度发布 (A/B),先 1% 流量,观察 1 周 |
| SHA256 哈希开销 (>1ms/请求) | 低 | 中 | 块大小调优,异步计算 |
| 规则顺序错误 (priority 错) | 中 | 中 | 单元测试覆盖所有顺序排列 |
| 工具 description 变长,块数爆炸 | 低 | 低 | 块大小限制,合并相邻块 |
| 用户自定义规则引入 | 低 | 中 | 沙箱机制,不允许 exec/import |
| 缓存未清理 (cache_salt 不生效) | 低 | 高 | 单元测试 multi-tenant 隔离 |

---

## 12. 与现有 R3.2 的迁移路径

### 12.1 渐进式迁移

**Week 1-2 (Phase 1)**: 在 anthropic_proxy.py 中**新增** Layer 3.5 调用,但默认 `enabled=false`:
```python
# anthropic_proxy.py _handle_messages() 新增
if PROXY_PREFIX_STABILIZATION_ENABLED:
    from llama_defender.prefix_cache import StabilizationEngine
    engine = StabilizationEngine.from_config(...)
    body, prefix_stats = engine.stabilize(body, context)
    # 记录 stats
```

**Week 3-4 (Phase 2)**: 灰度开启 (10% 流量),观察 common_prefix_ratio

**Week 5-6 (Phase 3)**: 全量开启,**但保留** R3.2 老逻辑 (作为兜底)

**Week 7-8 (Phase 4)**: 老逻辑标记 DEPRECATED,新规则为默认

### 12.2 回滚机制

若新规则破坏 prompt 语义:
- 环境变量 `PROXY_PREFIX_STABILIZATION_ENABLED=false` → 立即关闭
- 旧逻辑 (L1530/L1616/L3071/L3127) 始终保留,**保证回滚后行为不变**

### 12.3 监控指标

上线后必须监控:

| 指标 | 健康阈值 | 告警阈值 |
|------|----------|----------|
| `common_prefix_ratio` | > 0.7 | < 0.5 |
| `stable_blocks / total_blocks` | > 0.6 | < 0.4 |
| `prefix_stabilize_duration_ms` | < 5ms | > 20ms |
| `cache_key_collision_rate` (sha256) | ~0 | > 0.0001% |
| **TTFT** (与 prefix cache 直接相关) | < 5s | > 30s |

---

## 13. 未来扩展 (v2.0+)

### 13.1 跨会话 Prefix Sharing

当前 `cache_key` 是 per-session 的。若两个 Claude Code 项目使用**相同的 system + tools** (例如都用 Read/Write/Edit/Bash),它们的 prefix 仍可共享。

**实现**: 增加 `cache_namespace` 字段,基于 system prompt 的 SHA256:
```python
cache_key_with_namespace = f"{namespace}:{cache_key}"
```

### 13.2 增量编译 (Incremental Compilation)

每次请求**只计算新块的 hash**,复用已计算块:
```python
# 伪代码
def incremental_hash(blocks: list, prev_blocks: list) -> str:
    common = longest_common_prefix(blocks, prev_blocks)
    if common == len(blocks):
        return prev_cache_key  # 全部复用
    return compute_from(common, blocks)  # 只算新的部分
```

性能提升: 50-100x (典型 agent 会话中,前 80% 块不变)。

### 13.3 自适应块大小

当前固定 `BLOCK_SIZE=16`。未来可基于消息类型自适应:
- system prompt: 块大小 64 (低频变化)
- tool_result: 块大小 8 (高频变化)
- user text: 块大小 16 (默认)

### 13.4 与 vLLM/llama.cpp KV Cache File 集成

llama.cpp 已支持 `--prompt-cache` 持久化。**未来**: 代理层可直接写 `~/.cache/llama.cpp/prompt_cache.bin`,**跨重启保留** prefix cache。

---

## 14. 总结

### 14.1 核心创新点

1. **借鉴 vLLM 块哈希算法到代理层** — 业界首次 (vLLM 是后端,Anthropic 是 API,我们是代理)
2. **5 个内置规则引擎** — 替代 4 个 ad-hoc 实现,可配置可扩展
3. **真正的 common_prefix_ratio metrics** — 修复 DEF-003 (re_read_rate=2862%)
4. **双层优化模型** — 代理层稳定化 + 后端 prefix cache 协同
5. **零依赖** — 仅用 stdlib,无 tokenizer,无云 API

### 14.2 与 R3.2 的对比

| 维度 | R3.2 (当前) | 本设计 |
|------|-----------|--------|
| 规则数 | 4 (硬编码) | 5+ (可配置) |
| 哈希算法 | 无 | sha256 (抗碰撞) |
| 跨租户隔离 | 无 | `cache_salt` |
| Metrics | `re_read_rate` 失效 | `common_prefix_ratio` 真实 |
| 块粒度 | 全文 / 整消息 | 16 char 细粒度 |
| 工具列表 | 未处理 | 排序 + 规范化 |
| 与后端协同 | 隐式 | 显式双层优化 |
| 代码量 | ~150 行 (4 处) | ~800 行 (集中) |

### 14.3 一句话总结

> **本设计把 R3.2 的 4 个 ad-hoc 占位文本规则,重构为 vLLM 启发的 5 规则 + 分层块哈希引擎,首次在代理层实现 prefix cache 稳定化,填补 OSS 生态空白,为 llama_defender 库提供核心模块。**

---

## 15. 关联文档

- `docs/PRD-anthropic-proxy.md` — R3.2 当前定义
- `docs/DEFECT-LIST.md` — DEF-003 (re_read_rate 失效) / DEF-102 (rounds 未生效) / DEF-202 (cleared dedup 反复触发)
- `docs/prefix-cache-analysis-20260605.md` — 后端 prefix cache 现状
- `docs/PM-ANALYSIS-FUTURE-ROADMAP.md` — llama_defender 库化方向
- `docs/OSS-REPLACEMENT-EVALUATION.md` — vLLM APC 作为参考的来源
- vLLM 设计文档: https://docs.vllm.ai/en/latest/design/prefix_caching.html
- SGLang RadixAttention 论文: https://arxiv.org/abs/2312.07104
- Anthropic Prompt Caching: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching

---

> **设计版本**: v1.0  
> **目标实施时间**: 8 周 (Phase 1-4)  
> **关联 commit**: `6060552` (prefix cache analysis), `08925bb` (HEAD=6), `a6952e6` (static placeholder)  
> **下一步**: 启动 Phase 1,创建 `llama_defender/prefix_cache/` 目录骨架
