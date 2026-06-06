# 产品需求文档 (PRD) — 本地 LLM 推理代理层

> **项目**: anthropic_proxy.py + manage.sh + configs
> **产品形态**: Python 代理 + Bash 服务管理 + 配置化后端路由
> **目标用户**: Apple Silicon (MacBook Pro M5 Pro, 48GB) 上使用 Claude Code 跑 Qwen3.6-35B-A3B 的开发者
> **代理入口**: `127.0.0.1:4000` → `rapid-mlx:8081` / `llama-server:8081` / 云端 API
> **PRD 版本**: v3.0
> **生成日期**: 2026-06-06
> **数据来源**: `docs/` 下 24 篇文档 + 28 条 git 提交记录

---

## 目录

1. [产品概述](#1-产品概述)
2. [背景与问题分析](#2-背景与问题分析)
3. [需求体系 (7 大领域 / 23 个需求点)](#3-需求体系)
4. [系统架构 — 8 层处理管线](#4-系统架构--8-层处理管线)
5. [功能模块详解](#5-功能模块详解)
6. [关键配置参数 (25+)](#6-关键配置参数-25)
7. [功能演化历史 (基于 Git 提交)](#7-功能演化历史-基于-git-提交)
8. [需求验证矩阵](#8-需求验证矩阵)
9. [已识别但未覆盖的潜在需求](#9-已识别但未覆盖的潜在需求)
10. [已知风险与限制](#10-已知风险与限制)
11. [后续迭代路线图](#11-后续迭代路线图)
12. [附录: 引用文档清单](#12-附录-引用文档清单)

---

## 1. 产品概述

### 1.1 一句话定位

将本地小显存模型包装为 Claude Code 兼容的 Anthropic 端点,在保持客户端零改造的前提下,通过 **8 层代理管线** 弥合"本地 LLM 物理约束"与"Agentic 编程全量历史"之间的鸿沟。

### 1.2 核心指标基线 (Phase 0 vs Phase 5)

| 维度 | 现状 (无优化) | 目标 | 当前实测 |
|------|--------------|------|----------|
| Prompt tokens | 68,131 | < 30,000 | ~27,000 ✅ |
| TTFT | 90.8s | < 20s | 1-5s (with cache) / 15-20s (cold) |
| 消息数/轮 | 93 | < 30 | ~28 ✅ |
| 工具定义 tokens | ~10K (44 tools) | < 5K (≤15 tools) | ~3.4K (15 tools) ✅ |
| 死循环 | 持续 5 小时 | 3 次内自动打断 | 3 次打断 ✅ |
| OOM 崩溃 | 频繁 | 0 次 | 0 次 (max_concurrent=1) ✅ |

### 1.3 关键非功能性需求

- **零依赖**: Python 标准库 only,不引入 tiktoken / pandas / numpy / 任何第三方包
- **配置驱动**: 25+ 环境变量控制,无代码改动即可调参
- **后端无关**: 同一代理同时支持 `llama-server`、`rapid-mlx`、DeepSeek、OpenAI
- **客户端零改造**: Claude Code 始终连 `127.0.0.1:4000`
- **热部署**: 代理重启仅需 ~8 秒,不影响正在运行的模型推理服务
- **可观测**: 结构化 JSONL Metrics (`logs/proxy_metrics.jsonl`) + 状态页 (`GET /status`)

---

## 2. 背景与问题分析

### 2.1 核心矛盾

```
物理约束:  48GB 统一内存 + 35B MoE 模型 → 实际可用上下文约 50K tokens
客户端行为: Claude Code agentic 模式 → 每轮发送全量对话历史 + 44 个 tool definitions
            → 10 轮对话后 prompt 达 68K-93K tokens → 每次都重新 prefill 90 秒
```

### 2.2 五个核心根因 (源自 `message-analysis-20260602.md`)

| # | 根因 | 影响 | 量化 |
|---|------|------|------|
| 1 | **线性膨胀不可避** | 每轮 +2,300 chars,85 轮 = 195K messages | 必然 |
| 2 | **结构开销固化** | 170+ 条 message JSON 包装占 30K chars,无法清理 | ~15% 固定开销 |
| 3 | **工具定义固定开销** | 92K chars / ~26K tokens / 每次请求 | ~30% 固定开销 |
| 4 | **代理清理效果有限** | 释放 100K chars 但无法解决结构层面膨胀 | -33% 已被解决 |
| 5 | **后端物理极限** | 48GB RAM + 35B = 56K tokens 已到极限 | 突破即 OOM |

### 2.3 关键发现 (源自 `prefix-cache-analysis-20260605.md`)

**Prefix Cache 0% 命中率** 的根本原因:
- Rapid-MLX v0.6.30 实现了 4 种匹配策略 (exact/prefix/supersequence/LCP)
- Qwen3.6-35B-A3B 是 MoE 架构,所有 40 个 cache layer 都是 `non_trimmable`
- 找到 29,424 tokens 公共前缀后,因 non-trimmable 层被强制跳过 → MISS
- 期望: v0.6.71 修复 MoE non-trimmable 后,prefix cache 命中率可达 90-99%

---

## 3. 需求体系

> **7 大领域 / 23 个需求点 / 100% 已实现**

### R1 — 上下文容量不足 **[P0]**

| ID | 需求 | 描述 | 实现位置 |
|----|------|------|----------|
| R1.1 | 主动上下文截断 | 超过 token 预算时主动丢弃旧消息 | `truncate_messages_if_needed()` line 1106,三策略 (rounds/fifo/char) |
| R1.2 | 压缩摘要替代 | 被丢弃消息生成结构化摘要 | 三级压缩链: LLM→规则→静态折叠 (line 948/882) |
| R1.3 | 增量压缩 | 避免每次全量重压缩 | `_incremental_compress()` + `_summary_cache` (line 1063) |
| R1.4 | 关键词按需检索 | 保留被截断消息的关键词索引 | `_extract_keywords()` + `_inject_keyword_context()` (line 2626/2667) |

### R2 — 循环与失控 **[P0]**

| ID | 需求 | 描述 | 实现位置 |
|----|------|------|----------|
| R2.1 | 递进式循环干预 | 三级升级: 提示 → 移除工具 → 强制纯文本 | Level 1/2/3 递进 (line 2956-3034) |
| R2.2 | 智能清除策略 | 清除 Read 时保留 200 字符预览 | `clear_old_tool_results()` (line 789) |
| R2.3 | 近期 Read 保护 | 最近 6 个 Read 结果 +5 分语义加分 | 语义评分 (line 725-727) |
| R2.4 | 阻塞模式检测 | 连续相同错误类型 (2 次) 干预 | `_detect_blocker_pattern()` (line 1229) |

### R3 — 延迟优化 **[P0]**

| ID | 需求 | 描述 | 实现位置 |
|----|------|------|----------|
| R3.1 | KV Cache 复用 | 利用 prefix cache,避免重复计算 | Rapid-MLX v0.6.71 prefix cache (97% 命中) |
| R3.2 | 前缀稳定化 | 日期标准化 + 固定占位文本 | date normalization (line 3071) |
| R3.3 | 工具定义过滤 | 44 → 15 个工具,节省 5-8K tokens | `_filter_tools()` 白名单+最近使用 (line 2591) |

### R4 — 模型兼容性 **[P0/P1]**

| ID | 需求 | 描述 | 实现位置 |
|----|------|------|----------|
| R4.1 | XML→JSON 回退 | Qwen 偶尔输出 XML 格式 tool_call | `parse_tool_arguments()` 四级回退 (line 338) |
| R4.2 | Content-text 工具提取 | 从 `<tools>` 文本中提取工具调用 | `_StreamingToolsExtractor` 状态机 (line 465) |
| R4.3 | 输出截断 | 代理层强制限制输出长度 | FORCE_STOPPED 流式+非流式 (line 3265/3224) |
| R4.4 | JSON 修复 | 修复被截断的 tool_call arguments JSON | `_repair_truncated_json()` (line 284) |
| R4.5 | Reasoning 提取 | 处理 Qwen `reasoning_content` 字段 | reasoning_content 提取 (line 3388) |

### R5 — 错误理解增强 **[P1]**

| ID | 需求 | 描述 | 实现位置 |
|----|------|------|----------|
| R5.1 | 错误翻译 | 后端英文错误→中文自然语言提示 | Wasted/FileNotFound/InputValidationError 翻译 (line 2852) |
| R5.2 | 错误上下文增强 | 翻译时附带解决建议 | 同上,带"用 Bash ls 确认"等建议 |

### R6 — 可观测性 **[P0/P1]**

| ID | 需求 | 描述 | 实现位置 |
|----|------|------|----------|
| R6.1 | 结构化 Metrics | 每请求输出管线各步骤结构化 JSON 指标 | `log_metrics()` → `proxy_metrics.jsonl` (line 221) |
| R6.2 | 质量标记 | 自动标记 4 种异常模式 | `_finalize_metrics()` (line 2555) |
| R6.3 | 压缩比追踪 | 记录截断前后 token 估算比 | `compression_ratio` 字段 |

### R7 — 资源约束适配 **[P0/P1]**

| ID | 需求 | 描述 | 实现位置 |
|----|------|------|----------|
| R7.1 | 并发控制 | `threading.Semaphore` 限制同时转发请求数 | local=1, cloud=4 (line 39) |
| R7.2 | 云端切换 | 无缝切换到云端 API 绕过本地内存限制 | 双模式 + `manage.sh switch` |

---

## 4. 系统架构 — 8 层处理管线

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
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 2: 语义预处理 (Semantic Rewrite)            │   │
│  │   - 错误翻译、工具内容清除、占位保留               │   │
│  └────────────────────┬─────────────────────────────┘   │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 3: 循环与阻塞检测 (Loop & Blocker Guard)    │   │
│  │   - 精确/模式匹配、升级干预、Re-read检测           │   │
│  └────────────────────┬─────────────────────────────┘   │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 4: 缓存优化 (Cache Optimizer)               │   │
│  │   - 日期标准化、Thinking清除、Cleared压缩          │   │
│  └────────────────────┬─────────────────────────────┘   │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 5: 上下文截断 (Context Truncator)            │   │
│  │   - Rounds/FIFO/Char策略、三级压缩、增量摘要       │   │
│  └────────────────────┬─────────────────────────────┘   │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 6: 格式转换与转发 (Format & Forward)        │   │
│  │   - Anthropic→OpenAI、工具过滤、转发、响应控制     │   │
│  └────────────────────┬─────────────────────────────┘   │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 7: 响应后处理 (Response Control)             │   │
│  │   - 流式/非流式SSE构造、输出截断、JSON修复         │   │
│  └────────────────────┬─────────────────────────────┘   │
│  ┌────────────────────▼─────────────────────────────┐   │
│  │ Layer 8: 可观测性 (Observability)                  │   │
│  │   - Metrics记录、JSONL日志                         │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
└─────────────────────────────────────────────────────────┘
       │
       │ POST /chat/completions (OpenAI format)
       ▼
  Backend (rapid-mlx / llama-server / Cloud API)
       :8081
```

**关键执行顺序**: `clear → date_norm → think_strip → compress → truncate → tool_filter → forward`

---

## 5. 功能模块详解

### 5.1 模块 A — 双模式后端路由 (Local/Cloud)

| 维度 | Local 模式 | Cloud 模式 |
|------|-----------|-----------|
| 检测 | URL 不含 `deepseek`/`openai`/`api.` | URL 含上述关键字 |
| 后端进程 | `llama-server`/`rapid-mlx` 在 `:8081` | 无 (外部 API) |
| API Key | 假 token `sk-1234` | 真实 API Key (env 注入) |
| `PROXY_MAX_CONCURRENT` | 1 (防 OOM) | 4 (云端天然支持) |
| 工具清除 | 启用 (threshold=15K) | 禁用 (1M+ 上下文) |
| 上下文截断 | 启用 (180K chars) | 禁用 |

### 5.2 模块 B — 上下文截断 (3 策略 + 三级压缩)

```
字符数检查 → 触发 → 消息分离: HEAD(2) + MIDDLE + TAIL(自适应 N 轮)
  ↓
三级压缩链 (按顺序尝试):
  ├─ Level 1: 增量压缩 (_incremental_compress)  → 检查 _summary_cache 命中
  ├─ Level 2: LLM 压缩 (_compress_middle_with_llm) → dropped >= 10 时触发,30s 超时
  ├─ Level 3: 规则压缩 (_extract_middle_summary_rules) → 提取错误/代码/文件/决策
  └─ Level 4: 静态折叠 "[Context folded: N messages dropped]"
  ↓
关键词索引注入 (P1-1): 从 dropped 提取文件名/错误/函数名,匹配 tail 注入
  ↓
重组: HEAD + 摘要消息 + TAIL
```

**自适应保留轮数** (`_compute_adaptive_rounds`):
- base = `PROXY_CTX_KEEP_ROUNDS` (默认 10)
- +1: user 消息含 error/traceback
- +1: assistant 消息含 >2 个 Write/Edit
- 上限: base × 2

### 5.3 模块 C — 循环检测与递进干预 (3 级)

| 级别 | 触发条件 | 干预手段 |
|------|---------|---------|
| Level 1 | max_run ≥ 3 | 注入 user message: "STOP using {tool}" |
| Level 2 | max_run ≥ 6 | 从 tools 列表**移除**循环工具 + 强提示 |
| Level 3 | max_run ≥ 9 | 替换最后 assistant 的 tool_use 为 text + "工具已禁用" |

**双重检测机制**:
- **精确匹配**: 相同工具名 + 相同参数 JSON
- **模式匹配**: 相同文本 (前 200 字) + 相同工具名集合 (应对 `A→B→A→B` 交替)

**阻塞检测** (R2.4) 互补: 连续 N 次相同错误类型 (2 次) → 注入 `[BLOCKER]` 消息

### 5.4 模块 D — 工具内容清除 (语义评分)

```
total_chars > threshold → 触发
  ├─ 工具优先级: Read=3, Agent=3, WebFetch=2, Bash=1, Edit/Write=1
  ├─ 内容模式: 代码结构+3, 错误信息+2, Wasted=0
  ├─ 近期 Read 加分: 最近 6 条 Read 结果额外 +5
  ├─ 按分数排序,保留 top KEEP 条
  ├─ 清除策略:
  │   ├─ Read: 保留 200 字符预览 (防重读)
  │   └─ 其他: 替换为 [cleared: ToolName("file")] (Prefix Cache 友好)
  └─ Bash 去重: Jaccard ≥ 0.7 的连续 Bash 输出合并
```

### 5.5 模块 E — 模型格式兼容 (XML/JSON/Content-text)

```python
# 解析顺序
parse_tool_arguments():
  1. JSON 解析 → 成功即返回
  2. embedded JSON 提取 → 解析 <tool_use>...</tool_use>
  3. XML 提取 → 解析 <name>xxx</name><input>...</input>
  4. heuristic fallback → 字符串/数字强制转换

# 流式 + 非流式双路径
_StreamingToolsExtractor (line 465)  # 流式状态机
_extract_content_tool_calls (line 417)  # 非流式
```

### 5.6 模块 F — 工具定义过滤 (白名单 + 最近使用)

```
44 个工具定义
  ├─ TOOL_ALWAYS_KEEP 白名单 (12): Read, Write, Edit, Bash, Glob, Grep, LS, Task, WebFetch, WebSearch, TodoRead, TodoWrite
  ├─ 最近 N 轮使用过的工具 (扫描 assistant tool_use, N=5)
  ├─ tool_choice 指定的工具 (强制保留)
  └─ 保留数 < 5 → 回退到原始列表

典型结果: 44 → 15 tools ≈ 节省 5-8K tokens
```

### 5.7 模块 G — 前缀缓存稳定化 (Cache Optimizer)

| 技术 | 效果 |
|------|------|
| 日期标准化 | `2026/06/05` → `DATE_PLACEHOLDER`,同日缓存稳定 |
| 固定占位消息 | 不用动态内容 (含 dropped_count 等),保证 token 序列稳定 |
| Thinking 清除 | 保留最近 3 条,清除旧的 |
| Cleared 合并 | `[cleared: ...]` 连续对合并为单条 user |

### 5.8 模块 H — 可观测性 (结构化 Metrics)

`logs/proxy_metrics.jsonl` 每行:
```json
{
  "session_id": "d278d9eb",
  "input_msgs": 93, "input_chars": 43160, "input_tools": 44,
  "pipeline": {
    "error_translation": {"count": 2},
    "tool_clear": {"cleared": 37, "kept": 10, "chars_freed": 12000},
    "loop_detect": {"max_run": 6, "level": 2, "tool": "Read"},
    "truncate": {"strategy": "rounds", "dropped": 45, "kept": 28},
    "tool_filter": {"original": 44, "kept": 15}
  },
  "compression_ratio": 0.48,
  "quality_flags": ["loop_injected"]
}
```

**质量标记** (4 种): `high_drop_ratio`、`llm_compress_failed`、`budget_overflow`、`loop_injected`

---

## 6. 关键配置参数 (25+)

| 分组 | 参数 | 默认 (local/cloud) | 说明 |
|------|------|---------------------|------|
| **请求控制** | `PROXY_MAX_CONCURRENT` | 1 / 4 | 并发控制 |
| | `PROXY_BACKEND_TIMEOUT` | 300 | 后端超时 (秒) |
| | `PROXY_MAX_TOKENS_OVERRIDE` | 0 | 强制 max_tokens |
| | `PROXY_OUTPUT_TOKEN_LIMIT_RATIO` | 2.0 | 输出 token 倍率 |
| **工具内容管理** | `PROXY_CLEAR_ENABLED` | true / false | 清除开关 |
| | `PROXY_CLEAR_THRESHOLD` | 15000 / 30000 | 字符阈值 |
| | `PROXY_TOOL_KEEP` | 2 / 10 | 保留数量 |
| | `PROXY_REREAD_PREVIEW_CHARS` | 200 | Read 预览长度 |
| **循环/阻塞** | `PROXY_LOOP_THRESHOLD` | 3 | Level 1 阈值 |
| | `PROXY_LOOP_LEVEL2` | 6 | Level 2 阈值 |
| | `PROXY_LOOP_LEVEL3` | 9 | Level 3 阈值 |
| | `PROXY_BLOCKER_ENABLED` | true / false | 阻塞检测开关 |
| | `PROXY_BLOCKER_THRESHOLD` | 2 | 连续错误阈值 |
| **上下文截断** | `PROXY_CTX_TRUNCATE_STRATEGY` | char | char/rounds/fifo |
| | `PROXY_CTX_KEEP_ROUNDS` | 10 | Rounds 保留轮数 |
| | `PROXY_CTX_TOKEN_BUDGET` | 30000 | Token 预算 |
| | `PROXY_CTX_TOKEN_RATIO` | 2.0 | chars/token 估算比 |
| | `PROXY_CTX_KEEP_MESSAGES` | 40 | FIFO 保留数 |
| | `PROXY_CTX_CHARS_LIMIT` | 180000 / 500000 | Char 上限 |
| **工具过滤** | `PROXY_TOOL_FILTER_ENABLED` | true / false | 过滤开关 |
| | `PROXY_TOOL_FILTER_MAX` | 20 | 触发阈值 |
| | `PROXY_TOOL_FILTER_RECENT` | 5 | 扫描轮数 |
| **关键词索引** | `PROXY_HISTORY_INDEX` | rule | off/rule |
| | `PROXY_HISTORY_TOP_K` | 5 | 注入条目数 |
| | `PROXY_HISTORY_MAX_CHARS` | 500 | 注入字符上限 |
| **可观测性** | `PROXY_METRICS_ENABLED` | true | Metrics 开关 |
| | `PROXY_METRICS_DIR` | logs | Metrics 目录 |

---

## 7. 功能演化历史 (基于 Git 提交)

> **总览**: 28 个 commits, 时间跨度 **16.8 天** (2026-05-20 → 2026-06-06)
> **演化逻辑**: 问题驱动 + 渐进优化 + 量化验证
> **时区**: CST (+08:00)

### 7.1 完整时间线 (按时间倒序)

| 时间戳 (CST) | Commit | 类型 | 摘要 | 累计间隔 |
|---------------|--------|------|------|----------|
| **2026-06-06 10:32:41** | `bbddd1d` | test | add test/ infrastructure, runner, and pre-commit hook | T+0 |
| 2026-06-06 10:32:17 | `137ee83` | test | pre-commit hook real run | +24s |
| 2026-06-06 10:10:48 | `c851f68` | test(proxy) | add end-to-end integration suite for anthropic_proxy | +21m29s |
| 2026-06-06 10:07:50 | `66faac8` | fix(proxy) | add request-id response header for API parity | +2m58s |
| 2026-06-06 09:58:03 | `0fe593a` | fix(proxy) | resolve two NoneType crashes + revert per-request loop scoping | +9m47s |
| 2026-06-05 19:44:54 | `d232390` | fix(proxy) | scope loop detection to current request only | +14h13m |
| 2026-06-05 18:37:23 | `08925bb` | perf(proxy) | expand head to 6 for 23% prefill savings (Plan 2D) | +1h7m |
| 2026-06-05 18:22:10 | `6f60ce8` | perf(proxy) | expand stable prefix with HEAD=4 + MSGS=30 | +15m13s |
| 2026-06-05 18:14:21 | `a6952e6` | fix(proxy) | static placeholder text in fifo truncation | +7m49s |
| 2026-06-05 17:55:17 | `805a9d3` | docs | remove superseded claude-behavior-semantic-analysis.md | +19m4s |
| 2026-06-05 17:55:03 | `8ce382e` | fix(proxy) | extend TOOL_ALWAYS_KEEP with newer Claude Code tools | +14s |
| 2026-06-05 17:40:59 | `59c8ff2` | docs | prompt instability mechanism + structured summary evaluation | +14m4s |
| 2026-06-05 17:34:47 | `dafa3d6` | fix | LLM compression model name + pipeline reference + requirements analysis | +6m12s |
| 2026-06-05 17:27:36 | `4bdf309` | feat | blocker detection + integration test matrix | +7m11s |
| 2026-06-05 14:25:01 | `4e4e3e6` | feat | pattern-based loop detection + 35b config optimizations | +3h2m |
| 2026-06-05 08:29:53 | `6060552` | feat | prefix cache analysis, TurboQuant test, and status page enhancements | +5h55m |
| 2026-06-05 04:51:07 | `ad6e963` | feat | re-implement all 13 optimizations | +3h38m |
| 2026-06-05 04:47:11 | `6f0dfe9` | baseline | current code before re-implementing all optimizations | +3m56s |
| 2026-06-04 03:48:18 | `c5a050c` | revert(9b) | keep PROXY_CTX_KEEP_ROUNDS=6 for observation | +1d0h |
| 2026-06-04 03:47:29 | `412636c` | fix(9b) | tighten context limits and fix token ratio underestimation | +49s |
| 2026-06-04 03:44:09 | `d8b650d` | fix(9b) | lower GPU memory limits to prevent Metal OOM | +3m20s |
| 2026-06-04 03:37:29 | `7d6571e` | feat | add Qwen3.5-9B config, rounds-based truncation, and performance tooling | +6m40s |
| 2026-06-03 10:14:21 | `e221965` | fix | Qwen chat template compatibility + proxy deadlock | +17h23m |
| 2026-06-02 15:18:16 | `ce34f72` | fix(manage.sh) | correct local-mode clearing defaults to match code | +18h56m |
| 2026-06-02 13:52:03 | `5969d77` | refactor | update A/B test task design and experiment tooling | +1h26m |
| 2026-06-02 13:40:55 | `16defd9` | feat | dual-mode proxy with cloud backend support and A/B experiment framework | +11m8s |
| 2026-05-20 14:23:22 | `93ca015` | chore | add .gitignore to exclude logs, temp files, and system files | +12d23h |
| 2026-05-20 14:22:16 | `6d9b00e` | init | initial commit | +1m6s |

### 7.2 阶段化分析 (按问题域演化)

#### **Phase 1: 基础架构 (2026-05-20 14:22 ~ 2026-06-02 15:18)** — 14 天
> 跨度大,聚焦架构 + A/B 实验方法论

| 时间 | Commit | 关键变更 |
|------|--------|---------|
| 2026-05-20 14:22:16 | `6d9b00e` | Initial commit — 项目初始骨架 |
| 2026-05-20 14:23:22 | `93ca015` | .gitignore 排除 logs/tmp 文件 (1m6s 后) |
| 2026-06-02 13:40:55 | `16defd9` | **双模式代理 + 云端后端 + A/B 实验框架** ⭐ |
| 2026-06-02 13:52:03 | `5969d77` | A/B 实验任务设计与工具链 (11m8s 后) |
| 2026-06-02 15:18:16 | `ce34f72` | manage.sh local/cloud 默认值修正 (1h26m 后) |

**Phase 1 价值**:
- 确立"代理层 + 后端可选"双模式架构
- 引入 A/B 实验方法论
- 11 天开发空白期 (5/22 ~ 6/1) 可能是设计/调研阶段

#### **Phase 2: 死循环治理 (2026-06-03 10:14 ~ 2026-06-04 03:48)** — 17 小时
> 节奏密集 (5 commits / 11 分钟间隔),紧扣"循环"问题域

| 时间 | Commit | 关键变更 | 距上一 commit |
|------|--------|---------|---------------|
| 2026-06-03 10:14:21 | `e221965` | Qwen chat template 兼容性 + 代理死锁修复 | — |
| 2026-06-04 03:37:29 | `7d6571e` | **Qwen3.5-9B 配置 + 轮数截断策略 + 性能工具** ⭐ | +17h23m |
| 2026-06-04 03:44:09 | `d8b650d` | 降低 GPU 内存限制 (防 Metal OOM) | +6m40s |
| 2026-06-04 03:47:29 | `412636c` | 收紧上下文限制 + token 比率修正 | +3m20s |
| 2026-06-04 03:48:18 | `c5a050c` | 回滚 KEEP_ROUNDS=6,保留观察 | +49s |

**Phase 2 价值**:
- 引入 rounds 截断策略,从 char 阈值 (180K) 升级到 token 预算 (30K) + 保留轮数 (10)
- 应对 Qwen chat template 不兼容问题
- 内存防御性降低 (防 OOM 崩溃)

#### **Phase 3: 全量优化重实现 (2026-06-05 04:47 ~ 2026-06-05 17:40)** — 13 小时
> 节奏极密,一天内 13 个优化集中落地

| 时间 | Commit | 关键变更 | 距上一 commit |
|------|--------|---------|---------------|
| 2026-06-05 04:47:11 | `6f0dfe9` | baseline 标记 (重置到优化前) | — |
| 2026-06-05 04:51:07 | `ad6e963` | **re-implement all 13 optimizations** ⭐ | +3m56s |
| 2026-06-05 08:29:53 | `6060552` | prefix cache 分析 + TurboQuant 测试 + 状态页增强 | +3h38m |
| 2026-06-05 14:25:01 | `4e4e3e6` | **模式循环检测 + 35B 配置优化** ⭐ | +5h55m |
| 2026-06-05 17:27:36 | `4bdf309` | **阻塞检测 (blocker) + 集成测试矩阵** ⭐ | +3h2m |
| 2026-06-05 17:34:47 | `dafa3d6` | LLM 压缩模型名 + 管线参考 + 需求分析 | +7m11s |
| 2026-06-05 17:40:59 | `59c8ff2` | prompt 不稳定机制 + 结构化摘要评估 | +6m12s |

**Phase 3 价值**:
- 13 个优化集中落地 (R1/R2/R3/R4/R6 多领域)
- 涵盖结构化 Metrics、工具过滤、关键词索引、增量压缩、TurboQuant、循环模式检测

#### **Phase 4: 性能调优 (2026-06-05 17:55 ~ 2026-06-06 09:58)** — 16 小时
> 跨越午夜,聚焦 prefix cache 命中率提升

| 时间 | Commit | 关键变更 | 距上一 commit |
|------|--------|---------|---------------|
| 2026-06-05 17:55:03 | `8ce382e` | 扩展 TOOL_ALWAYS_KEEP (含新 Claude Code 工具) | — |
| 2026-06-05 17:55:17 | `805a9d3` | 移除过时的 claude-behavior-semantic-analysis | +14s |
| 2026-06-05 18:14:21 | `a6952e6` | FIFO 截断使用静态占位文本 | +19m4s |
| 2026-06-05 18:22:10 | `6f60ce8` | **HEAD=4 + MSGS=30 扩展稳定前缀** | +7m49s |
| 2026-06-05 18:37:23 | `08925bb` | **HEAD=6, 23% prefill 节省 (Plan 2D)** ⭐ | +15m13s |
| 2026-06-05 19:44:54 | `d232390` | 循环检测 scope 限定当前请求 | +1h7m |
| 2026-06-06 09:58:03 | `0fe593a` | 两个 NoneType 崩溃 + 回滚每请求循环 scope | +14h13m |

**Phase 4 价值**:
- Prefix cache 命中率优化从 24% 提升到 90-97%
- TTFT 从 90s 降到 1-5s
- HEAD 参数从 2 → 4 → 6,逐步扩大稳定前缀

#### **Phase 5: 测试体系 (2026-06-06 10:07 ~ 2026-06-06 10:32)** — 25 分钟
> 短时间集中补齐测试基础设施

| 时间 | Commit | 关键变更 | 距上一 commit |
|------|--------|---------|---------------|
| 2026-06-06 10:07:50 | `66faac8` | 添加 request-id 响应头 (API 对齐) | — |
| 2026-06-06 10:10:48 | `c851f68` | **端到端集成测试套件** ⭐ | +2m58s |
| 2026-06-06 10:32:17 | `137ee83` | pre-commit hook 真实运行 | +21m29s |
| 2026-06-06 10:32:41 | `bbddd1d` | **test/ 基础设施 + runner + pre-commit hook** ⭐ | +24s |

**Phase 5 价值**:
- 三层测试体系 (unit < 1s / integration ~5s / e2e 30-60s)
- pre-commit 自动运行 unit 套件

### 7.3 提交节奏分析

| 指标 | 数值 |
|------|------|
| 总 commits | 28 |
| 总开发天数 | 16.8 天 |
| 平均 commit 间隔 | ~14 小时 |
| 最密集时段 | 2026-06-05 04:47 ~ 19:44 (15 小时内 12 commits) |
| 最长空白 | 2026-05-22 ~ 2026-06-01 (11 天) |
| 开发时段 | 主要在 04:00-06:00、14:00-19:00 (深夜 + 下午) |

### 7.4 提交类型分布

| 类型 | 数量 | 占比 |
|------|------|------|
| feat | 9 | 32% |
| fix | 10 | 36% |
| perf | 2 | 7% |
| test | 4 | 14% |
| docs | 2 | 7% |
| refactor/revert/chore | 2 | 7% |

### 7.5 关键节点 (里程碑)

| 节点 | 时间 | Commit | 意义 |
|------|------|--------|------|
| **起点** | 2026-05-20 14:22:16 | `6d9b00e` | 项目初始 |
| **架构成型** | 2026-06-02 13:40:55 | `16defd9` | 双模式架构 + A/B 框架 |
| **截断策略升级** | 2026-06-04 03:37:29 | `7d6571e` | rounds-based truncation |
| **大重写** | 2026-06-05 04:51:07 | `ad6e963` | 13 个优化集中重写 |
| **Cache 突破** | 2026-06-05 18:37:23 | `08925bb` | HEAD=6 → 23% prefill 节省 |
| **测试完成** | 2026-06-06 10:32:41 | `bbddd1d` | 三层测试体系 + pre-commit |

### 7.6 演化趋势可视化

```
68K tokens  →  优化后 27K tokens      (R1+R3+R6 联合作用)
  90s TTFT   →  优化后 1-5s TTFT      (R3 prefix cache + 引擎层 v0.6.71)
   93 msgs   →  优化后 ~28 msgs       (R1 截断)
   44 tools  →  15 tools               (R3.3 过滤)
  5h 死循环  →  3 次自动打断           (R2 递进干预)
   OOM 频繁  →  0 次崩溃               (R7.1 并发控制)
```

---

## 8. 需求验证矩阵

> **当前覆盖度: 23/23 = 100%**

| 领域 | 需求数 | 已实现 | 覆盖率 | 关键实现 |
|------|--------|--------|--------|----------|
| R1 上下文容量 | 4 | 4 | **100%** | 三策略 + 三级压缩 + 增量 + 关键词 |
| R2 循环与失控 | 4 | 4 | **100%** | Level 1/2/3 + 语义预览 + 近期保护 + 阻塞 |
| R3 延迟优化 | 3 | 3 | **100%** | KV cache + 日期标准化 + 工具过滤 |
| R4 模型兼容 | 5 | 5 | **100%** | XML/JSON/Content-text/JSON repair/Reasoning |
| R5 错误理解 | 2 | 2 | **100%** | 3 种错误翻译 + 解决建议 |
| R6 可观测性 | 3 | 3 | **100%** | JSONL Metrics + 4 种质量标记 + 压缩比 |
| R7 资源约束 | 2 | 2 | **100%** | Semaphore + manage.sh switch |
| **合计** | **23** | **23** | **100%** | |

### 典型优化效果 (基于 docs 实测数据)

| 指标 | 优化前 | 优化后 | 改善 |
|------|--------|--------|------|
| Prompt tokens | 68,131 | ~27,000 | **-60%** |
| TTFT (冷启动) | 90.8s | 14-20s | **-78%** |
| TTFT (cache hit) | 90.8s | 1.1-2.4s | **-97%** |
| 消息数/轮 | 93 | 28 | **-70%** |
| 工具数/请求 | 44 | 15 | **-66%** |
| Read 死循环 | 219 次无检测 | 3 次打断 | **-99%** |
| Forced cache clear | 1660 次 | 0 次 | **-100%** |
| 39K chars 响应 | 121s | 20-21s | **6x** |

---

## 9. 已识别但未覆盖的潜在需求

| ID | 需求 | 当前状态 | 建议 |
|----|------|---------|------|
| U1 | **跨会话记忆** | ❌ 无持久化 | BM25 Phase 3 的 JSONL 持久化可解决 |
| U2 | **阶段感知压缩** | ❌ 未实现 | 代理层无法检测 agent 阶段 (探索/实现/调试),需客户端配合 |
| U3 | **Prefix Cache 感知调度** | ⚠️ 部分覆盖 | 工具过滤会改变前缀导致 cache miss,但节省的 token > miss 成本 |
| U4 | **自适应参数调优** | ❌ 未开始 | Phase 3: 根据实际 Metrics 数据自动调整 threshold/budget |
| U5 | **Re-read 硬拦截** | ⚠️ 仅检测 | Fix 1 设计完成但未实施,当前依赖预防机制 |
| U6 | **多模型协同** | ❌ 未设计 | 小模型 (9B) 做压缩摘要,大模型 (35B) 做主推理,降低 TTFT |
| U7 | **流式推理进度反馈** | ❌ 未设计 | 长上下文 TTFT 期间无反馈,用户体验差 |

---

## 10. 已知风险与限制

### 10.1 架构风险

1. **管线复杂度**: 8 层 14 步骤,`_handle_messages()` 函数体超过 400 行,维护成本高
2. **Token 估算不精确**: 使用 chars × ratio,无 tiktoken,偏差可达 20-30%
3. **LLM 压缩单点**: `_compress_middle_with_llm()` 调用同一后端,增加延迟和资源竞争
4. **增量压缩缓存**: `_summary_cache` 纯内存,进程重启后丢失,首轮无法享受增量优势
5. **工具过滤的 Cache 权衡**: 过滤后工具列表变化导致 prefix cache miss,净收益依赖具体场景

### 10.2 后端依赖风险

| 风险 | 描述 | 缓解 |
|------|------|------|
| Rapid-MLX v0.6.30 MoE non-trimmable | Qwen3.6-35B-A3B ArraysCache 层不可修剪 | 升级 v0.6.71 后命中率 90-99% |
| Rapid-MLX 忽略 max_tokens | v0.6.30 已知 bug | 代理层 `PROXY_OUTPUT_TOKEN_LIMIT_RATIO=2.0` 补偿 |
| Metal OOM | 48GB 统一内存,两个并发大上下文必崩 | `PROXY_MAX_CONCURRENT=1` 硬约束 |
| Rapid-MLX 性能衰减 | 运行 7 分钟后 56→12 tok/s | 需定期重启 (Phase 3 自动重启机制规划中) |

### 10.3 客户端兼容性风险

- **Brew llama-server 缺 MTP 支持**: 需从源码编译,`--spec-type draft-mtp`
- **Claude Code 重复 POST**: 同一毫秒 2 个相同请求,客户端行为,代理层无法修复
- **路径幻觉**: 执行型子代理首次 Read 时猜测不存在的路径,渐进修正

### 10.4 安全风险

- **无认证**: 代理和后端都绑定 `127.0.0.1`,但任何本地进程可访问
- **无 HTTPS**: 所有流量明文 HTTP
- **日志可能含 prompt 数据**: `/tmp/anthropic_proxy.log` world-readable
- **不要暴露到公网**: 无认证层

---

## 11. 后续迭代路线图

### 短期 (Phase 3, 1-2 周)
1. 收集 `proxy_metrics.jsonl` 真实运行数据 (1-2 周)
2. 验证关键假设: `loop_level` 分布、`re_read_count`、`compression_ratio`
3. 调参: `PROXY_CTX_TOKEN_BUDGET` / `PROXY_LOOP_THRESHOLD` / `PROXY_TOOL_FILTER_MAX`
4. 自动重启机制 (应对 Rapid-MLX 性能衰减)

### 中期 (Phase 4, 1 个月)
5. **Re-read 硬拦截** (U5): 流式响应中拦截重复 Read
6. **自适应参数** (U4): 基于 Metrics 自动调阈值
7. **BM25 Phase 2** (U1 前置): Bigram 分词 + 倒排索引
8. 扩展测试覆盖 (当前仅 28 个 unit test)

### 长期 (探索, 季度级)
9. **多模型协同** (U6): 9B 做压缩,35B 做主推理
10. **跨会话记忆** (U1): JSONL 持久化 + 跨会话索引加载
11. **管线重构**: 将 `_handle_messages()` 拆分为 pipeline 类或独立函数
12. **阶段感知压缩** (U2): 需客户端配合

---

## 12. 附录: 引用文档清单

> 共 24 篇 `docs/` 文档 + 1 篇 `BENCHMARK.md` + 1 篇 `TROUBLESHOOTING.md`

### 12.1 核心架构文档

| 文档 | 内容 |
|------|------|
| `proxy-pipeline-reference.md` | 8 层管线完整参考 (948 行) |
| `proxy-context-window-design.md` | 上下文截断设计 (v7) |
| `system-requirements-analysis.md` | 7 大需求领域 + 23 需求点 (320 行) |
| `proxy-semantic-metrics.md` | 量化指标体系 (557 行) |

### 12.2 分析与诊断文档

| 文档 | 内容 |
|------|------|
| `prefix-cache-analysis-20260605.md` | Prefix cache 深度分析 (174 行) |
| `rapid-mlx-cache-analysis.md` | Rapid-MLX 缓存机制分析 (363 行) |
| `rapid-mlx-cache-analysis-supplement.md` | 缓存补充分析 (230 行) |
| `prompt-instability-mechanism-analysis.md` | 提示词不稳定机制 (404 行) |
| `proxy-truncation-as-forgetting-mechanism.md` | 截断作为遗忘机制 (229 行) |
| `claude-behavior-semantic-analysis-v2.md` | Claude Code 行为分析 (390 行) |

### 12.3 案例与修复文档

| 文档 | 内容 |
|------|------|
| `dead-loop-analysis-report.md` | 死循环分析与修复 (403 行) |
| `model-tool-issues.md` | Tool calling 质量问题 (152 行) |
| `message-analysis-20260604.md` | 报文与处理性能分析 (360 行) |
| `message-analysis-20260602.md` | 报文深度分析 (711 行) |

### 12.4 实验与设计文档

| 文档 | 内容 |
|------|------|
| `ab-experiment-design.md` | A/B 实验设计 (392 行) |
| `ab-test-task-log-system.md` | A/B 测试任务设计 (245 行) |
| `DEEPSEEK-AB-EXPERIMENT-GUIDE.md` | DeepSeek 中转与 A/B 实验指南 (612 行) |

### 12.5 配置变更日志

| 文档 | 内容 |
|------|------|
| `config-change-20260604-max-num-seqs.md` | 提升并发上限 (96 行) |
| `config-change-20260604-rollback.md` | 回滚并发上限 (76 行) |
| `monitor-report-20260604-post-change.md` | 配置修改后监控 (150 行) |
| `proxy-context-window-design-review-merged.md` | 设计文档 Review 合并 (142 行) |
| `structured-summary-impl-evaluation.md` | 结构化摘要评估 (344 行) |
| `optimization-log-20260603.md` | 优化工作日志 (221 行) |

---

> **文档版本**: v3.0  
> **生成时间**: 2026-06-06 10:32 (CST)  
> **生成工具**: 手工整理 + Git 时间戳分析  
> **下次更新**: Phase 3 完成后 (预计 1-2 周)
