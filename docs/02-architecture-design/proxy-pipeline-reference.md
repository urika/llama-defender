# 代理层请求处理管线参考文档

> 状态: 与 anthropic_proxy.py 同步
> 日期: 2026-06-08
> 版本: v3（统一 char 阈值 + 生命周期阶段 + L2/L4 合并为 _compress_content_pass + 移除 cleared merge）

---

## 0. 全局视图

```
Claude Code (Anthropic SDK)
       │
       │ POST /v1/messages (Anthropic format)
       ▼
┌─────────────────────────────────────────────────────────┐
│                    anthropic_proxy.py :4000              │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Layer 1: 请求入口 (Handler)                       │   │
│  │   - 路由、JSON解析、会话跟踪、Metrics初始化        │   │
│  └────────────────────┬─────────────────────────────┘   │
│                       │                                  │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 2: 内容压缩 (Content Compression)           │   │
│  │   - 错误翻译、工具内容清除、Thinking 剥离、占位保留│   │
│  │     (合并为单次 _compress_content_pass)            │   │
│  └────────────────────┬─────────────────────────────┘   │
│                       │                                  │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 3: 循环与阻塞检测 (Loop & Blocker Guard)    │   │
│  │   - 精确/模式匹配、升级干预、Re-read检测           │   │
│  └────────────────────┬─────────────────────────────┘   │
│                       │                                  │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 4: 缓存优化 (Cache Optimizer)               │   │
│  │   - 日期标准化                                     │   │
│  └────────────────────┬─────────────────────────────┘   │
│                       │                                  │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 5: 上下文截断 (Context Truncator)            │   │
│  │   - Rounds/FIFO/Char策略、三级压缩、增量摘要       │   │
│  └────────────────────┬─────────────────────────────┘   │
│                       │                                  │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 6: 格式转换与转发 (Format & Forward)        │   │
│  │   - Anthropic→OpenAI、工具过滤、转发               │   │
│  └──────────────────────────────────────────────────┘   │
│                       │                                  │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 7: 响应后处理 (Response Control)             │   │
│  │   - 流式/非流式SSE构造、输出截断、JSON修复         │   │
│  └──────────────────────────────────────────────────┘   │
│                       │                                  │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 8: 可观测性 (Observability)                  │   │
│  │   - Metrics记录、JSONL日志                         │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌──── lifecycle stage ────────────────────────────┐   │
│  │ _classify_lifecycle_stage()                     │   │
│  │   统一 char 阈值，单调递增:                       │   │
│  │   INIT(15K) → GROWTH(40K) → EXPANSION(90K)     │   │
│  │   → SATURATION(180K) → OOM_DANGER(350K)         │   │
│  │   → PRE_TRUNC(400K)                              │   │
│  └─────────────────────────────────────────────────┘   │
│                                                          │
└─────────────────────────────────────────────────────────┘
       │
       │ POST /chat/completions (OpenAI format)
       ▼
  Backend (rapid-mlx / llama-server / Cloud API)
       :8081
```

---

## 1. Layer 1: 请求入口 (Handler)

**职责**: 接收 HTTP 请求，解析 JSON，管理会话和日志上下文。

**入口**: `Handler.do_POST()` (line 3017)

### 1.1 路由

| 路径 | 方法 | 处理函数 |
|------|------|---------|
| `/v1/models` | GET | 返回模型别名列表 |
| `/v1/messages` | POST | 核心请求处理 |
| `/status` | GET | 返回 HTML 状态页 |
| 其他 | - | 404 |

### 1.2 请求处理流程

```
do_POST()
  ├─ 解析 session_id (X-Claude-Code-Session-Id header, 前8位)
  ├─ 初始化 _metrics_ctx.mc (结构化 metrics dict)
  ├─ Headers 日志脱敏 _mask_sensitive() (DEF-302)
  ├─ 读取 + 解析 JSON body
  ├─ 请求去重检查 _check_dedup(body) → 429 (重复请求, DEF-205)
  ├─ 写入 /tmp/anthropic_request_body.json (调试)
  │
  ├─ [DEF-001] Pre-truncation: total_chars > 400K 时强制 keep_rounds=2
  │
  └─ 调用 _handle_messages(body)          ← 核心管线 (2-8层)
       ├─ 成功: log_request() + log_metrics()
       ├─ 失败: _classify_exception(e) → 503/504/500 + Retry-After (retryable)
       └─ finally: 清理 _log_ctx, _metrics_ctx
```

### 1.3 会话跟踪

- **Thread-local**: `_log_ctx.session_id` — 贯穿整个请求周期
- **JSONL**: 每请求分配唯一 token (`req_N_hex`) 关联输入/输出
- **Metrics**: `_metrics_ctx.mc` dict，各步骤追加数据

### 1.4 并发控制

```python
_llama_lock = threading.Semaphore(PROXY_MAX_CONCURRENT)
# local: 默认 1 (防止 Metal OOM)
# cloud: 默认 4 (云端天然支持并发)
```

### 1.5 配置参数

| 参数 | 默认值 (local/cloud) | 说明 |
|------|---------------------|------|
| `PROXY_MAX_CONCURRENT` | 1 / 4 | 最大并发请求数 |
| `PROXY_BACKEND_TIMEOUT` | 300 | 后端超时(秒) |
| `PROXY_DEDUP_WINDOW` | 2 | 请求去重时间窗口(秒) |

---

## 2. Layer 2: 内容压缩 (Content Compression)

**职责**: 单次遍历完成工具内容清除和 Thinking 剥离，由 lifecycle stage 决定压缩力度。合并了 v2 的 L2（工具清除）和 L4（Thinking 清除）。

**核心函数**: `_compress_content_pass()` — 合并了 `clear_old_tool_results()` 和 `strip_old_thinking_blocks()` 的逻辑。

**入口**: `_handle_messages()` 中段

### 2.1 生命周期阶段驱动

所有压缩参数由 `_classify_lifecycle_stage()` 统一判定。报文越大，阶段越高，压缩力度越强。

