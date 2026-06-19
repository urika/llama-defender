# DCP 动态上下文剪枝 vs 当前代理层上下文压缩策略对比

> 来源：OpenCode Dynamic Context Pruning（https://github.com/Opencode-DCP/opencode-dynamic-context-pruning）
> 对比对象：本项目 `anthropic_proxy.py` 8 层代理管线（Layer 2 内容压缩 + Layer 5 上下文截断）
> 日期：2026-06-18
> DCP 版本：latest (OpenCode plugin, npm `@tarquinen/opencode-dcp`)
> Proxy 版本：当前 main 分支 (v0.5.6+)

---

## 1. 架构定位差异

| 维度 | DCP | Anthropic Proxy |
|------|-----|-----------------|
| **运行位置** | OpenCode 插件层（客户端侧 Hook） | 代理层（客户端与后端之间） |
| **触发方式** | 模型主动调用 `compress` 工具 + 自动清理 | 请求拦截，规则驱动（每条请求经过 pipeline） |
| **可见性** | 模型完全感知压缩过程和结果 | 模型无感知（透明截断/清除） |
| **会话历史** | 不修改原始历史，用占位符替换出站副本 | 直接修改消息列表（in-place mutation）后转发 |
| **缓存策略** | 接受约 5% 缓存命中下降（90%→85%） | 严格保护前缀缓存（Frozen Zone、固定占位符文本） |
| **目标场景** | 云 API 通用场景（Claude、GPT、Copilot、OpenCode 云端） | 本地 Apple Silicon 部署（48GB 统一内存，Qwen 系列） |

**核心定位差异**：DCP 把上下文管理权部分交给模型，追求压缩质量与 token 效率；Proxy 作为本地推理的安全网，追求确定性、缓存稳定性和 OOM 防护。

---

## 2. DCP 策略总览

DCP（Dynamic Context Pruning）是 OpenCode 的一款插件，目标是在**不修改会话原始历史**的前提下，动态减少发往 LLM 的上下文 token 量。

### 2.1 核心组件

| 组件 | 作用 | 触发方式 |
|------|------|----------|
| **Compress 工具** | 将已完成、陈旧的对话区间替换为高保真技术摘要 | 模型主动调用（基于任务完成度），或由上下文压力 nudge 触发 |
| **Deduplication** | 相同工具 + 相同参数的重复调用只保留最新输出 | 自动，每次 compress 时重算 |
| **Purge Errors** | N 轮前的错误工具输入被剪枝，仅保留错误消息 | 自动，默认 4 轮 |
| **Supersede Writes** | 文件被后续读取后，旧的 write 输入可剪枝 | 自动 |
| **Nudge 机制** | 在上下文接近阈值时向模型注入压缩提示 | 自动，基于 min/maxContextLimit 和 nudgeFrequency |

### 2.2 Compress 工具的工作方式

DCP 把 `compress` 暴露为模型可调用的工具，支持两种模式：

- **range 模式**：压缩一段连续的消息区间，生成一个或多个摘要块。新的压缩若与旧摘要重叠，旧摘要会被**嵌套**进新摘要，避免信息随多次压缩被稀释。
- **message 模式（实验性）**：逐条压缩单条原始消息，粒度更细，让模型像做手术一样管理上下文。

压缩时保护以下内容：

- 指定工具的输出（默认保护 `task`, `skill`, `todowrite`, `todoread`, `compress`, `batch`, `plan_enter`, `plan_exit`, `write`, `edit`）
- 子代理和 skill 的输出
- 用户通过 `protectedFilePatterns` 指定的文件操作
- 用户消息（可选 `protectUserMessages`）
- `<protect>` 标签包裹的内容

### 2.3 非破坏性转换

DCP 的关键设计：**原始会话历史永远不被修改**。所有剪枝/压缩只作用于**即将发往 LLM 的出站消息副本**。这意味着：

- 本地历史保持完整，可审计、可回退。
- 摘要块引用稳定的 message ID（如 `m0001`）和 compression block ID（如 `b1`），模型可以明确知道哪些内容被折叠了。

### 2.4 默认配置要点

```jsonc
{
  "compress": {
    "permission": "allow",
    "minContextLimit": 50000,
    "maxContextLimit": 150000,
    "nudgeFrequency": 5,
    "iterationNudgeThreshold": 15,
    "nudgeForce": "soft",
    "protectUserMessages": false,
    "protectedTools": []
  },
  "strategies": {
    "deduplication": { "enabled": true },
    "supersedeWrites": { "enabled": true },
    "purgeErrors": { "enabled": true, "turns": 4 }
  }
}
```

- `minContextLimit`：低于此值不提醒压缩；到达/超过后开启 turn/iteration nudge。
- `maxContextLimit`: 超过后强力注入压缩提示，大幅提高压缩概率。
- 阈值支持绝对字符数或模型上下文窗口百分比。

---

## 3. 当前项目（anthropic_proxy.py）策略总览

本项目的代理层没有模型可主动调用的上下文管理工具，而是采用**被动、自动、基于字符阈值**的 8 层管线，在请求进入后端前统一处理。其中与 DCP 对应的是 **Layer 2 内容压缩** 和 **Layer 5 上下文截断**。

### 3.1 生命周期阶段驱动的统一阈值

所有压缩/截断力度由 `_classify_lifecycle_stage()` 根据总字符数单调递增决定：

```
chars →    15K       40K        90K        180K       350K     400K
           │         │          │           │          │        │
Stage:   INIT     GROWTH    EXPANSION   SATURATION  OOM_DANGER  PRE_TRUNC
L2+L4:   跳过    尾40%清除   尾60%+     全dynamic+   全量+     全量+
                 (无think)  think=5    think=3     think=1   think=1
L5截断:   关       关       预算触发    rounds=8   rounds=3  rounds=2
Frozen:   12       12         12          6          0         0
```

