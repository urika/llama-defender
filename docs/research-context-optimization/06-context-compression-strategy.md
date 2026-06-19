# 上下文压缩管理策略总览

> 状态：Phase 1-3 已实施  
> 日期：2026-06-18  
> 定位：对 `anthropic_proxy.py` 上下文压缩管理体系进行整体梳理，整合 Phase 1/2/3 能力，形成统一策略视图。

---

## 1. 目标与约束

### 1.1 核心目标

在 Claude Code 与本地后端（rapid-mlx / llama-server）之间构建一个**可持续的上下文管理机制**：

1. **降低单轮 prompt 长度**，将 TTFT 控制在可用范围（本地 48GB 硬约束下目标 22-35s）。
2. **保持 prefix cache 稳定**，减少无意义的 prefill 重算。
3. **避免语义丢失导致的死循环**（尤其是 `Wasted call` → re-read 循环）。
4. **防止本地后端 OOM**，通过资源护栏实现 graceful degradation。
5. **提供可观测性**，让每次优化都能被测量、验证和回滚。

### 1.2 硬约束

| 约束 | 影响 |
|------|------|
| 48 GB 统一内存（Apple Silicon M5 Pro） | KV 存储充裕，但 prefill 激活内存易超限；并发被锁死在 1 |
| rapid-mlx Qwen3.6-35B-A3B（MoE） | `ArraysCache` 不可修剪，agentic 场景 prefix cache 命中率实际为 0% |
| Python stdlib only | 不可引入 tiktoken/torch/tree-sitter 等第三方库 |
| Claude Code 不可修改 | 所有策略必须在代理层静默完成，保持 Anthropic API 兼容 |
| 本地 vs 云双模式 | 同一套代码需适配资源紧张本地与资源充裕云端 |

### 1.3 成功标准

| 指标 | 当前基线 | 现实目标 |
|------|---------|---------|
| 单轮 TTFT（60K tokens） | ~60s | 22-35s（全部策略落地） |
| 死亡循环 wasted 错误 | 可持续增长 | 0 或可控 |
| OOM 频率 | 偶发 | 显著下降 |
| `common_prefix_ratio` | 可观测 | ≥ 0.6（prefix zone 稳定） |
| 上下文压缩比 | 无 | 20-40%（语义压缩贡献） |

---

## 2. 核心设计原则

### 2.1 三层防御体系

所有策略按作用域分为三层，逐层叠加：