| 阶段 | 触发 chars | Frozen | 清除范围 | Thinking keep |
|------|:---:|:---:|------|:---:|
| INIT | < 15K | 12 | 跳过 | 0 (跳过) |
| GROWTH | 15K-40K | 12 | 尾 40% dynamic | 0 (跳过) |
| EXPANSION | 40K-90K | 12 | 尾 60% dynamic | 5 |
| SATURATION | 90K-180K | 6 | 全 dynamic | 3 |
| OOM_DANGER | 180K-350K | 0 | 全量 | 1 |
| PRE_TRUNC | > 350K | 0 | 全量 | 1 |

### 2.2 Pre-truncation (DEF-001)

**触发条件**: `total_chars > PROXY_PRE_TRUNCATE_CHARS=400000`

**处理**: 在 `_handle_messages` 调用前，直接用 `keep_rounds=2` 强制截断原始消息列表，防止 rapid-mlx OOM。

```
if total_chars > 400K:
    msgs_truncated, pre_stats = _apply_rounds_truncation(msgs, keep_rounds=2, session_id=session_id)
    if pre_stats.get("truncated"):
        parsed["messages"] = msgs_truncated
        # 记录 pre_truncate metrics
```

### 2.3 max_tokens 覆盖

```
输入: body.max_tokens = 16384 (Claude Code 默认)
覆盖: PROXY_MAX_TOKENS_OVERRIDE > 0 时生效
目的: 限制本地模型输出长度，防止 rapid-mlx 忽略 max_tokens 问题
```

### 2.4 错误翻译 (Error Translation)

**原理**: 将后端返回的英文/技术性错误信息翻译为中文自然语言提示，帮助 Qwen 模型理解错误含义。

| 原始内容 | 翻译后 |
|---------|--------|
| `Wasted call` | "该文件自上次读取后未发生变化，不要再使用 Read 工具反复读取" |
| `File does not exist` / `No such file` | "文件不存在。请先用 Bash ls 或 find 确认项目结构" |
| `InputValidationError` | "工具调用参数错误。请检查工具参数格式" |

**实现**: 遍历 user 消息中的 tool_result blocks，子串匹配 + 替换 content。

### 2.5 工具内容清除 (Tool-Result Clearing)

**核心函数**: `clear_old_tool_results()` (line 756)

**目的**: 用轻量占位文本替换旧 tool_result 的完整内容，释放 token 空间。

**Frozen Zone 保护**:
前 `PROXY_FROZEN_HEAD` (默认 12) 条消息受 Frozen Zone 保护，其中的 tool_result 不会被清除。只有在 dynamic zone (frozen 之后的消息) 中的 tool_result 才参与评分和清除。如果 dynamic zone 中可清除的 tool_result 太少，会尝试将 Frozen Zone 缩小一半重试一次，否则跳过本次清除。

**处理流程**:

```
输入: N 条消息
  │
  ├─ 0. 分区: frozen = messages[:PROXY_FROZEN_HEAD], dynamic = 剩余
  │
  ├─ 1. 计算 total_chars → 低于阈值则跳过
  ├─ 2. 仅在 dynamic zone 中定位所有 tool_result blocks
  ├─ 3. 若 dynamic zone 中 tool_result 太少且 frozen > 0:
  │     → 将 frozen_head 缩小一半，重新扫描
  ├─ 4. 动态 KEEP: 检测子代理(auto=15) vs 主代理(default=2)
  ├─ 5. 语义评分 (每条 tool_result, 仅 dynamic):
  │     ├─ 工具优先级: Read=3, Agent=3, WebFetch=2, Bash=1
  │     ├─ 内容模式: 代码结构+3, 错误信息+2, Wasted=0
  │     └─ 近期 Read 加分: 最近 6 条 Read 结果额外 +5
  ├─ 6. 按分数排序，保留 top KEEP 条
  ├─ 7. 清除低分 tool_result (仅 dynamic zone):
  │     ├─ Read: 保留 200 字符预览 (防重读)
  │     ├─ 其他: 替换为 [cleared: ToolName("file")] (Prefix Cache 友好)
  │     └─ 记录 cleared_files 集合
  ├─ 8. Bash 去重: Jaccard >= 0.7 的连续 Bash 输出合并
  └─ 9. 返回 (messages, stats_dict, frozen_used)
```

**Frozen Zone 示意图**:

```
┌──────────────────────────────────────────────────────┐
│  Frozen Zone (前 12 条)            Dynamic Zone        │
│  ← NEVER modified →              ← 可清除/压缩 →      │
│                                                       │
│  system msg                                           │
│  msg0: "Today's date is..." (日期标准化仍生效)        │
│  msg1-3: 初始工具调用/读取                            │
│  msg4-11: 早期交互                                    │
│                       ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│
│                                     tool_result #12+  │
│                                     可被清除           │
│                                                       │
│  prefix cache 稳定命中 ←────────────────→             │
└──────────────────────────────────────────────────────┘
```

**语义评分表示例**:

```
tool_result #45: Read("board.js") + 代码内容 → score = 3(base) + 3(code) + 5(recent) = 11
tool_result #12: Read("README.md") → score = 3(base) = 3
tool_result #8: Bash("ls") → score = 1(base) = 1
```

**Read 预览机制** (Fix 3):

清除 Read 的 tool_result 时，不直接丢弃内容，而是保留前 200 字符作为预览：
```
原始: board.js 的 5000 字符文件内容
清除后: [cleared: Read("board.js")]\nimport React from 'react';\n\nfunction Board() {\n  const [state, setState]...
```
这显著降低了模型因"看不到文件内容"而触发重读循环的概率。

### 2.6 配置参数

| 参数 | 默认值 (local/cloud) | 说明 |
|------|---------------------|------|
| `PROXY_MAX_TOKENS_OVERRIDE` | 0 (不覆盖) | 强制 max_tokens 上限 |
| `PROXY_CLEAR_ENABLED` | true / false | 工具内容清除开关 |
| `PROXY_CLEAR_THRESHOLD` | 15000 / 30000 | 触发清除的字符阈值 |
| `PROXY_TOOL_KEEP` | 2 / 10 | 保留的最近 tool_result 数量 |
| `PROXY_REREAD_PREVIEW_CHARS` | 200 | Read 清除时保留的预览字符数 |
| `PROXY_FROZEN_HEAD` | 12 / 0 | Frozen Zone 保护的前 N 条消息 |
| `PROXY_CLEAR_TAIL_FIRST` | true | 尾优先清除 (从尾部向前扫描) |