### 3.2 Layer 2：内容压缩（_compress_content_pass）

单次遍历完成：

- **错误翻译**：把后端英文错误改写为中文自然语言提示（`Wasted call` → 不要反复读取）。
- **工具内容清除**：用 `[cleared: ...]` 占位符替换旧 `tool_result` 内容。
  - 按语义评分保留高价值结果（Read/Agent 优先级高、代码/错误加分、近期 Read 额外加分）。
  - Read 结果保留前 200 字符预览，降低重读循环。
  - Frozen Zone（默认前 12 条）保护 prefix cache 稳定。
- **Thinking 剥离**：清除旧 `reasoning_content` / `thinking` blocks。
- **Bash 去重**：Jaccard ≥ 0.7 的连续 Bash 输出合并。

### 3.3 Layer 5：上下文截断（truncate_messages_if_needed）

提供三种策略：

| 策略 | 说明 |
|------|------|
| **rounds** | 保留最近 N 轮 assistant 对话 + HEAD，MIDDLE 压缩摘要（默认） |
| **fifo** | 保留 HEAD + TAIL 固定条数，中间直接丢弃 |
| **char** | 丢弃最旧中间消息直到总字符低于阈值 |

截断后的 MIDDLE 通过**四级压缩链**处理：

1. **增量压缩**：会话级 `_summary_cache`，只压缩新增 dropped 消息。
2. **LLM 压缩**：调用本地模型生成结构化摘要（30s 超时，失败降级）。
3. **规则压缩**：提取错误、代码状态、文件变更、决策等结构化信息。
4. **静态折叠**：`[Context folded: N messages dropped]`。

并支持：

- **Read 结果智能保留**：`rounds` 策略下，从 dropped 区间提取所有 Read tool_result 并完整保留，避免 DCP 式"清除后重读"问题。
- **关键词索引注入**：从 dropped 消息提取文件名/错误类型/函数名，匹配当前 tail 后注入相关历史。
- **严重截断通知**：丢弃比例 > 85% 时注入 `[System: Context severely truncated]`。

### 3.4 Layer 3：循环与阻塞检测

DCP 没有的主动干预层：

- **精确/模式/文本循环检测**：连续相同工具调用或相似文本触发三级干预（提示 → 移除工具 → 强制纯文本）。
- **阻塞检测**：连续 N 次相同错误类型注入 `[BLOCKER]` 提示。
- **Re-read 检测**：检测模型是否尝试读取已被清除的文件，注入 HARD BLOCK。

### 3.5 工具定义过滤（Layer 6）

44 个工具 → 通过白名单 + 最近使用扫描压缩到约 15 个，节省 5-8K tokens。

### 3.6 请求级去重（Layer 1）

`_check_dedup()` 对 2s 窗口内的相同请求体（MD5 哈希）返回 429，防止客户端重试导致重复转发。这是**请求级**去重，不是**工具调用级**去重。

---

## 4. 核心策略对比

### 4.1 压缩（Compression）

| 维度 | DCP | Anthropic Proxy |
|------|-----|-----------------|
| **决策者** | **模型自主决策**何时压缩、压缩哪些内容 | **规则驱动**，基于字符数阈值的生命周期阶段 |
| **压缩粒度** | `range` 模式压缩连续消息跨度；`message` 模式压缩单条消息 | **分层多策略**：L2 tool-result 清除 + L4 thinking 剥离 + L5 消息截断 |
| **摘要生成** | **由调用 compress 工具的模型自身生成**（高质量技术摘要） | **LLM 压缩**（优选，30s 超时）→ **规则提取**（fallback）→ **折叠占位符**（最终 fallback） |
| **增量压缩** | 新压缩嵌套旧摘要（信息层层保留） | `_incremental_compress`：新摘要 LLM 合并到缓存摘要 |
| **保护机制** | `protectedTools`、`protectedFilePatterns`、`protectUserMessages`、`<protect>` 标签 | Frozen Zone（前缀保护）、语义优先级评分、Read 结果预览保留 |

**关键差异**：DCP 的压缩质量更高（由大模型自身生成摘要），但依赖模型配合；Proxy 的压缩是确定性的，不依赖模型行为，但摘要质量受限于规则/小模型。

### 4.2 去重（Deduplication）

| 维度 | DCP | Anthropic Proxy |
|------|-----|-----------------|
| **检测范围** | **跨整个会话**，工具名+参数签名匹配 | 仅有**请求级**去重（2s TTL 窗口，MD5 哈希匹配）；**无工具调用级去重** |
| **检测方法** | `工具名::JSON.stringify(sorted(params))` | `hashlib.md5(request_body)` |
| **处理方式** | 标记旧调用为 pruned，保留最新结果 | 直接拒绝重复请求（返回 429） |
| **保护机制** | 排除受保护工具、受保护文件路径 | 无（仅请求级别） |
| **触发时机** | compress 工具调用时重新计算 | 每个请求到达时检查 |

**关键差异**：DCP 的去重是语义级的（理解参数含义），Proxy 的请求级去重只能检测完全相同的请求体。Proxy 缺少工具调用级去重，这是可借鉴的改进点。

### 4.3 错误清理（Purge Errors / Blocker Detection）

| 维度 | DCP (Purge Errors) | Proxy (Blocker Detection) |
|------|---------------------|---------------------------|
| **触发条件** | 工具调用出错后 ≥N 轮（默认 4 轮） | 连续 N 次相同错误类型（默认 2 次） |
| **处理方式** | 清除工具输入内容，保留错误消息 | 注入 `[BLOCKER]` 用户消息，引导模型切换工具 |
| **错误分类** | 只要 status==="error" 即触发 | 三类：wasted / file_not_found / input_validation |
| **干预程度** | 被动（仅清理） | 主动（注入提示 + 解决方案建议） |