```
┌─────────────────────────────────────────────────────────────┐
│ 稳定层 (Phase 1)                                             │
│ 目标：让相邻请求的共同前缀尽可能稳定                           │
│ 手段：Cache Aligner、system 消息规范化、工具定义稳定排序        │
├─────────────────────────────────────────────────────────────┤
│ 压缩层 (Phase 2)                                             │
│ 目标：在保留语义的前提下减少 token 量                          │
│ 手段：语义压缩、智能保留 Read 结果、增量摘要、关键词索引        │
├─────────────────────────────────────────────────────────────┤
│ 护栏层 (Phase 3)                                             │
│ 目标：防止资源耗尽并提供可观测性                               │
│ 手段：内存拒绝、动态 max_tokens、动态并发、失败快照、schema v1  │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 模式感知

同一功能在 local 与 cloud 模式下默认行为不同：

| 策略 | local 默认 | cloud 默认 | 原因 |
|------|-----------|-----------|------|
| context truncation | 启用 | 关闭 | cloud 支持 1M+ tokens |
| tool clearing | 关闭 | 关闭 | 本地 backend 语义模糊导致死循环 |
| tool filtering | 启用 | 关闭 | 本地工具定义 token 压力大 |
| blocker/loop 多层防御 | 多层 | 轻量 | 本地模型自纠错能力弱 |
| memory reject | 启用（阈值 90） | 启用（阈值 95） | cloud 后端更稳定 |
| dynamic max_tokens | 启用 | 关闭 | 本地需严格控制输出长度 |

### 2.3 可逆与可回退

- 每个新功能必须有 `PROXY_*_ENABLED` 开关。
- 语义压缩失败时自动回退原始内容。
- 配置文件支持热重载（SIGHUP），可在不重启代理的情况下切换策略。

### 2.4 metrics 先行

任何策略改动前必须：

1. 定义基线指标。
2. 在 `logs/proxy_metrics.jsonl` 中采集。
3. 在 `/status` 中可查看。
4. 通过单元/集成测试验证。

---

## 3. 三层防御体系详解

### 3.1 稳定层：Phase 1 — Cache Aligner 与 Prefix 稳定化

#### 3.1.1 问题

- Claude Code 每轮请求携带的 system prompt、skills、工具定义构成一个**稳定前缀**。
- 但 mid-conversation system 消息、工具定义顺序变化、动态日期会破坏前缀一致性，导致 prefix cache 失效。
- 即使 rapid-mlx MoE 当前 prefix cache 命中率为 0%，**保持前缀稳定仍是未来切非 MoE 模型时的关键收益来源**。

#### 3.1.2 策略

| 策略 | 实现 | 效果 |
|------|------|------|
| Cache Aligner | `_apply_cache_aligner()` 保护前 `PROXY_CACHE_ALIGN_HEAD` 条消息不被压缩/截断 | prefix zone 完全稳定 |
| system 消息规范化 | `_normalize_system_messages()` 将非首条 system 转为 user 消息 | 避免 Qwen chat template 报错 |
| 工具定义稳定排序 | `TOOL_ALWAYS_KEEP` 改为有序 tuple，过滤后按固定顺序排列 | 不同请求间工具定义序列一致 |
| 日期标准化 | 将 `2026-06-18` 替换为 `{{DATE}}` | 减少时间敏感内容对前缀的扰动 |
| 共同前缀指标 | `_compute_common_prefix_ratio()` | 量化稳定性 |

#### 3.1.3 验证

- 集成测试 `test_cache_align_integration.sh` 测得两请求间 `common_prefix_ratio` 可达 **0.87**。

### 3.2 压缩层：Phase 2 — 语义保留压缩

#### 3.2.1 问题

- 粗暴 clearing 旧 tool_result 为 `[cleared: ...]` 会导致模型无法区分"内容被清理"与"读取失败"，引发 re-read 死循环。
- agentic 会话中 60-80% 内容为结构化数据（JSON、代码、日志）。

#### 3.2.2 策略

| 策略 | 实现 | 效果 |
|------|------|------|
| 内容类型路由 | `_detect_content_type()` → json/code/log/text | 按类型选最优压缩器 |
| JSON 结构化摘要 | `_sieve_json()` | 保留 schema，截断长 value，数组保留前 N 项 |
| 代码压缩 | `_compress_code()` | 删除非语义空白/注释，保留结构 |
| 日志压缩 | `_compress_log()` | 聚合重复行，保留错误/异常行 |
| ANSI 清洗 | `_scrub_ansi()` | 去除颜色码与控制字符 |
| 压缩审计 | `_audit_compression()` | JSON 可解析、代码括号平衡，失败回退 |
| 标量去重 | `_dedupe_scalars()` | aggressive 模式下长字符串首次出现后去重 |
| 智能保留 Read 结果 | `_apply_rounds_truncation()` 提取 dropped 区间 Read 结果完整保留 | 避免 re-read 死循环 |
| 增量摘要 | `_incremental_compress()` + `_summary_cache` | 只压缩新增 dropped 消息 |
| 关键词索引 | `_extract_keywords()` + `_inject_keyword_context()` | 从 dropped 消息提取文件名/错误/函数名注入 tail |

#### 3.2.3 关键决策：Tool Clearing 默认关闭

> 生产配置：`PROXY_CLEAR_ENABLED=false`
>
> 实测：开启 clearing 后 `wasted` 错误 7→9→11→13 持续增长，上下文膨胀至 250K+；关闭后 `wasted=0`，会话稳定 30+ 分钟。
>
> 替代方案：语义压缩 + 智能保留 Read 结果 + Truncate rounds 策略。

#### 3.2.4 验证

- 集成测试 `test_compress_integration.sh`：大 JSON tool_result 从 12480 字符压缩至 2442 字符（ratio ≈ 0.20）。
- 真实会话：drop ratio 80% → 20%，`wasted=0`。

### 3.3 护栏层：Phase 3 — 资源与观测

#### 3.3.1 问题

- 本地后端 prefill 激活内存随 prompt 长度超线性增长，易触发 `[METAL] Insufficient Memory`。
- `max_tokens` 被 rapid-mlx 忽略，长输出进一步加剧 OOM。
- 固定并发无法适应负载波动。
- 大请求失败后缺乏现场快照。

#### 3.3.2 策略

| 策略 | 实现 | 效果 |
|------|------|------|
| 动态 token 估算 | `_estimate_tokens_dynamic()` | 按中文/英文/代码选择 chars-per-token ratio |
| 内存压力主动拒绝 | `_should_reject_for_memory()` | used_pct 超阈值时 503 + Retry-After |
| 动态 max_tokens | `_compute_dynamic_max_tokens()` | SATURATION 后上限降至 2048 |
| 动态并发控制 | `_adjust_concurrency()` | 基于 P95 延迟与错误率自动调整 semaphore |
| 请求失败快照 | `_write_request_snapshot()` | 保存 before/after JSON 到 `logs/snapshots/` |
| `/status` 增强 | `_get_context_optimization_stats()` | Context Optimization 卡片 |
| metrics schema v1 | `_finalize_metrics()` | 固定字段集合，新增 schema_version、memory_rejected 等 |

#### 3.3.3 生命周期阶段统一阈值

```
chars →    15K       40K        90K        180K       350K     400K
           │         │          │           │          │        │