---

## 3. Layer 3: 循环与阻塞检测 (Loop & Blocker Guard)

**职责**: 检测模型陷入重复行为模式的场景，并施加递进式干预。

**入口**: `_handle_messages()` 中段 (line 3267-3385)

### 3.1 循环检测 (Loop Detection)

**检测方式**: 三重匹配

| 方式 | 匹配规则 | 示例 |
|------|---------|------|
| **精确匹配** | 相同工具名 + 相同参数 JSON | `Read({"file_path":"board.js"})` 连续出现 |
| **模式匹配** | 相同文本(前200字) + 相同工具集合 | 每次回复 "让我重新读取" + Read |
| **文本输出匹配** | 连续相似的纯文本输出 | 模型重复输出相同的分析段落 |

**追踪逻辑**: 
- 工具循环: 扫描最后 15 条 assistant 消息，维护 `consecutive` dict 和 `pattern_run` 计数器
- 文本循环: 扫描最后 15 条 assistant 消息的纯文本内容，使用 bigram Jaccard 相似度检测

### 3.1.1 文本输出循环检测 (v0.5.3)

**背景**: 模型有时会陷入"解释模式"，不断重复输出相同的文本段落（如重复分析 minimax 算法），而没有工具调用。传统工具循环检测无法捕获这种情况。

**检测算法**:
```python
def _compute_text_similarity(text1, text2):
    """基于 bigram 的 Jaccard 相似度"""
    bigrams1 = set(text1[i:i+2] for i in range(len(text1)-1))
    bigrams2 = set(text2[i:i+2] for i in range(len(text2)-1))
    intersection = len(bigrams1 & bigrams2)
    union = len(bigrams1 | bigrams2)
    return intersection / union if union > 0 else 0.0
```

**检测流程**:
1. 提取最后 N 条 assistant 消息的纯文本内容
2. 短于 `PROXY_TEXT_LOOP_MIN_CHARS` (默认 100) 的消息被视为断链点
3. 计算相邻消息的文本相似度
4. 相似度 >= `PROXY_TEXT_LOOP_SIMILARITY` (默认 0.85) 时增加连续计数
5. 连续计数 >= `PROXY_TEXT_LOOP_THRESHOLD` (默认 3) 时触发干预

**干预消息** (与工具循环共享阈值，但消息内容不同):
- Level 1: "You have repeated similar text output N times. STOP repeating yourself."
- Level 2: "You are stuck in a loop. STOP repeating the same explanation."
- Level 3: "ALL tools have been DISABLED. Describe the problem you are stuck on."

**局限性**:
- 检测时机在**下一次请求**，当前请求无法中断
- 用户需按 Ctrl+C 中断后发送新请求才能触发干预
- 代理层是被动的，无法在模型生成过程中干预

### 3.2 升级干预 (Escalating Intervention)

三级递进，基于重复次数 `max_run`:

```
           PROXY_LOOP_THRESHOLD=3    PROXY_LOOP_LEVEL2=6    PROXY_LOOP_LEVEL3=9
                 │                        │                       │
    ┌────────────┤────────────────────────┤───────────────────────┤
    │  Level 1   │       Level 2          │      Level 3          │
    │  警告提示   │    移除循环工具          │   强制纯文本响应       │
    └────────────┘                        │                       │
                                          └───────────────────────┘
```

| 级别 | 触发条件 | 干预手段 | 典型场景 |
|------|---------|---------|---------|
| **Level 1** | max_run >= 3 | 注入 user message："STOP using {tool}" | 模型连续读同一文件3次 |
| **Level 2** | max_run >= 6 | 从 tools 列表**移除**循环工具 + 强提示 | Level 1 无效，模型仍继续 |
| **Level 3** | max_run >= 9 | 替换最后 assistant 的 tool_use 为 text + "工具已禁用" | 模型完全无视 Level 1&2 |

**Level 2 效果**: 模型看不到 Read 工具定义 → 无法发出 Read tool_call → 被迫用 Bash cat 或纯文本回复。

**Level 3 效果**: 修改历史中最后一条 assistant 消息，移除所有 tool_use blocks → 模型"看到"自己已经放弃了工具调用 → 倾向于继续纯文本。

### 3.3 阻塞检测 (Blocker Detection)

**核心函数**: `_detect_blocker_pattern()` (line 1229)

**检测逻辑**: 扫描尾部 tool_result，查找连续 N 次相同错误类型的失败。

| 错误类型 | 标记文本 |
|---------|---------|
| `wasted` | "该文件自上次读取后未发生变化" |
| `file_not_found` | "文件不存在" |
| `input_validation` | "工具调用参数错误" |

**干预**: 注入 `[BLOCKER]` user message，引导模型切换策略。与 Loop Detection 互补：Loop 检测重复工具调用，Blocker 检测重复错误类型。

### 3.4 Re-read 检测

**目的**: 检测模型是否在被清除的文件上发起 Read。

**实现**: 检查最后一条 assistant 消息中是否有 Read tool_use 指向 `cleared_files` 集合中的文件。

**干预**: 若检测到 re-read，注入 HARD BLOCK user message，告知模型文件已被清除，禁止重新读取。（P0 fix）

```
[HARD BLOCK] — Read calls to the following files were intercepted because
their contents were previously cleared: file1.js, file2.py. DO NOT attempt
to read these files again. Use your existing knowledge or proceed without
re-reading.
```

### 3.5 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_LOOP_THRESHOLD` | 3 | Level 1 触发阈值 |
| `PROXY_LOOP_LEVEL2` | 6 | Level 2 触发阈值 (threshold × 2) |
| `PROXY_LOOP_LEVEL3` | 9 | Level 3 触发阈值 (threshold × 3) |
| `PROXY_TEXT_LOOP_ENABLED` | true | 文本输出循环检测开关 |
| `PROXY_TEXT_LOOP_THRESHOLD` | 3 | 连续相似文本触发阈值 |
| `PROXY_TEXT_LOOP_MIN_CHARS` | 100 | 最小文本长度（短于此的消息不参与检测） |
| `PROXY_TEXT_LOOP_SIMILARITY` | 0.85 | 文本相似度阈值 (0.0-1.0) |
| `PROXY_BLOCKER_ENABLED` | true (local) / false (cloud) | 阻塞检测开关 |
| `PROXY_BLOCKER_THRESHOLD` | 2 | 连续相同错误次数阈值 |