**关键差异**：DCP 是「静默清理」，Proxy 是「主动干预」。Proxy 的 blocker 机制更激进，直接中止模型的错误循环；但 Proxy 缺少对旧错误输入的静默清理。

### 4.4 上下文限制管理（Context Limit）

| 维度 | DCP | Anthropic Proxy |
|------|-----|-----------------|
| **阈值模型** | 双层软阈值：`minContextLimit`(50K) / `maxContextLimit`(150K) | **六阶段生命周期**：INIT→GROWTH→EXPANSION→SATURATION→OOM_DANGER→PRE_TRUNC |
| **Nudge 机制** | 超过 minContextLimit 开始注入压缩提醒；超过 maxContextLimit 加强提醒频率 | 无 nudging（模型不感知）；规则自动执行 |
| **预算单位** | Token（模型原生） | **字符数**（`_estimate_message_chars()`，免 tokenizer 依赖） |
| **阶段感知** | 无阶段概念 | 每阶段有不同策略强度（frozen_head、clear_zone_pct、thinking_keep、truncate_rounds） |

### 4.5 截断策略（Truncation）

| 维度 | DCP | Anthropic Proxy |
|------|-----|-----------------|
| **策略** | 无独立截断策略（依赖 compress 工具主动压缩） | **三种策略**：`rounds`（按轮次）、`fifo`（按消息数）、`char`（按字符阈值） |
| **smart 策略** | — | system 消息 + tool_result 消息优先保留；assistant 推理文本压缩为占位符 |
| **Read 保护** | — | 截断时保留被丢弃区中的 Read tool_result（防止 re-read 循环） |
| **关键词索引** | — | BM25 MVP：从丢弃消息中提取关键词，注入相关上下文到保留尾部 |

### 4.6 会话连续性（Session Continuity）

| 维度 | DCP | Anthropic Proxy |
|------|-----|-----------------|
| **会话追踪** | OpenCode 原生 session ID | `_SESSION_REQUEST_COUNT` 字典 |
| **续接检测** | 无特殊处理 | 监测 session 已积累请求数，续接时自动升级为激进配置（frozen_head=2） |
| **跨请求缓存** | 无 | `_summary_cache`（按 session_id，最多 10 个 session） |

---

## 5. 策略矩阵总览

```
                        DCP          Proxy
                        ───          ─────
模型驱动压缩             ✅            ❌ (规则驱动)
LLM 生成摘要             ✅ (大模型)    ✅ (小模型/规则 fallback)
规则生成摘要             ❌            ✅
Tool-result 清除         ❌            ✅ (语义评分)
Thinking 块剥离          ❌            ✅
消息截断                 ❌            ✅ (3 策略)
Frozen Zone (前缀保护)   ❌            ✅
请求级去重               ❌            ✅ (MD5, 2s 窗口)
工具调用级去重           ✅            ❌ (可借鉴)
错误输入清理             ✅            ❌ (可借鉴)
Blocker 主动干预         ❌            ✅ (中文提示)
生命周期阶段感知         ❌            ✅ (6 阶段)
工具定义过滤             ❌            ✅
文本循环检测             ❌            ✅
OOM 安全防护             ❌            ✅ (硬上限)
会话续接检测             ❌            ✅
Nudge/提示注入           ✅            ❌
Per-model 阈值覆盖       ✅            ❌ (可借鉴)
Protected tag (<protect>) ✅           ❌ (可借鉴)
压缩块 ID / 嵌套摘要     ✅            ❌ (可借鉴)
```

---

## 6. 优劣势总结

### 6.1 DCP 优势

1. **模型驱动的压缩更智能**：模型知道「当前任务已完成」，能在最佳时机触发压缩，生成高质量技术摘要。
2. **非破坏性历史**：原始会话永远完整，便于审计、调试和回退。
3. **嵌套摘要避免信息稀释**：多次压缩不会把旧摘要压成无意义碎片。
4. **自动策略覆盖常见浪费**：deduplication、purge errors、supersede writes 都是零成本收益。
5. **可配置程度高**：支持模型特定阈值、自定义 prompt、手动模式、文件模式保护、`<protect>` 标签。

### 6.2 DCP 劣势

1. **依赖模型配合**：如果模型不主动调用 compress，效果会打折扣（虽然 nudge 可以缓解）。
2. **破坏 prefix cache**：每次压缩都会改变后续前缀，本地后端对 cache 敏感时代价高。
3. **不解决本地后端 OOM**：它优化的是 token 量，不是并发或峰值内存。
4. **无循环/阻塞干预**：重复错误和工具循环需要依赖模型自身或上游客户端处理。

### 6.3 当前项目代理层优势

1. **本地后端适配强**：生命周期阶段、Frozen Zone、并发控制、输出截断都是为了 rapid-mlx/llama-server 的稳定性设计。
2. **积极的循环/阻塞干预**：三层循环干预 + Blocker 检测 + Re-read HARD BLOCK，能打断模型自陷。
3. **Read 结果智能保留**：`rounds` 策略下完整保留 dropped 区间的 Read 输出，避免"清除 → 重读"死循环。
4. **Cache 友好**：日期标准化、Frozen Zone 保护前缀，适合本地 prefix cache。
5. **无需模型配合**：所有压缩/截断在代理层静默完成，对客户端透明。

### 6.4 当前项目代理层劣势