Stage:   INIT     GROWTH    EXPANSION   SATURATION  OOM_DANGER  PRE_TRUNC
语义压缩:  跳过     尾40%      尾60%+      全dynamic   全量       全量
截断:     关        关       预算触发     rounds=8   rounds=3  rounds=2
Frozen:   12        12         12          6          0         0
max_tok: 4096     4096       4096        2048       2048      2048
```

#### 3.3.4 验证

- 281 个单元测试通过。
- 6 套集成测试通过：blocker、loop、cache-align、compress、memory-reject、status。

---

## 4. 请求处理管线总览

```
请求进入 anthropic_proxy.py
  │
  ├─ 1. 解析 messages, tools；初始化 metrics
  ├─ 2. 内存压力检查 → 503 + Retry-After（超限）
  ├─ 3. 并发窗口记录
  ├─ 4. 日期标准化
  ├─ 5. Error translation
  ├─ 6. Tool clearing（默认关闭）
  ├─ 7. Loop / Blocker / Re-read 检测
  ├─ 8. Thinking block 清理
  ├─ 9. Phase 2 语义压缩（json/code/log/text + auditor）
  ├─ 10. Cache Aligner 拆分 prefix zone + dynamic zone
  ├─ 11. Context truncation
  │      ├─ 生命周期阶段判断
  │      ├─ 自适应保留深度
  │      ├─ 增量摘要 / LLM 摘要 / 规则摘要 / 静态折叠
  │      ├─ Read 结果智能保留
  │      └─ 关键词索引注入
  ├─ 12. 动态 max_tokens
  ├─ 13. 工具定义过滤
  ├─ 14. 转发到后端
  ├─ 15. 输出控制（截断 / JSON 修复）
  ├─ 16. 动态并发调整
  ├─ 17. 失败快照
  └─ 18. Metrics 记录（schema v1）