---

## 4. Layer 4: 缓存优化 (Cache Optimizer)

**职责**: 优化消息以提升 Rapid-MLX prefix cache 命中率。Thinking 剥离已合并至 Layer 2 的 `_compress_content_pass()` 中。

**入口**: `_handle_messages()` 中段

### 4.1 日期标准化 (Date Normalization)

**原理**: Claude Code 在首条消息中注入 `Today's date is 2026/06/05`。每次请求日期可能变化，导致 prefix cache miss。

**实现**: 将日期替换为固定字符串 `DATE_PLACEHOLDER`。

```
Before: "Today's date is 2026/06/05."
After:  "Today's date is DATE_PLACEHOLDER."
```

**效果**: 首条 user 消息在同日内保持不变 → prefix cache 持续命中。

### 4.2 Thinking Block 清除

**核心函数**: `strip_old_thinking_blocks()` (line 1715)

**原理**: Qwen 模型在 assistant 消息中产生 `thinking`/`reasoning_content` blocks，占据大量 token 但对后续推理无价值。

**Frozen Zone 保护**:
扫描所有消息但只清除 `dynamic zone` (messages[PROXY_FROZEN_HEAD:]) 中的 thinking blocks。Frozen Zone 内的 thinking blocks 不受影响，确保 prefix cache 稳定性。

**处理**:
- 仅扫描 dynamic zone 中的 assistant 消息
- 保留最近 3 条 dynamic zone thinking（最旧的一条优先被清除）
- Frozen Zone 内的 thinking 永不触及
- 返回 (messages, stats)

---

## 5. Layer 5: 上下文截断 (Context Truncator)

**职责**: 当消息总量超过预算时，主动截断旧消息并生成压缩摘要。

**核心函数**: `truncate_messages_if_needed()` (line 1227)

### 5.1 三种策略

| 策略 | 配置 | 截断方式 | 适用场景 |
|------|------|---------|---------|
| **rounds** | `PROXY_CTX_TRUNCATE_STRATEGY=rounds` | 保留最近 N 轮 assistant 对话 | 主要策略，有 token 预算控制 |
| **fifo** | `PROXY_CTX_TRUNCATE_STRATEGY=fifo` | 保留头部 + 尾部固定条数 | 简单场景，无压缩 |
| **char** | `PROXY_CTX_TRUNCATE_STRATEGY=char` | 丢弃最旧的中间消息直到总字符数 < 阈值 | 基础策略，最早实现 |

### 5.2 Rounds 策略详解

Round 策略由 lifecycle stage 的 `truncate_rounds` 字段驱动。当 `_classify_lifecycle_stage()` 判定进入 EXPANSION 阶段 (chars ≥ `PROXY_CHARS_EXPANSION`, 默认 90K) 时启用。

```
输入: 93 条消息
  │
   ├─ 1. Char 预算检查: total_chars > PROXY_CHARS_EXPANSION?
   │     低于预算 → 跳过截断
   │     超过预算 → 进入截断流程
  │
   ├─ 2. 保留轮数: 优先使用 stage_config["truncate_rounds"]
   │     ├─ INIT/GROWTH: None → 跳过截断
   │     ├─ EXPANSION/SATURATION: PROXY_CTX_KEEP_ROUNDS (10)
   │     ├─ OOM_DANGER: 3
   │     └─ PRE_TRUNC: 2
   │     无 stage 值时 fallback 到自适应 _compute_adaptive_rounds()
   │
   ├─ 3. 消息分离:
   │     HEAD (前 PROXY_CTX_KEEP_HEAD 条, system context) — 固定保留
   │     TAIL (最近 N 轮 assistant 对话) — 保留
   │     MIDDLE (其余) — 被截断
  │
   ├─ 4. 三级压缩链 (MIDDLE → 压缩摘要):
   │     ├─ Level 1: 增量压缩 (_incremental_compress)
   │     │   ├─ 检查 session 级 _summary_cache 命中?
   │     │   ├─ 命中: 只压缩新增消息 → 合并缓存摘要
   │     │   └─ 未命中: 走后续 fallback
   │     ├─ Level 2: LLM 压缩 (_compress_middle_with_llm)
   │     │   ├─ dropped >= 10 条时触发
   │     │   ├─ 调用本地 LLM 生成结构化摘要
   │     │   └─ 30s 超时，失败降级
   │     ├─ Level 3: 规则压缩 (_extract_middle_summary_rules)
   │     │   ├─ 提取: 错误/代码变更/文件状态/决策
   │     │   └─ 结构化文本输出
   │     └─ Level 4: 静态折叠
   │         └─ "[Context folded: N messages dropped]"
  │
  ├─ 5. 关键词索引注入 (P1-1)
  │     ├─ 从 dropped 消息提取: 文件名/错误类型/函数名
  │     └─ 在 TAIL 中子串匹配 → 注入相关历史
  │
  └─ 6. 重组: HEAD + 摘要消息 + Read 保留 + TAIL
```

### 5.3 增量压缩 (Incremental Compress)

**核心函数**: `_incremental_compress()` (line 1172)

**原理**: 避免每次全量重压缩。维护会话级摘要缓存 `_summary_cache`。

```
Session 第 5 次请求:
  上次缓存: last_compressed_msg_index=45, summary="..."
  当前 dropped: 65 条消息
  
  → new_dropped = dropped[45:65] = 20 条
  → 只压缩这 20 条 (而非全部 65 条)
  → 合并: cached_summary + new_summary
  → 如果总长 > 2000 字符 → LLM 合并
  → 更新缓存: index=65, summary=merged
```

**缓存管理**:
- LRU 淘汰: 最多 10 个会话
- 每个摘要不超过 3000 字符
- 会话结束自动清理

### 5.4 LLM 压缩

**核心函数**: `_compress_middle_with_llm()` (line 1057)

**调用方式**: `POST /chat/completions` 到本地后端 (同模型)