1. **压缩被动粗糙**：按字符阈值一刀切，容易在不必要的时候压缩，或该压缩时不够精细。
2. **摘要质量依赖 LLM/规则**：LLM 压缩有 30s 超时和失败降级，规则压缩信息密度有限。
3. **历史不可回退**：代理层修改后的消息直接发给后端，没有稳定的 block ID 供引用。
4. **无自动工具调用级 dedup/purge errors/supersede writes**：重复工具输出和旧错误输入不会自动清理。
5. **用户无法手动干预**：没有 `/compress` 或类似入口。

---

## 7. 可借鉴到当前项目的改进点（含实现细节）

以下改进按落地难度分层。所有短期改进都可以在 `anthropic_proxy.py` 现有管线内完成，无需改动外部协议；中期改进需要新增工具或会话状态。

---

### 7.1 短期可落地（无需架构大改）

#### 7.1.1 自动工具调用级 Deduplication（重复工具输出去重）

**目标**：当同一工具以相同参数被多次调用时，只保留最新一次 `tool_result`，旧输出替换为占位。

**接入位置**：在 `_compress_content_pass()` 的 Phase 2a 中、语义评分之前新增一个预处理步骤。

**实现步骤**：

1. **新增配置常量**（文件顶部，与 `PROXY_CLEAR_ENABLED` 相邻）：

```python
PROXY_DEDUP_ENABLED = os.environ.get("PROXY_DEDUP_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_DEDUP_TOOLS = set(os.environ.get("PROXY_DEDUP_TOOLS", "Bash,Read,Glob,Grep").split(","))
# 受保护不去重的工具（例如 Agent 子代理输出通常唯一且重要）
PROXY_DEDUP_PROTECTED = set(os.environ.get("PROXY_DEDUP_PROTECTED", "Agent,Task,EnterPlanMode,ExitPlanMode").split(","))
```

2. **新增辅助函数**（放在 `_compress_content_pass` 附近）：

```python
def _normalize_tool_args(tool_name, inp):
    """生成用于去重的稳定 key。对 Read/Write/Edit 只保留文件路径，对 Bash 规范化空白。"""
    if not isinstance(inp, dict):
        return str(inp)
    if tool_name in ("Read", "Write", "Edit"):
        return inp.get("file_path") or inp.get("path") or inp.get("filePath") or ""
    if tool_name == "Bash":
        cmd = inp.get("command", "")
        return " ".join(cmd.split())
    # 其他工具按排序后的 JSON 序列化
    return json.dumps(inp, sort_keys=True, ensure_ascii=False)


def _deduplicate_tool_results(messages, dynamic_zone_start=0):
    """
    扫描 dynamic zone 中的 tool_result，对相同 (tool_name, normalized_args)
    只保留最新一条。返回 (messages, dedup_stats)。
    """
    tool_use_map = {}
    for m in messages:
        if m.get("role") == "assistant":
            for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
                if b.get("type") == "tool_use":
                    tool_use_map[b.get("id", "")] = b

    seen = {}  # key -> (msg_idx, block_idx)
    duplicates = []  # [(msg_idx, block_idx, key)]

    for msg_idx in range(dynamic_zone_start, len(messages)):
        msg = messages[msg_idx]
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block_idx, block in enumerate(content):
            if block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id", "")
            tool_use = tool_use_map.get(tool_use_id)
            if not tool_use:
                continue
            tool_name = tool_use.get("name", "")
            if tool_name not in PROXY_DEDUP_TOOLS or tool_name in PROXY_DEDUP_PROTECTED:
                continue
            key = (tool_name, _normalize_tool_args(tool_name, tool_use.get("input", {})))
            if key in seen:
                duplicates.append((msg_idx, block_idx, key))
            else:
                seen[key] = (msg_idx, block_idx)

    deduped_count = 0
    deduped_chars = 0
    for msg_idx, block_idx, key in duplicates:
        block = messages[msg_idx]["content"][block_idx]
        original = block.get("content", "")
        original_len = len(str(original)) if original else 0
        tool_name, args = key
        block["content"] = f"[deduplicated: previous {tool_name}({args}) output removed; most recent output retained]"
        deduped_count += 1
        deduped_chars += original_len

    return messages, {
        "deduplicated": deduped_count,
        "deduplicated_chars": deduped_chars,
        "tools": sorted(PROXY_DEDUP_TOOLS),
    }
```

3. **在 `_compress_content_pass` 中调用**：在 Phase 2a 的 `if PROXY_CLEAR_ENABLED ...` 块之前插入：

```python
    # ---- Phase 1b: deduplicate repeated tool results ----
    dedup_stats = {"enabled": False}
    if PROXY_DEDUP_ENABLED:
        messages, dedup_stats = _deduplicate_tool_results(messages, frozen_head)
        if dedup_stats.get("deduplicated"):
            log(f"  -> Deduplication: {dedup_stats['deduplicated']} repeated tool_results collapsed, "
                f"{dedup_stats['deduplicated_chars']:,} chars freed")
```

4. **Metrics 输出**：在 `_mc_put("tool_clear", {...})` 附近追加 `_mc_put("dedup", dedup_stats)`。

**边界情况处理**：

- 只处理 `dynamic zone`（`msg_idx >= frozen_head`），避免破坏 prefix cache。
- 保留最新一次输出，不是最旧一次（与 DCP 一致）。
- 如果某条消息包含多个 `tool_result` 块，只替换重复的那一块，不影响同消息中的其他块。

**预期收益**：在长时间调试或反复 `Read`/`Bash` 同一路径的场景下，可减少 5-20% 的上下文字符。

---

#### 7.1.2 Purge Errors（错误工具输入清理）

**目标**：对 N 轮之前的失败 `tool_result`，只保留首行错误提示，删除可能很大的输入/堆栈内容。

**接入位置**：同样在 `_compress_content_pass()` Phase 2a 中，可在 `_deduplicate_tool_results` 之后、语义评分之前执行。

**实现步骤**：

1. **新增配置常量**：