```

---

## 5. 关键策略决策矩阵

| 问题 | 首选策略 | 禁用/回退策略 | 关键配置 |
|------|---------|--------------|---------|
| 上下文太长 | Truncate rounds + 语义压缩 | Tool clearing（不推荐） | `PROXY_CTX_TRUNCATE_STRATEGY=rounds`, `PROXY_COMPRESS_ENABLED=true` |
| 工具定义 token 多 | 工具过滤白名单 | 关闭过滤 | `PROXY_TOOL_FILTER_ENABLED=true` |
| 模型反复 Read 同一文件 | 智能保留 Read 结果 + Re-read HARD BLOCK | Tool clearing | `PROXY_CLEAR_ENABLED=false` |
| 后端 OOM | 内存拒绝 + 动态 max_tokens + 动态并发 | 无 | `PROXY_MEMORY_REJECT_THRESHOLD=90` |
| 循环检测失效 | 精确 + 模式 + 文本三级检测 | 单一阈值 | `PROXY_LOOP_THRESHOLD=3` |
| 旧错误输入占用上下文 | Blocker 注入 + 错误翻译 | Purge errors（待实现） | `PROXY_BLOCKER_ENABLED=true` |
| Prefix cache 不稳定 | Cache Aligner + system 规范化 | 动态排序 | `PROXY_CACHE_ALIGN_ENABLED=true` |

---

## 6. 可观测性指标体系

### 6.1 每请求指标（`logs/proxy_metrics.jsonl`）

| 字段 | 说明 |
|------|------|
| `schema_version` | `"v1"` |
| `common_prefix_ratio` | 与上一请求共同前缀比例 |
| `re_read_rate` | 重复 Read 率（0-100%） |
| `compression_ratio` | 压缩后/压缩前字符比 |
| `token_ratio` | 本次使用的 chars-per-token |
| `est_input_tokens` | 估算输入 tokens |
| `est_output_tokens` | 估算输出 tokens |
| `memory_rejected` | 是否因内存压力被拒绝 |
| `used_pct` | 系统内存使用百分比 |
| `max_tokens_original` | 原始 max_tokens |
| `max_tokens_dynamic` | 动态限制后的 max_tokens |
| `snapshot_written` | 是否写入失败快照 |
| `dynamic_concurrent` | 当前并发值 |
| `quality_flags` | 质量标记数组 |

### 6.2 聚合看板（`/status`）

- Context Optimization 卡片：平均 `common_prefix_ratio`、平均 `compression_ratio`、loop/blocker 次数、最近 blocker、当前并发。
- `/metrics` 端点：返回 schema v1 与聚合字段。

### 6.3 质量标记

| 标记 | 含义 |
|------|------|
| `loop_injected` | 循环检测注入提示 |
| `blocker_injected` | blocker 提示注入 |
| `high_drop_ratio` | 截断丢弃比例 > 85% |
| `llm_compress_failed` | LLM 摘要失败并降级 |
| `budget_overflow` | token budget 超限 |
| `memory_rejected` | 内存压力拒绝 |
| `snapshot_written` | 失败快照已写入 |

---

## 7. 配置治理建议

### 7.1 推荐配置模板

| 场景 | 关键配置 |
|------|---------|
| **local-balanced（默认）** | `PROXY_CLEAR_ENABLED=false`, `PROXY_CTX_TRUNCATE_STRATEGY=rounds`, `PROXY_COMPRESS_ENABLED=true`, `PROXY_TOOL_FILTER_ENABLED=true`, `PROXY_DYNAMIC_MAX_TOKENS_ENABLED=true`, `PROXY_MEMORY_REJECT_THRESHOLD=90` |
| **local-aggressive** | 在 balanced 基础上降低 `PROXY_CTX_TOKEN_BUDGET=20000`、`PROXY_COMPRESS_MODE=aggressive` |
| **cloud** | `PROXY_CTX_LIMIT_ENABLED=false`, `PROXY_TOOL_FILTER_ENABLED=false`, `PROXY_DYNAMIC_MAX_TOKENS_ENABLED=false`, `PROXY_MEMORY_REJECT_THRESHOLD=95` |

### 7.2 参数变更原则

1. 每次只改一个参数。
2. 变更前后对比 metrics。
3. 高影响参数（如 `PROXY_CLEAR_ENABLED`、`PROXY_CTX_TRUNCATE_STRATEGY`）需经集成测试验证。
4. 配置热重载后检查 `/status` 确认生效。

---

## 8. 已验证收益

| 维度 | 指标 | 改善 |
|------|------|------|
| Prefix 稳定性 | `common_prefix_ratio` | 0.35 → 0.87（集成测试） |
| 语义压缩 | 大 JSON tool_result ratio | 1.0 → 0.20 |
| 死循环 | `wasted` 错误 | 13 → 0 |
| 截断丢弃率 | drop ratio | 80% → 20% |
| OOM 防护 | 内存压力请求 | 503 + Retry-After 主动拒绝 |
| 可观测性 | metrics 字段完整性 | schema v1 100% 固定字段 |
| 测试覆盖 | 单元/集成测试 | 281 + 6 套通过 |

---

## 9. 未来路线图

### 9.1 短期（1-2 周）

| 改进点 | 来源 | 预期收益 |
|--------|------|----------|
| 可逆压缩（CCR 思路） | Headroom-ai | 解决死亡循环 |
| ContentRouter 增强 | Headroom-ai | 更细粒度内容路由 |
| 文件块指纹去重 | TokenSieve | 反复 Read 同文件节省 20-40% |
| 工具参数级去重 | DCP | 精确识别重复工具调用 |
| 过期错误输入剪枝 | DCP | 清理旧错误输入 |

### 9.2 中期（1-3 个月）

| 改进点 | 来源 | 预期收益 |
|--------|------|----------|
| SmartCrusher JSON 压缩 | Headroom-ai | JSON 工具输出压缩 30-50% |
| 三级降级 Bash 输出压缩 | Skim | 测试/构建日志节省 50-70% |
| Python AST 代码压缩 | Skim | .py 文件 Read 节省 60-80% |
| BM25 升级为 SQLite FTS5 | Token Reducer | 持久化关键词索引 |
| 嵌套摘要占位符 | DCP | 多层累积摘要 |

### 9.3 长期（架构级）

| 改进点 | 来源 | 预期收益 |
|--------|------|----------|
| 模型可调用 compress 工具 | DCP | 模型自主管理上下文 |
| 会话状态持久化 | DCP / Headroom | 跨请求去重与嵌套摘要 |
| 跨 agent 记忆 | Headroom-ai | 跨会话上下文复用 |

---

## 10. 相关文档

- `docs/02-architecture-design/proxy-context-window-design.md` — 上下文窗口替换设计（v8，含 Phase 3 细节）
- `docs/02-architecture-design/proxy-pipeline-reference.md` — 8 层代理管线参考
- `docs/research-context-optimization/05-plan.md` — 分阶段落地路线图
- `docs/research-context-optimization/04-solutions.md` — 代理层可落地的优化方向
- `docs/04-analysis-diagnostics/dcp-strategy-analysis-20260618.md` — 竞品上下文压缩策略分析
- `AGENTS.md` — 环境变量与编码规范
- `CHANGELOG.md` — Phase 1-3 变更日志