**Prompt 结构**:
```
请将以下对话历史压缩为结构化摘要，保留所有关键信息...
<current_focus> 当前工作焦点 </current_focus>
<errors_solutions> 错误和解决方案 </errors_solutions>
<code_state> 代码状态和文件变更 </code_state>
<decisions> 设计决策和待办事项 </decisions>
<pending> 未完成的操作 </pending>
```

**输入限制**: 每条消息截断至 300 字符，总输入上限 8000 字符。

### 5.5 配置参数

| 参数 | 默认值 (local/cloud) | 说明 |
|------|---------------------|------|
| `PROXY_CTX_LIMIT_ENABLED` | true (local) / false (cloud) | 截断开关 |
| `PROXY_CTX_TRUNCATE_STRATEGY` | char | 截断策略: rounds / fifo / char |
| `PROXY_CTX_KEEP_ROUNDS` | 10 | Rounds 策略保留轮数（EXPANSION/SATURATION） |
| `PROXY_CTX_KEEP_HEAD` | 2 | FIFO/char 保留头部消息数 |
| `PROXY_CTX_KEEP_TAIL` | 4 | FIFO/char 保留尾部消息数 |
| `PROXY_CTX_KEEP_MESSAGES` | 40 | FIFO 策略保留消息数 |
| `PROXY_CHARS_EXPANSION` | 90000 / 200000 | **Char 预算** — 触发截断的字符阈值 |
| `PROXY_CHARS_SATURATION` | 180000 / 500000 | SATURATION 阈值 (fallback: `PROXY_CTX_CHARS_LIMIT`) |
| `PROXY_CTX_TOKEN_BUDGET` | 30000 | ⚠️ Deprecated — 用 `PROXY_CHARS_EXPANSION` |
| `PROXY_CTX_TOKEN_RATIO` | 2.0 | ⚠️ 仅估算辅助，不用于阈值比较 |
| `PROXY_CTX_CHARS_LIMIT` | 180000 / 500000 | ⚠️ Deprecated — 用 `PROXY_CHARS_SATURATION` |
| `PROXY_HISTORY_INDEX` | rule | 关键词索引模式: off / rule |
| `PROXY_HISTORY_TOP_K` | 5 | 注入关键词条目数 |
| `PROXY_HISTORY_MAX_CHARS` | 500 | 注入文本最大字符数 |

---

## 6. Layer 6: 格式转换与转发 (Format & Forward)

**职责**: 将 Anthropic 格式转换为 OpenAI 格式，优化工具定义，发送到后端。

**入口**: `_handle_messages()` 后段 (line 3528-3584)

### 6.1 工具定义过滤 (Tool Filter)

**核心函数**: `_filter_tools()` (line 2591)

**原理**: 44 个工具定义 ≈ 8K-12K tokens 固定开销。大部分 agentic coding 场景只用 3-8 个工具。

**过滤策略**:

```
44 个工具定义
  │
  ├─ 1. TOOL_ALWAYS_KEEP 白名单 (12个核心工具):
  │     Read, Write, Edit, Bash, Glob, Grep, LS, Task,
  │     WebFetch, WebSearch, TodoRead, TodoWrite
  │
  ├─ 2. 最近 N 轮使用过的工具 (扫描 assistant tool_use)
  │
  ├─ 3. tool_choice 指定的工具 (强制保留)
  │
  └─ 4. 保留 = 白名单 ∪ 最近使用 ∪ tool_choice
       如果保留数 < 5 → 回退到原始列表

典型结果: 44 → 15 tools ≈ 节省 5-8K tokens
```

### 6.2 格式转换

**消息转换**: `convert_anthropic_messages_to_openai()` (line 1715)

| Anthropic | OpenAI |
|-----------|--------|
| `user` with `[text, tool_result]` | `user` (text) + `tool` (tool_result) |
| `assistant` with `[text, tool_use]` | `assistant` (text + tool_calls) |
| `system` field (body-level) | `system` role message (前置) |

**工具转换**: `convert_anthropic_tools_to_openai()` (line 571)

```
Anthropic: {"name": "Read", "description": "...", "input_schema": {...}}
OpenAI:    {"type": "function", "function": {"name": "Read", "description": "...", "parameters": {...}}}
```

**工具选择转换**: `convert_anthropic_tool_choice_to_openai()` (line 600)

### 6.3 转发

```python
urllib.request.urlopen(
    f"{LLAMA_BASE}/chat/completions",
    data=json.dumps(openai_body),
    timeout=PROXY_BACKEND_TIMEOUT
)
```

在 `_llama_lock` (Semaphore) 保护下执行。

### 6.4 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_TOOL_FILTER_ENABLED` | true (local) / false (cloud) | 工具过滤开关 |
| `PROXY_TOOL_FILTER_MAX` | 20 | 超过此数量才触发过滤 |
| `PROXY_TOOL_FILTER_RECENT` | 5 | 扫描最近 N 轮 |

---

## 7. Layer 7: 响应后处理 (Response Control)

**职责**: 将后端的 OpenAI 格式响应转换回 Anthropic SSE 流格式，控制输出长度。

### 7.1 流式响应 (Streaming)

**函数**: `Handler._handle_streaming_response()` (line 3600+)

**SSE 事件序列**:
```
event: message_start     → 消息元数据 (id, model, usage)
event: content_block_start → 文本块开始
event: content_block_delta → 文本增量 (多次)
event: content_block_stop  → 文本块结束
event: content_block_start → tool_use 块开始 (如有)
event: content_block_delta → input_json_delta (如有)
event: content_block_stop  → tool_use 块结束
event: message_delta      → stop_reason + output_tokens
event: message_stop       → 消息结束
```

**特殊处理**:
- **Content-text 工具提取**: Qwen 模型有时将 tool_call 作为 `<tools>` XML 嵌入 content text 中，`_StreamingToolsExtractor` 状态机实时提取
- **工具 ID 补全**: 某些后端流式输出时省略 `tool_call_id`，代理生成 `call_<hex>`

### 7.2 非流式响应 (Non-Streaming)

**函数**: `Handler._handle_non_streaming_response()` (line 3598)

**处理**: 一次性读取完整响应 → `convert_openai_response_to_anthropic()` → 返回 JSON。

### 7.3 输出截断 (Output Token Control)

**原因**: Rapid-MLX 已知会忽略 `max_tokens`，可能生成远超预期的 token。