```python
PROXY_PURGE_ERRORS_ENABLED = os.environ.get("PROXY_PURGE_ERRORS_ENABLED", "true").lower() in ("1", "true", "yes")
PROXY_PURGE_ERRORS_TURNS = int(os.environ.get("PROXY_PURGE_ERRORS_TURNS", "4"))
PROXY_PURGE_ERRORS_PROTECTED = set(os.environ.get(
    "PROXY_PURGE_ERRORS_PROTECTED", "Agent,Task").split(","))
```

2. **新增辅助函数**：

```python
def _is_error_tool_result(content_str):
    """判断 tool_result 内容是否为错误。与 _translate_tool_result_errors 的判定保持一致。"""
    error_markers = (
        "该文件自上次读取后未发生变化", "文件不存在", "工具调用参数错误",
        "Wasted call", "File does not exist", "No such file",
        "InputValidationError", "Command failed", "Error:", "error:",
        "Traceback", "Exception", "FAILED", "exit code",
    )
    text = str(content_str)[:500]
    return any(m in text for m in error_markers)


def _purge_old_error_inputs(messages, dynamic_zone_start=0, turns=4):
    """
    对 dynamic zone 中超过 `turns` 轮之前的错误 tool_result，仅保留首行错误消息。
    """
    tool_use_map = {}
    for m in messages:
        if m.get("role") == "assistant":
            for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
                if b.get("type") == "tool_use":
                    tool_use_map[b.get("id", "")] = b

    # 计算每条 tool_result 距离当前末尾的 "turn" 数（以 assistant 消息为单位）
    assistant_positions = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    current_assistant_turn = len(assistant_positions)
    turn_of_msg = {}
    for turn_idx, msg_idx in enumerate(assistant_positions, start=1):
        turn_of_msg[msg_idx] = turn_idx

    purged_count = 0
    purged_chars = 0
    for msg_idx in range(dynamic_zone_start, len(messages)):
        msg = messages[msg_idx]
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id", "")
            tool_use = tool_use_map.get(tool_use_id)
            if not tool_use:
                continue
            tool_name = tool_use.get("name", "")
            if tool_name in PROXY_PURGE_ERRORS_PROTECTED:
                continue
            content_str = str(block.get("content", ""))
            if not _is_error_tool_result(content_str):
                continue
            # 只处理 assistant tool_use 所在消息已足够旧的 case
            call_msg_idx = None
            for mid in range(msg_idx - 1, -1, -1):
                if messages[mid].get("role") == "assistant":
                    for b in messages[mid].get("content", []) if isinstance(messages[mid].get("content"), list) else []:
                        if b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                            call_msg_idx = mid
                            break
                if call_msg_idx is not None:
                    break
            call_turn = turn_of_msg.get(call_msg_idx, current_assistant_turn)
            if current_assistant_turn - call_turn < turns:
                continue
            # 保留首行，其余替换
            first_line = content_str.splitlines()[0] if content_str.splitlines() else ""
            if len(content_str) > len(first_line) + 10:
                block["content"] = (
                    f"{first_line}\n"
                    f"[purged: older error details removed after {turns} turns; "
                    f"only error summary retained]"
                )
                purged_count += 1
                purged_chars += len(content_str) - len(block["content"])

    return messages, {
        "purged": purged_count,
        "purged_chars": purged_chars,
        "turns": turns,
    }
```

3. **在 `_compress_content_pass` 中调用**：

```python
    # ---- Phase 1c: purge old error tool inputs ----
    purge_stats = {"enabled": False}
    if PROXY_PURGE_ERRORS_ENABLED:
        messages, purge_stats = _purge_old_error_inputs(
            messages, frozen_head, PROXY_PURGE_ERRORS_TURNS)
        if purge_stats.get("purged"):
            log(f"  -> Purge errors: {purge_stats['purged']} old error inputs purged, "
                f"{purge_stats['purged_chars']:,} chars freed")
```

**边界情况**：

- 保留错误首行，模型仍知道发生了什么错误。
- 不处理受保护工具（如 Agent 任务失败可能仍需要完整堆栈）。
- `turns` 以 assistant 消息计数，而不是绝对消息索引，避免多 tool_result 单轮场景误判。

---

#### 7.1.3 Supersede Writes（Write 输入被后续 Read 覆盖后清理）

**目标**：如果某文件在后续被 `Read` 读取，那么在此之前对该文件的 `Write`/`Edit` 的输入内容可以被替换为占位，因为当前文件内容已经通过 Read 可知。

**接入位置**：`_compress_content_pass()` 中，在 Purge Errors 之后。

**实现步骤**：

1. **新增配置常量**：

```python
PROXY_SUPERSEDE_WRITES_ENABLED = os.environ.get(
    "PROXY_SUPERSEDE_WRITES_ENABLED", "true").lower() in ("1", "true", "yes")
```

2. **新增辅助函数**：