**机制**:
```
max_tokens (请求值)
  × PROXY_OUTPUT_TOKEN_LIMIT_RATIO (默认 2.0)
  = output_token_hard_limit

输出字符数 × 0.4 > hard_limit → FORCE_STOPPED
  ├─ 流式: 停止读取 + 修复截断 JSON
  └─ 非流式: 截断文本 + 追加 [Output truncated]
```

### 7.4 JSON 修复

**函数**: `_repair_truncated_json()` (line 282)

**场景**: FORCE_STOPPED 截断了 tool_call 的 arguments JSON（如 `"file_path": "/src/boar`）。

**修复策略**: 从最后一个完整的 key-value 对截断，闭合所有未关闭的 `{` 和 `"`。

### 7.5 XML→JSON 回退

**函数**: `parse_tool_arguments()` (line 336)

**场景**: Qwen 模型偶尔以 XML 格式输出 tool_call 参数，而非标准 JSON。

**尝试顺序**: JSON → embedded JSON → XML extraction → heuristic fallback

---

## 8. Layer 8: 可观测性 (Observability)

### 8.1 文本日志

**输出**: stdout + `/tmp/anthropic_proxy.log`

**关键日志行**:
```
[REQ_SUMMARY] chars=43160 tools=44
-> Error translation: 2 tool_result errors rewritten
-> Tool clearing: 37 tool_results cleared, 12,000 chars freed
-> Consecutive calls: max_run=25
-> LOOP LEVEL 2: Read called 6 times, removing tool
-> Context truncation (rounds): 45 messages dropped, 28 kept
-> Tool filter: 44 -> 15
<- Streamed text=1520 chars, tools=1
```

### 8.2 结构化请求日志

**输出**: `logs/proxy_requests.jsonl`

**记录内容**: start_time, end_time, model, input_chars, output_chars, status, duration_ms

### 8.3 结构化 Metrics 日志

**输出**: `logs/proxy_metrics.jsonl`

**记录内容**:
```json
{
  "ts": "2026-06-05T14:32:01",
  "session_id": "d278d9eb",
  "input_msgs": 93,
  "input_chars": 43160,
  "input_tools": 44,
  "pipeline": {
    "error_translation": {"count": 2},
    "tool_clear": {"cleared": 37, "kept": 10, "chars_freed": 12000},
    "loop_detect": {"max_run": 6, "level": 2, "tool": "Read"},
    "blocker_detect": {"triggered": false},
    "think_strip": {"stripped": 3},
    "compress": {"merged_cycles": 2, "msgs_removed": 4},
    "truncate": {
      "triggered": true,
      "strategy": "rounds",
      "compression": "llm",
      "dropped": 45,
      "kept": 28,
      "rounds": 8
    },
    "tool_filter": {"original": 44, "kept": 15}
  },
  "output_chars": 1520,
  "duration_ms": 3200,
  "compression_ratio": 0.48,
  "quality_flags": ["loop_injected"]
}
```

**质量标记自动生成**:
| Flag | 触发条件 |
|------|---------|
| `high_drop_ratio` | dropped / (dropped + kept) > 0.7; 当 > 0.85 时注入 `[System: Context severely truncated]` 通知 (DEF-107) |
| `llm_compress_failed` | 压缩方式为 rules/folded 且 dropped >= 10 |
| `budget_overflow` | 截断后估算 token > budget × 1.1 |
| `loop_injected` | loop_detect.max_run >= threshold |

### 8.4 状态页面

**端点**: `GET /status`

**显示**: PID, 内存, CPU, 后端模型, 最近请求摘要, 会话追踪, Prefix cache 统计

---

## 9. 辅助函数索引

### 格式转换 (Layer 6)

| 函数 | 行号 | 用途 |
|------|------|------|
| `convert_anthropic_messages_to_openai()` | 1683 | Anthropic 消息 → OpenAI 消息 |
| `convert_anthropic_tools_to_openai()` | 540 | Anthropic 工具 → OpenAI 工具 |
| `convert_anthropic_tool_choice_to_openai()` | 569 | Anthropic tool_choice → OpenAI |
| `convert_openai_response_to_anthropic()` | 1758 | OpenAI 响应 → Anthropic 响应 |
| `parse_tool_arguments()` | 336 | JSON/XML/混合 → dict |
| `_extract_content_tool_calls()` | 415 | `<tools>` 文本提取 |
| `_StreamingToolsExtractor` | 463 | 流式 `<tools>` 状态机 |
| `_repair_truncated_json()` | 282 | 截断 JSON 修复 |

### 上下文管理 (Layer 2-5)

| 函数 | 用途 |
|------|------|
| `_classify_lifecycle_stage()` | 统一 char 阈值阶段判定 → 返回 stage_config |
| `_compress_content_pass()` | 合并 L2+L4 thinking 单次内容压缩 |
| `clear_old_tool_results()` | 语义优先级工具内容清除（legacy） |
| `_generate_tool_summary()` | 确定性清除摘要 (Cache 友好) |
| `_estimate_message_chars()` | 字符级 token 估算 |
| `truncate_messages_if_needed()` | 统一截断入口 (char-budget + keep_rounds) |
| `_apply_rounds_truncation()` | Rounds 策略实现 |
| `_extract_middle_summary_rules()` | 规则压缩摘要 |
| `_compress_middle_with_llm()` | LLM 压缩摘要 |
| `_merge_summaries_with_llm()` | LLM 合并两个摘要 |
| `_incremental_compress()` | 增量压缩 (缓存) |
| `_extract_keywords()` | 关键词提取 (TF-IDF MVP) |
| `_inject_keyword_context()` | 关键词匹配注入 |
| `strip_old_thinking_blocks()` | Thinking block 清除（legacy） |

### 检测与干预 (Layer 3)

| 函数 | 行号 | 用途 |
|------|------|------|
| `_detect_blocker_pattern()` | 1354 | 连续错误检测 |
| `_build_blocker_message()` | 1437 | 阻塞干预消息生成 |

### 工具过滤 (Layer 6)

| 函数 | 行号 | 用途 |
|------|------|------|
| `_filter_tools()` | 2732 | 动态工具定义过滤 |

### 可观测性 (Layer 8)

| 函数 | 行号 | 用途 |
|------|------|------|
| `log()` | 309 | 文本日志输出 |
| `log_request()` | 236 | JSONL 请求日志 |
| `log_metrics()` | 279 | JSONL Metrics 日志 |
| `_finalize_metrics()` | 2696 | 质量标记生成 |
| `_mc_put()` | 2726 | Metrics 数据安全追加 |
| `_build_status_html()` | 2453 | 状态页面 HTML |

---

## 10. 数据流示例

### 典型 Agentic 请求 (93 messages → 处理后 28 messages)

```
输入: 93 条消息, 43160 chars, 44 tools
  │
  ├─ Layer 2: Error translation
  │   2 条 tool_result 错误翻译为中文提示
  │
  ├─ Layer 2: Content compression (_compress_content_pass)
  │   37 个 tool_result 清除, 释放 12000 chars
  │   Read("board.js") → [cleared: Read("board.js")]\nimport React... (200 chars preview)
  │   cleared_files = {"board.js", "utils.py", ...}
  │   3 条旧 thinking blocks 剥离
  │
  ├─ Layer 3: Loop detection
  │   max_run = 2 (< threshold=3, 不触发)
  │
  ├─ Layer 3: Blocker detection
  │   未触发
  │
  ├─ Layer 3: Re-read detection
  │   0 次重读 (清除文件未被再次读取)
  │
  ├─ Layer 4: Date normalization
  │   "2026/06/05" → "DATE_PLACEHOLDER"
  │
  ├─ Layer 5: Rounds truncation
  │   Lifecycle stage: EXPANSION (chars=43160, budget=PROXY_CHARS_EXPANSION=90000)
  │   → 低于 budget，跳过截断
  │
  ├─ Layer 6: Tool filter
  │   44 → 15 tools (保留白名单 + 最近使用)
  │
  ├─ Layer 6: Format conversion
  │   Anthropic → OpenAI 格式
  │
  ├─ Layer 6: Forward
  │   POST → rapid-mlx:8081/chat/completions
  │
  ├─ Layer 7: Streaming response
  │   SSE 事件序列: message_start → content_block_start/delta/stop → message_delta/stop
  │
  └─ Layer 8: Observability
      log: "REQ_SUMMARY chars=43160 tools=44"
      log: "Tool clearing: 37 cleared, 12000 chars freed"
      log: "Context truncation (rounds): 61 dropped, 28 kept"
      metrics.jsonl: compression_ratio=0.48, quality_flags=[]
      requests.jsonl: duration_ms=3200
```

### 严重循环场景 (d278d9eb 类)

```
输入: 196 条消息, 328K chars, 44 tools
  │
  ├─ Layer 2: Tool clearing
  │   94 个 tool_result 清除 (几乎全部)
  │   cleared_files = {board.js, game.py, ...} (15 个文件)
  │
  ├─ Layer 3: Loop detection
  │   max_run = 25 (Read("board.js") 重复 25 次)
  │   → Level 3 触发!
  │     1. 替换最后 assistant 的 tool_use 为 text
  │     2. 注入 "CRITICAL: 工具已禁用" message
  │
  ├─ Layer 5: Rounds truncation
  │   截断 137 条消息, 压缩摘要
  │
  ├─ Layer 6: Tool filter
  │   Level 3 已在 Layer 3 处理, tools 列表不变
  │
  └─ 预期: 模型收到 "工具已禁用" + 纯文本历史 → 被迫文本回复 → 循环中断
```

---

## 11. Layer 交互关系图

```
                    输入消息流
                       │
        ┌──────────────▼──────────────┐
        │    Layer 2: 语义预处理       │
        │  ┌────────────────────────┐ │
        │  │ Error Translation      │ │
        │  │ Tool-Result Clearing   │──── 生成 cleared_files
        │  └────────────────────────┘ │
        └──────────────┬──────────────┘
                       │
        ┌──────────────▼──────────────┐
        │    Layer 3: 循环检测         │
        │  ┌────────────────────────┐ │
        │  │ Loop Detection ←───────│──── 使用 cleared_files
        │  │ Blocker Detection      │ │   (Re-read 检测)
        │  │ Escalating Intervention│ │
        │  │   ├ Level 1: 注入消息   │ │
        │  │   ├ Level 2: 移除工具   │──── 修改 body["tools"]
        │  │   └ Level 3: 替换响应   │──── 修改 raw_messages
        │  └────────────────────────┘ │
        └──────────────┬──────────────┘
                       │
        ┌──────────────▼──────────────┐
        │    Layer 4: 缓存优化         │
        │  ┌────────────────────────┐ │
        │  │ Date Normalization     │ │
        │  │ Thinking Strip         │ │
        │  │ Cleared Compression    │ │
        │  └────────────────────────┘ │
        └──────────────┬──────────────┘
                       │
        ┌──────────────▼──────────────┐
        │    Layer 5: 上下文截断       │
        │  ┌────────────────────────┐ │
        │  │ Adaptive Rounds        │ │
        │  │ Incremental Compress   │──── 使用 _summary_cache
        │  │ LLM/Rules/Static Chain │ │
        │  │ Keyword Index          │ │
        │  └────────────────────────┘ │
        └──────────────┬──────────────┘
                       │
        ┌──────────────▼──────────────┐
        │    Layer 6: 转发             │
        │  ┌────────────────────────┐ │
        │  │ Tool Filter            │ │
        │  │ Format Conversion      │ │
        │  │ Forward to Backend     │ │
        │  └────────────────────────┘ │
        └──────────────┬──────────────┘
                       │
                 Backend 响应
                       │
        ┌──────────────▼──────────────┐
        │    Layer 7: 响应控制         │
        │  ┌────────────────────────┐ │
        │  │ SSE Event Construction │ │
        │  │ Output Truncation      │ │
        │  │ JSON Repair            │ │
        │  │ XML→JSON Fallback      │ │
        │  └────────────────────────┘ │
        └──────────────┬──────────────┘
                       │
        ┌──────────────▼──────────────┐
        │    Layer 8: 可观测性         │
        │  ┌────────────────────────┐ │
        │  │ Text Log               │ │
        │  │ proxy_requests.jsonl   │ │
        │  │ proxy_metrics.jsonl    │ │
        │  │ Status Page            │ │
        │  └────────────────────────┘ │
        └─────────────────────────────┘
```