```python
def _extract_file_path(tool_use):
    """从 Write/Edit/Read 工具调用中提取标准化文件路径。"""
    inp = tool_use.get("input", {}) if isinstance(tool_use, dict) else {}
    if not isinstance(inp, dict):
        return ""
    path = (inp.get("file_path") or inp.get("path") or
            inp.get("filePath") or inp.get("file") or "")
    return path.strip()


def _supersede_write_inputs(messages, dynamic_zone_start=0):
    """
    如果文件在后续被 Read，则将该文件之前的 Write/Edit 的 content 输入替换为占位。
    注意：只替换 assistant tool_use 中携带的 content 参数，不是 tool_result。
    """
    read_files = set()
    superseded_count = 0
    superseded_chars = 0

    # 第一遍：从后往前收集所有被 Read 过的文件
    for msg in reversed(messages[dynamic_zone_start:]):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for b in content:
            if b.get("type") == "tool_use" and b.get("name") == "Read":
                fp = _extract_file_path(b)
                if fp:
                    read_files.add(fp)

    # 第二遍：从后往前处理 Write/Edit；遇到同文件最近一次后停止
    last_write_seen = {}
    for msg in reversed(messages[dynamic_zone_start:]):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for b in content:
            if b.get("type") != "tool_use":
                continue
            name = b.get("name", "")
            if name not in ("Write", "Edit"):
                continue
            fp = _extract_file_path(b)
            if not fp or fp not in read_files:
                continue
            # 只保留最近一次 Write/Edit 的完整内容，更早的替换
            if fp in last_write_seen:
                inp = b.get("input", {})
                if isinstance(inp, dict) and "content" in inp and isinstance(inp["content"], str):
                    original = inp["content"]
                    inp["content"] = (
                        f"[superseded: file was later read at {last_write_seen[fp]}; "
                        f"earlier {name} content removed]"
                    )
                    superseded_count += 1
                    superseded_chars += len(original) - len(inp["content"])
            else:
                # 记录该文件最近一次 Write/Edit 的位置（assistant 消息索引）
                last_write_seen[fp] = len(messages) - list(reversed(messages[dynamic_zone_start:])).index(msg) - 1

    return messages, {
        "superseded": superseded_count,
        "superseded_chars": superseded_chars,
    }
```

3. **在 `_compress_content_pass` 中调用**：

```python
    # ---- Phase 1d: supersede write inputs that are later read ----
    supersede_stats = {"enabled": False}
    if PROXY_SUPERSEDE_WRITES_ENABLED:
        messages, supersede_stats = _supersede_write_inputs(messages, frozen_head)
        if supersede_stats.get("superseded"):
            log(f"  -> Supersede writes: {supersede_stats['superseded']} write inputs superseded, "
                f"{supersede_stats['superseded_chars']:,} chars freed")
```

**边界情况**：

- 只替换 `Write`/`Edit` 的 `input.content`，不替换 `tool_result`（因为 tool_result 是模型对结果的确认，通常很短）。
- 仅当文件**后续确实被 Read** 时才触发，避免删除尚未被验证的写入内容。
- 保留最近一次 Write/Edit 的完整内容作为当前状态快照。

---

#### 7.1.4 模型特定阈值覆盖

**目标**：不同本地模型上下文窗口差异大（如 9B vs 35B、Q4 vs Q8），用同一套字符阈值不够精细。

**实现步骤**：

1. **在 `configs/*.conf` 中支持以 JSON 字符串声明模型阈值**：

```bash
# rapid-mlx-9b.conf
PROXY_MODEL_THRESHOLDS='{"mlx-community/Qwen3.6-9B": {"clear":8000,"growth":20000,"expansion":45000,"saturation":90000,"oom":180000}}'
```

2. **新增解析函数**（文件顶部常量区之后）：

```python
def _load_model_thresholds():
    raw = os.environ.get("PROXY_MODEL_THRESHOLDS", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        log(f"WARNING: invalid PROXY_MODEL_THRESHOLDS: {raw}")
        return {}


MODEL_THRESHOLDS = _load_model_thresholds()


def _get_model_threshold(key, default):
    """key 如 'clear', 'growth', 'expansion', 'saturation', 'oom'"""
    overrides = MODEL_THRESHOLDS.get(MODEL_NAME, {})
    return overrides.get(key, default)
```

3. **替换现有常量定义**为延迟求值（或加一层 wrapper）：

```python
PROXY_CLEAR_THRESHOLD = int(os.environ.get(
    "PROXY_CLEAR_THRESHOLD",
    _get_model_threshold("clear", "30000" if IS_CLOUD else "15000")))
PROXY_CHARS_GROWTH = int(os.environ.get(
    "PROXY_CHARS_GROWTH",
    _get_model_threshold("growth", "80000" if IS_CLOUD else "40000")))
# ... 其余阈值同理
```

4. **在 `/status` 页面显示当前生效的模型覆盖**：便于调试。

**注意事项**：

- 环境变量优先级高于配置文件中的 JSON 覆盖，保持向后兼容。
- 只在启动时解析一次 `MODEL_THRESHOLDS`，避免每次请求重复 JSON 解析。

---

#### 7.1.5 `<protect>` 标签支持（新增）

**目标**：允许用户在对话中用 `<protect>关键内容</protect>` 标记不可压缩内容。

**接入位置**：在 `_compress_content_pass()` Phase 2a 中，检测 tool_result 内容是否包含 `<protect>...</protect>`，若包含则跳过清除。

**实现步骤**：

1. **新增配置常量**：

```python
PROXY_PROTECT_TAG_ENABLED = os.environ.get(
    "PROXY_PROTECT_TAG_ENABLED", "true").lower() in ("1", "true", "yes")
```

2. **在语义评分阶段跳过受保护内容**：在 `_compress_content_pass` 的评分循环中：

```python
content_str = str(block.get("content", ""))
if PROXY_PROTECT_TAG_ENABLED and "<protect>" in content_str and "</protect>" in content_str:
    # 强制保留该 tool_result
    score += 1000  # 确保进入 keep_positions
```

3. **占位摘要中保留提示**：即使其他内容被清除，也保留 "contains <protect> blocks" 提示。

**边界情况**：

- 仅保护包含完整 `<protect>...</protect>` 标签的内容。
- 不展开或解释标签语义，只是跳过清除。

---

### 7.2 中期改进（需要新增能力）

#### 7.2.1 模型可调用的 `compress` 工具

**目标**：让模型主动决定何时压缩哪些区间，替代部分被动 `rounds` 截断。

**整体设计**：