---

## 12. 配置参数总览

### 按功能分组

#### 请求控制

| 参数 | 默认 (local/cloud) | 说明 |
|------|-------------------|------|
| `PROXY_MAX_CONCURRENT` | 1 / 4 | 并发控制 |
| `PROXY_BACKEND_TIMEOUT` | 300 | 后端超时(秒) |
| `PROXY_MAX_TOKENS_OVERRIDE` | 0 | 强制 max_tokens |
| `PROXY_OUTPUT_TOKEN_LIMIT_RATIO` | 2.0 | 输出 token 倍率 |

#### 工具内容管理

| 参数 | 默认 (local/cloud) | 说明 |
|------|-------------------|------|
| `PROXY_CLEAR_ENABLED` | true / false | 清除开关 |
| `PROXY_CLEAR_THRESHOLD` | 15000 / 30000 | 字符阈值 |
| `PROXY_TOOL_KEEP` | 2 / 10 | 保留数量 |
| `PROXY_REREAD_PREVIEW_CHARS` | 200 | Read 预览长度 |
| `PROXY_FROZEN_HEAD` | 12 / 0 | Frozen Zone 保护前 N 条消息 |
| `PROXY_CLEAR_TAIL_FIRST` | true | 尾优先清除 |
| `PROXY_CONTENT_TOOLS_FALLBACK` | true | `<tools>` 回退 |

#### 循环与阻塞检测

| 参数 | 默认 | 说明 |
|------|------|------|
| `PROXY_LOOP_THRESHOLD` | 3 | Level 1 阈值 |
| `PROXY_LOOP_LEVEL2` | 6 | Level 2 阈值 |
| `PROXY_LOOP_LEVEL3` | 9 | Level 3 阈值 |
| `PROXY_TEXT_LOOP_ENABLED` | true | 文本输出循环检测开关 |
| `PROXY_TEXT_LOOP_THRESHOLD` | 3 | 连续相似文本触发阈值 |
| `PROXY_TEXT_LOOP_MIN_CHARS` | 100 | 最小文本长度 |
| `PROXY_TEXT_LOOP_SIMILARITY` | 0.85 | 文本相似度阈值 (0.0-1.0) |
| `PROXY_BLOCKER_ENABLED` | true / false | 阻塞检测开关 |
| `PROXY_BLOCKER_THRESHOLD` | 2 | 连续错误阈值 |

#### 统一生命周期阶段阈值 (char-based)

> **所有空间阈值统一使用 `_estimate_message_chars()` 为度量。**
> 阈值严格递增，保证报文增长时压缩力度单调加强。

| 参数 | 默认 (local/cloud) | 阶段 | 自动启用的策略 |
|------|-------------------|------|--------------|
| `PROXY_CLEAR_THRESHOLD` | 15000 / 30000 | INIT→GROWTH | L2 尾 40% 清除 |
| `PROXY_CHARS_GROWTH` | 40000 / 80000 | GROWTH→EXPANSION | L2 尾 60% + L4 thinking keep=5 |
| `PROXY_CHARS_EXPANSION` | 90000 / 200000 | EXPANSION→SATURATION | L2 全动态 + L4 keep=3 + L5 截断 |
| `PROXY_CHARS_SATURATION` | 180000 / 500000 | SATURATION→OOM_DANGER | Frozen 缩半 + 高丢率通知 |
| `PROXY_CHARS_OOM_DANGER` | 350000 / 1000000 | OOM_DANGER→PRE_TRUNC | Frozen=0 + hard truncation keep=3 |
| `PROXY_PRE_TRUNCATE_CHARS` | 400000 / — | PRE_TRUNC | keep_rounds=2 + OOM Safety |

**生命周期阶梯**:

```
chars →    15K       40K        90K        180K       350K     400K
           │         │          │           │          │        │
Stage:   INIT     GROWTH    EXPANSION   SATURATION  OOM_DANGER  PRE_TRUNC
L2+L4:   跳过    尾40%清除   尾60%清除+   全dynamic+   全量+      全量+
                 (无think)  think=5    think=3     think=1    think=1
L5截断:   关       关       预算触发    rounds=8   rounds=3  rounds=2
Frozen:   全12     12条       12条        6条         0条       0条
```
L5截断:   关       关       预算触发    rounds=8   rounds=3  rounds=2
Frozen:   全12     12条       12条        6条         0条       0条
```

#### 上下文截断

| 参数 | 默认 (local/cloud) | 说明 |
|------|-------------------|------|
| `PROXY_CTX_LIMIT_ENABLED` | true / false | 截断开关 |
| `PROXY_CTX_TRUNCATE_STRATEGY` | char | 策略选择 |
| `PROXY_CTX_KEEP_ROUNDS` | 8 | Rounds 保留轮数 |
| `PROXY_CTX_TOKEN_BUDGET` | 30000 | ⚠️ Deprecated — 使用 PROXY_CHARS_EXPANSION |
| `PROXY_CTX_TOKEN_RATIO` | 2.0 | 字符/token 比率（仅估算辅助） |
| `PROXY_CTX_KEEP_MESSAGES` | 30 | FIFO 保留数 |
| `PROXY_CTX_CHARS_LIMIT` | 150000 / 500000 | ⚠️ Deprecated — 使用 PROXY_CHARS_SATURATION |

#### 工具过滤

| 参数 | 默认 (local/cloud) | 说明 |
|------|-------------------|------|
| `PROXY_TOOL_FILTER_ENABLED` | true / false | 过滤开关 |
| `PROXY_TOOL_FILTER_MAX` | 20 | 触发阈值 |
| `PROXY_TOOL_FILTER_RECENT` | 5 | 扫描轮数 |

#### 关键词索引

| 参数 | 默认 | 说明 |
|------|------|------|
| `PROXY_HISTORY_INDEX` | rule | 索引模式 |
| `PROXY_HISTORY_TOP_K` | 5 | 注入条目数 |
| `PROXY_HISTORY_MAX_CHARS` | 500 | 注入字符上限 |

#### 可观测性

| 参数 | 默认 | 说明 |
|------|------|------|
| `PROXY_METRICS_ENABLED` | true | Metrics 开关 |
| `PROXY_METRICS_DIR` | logs | Metrics 目录 |