- 在发往 Claude 的工具列表中注册一个名为 `compress_context` 的工具（注意：不是让 Claude 调用后端，而是让代理层在收到 `compress_context` tool_use 后执行本地压缩）。
- 由于当前代理层**不会**把 Claude 的 tool_use 结果回送给后端（代理只转发后端响应给 Claude），所以 `compress_context` 必须由代理层在**请求转发前**识别并处理：
  - 如果最后一条 assistant 消息包含 `compress_context` tool_use，则代理层立即执行压缩、移除该 tool_use，并把压缩结果作为新的 assistant text 消息插入历史，再进入正常转发流程。
- 这要求客户端（Claude Code）实际调用了 `compress_context`；由于代理层可以过滤/注入工具定义，可以在 `_filter_tools()` 中**保留**该工具（加入白名单）。

**工具定义示例**（添加到 `_filter_tools()` 的 `TOOL_ALWAYS_KEEP` 或动态注入）：

```json
{
  "name": "compress_context",
  "description": "Compress a range of earlier conversation messages into a structured summary to free context. Use when a task is complete and its detailed history is no longer needed verbatim.",
  "input_schema": {
    "type": "object",
    "properties": {
      "start_message_index": {
        "type": "integer",
        "description": "Index of first message to compress (inclusive). Must be >= 0 and not in frozen zone."
      },
      "end_message_index": {
        "type": "integer",
        "description": "Index of last message to compress (inclusive). Must be < current message count - keep_tail."
      },
      "focus": {
        "type": "string",
        "description": "Optional focus text to guide summary generation (e.g., 'refactor of auth module')."
      }
    },
    "required": ["start_message_index", "end_message_index"]
  }
}
```

**实现步骤**：

1. **新增配置开关**：

```python
PROXY_COMPRESS_TOOL_ENABLED = os.environ.get(
    "PROXY_COMPRESS_TOOL_ENABLED", "false").lower() in ("1", "true", "yes")
```

2. **在 `_handle_messages()` 中、调用 `_compress_content_pass()` 之前**，检测并处理 `compress_context` tool_use：

```python
# ---- Model-driven compression tool handling ----
compress_tool_stats = {"enabled": False}
if PROXY_COMPRESS_TOOL_ENABLED and raw_messages:
    last_assistant = [m for m in raw_messages if m.get("role") == "assistant"][-1] if any(m.get("role") == "assistant" for m in raw_messages) else None
    if last_assistant:
        content = last_assistant.get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use" and block.get("name") == "compress_context":
                    inp = block.get("input", {})
                    start_idx = int(inp.get("start_message_index", 0))
                    end_idx = int(inp.get("end_message_index", 0))
                    focus = inp.get("focus", "")
                    compressed = _compress_message_range(
                        raw_messages, start_idx, end_idx, focus=focus)
                    # 替换最后 assistant 消息：移除 tool_use，改为 text 告知压缩结果
                    new_content = [b for b in content if b.get("type") != "tool_use" or b.get("name") != "compress_context"]
                    new_content.append({
                        "type": "text",
                        "text": f"[compress_context completed] {compressed}"
                    })
                    last_assistant["content"] = new_content
                    compress_tool_stats = {
                        "enabled": True,
                        "start": start_idx,
                        "end": end_idx,
                        "chars": len(compressed),
                    }
                    log(f"  -> compress_context: compressed messages [{start_idx}:{end_idx}], "
                        f"summary length={len(compressed)}")
                    break
```

3. **新增 `_compress_message_range()` 函数**：复用现有 `_compress_middle_with_llm()` 或 `_extract_middle_summary_rules()`，但支持任意区间。

```python
def _compress_message_range(messages, start_idx, end_idx, focus=""):
    """压缩 messages[start_idx:end_idx+1]，返回摘要文本。"""
    if start_idx < 0 or end_idx >= len(messages) or start_idx > end_idx:
        return "[invalid range]"
    target = messages[start_idx:end_idx + 1]
    # 优先 LLM 压缩，失败降级到规则压缩
    summary = _compress_middle_with_llm(target, timeout=30) or _extract_middle_summary_rules(target)
    if focus:
        summary = f"Focus: {focus}\n{summary}"
    return summary or f"[compressed {len(target)} messages]"
```

4. **更新 `_filter_tools()`**：确保 `compress_context` 在白名单中。

**风险与缓解**：

- 模型可能滥用 compress 工具，把仍需的细节提前压缩。缓解：限制可压缩区间必须在 `frozen_head` 之后，且不能包含最近 N 轮。
- 本地模型可能不理解该工具。缓解：默认关闭，仅在明确测试过的配置中开启。

---

#### 7.2.2 压缩块 ID 与占位

**目标**：让截断/压缩后的摘要可引用、可审计，而不是直接删除消息。

**实现思路**：

- 在 `_apply_rounds_truncation()` 中，把生成的 `compressed_text` 包装成带 ID 的占位：

```text
[Context compressed block b1 (dropped 45 messages): <summary>]
```

- 维护会话级 `_COMPRESSION_BLOCKS` 字典：`session_id -> {block_id -> original_dropped_messages}`。
- 当后续模型请求中包含 `reference_block: b1` 时，代理层可以选择性展开该 block。

**最小实现**（无需真正展开，仅提升可读性）：

1. 在 `_apply_rounds_truncation()` 中：

```python
import itertools
block_id = f"b{(_COMPRESSION_BLOCK_COUNTERS.get(session_id, 0) + 1)}"
_COMPRESSION_BLOCK_COUNTERS[session_id] = int(block_id[1:])
compressed_text = f"[compressed block {block_id}: {dropped_count} messages summarized]\n{compressed_text}"
# 可选：保存原始 dropped 消息
if session_id:
    _COMPRESSION_BLOCKS.setdefault(session_id, {})[block_id] = list(dropped)
```

2. 在 `log_metrics()` 中记录 `compression_blocks` 列表。

---

#### 7.2.3 嵌套摘要

**目标**：当新的压缩区间覆盖旧摘要时，把旧摘要作为上下文嵌套进去，避免多次压缩后信息稀释。

**实现思路**：

- 在 `_apply_rounds_truncation()` 扫描 `dropped` 消息时，检测消息文本中是否包含 `[compressed block X: ...]` 或 `[Context folded: ...]`。
- 如果检测到旧摘要，将旧摘要文本附加到当前 LLM 压缩 prompt 的 `historical_summaries` 部分：

```python
nested_summaries = []
for m in dropped:
    text = _extract_text_from_message(m)
    if "[compressed block" in text or "[Context folded" in text:
        nested_summaries.append(text[:500])

if nested_summaries:
    prompt_prefix = "Previously compressed blocks (preserve their information in the new summary):\n" + "\n---\n".join(nested_summaries)
```

- 修改 `_compress_middle_with_llm()` 支持传入 `historical_summaries` 参数。

---

#### 7.2.4 阶段感知 Nudge（新增）

**目标**：在 SATURATION 阶段注入轻量提示，引导模型收尾当前任务，作为模型驱动 compress 工具的过渡方案。

**实现思路**：

- 在 `_handle_messages()` 中，当 `stage_config["stage"]` 为 `saturation` 或 `oom_danger` 时，向 tail 追加一条轻量 user 提示：

```python
if PROXY_STAGE_NUDGE_ENABLED and stage_config["stage"] in ("saturation", "oom_danger"):
    nudge_msg = {
        "role": "user",
        "content": [{
            "type": "text",
            "text": "[System: Context is approaching the local model limit. "
                   "Consider wrapping up the current sub-task or calling compress_context if available.]"
        }]
    }
    raw_messages.append(nudge_msg)
```

- 新增配置开关 `PROXY_STAGE_NUDGE_ENABLED`（默认 `false`）。
- 仅在云模式关闭或模型驱动 compress 工具开启时使用，避免对正常流程造成干扰。

---

### 7.3 需要谨慎评估的改进

| 改进点 | 风险 | 建议 |
|--------|------|------|
| **完全转向 DCP 式模型驱动压缩** | 本地模型（Qwen 3.6 4bit）可能不善于判断何时压缩，且破坏 prefix cache | 先作为可选策略，保留现有阈值触发作为 fallback |
| **关闭 Frozen Zone 以追求更高压缩率** | 会显著降低 prefix cache 命中率，增加本地后端负载 | 保持 Frozen Zone，仅在高阶段动态缩小 |
| **引入 `protectUserMessages`** | 本地模型上下文紧张，大段用户粘贴内容不压缩可能导致 OOM | 默认关闭，仅在小上下文模型配置中开启 |
| **将 DCP 的 nudge 机制照搬到本地后端** | nudge 会增加请求中的 system/user 文本量，对 128K 上下文模型可能是负收益 | 仅在云模式或模型驱动 compress 工具开启时启用 |
| **集成 Sleev 作为上游 context-management 层** | Sleev 是 DCP 作者的新项目，架构与当前 proxy 差异大，集成成本高 | 作为长期评估项，先落地上述可借鉴点后再考虑 |

---

## 8. 总结

### 8.1 定性评价

| 评价维度 | DCP | Anthropic Proxy |
|----------|-----|-----------------|
| 压缩质量 | ⭐⭐⭐⭐⭐ (大模型生成) | ⭐⭐⭐ (小模型/规则) |
| 可靠性 | ⭐⭐⭐ (依赖模型配合) | ⭐⭐⭐⭐⭐ (确定性规则) |
| 缓存友好 | ⭐⭐⭐ (85% hit rate) | ⭐⭐⭐⭐⭐ (Frozen Zone) |
| OOM 安全 | N/A (云 API) | ⭐⭐⭐⭐⭐ (多层硬限制) |
| 模型无关性 | ⭐⭐⭐ (OpenCode only) | ⭐⭐⭐⭐⭐ (任意 Anthropic 客户端) |
| 可配置性 | ⭐⭐⭐⭐ (JSON Schema) | ⭐⭐⭐⭐ (环境变量 + conf) |
| 可观测性 | ⭐⭐ (debug 日志) | ⭐⭐⭐⭐⭐ (metrics.jsonl + status page) |

### 8.2 核心结论

- **DCP 更适合大上下文、云 API 场景**：它把上下文管理权部分交给模型，通过非破坏性出站转换实现智能压缩，牺牲了部分 prompt cache 以换取 token 效率。
- **当前项目代理层更适合本地小上下文后端**：它以稳定性为第一目标，采用被动阈值触发、强循环/阻塞干预、cache 友好的 Frozen Zone，但压缩粒度较粗、缺乏自动工具调用级 dedup/purge errors/supersede writes 等精细化策略。
- **最佳实践不是二选一**：当前项目可以吸收 DCP 的 `deduplication`、`purgeErrors`、`supersedeWrites`、`<protect>` 标签、per-model 阈值、模型可调用的 `compress` 工具作为补充，同时保留现有的生命周期阶段、Frozen Zone、循环/阻塞检测和 OOM 安全作为安全网。这样既能提升长会话的 token 效率，又能维持本地后端的稳定性和 cache 命中率。

---

## 参考

- [DCP GitHub](https://github.com/Opencode-DCP/opencode-dynamic-context-pruning)
- [Sleev](https://sleev.ai) — DCP 作者的下一代 context-management proxy
- `anthropic_proxy.py` — 当前项目的核心代理实现
- `docs/02-architecture-design/proxy-pipeline-reference.md` — 代理层 8 层管线参考文档
