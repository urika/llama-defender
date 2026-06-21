# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/) and dates use ISO 8601.

---

## [Unreleased] - 2026-06-21

### 安全加固 + 长上下文支持 + 配置一致性修复

本次更新解决 code review 发现的 P0/P1 问题：请求体大小硬限制、长上下文超时统一、Qwen3.6 系列配置一致性、以及 repetition-penalty 副作用缓解。

### Added

- **`PROXY_MAX_REQUEST_BYTES` (P0)**: 请求体大小硬上限（默认 512000 bytes / 500KB），
  在 `do_POST` 读取 body 前检查 Content-Length，超限返回 413 Payload Too Large。
  防止 359KB+ tool+dialog 大 payload 触发 Metal OOM。所有 6 个本地/cloud 配置已显式设置。
  单元测试覆盖：`test/unit/test_payload_limit.py`（4 个用例：超限→413、等于/小于/零长度→不拒）。
  关联缺陷：DEF-005 (Metal OOM) 补充缓解。

### Changed

- **`PROXY_BACKEND_TIMEOUT` 300→600**: 所有本地配置统一提升至 600s，`manage.sh`/`anthropic_proxy.py`/
  `proxy_config.py` 默认值同步。100K+ token prefill 可需 ~5 分钟，300s 会误判超时。
  涉及配置：`rapid-mlx-35b-opt`、`mlx_vlm-27b`、`qwen3-8b`、`qwen2.5-coder-14b`、`gemma4-26b`。

- **`PROXY_MAX_TOKENS_OVERRIDE` 16384→32768**: `rapid-mlx-35b-opt`、`qwen3-8b`、`gemma4-26b` 提升至 32768，
  匹配 long-context 场景下的输出需求。

- **`RAPID_MLX_REASONING_PARSER` 统一为 `qwen3`**: `mlx_vlm-27b` 和 `qwen3-8b` 从 `""` 改为 `"qwen3"`，
  与 `rapid-mlx-35b-opt` 一致。正确剥离  nondel 块，输出纯回复，避免非推理场景下 think 标签泄漏到输出。

- **`--default-repetition-penalty` 1.1→1.05**: 1.1 对代码生成副作用过大，抑制 `self`/`return`/`import`
  等正常重复标识符；1.05 轻微抑制站点名/短语重复且不损害代码质量。调整前建议用 `tools/bench_quality.py` 验证。

### Fixed

- **`rapid-mlx-35b-opt.conf` 注释与实际值矛盾**: 头注释 `temp=0.3/推理解析器清空` 与实际
  `LLAMA_TEMP=0.7/RAPID_MLX_REASONING_PARSER=qwen3` 不符；`CONFIG_DESC` 同步修正。
- **`test/README.md` 引用不存在的 `/tmp/` 脚本**: `/tmp/safe_longctx_test.py` 和
  `/tmp/gemma_eval/test_gemma_processing.py` 是临时文件（重启即失），已移除，改为指向
  `tools/bench_perf.py --long-ctx-only` 和 `tools/bench_quality.py`。
- **`manage.sh` 默认值未同步**: `PROXY_BACKEND_TIMEOUT` 默认 300→600；
  新增 `PROXY_MAX_REQUEST_BYTES` 环境变量传递（与 proxy 自身默认值兜底一致）。
- **文档同步**: `AGENTS.md`、`docs/01-requirements-product/PRD-anthropic-proxy.md`、
  `docs/02-architecture-design/proxy-pipeline-reference.md`、`docs/02-architecture-design/proxy-context-window-design.md`
  中 `PROXY_BACKEND_TIMEOUT` 默认值 300→600，新增 `PROXY_MAX_REQUEST_BYTES` 行。
  `AGENTS.md` `RAPID_MLX_EXTRA_ARGS` 表新增 `--default-repetition-penalty` 说明及副作用警告。

---

## [Unreleased] - 2026-06-19

### 配置优化：27B 模型 + rapid-mlx v0.6.71 后端加固

本次更新基于 27B 模型（Qwen3.6-27B-OptiQ-4bit）在生产环境运行 20+ 小时的实测数据，对后端和代理配置进行系统性优化。

### Changed

- **Tool Clearing 关闭** (`PROXY_CLEAR_ENABLED=false`): 明确关闭，消除 re-read 死循环风险。
  之前被显式设为 `true`，覆盖了安全默认值 `false`。
  见 AGENTS.md ⚠️ WARNING。

- **GPU 内存上限** (`--gpu-memory-utilization 0.80`): Metal allocation_limit 从 90%（36.2GB）
  降至 80%（32.2GB）。之前无此参数使用默认 90%，峰值达 31.7GB（87.6%）逼近 kernel panic 阈值。
  降低后预留 ~3.5GB 安全余量。

- **EXTRA_ARGS 精简**: 移除冗余 `--continuous-batching`（BatchedEngine 已是 v0.6.71 默认引擎）。

### Added

- **`HF_HUB_OFFLINE=1` 写入配置**: vllm-mlx v0.6.71 启动时必连 huggingface.co 验证模型配置，
  网络不可用时陷入 ConnectTimeout 重试循环导致死锁。
  已通过 `export HF_HUB_OFFLINE=1` 写入 `configs/mlx_vlm-27b.conf`（manage.sh `_load_config()` 自动 source）。

### Known Issues (Updated)

- **跨请求前缀缓存不可用**: rapid-mlx v0.6.71 的 BatchedEngine 不集成 MemoryAwarePrefixCache。
  当前只有 PagedCache（块级管理），每次请求全量 prefill。等待上游支持。
  参考分析: `docs/04-analysis-diagnostics/rapid-mlx-cache-analysis-supplement.md`

- **Metal 设备死锁**: 多次 `kill -9` 快速重启后端可导致 Metal 设备初始化挂起（卡在
  `MLX step thread initialized`），需机器重启恢复。

- **旧引擎缓存确认可用**: rapid-mlx 旧版引擎（非 BatchedEngine）的 MemoryAwarePrefixCache
  实测支持跨请求前缀缓存（PID 92244, 98-99% 命中率）。
  PID 88059 的 `cache_fetch` HIT 日志为最直接的证据。

---

## [Unreleased] - 2026-06-18

### Phase 1: 基线与快速收益

本次更新聚焦上下文优化研究的第一阶段落地，全部改动围绕 **metrics 基线、Cache Aligner MVP、可观测性修复** 展开。

### Added

- **Cache Aligner MVP**: `PROXY_CACHE_ALIGN_ENABLED` + `PROXY_CACHE_ALIGN_HEAD` 保护前 N 条消息不被压缩/截断，提升 prefix cache 稳定性。
- **`common_prefix_ratio` 指标**: 每条请求记录与上一请求的公共前缀比例，用于量化 prefix cache 稳定性。
- **`_compute_re_read_rate` 辅助函数**: 统一 re-read 率计算，确保结果在 0–100% 之间。
- **`_normalize_system_messages`**: 自动将对话中间的 system 消息转换为 user 消息，避免 Qwen chat template 报错。
- **`_apply_cache_aligner` / `_compute_common_prefix_ratio` / `_message_stable_hash`**: 新增标准库实现的辅助函数。

### Changed

- **`_filter_tools` 排序稳定性**: `TOOL_ALWAYS_KEEP` 改为有序 tuple，过滤后工具按固定白名单顺序排列，减少不同请求间的 token 序列差异。
- **`_compress_content_pass` 调用方式**: 仅对 Cache Aligner 划分出的 dynamic zone 执行压缩，prefix zone 被完全保护。
- **请求处理流程**: 在 `_handle_messages` 中集成 system 消息规范化、cache aligner 拆分、common_prefix_ratio 计算与 session 消息缓存。

### Fixed

- **DEF-003**: `re_read_rate` 计算收敛到 `_compute_re_read_rate` 辅助函数，避免未来再次出现公式错误。
- **Qwen template 兼容性**: 中 conversation system 消息不再导致 `System message must be at the beginning` 错误。

### Tests

- 新增 15 个单元测试覆盖 re_read_rate、common_prefix_ratio、system 消息规范化、cache aligner、工具过滤稳定顺序。
- 新增 `test/integration/test_cache_align_integration.sh`：启动 mock backend + proxy，验证两请求间 `common_prefix_ratio > 0` 且首条消息保持 system 角色。
- 更新 `test/run_tests.sh` 将 cache-align 集成测试纳入 `--integration` 流程。

### Phase 2: 语义压缩与防御增强

本次更新聚焦上下文优化的第二阶段：用语义保留压缩替代粗暴 clearing，同时增强防御与可观测性。

### Added

- **语义压缩管线（Semantic Compression Pipeline）**：在 `_compress_content_pass` 中新增 Phase 1b 压缩层，仅对 Cache Aligner 划分出的 dynamic zone 中长度超过阈值的 `tool_result` 执行压缩。
- **JSON 结构化摘要（`_sieve_json`）**：借鉴 TokenSieve 的 `sieve` 思路，对 JSON 数组/对象保留 schema、截断长 value、保留前 N 项并计数剩余项，失败时回退原内容。
- **代码压缩器（`_compress_code`）**：删除不影响语法的空白与注释，保留代码结构。
- **日志压缩器（`_compress_log`）**：借鉴 TokenSieve 的 `log_compressor` 思路，聚合重复日志行并保留错误/异常行。
- **文本压缩器（`_compress_text`）**：对长文本进行段落/句子截断摘要。
- **ANSI Scrubber（`_scrub_ansi`）**：借鉴 TokenSieve 的 `scrubber` 思路，去除 Bash 输出中的 ANSI 颜色码与不可见控制字符。
- **内容类型路由（`_detect_content_type`）**：根据语法启发式将 tool_result 分类为 `json` / `code` / `log` / `text`，并选择对应压缩策略。
- **压缩审计器（`_audit_compression`）**：JSON 压缩后执行 `json.loads` 校验，代码压缩后校验括号平衡，失败时回退原内容，避免破坏语义。
- **标量去重器（`_dedupe_scalars`）**：默认关闭，仅在 `aggressive` 模式下启用，对首次出现后的相同长字符串进行去重。
- **语义压缩 Metrics**：`pipeline.semantic_compress` 字段记录 `compressed_count`、`saved_chars`、`ratio`、`strategies`、`audit_failures`。
- **新的环境变量**：`PROXY_COMPRESS_ENABLED`、`PROXY_COMPRESS_THRESHOLD`、`PROXY_COMPRESS_MODE`、`PROXY_SCRUB_ANSI`、`PROXY_DEDUPE_SCALARS`、`PROXY_COMPRESS_AUDIT`。

### Changed

- **`_compress_content_pass` 调用方式**：先执行 Phase 1b 语义压缩，再执行原有 clearing/截断逻辑，压缩后的内容仍然会被 audit 校验。
- **`_handle_messages` 流程**：在重组 prefix + dynamic zones 后，记录 `semantic_compress` 指标。

### Tests

- 新增 `TestSemanticCompression` 单元测试类，14 个用例覆盖 scrubber、content router、JSON/code/log/text 压缩、auditor、deduper。
- 新增 `test/integration/test_compress_integration.sh`：启动 mock backend + proxy，发送含大 JSON tool_result 的请求，验证转发内容显著变短且 metrics 中 `semantic_compress.ratio < 1.0`。
- 更新 `test/run_tests.sh` 将 compress 集成测试纳入 `--integration` 流程。

### Docs

- 更新 `AGENTS.md` 补充 `PROXY_COMPRESS_*` 环境变量说明。
- 更新 `docs/research-context-optimization/05-plan.md` 标记第二阶段任务完成状态。

### Phase 3: 资源与观测护栏

本次更新聚焦上下文优化的第三阶段：降低本地后端 OOM 风险、提升并发资源利用率、增强可观测性与排障能力。

### Added

- **动态 token 预估模型（`_estimate_tokens_dynamic`）**：按中文、英文、代码选择不同的 chars-per-token ratio（`PROXY_TOKEN_RATIO_*`），替代单一静态 ratio。
- **内存压力主动拒绝（`_should_reject_for_memory`）**：当 `used_pct > PROXY_MEMORY_REJECT_THRESHOLD` 时，在请求入口处返回 503 + Retry-After，避免后端 OOM。
- **动态输出 token 限制（`_compute_dynamic_max_tokens`）**：根据 lifecycle stage、后端类型、可用内存动态降低 `max_tokens`。
- **动态并发控制（`_adjust_concurrency`）**：基于最近请求的 P95 延迟与错误率自动调整 `_llama_lock` 大小，高负载降级、低负载升级。
- **请求失败快照（`_write_request_snapshot`）**：请求失败时保存 `logs/snapshots/<req_id>_{before,after}.json`，便于排障。
- **`/status` 增强**：新增「Context Optimization」卡片，展示平均 `common_prefix_ratio`、`compression_ratio`、loop/blocker 触发次数、最近 blocker 详情与当前并发状态。
- **统一 metrics schema v1**：所有 metrics 记录固定字段集合，新增 `schema_version: v1`、`token_ratio`、`est_input_tokens`、`est_output_tokens`、`memory_rejected`、`used_pct`、`max_tokens_*`、`snapshot_written`、`dynamic_concurrent` 等字段。

### Changed

- **`_finalize_metrics`**：使用动态 token ratio 估算输入/输出 token，补充 schema v1 固定字段。
- **`_handle_messages`**：在 `max_tokens` 处理流程中接入动态限制，并记录 `max_tokens_original`/`max_tokens_dynamic`/`used_pct`。
- **`do_POST`**：接入内存压力检查、失败快照、并发窗口记录与动态调整。
- **OOM 安全检查**：OOM safety 迭代截断同时考虑字符数与动态 token 估算。
- **`/metrics` 端点**：返回 schema v1，新增 `memory_rejected`、`snapshot_written`、`dynamic_concurrent_events` 聚合。

### Tests

- 新增 `TestDynamicTokenEstimation`、`TestMemoryRejection`、`TestDynamicMaxTokens`、`TestRequestSnapshots`、`TestDynamicConcurrency`、`TestMetricsSchemaV1` 等单元测试类，新增 22 个用例。
- 新增 `test/integration/test_memory_reject_integration.sh`：monkey-patch `_get_system_memory` 模拟 95% 内存压力，验证 `/v1/messages` 返回 503 + Retry-After + backend_oom。
- 新增 `test/integration/test_status_integration.sh`：验证 `/status` 页面 Context Optimization 卡片字段存在，且 `/metrics` 返回 schema v1。
- 更新 `test/run_tests.sh` 纳入 memory-reject 与 status 集成测试。

### Docs

- 更新 `AGENTS.md` 补充 Phase 3 环境变量说明。
- 更新 `docs/research-context-optimization/05-plan.md` 标记第三阶段任务完成状态。

---

## [0.5.0-baseline] - 2026-06-06

### Status: Pre-OSS-migration baseline

**Tag purpose**: 标记 Phase 1-5 完整实现快照,作为 llama_defender 库化 (路线图 B) 的起点。
**不推荐生产部署**: 7 个 P0 缺陷未修复 (详见 `docs/DEFECT-LIST.md`)。

### Added

#### 核心功能 (Phase 1-5)
- **双模式代理架构** (commit `16defd9`): Local (rapid-mlx/llama-server) + Cloud (DeepSeek/OpenAI) 路由
- **8 层处理管线** (Layer 1-8): 请求入口 → 语义预处理 → 循环/阻塞检测 → 缓存优化 → 上下文截断 → 格式转换 → 响应后处理 → 可观测性
- **23 个需求点全部实现** (R1.1-R7.2, 7 大领域 100% 覆盖)
- **2 种截断策略**: rounds (token 预算 + 保留轮数) + fifo + char
- **3 级压缩链**: LLM 压缩 → 规则压缩 → 静态折叠
- **3 级循环干预** (Level 1/2/3): 软提示 → 移除工具 → 强制纯文本
- **阻塞模式检测** (`_detect_blocker_pattern`): 连续 N 次相同错误类型干预
- **工具结果语义清除**: 工具优先级评分 + Read 200 字符预览 + 最近 Read 加分
- **工具定义动态过滤** (`_filter_tools`): 白名单 + 最近使用
- **XML→JSON 4 级回退** (`parse_tool_arguments`): JSON / embedded JSON / XML / heuristic
- **Content-text 工具提取** (`_StreamingToolsExtractor`): 流式状态机
- **JSON 修复** (`_repair_truncated_json`): 截断 tool_call arguments
- **MoE 特定处理**: Qwen chat template 修复 (5 个模型已覆盖)
- **结构化 Metrics** (`logs/proxy_metrics.jsonl`): 4 种 quality_flags
- **状态页** (`GET /status`): 实时指标展示
- **78 单元测试** (Phase A + B): 覆盖 18/23 需求
- **7 个集成测试** (含 blocker 矩阵)
- **19 个端到端测试** (含 12 case 集成矩阵)

#### 设计文档 (本次提交新增)
- `docs/PRD-anthropic-proxy.md` (688 行): 产品需求文档 v3.0
- `docs/DEFECT-LIST.md` (509 行): 30 项缺陷清单 (7 P0 + 8 P1 + 10 P2 + 5 P3)
- `docs/PM-ANALYSIS-FUTURE-ROADMAP.md` (377 行): 产品经理视角的核心功能取舍
- `docs/OSS-REPLACEMENT-EVALUATION.md` (688 行): OSS 替代品深度评估
- `docs/proxy-prefix-cache-design.md` (1001 行): 代理层 prefix cache 稳定化设计 v1.0
- `docs/README.md`: 文档目录导航

#### 文档重构
- 按 6 大类重组: 01-requirements-product / 02-architecture-design / 03-experiments-testing / 04-analysis-diagnostics / 05-operations-changelog / 06-reference-metrics
- 24 篇原始文档按主题归位

### Known Issues (P0, 7 项)

来自 `docs/DEFECT-LIST.md`:
- **DEF-001**: 22% 请求返回 500 错误 — **🟡 部分修复** (预截断 + 错误分类 + Retry-After)
- **DEF-002**: 37% 请求触发循环注入 (R2.1 未根治跨请求循环)
- **DEF-003**: re_read_rate 公式错误 (2,862%, 应 ≤ 100%)
- **DEF-004**: 工具过滤 recent 扫描 99% 失效
- **DEF-005**: Metal OOM 仍偶发
- **DEF-006**: Apple Silicon Kernel Panic 风险
- **DEF-007**: Chat template 修复未工具化

### Roadmap (路向 v1.0)

依据 `docs/PM-ANALYSIS-FUTURE-ROADMAP.md` 路线图 B:
- **Phase 1 (0-3 月)**: 稳态化 + 引入旁路观测 (Langfuse)
- **Phase 2 (3-6 月)**: OSS 替换 (LiteLLM 协议转换) + 提取 llama_defender 库
- **Phase 3 (6-12 月)**: 业务转型 (llama_defender OSS 库 + DSPy/GEPA 提示词优化模板)

### Metrics (snapshot at 0.5.0-baseline)

| 指标 | 数值 |
|------|------|
| 总 commits | 33 |
| 代码行数 (`anthropic_proxy.py`) | 3,611 |
| 函数数量 | 63 |
| 配置项 | 31 env vars |
| 需求点 | 23 (100% 实现) |
| 单元测试 | 78 cases (Phase A + B) |
| 集成测试 | 7 blocker scenarios |
| 端到端测试 | 19 integration cases |
| 文档总数 | 29 篇 (含本次新增 5 篇) |

### Known Compatibility

- Python 3.9+
- macOS (Apple Silicon M5 Pro tested, 48GB unified memory)
- Backends: rapid-mlx v0.6.71+ (recommended), rapid-mlx v0.6.30 (degraded), llama-server (partial)
- Clients: Claude Code (verified), Anthropic SDK, any OpenAI-compatible client

---

## [0.5.3-text-loop-detection] - 2026-06-08

### Status: 文本输出循环检测实现

**范围**: 代理层新增对 assistant 纯文本输出的循环检测，解决模型重复输出相同文本的问题。

### Added

- **文本输出循环检测** (`_detect_text_loop`): 扫描最近 N 条 assistant 消息的纯文本内容，使用字符级 bigram Jaccard 相似度检测重复模式
- **相似度计算函数** (`_compute_text_similarity`): 基于 bigram 的 Jaccard 相似度，对文本重复敏感且计算高效
- **3 级文本循环干预**: 与工具循环共享阈值，但生成针对性的干预消息
  - Level 1: 提示停止重复
  - Level 2: 强烈警告
  - Level 3: 剥夺所有工具，强制纯文本响应

### Configuration

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_TEXT_LOOP_ENABLED` | `true` | 文本循环检测开关 |
| `PROXY_TEXT_LOOP_THRESHOLD` | `3` | 连续相似文本触发阈值 |
| `PROXY_TEXT_LOOP_MIN_CHARS` | `100` | 最小文本长度（短于此的消息不参与检测） |
| `PROXY_TEXT_LOOP_SIMILARITY` | `0.85` | 相似度阈值 (0.0-1.0) |

### Verified

- 单元测试: 11 个新测试覆盖相似度计算、循环检测、干预消息生成
- 总测试数: 182 → 193

---

## [0.5.2-truncate-fix] - 2026-06-08

### Status: 死循环根因修复完成

**范围**: 代理层报文处理逻辑重构，彻底消除 `wasted` 错误死循环。

### Root Cause Analysis

**死循环完整因果链（已验证）:**
```
Claude Read file → tool_result 存入 context
        ↓
Tool Clearing 清理旧 tool_result → [cleared: ...] 占位符
        ↓
Claude 需要综合分析 → 重新 Read 被清理文件
        ↓
后端返回 "Wasted call"（文件未变化）
        ↓
Error translation 翻译为中文提示
        ↓
Tool Clearing 清理掉翻译结果（提示丢失）
        ↓
Truncate 丢弃更多消息 → Claude "失忆"
        ↓
Loop/Blocker/HARD BLOCK 干预注入
        ↓
Truncate 丢弃干预消息 → 干预无效
        ↓
循环加剧: context 250K+ / wasted 13+ / loop 4
```

**根因**: Tool Clearing 的 `[cleared: ...]` 对 Claude 语义模糊。Claude 无法区分 "内容被清理" 和 "读取失败"，必然尝试 re-read。后端返回 "Wasted call" 后，Error translation 的提示又被 Tool Clearing 清理，形成闭环。

### Fixed

#### 致命缺陷修复
- **Tool Clearing 关闭** (`PROXY_CLEAR_ENABLED=false`): 完全停止语义清理，所有 tool_result 完整保留
- **Truncate 智能保留 Read 结果** (`_apply_rounds_truncation`): 在 rounds 策略下，从 dropped 区域提取 Read 的 tool_result 完整保留，只压缩非 Read 消息
  - 丢弃率从 **80%** 降至 **20%**
  - 文件内容不再丢失，Claude 无需 re-read
- **Error translation 高优先级保留** (`clear_old_tool_results` scoring): 翻译后的 `[System: ...]` 提示获得 `score += 10`，不被清理
- **Re-read HARD BLOCK** (`_handle_messages`): 检测到最后 assistant 消息在 Read 已清理文件时，注入 `[System: HARD BLOCK...]` user 消息强制阻断
- **`kept_names` NameError** (`_filter_tools`): 修复 `kept_names` 在 `_sort_key` 闭包中引用前未定义的问题，消除 4 次 500 错误
- **`auto_keep` 累积失效移除** (`_filter_tools`): 删除 `_tool_freq` 全局计数器（永不重置、计数对象错误），改为 `TOOL_ALWAYS_KEEP | recent_tools | tool_choice_name` 静态策略

#### Metrics 完整性修复
- 所有 pipeline 步骤统一写入 `applied: True/False` 标志，消除字段缺失

### Verified

| 指标 | 旧会话 (`Tool Clearing ON`) | 新会话 (`Tool Clearing OFF + 智能保留`) |
|------|---------------------------|----------------------------------------|
| `wasted` | 7→9→11→13 持续增长 | **0** (全程) |
| `loop` | 3→4 | **1** (低于阈值 3) |
| `reread` | 1-2/轮 | **0** |
| `truncate` 丢弃 | 91-106 条 (80%) | **5-7 条 (20%)** |
| `qf` | `loop_injected, high_drop_ratio` | **[]** |
| 模型产出 | 0-500 chars (哑巴) | 10K+ chars (正常) |
| 会话消息数 | 120+ (循环膨胀) | 34 (正常增长) |
| 状态 | 死循环至断开 | **稳定 30+ 分钟** |

### Changed
- **配置** (`configs/rapid-mlx-35b.conf`): `PROXY_CLEAR_ENABLED=true` → `false`
- **代码行数** (`anthropic_proxy.py`): ~3,700 → ~4,050 (+~350)

### Known Issues
- **Context 增长速度**: Tool Clearing OFF 后，context 从 26K→66K 增长更快（全部保留）。但在 34 条消息时触发温和 truncate（丢弃 7 条），仍在可控范围
- **极端场景**: 若 Claude 读取 50+ 文件，context 可能难以控制。当前方案通过智能保留 Read 结果 + Truncate 压缩非 Read 内容来平衡

### Recommendation

**生产环境配置（当前）:**
```bash
PROXY_CLEAR_ENABLED=false           # 关闭 Tool Clearing
PROXY_CTX_TRUNCATE_STRATEGY=rounds  # 保留 rounds 策略
PROXY_TOOL_KEEP=8                   # 保持默认值
```

**Tool Clearing 不推荐在当前架构下启用**，除非后端支持 "文件未变化" 的显式语义（而非 "Wasted call" 错误）。

---

## [0.5.1-progress] - 2026-06-08

### Status: P0 修复冲刺中（v0.6.0 前置）

**范围**: 6 个 commit，修复/缓解 12 项缺陷（7 项完全修复 + 5 项部分修复）。

### Fixed

#### P0-Critical（7 项中 3 项完全修复，4 项部分修复）
- **DEF-001** 🟡 — 500 错误率缓解：预截断 (400K chars) + 503/504/499 错误分类 + Retry-After header + BrokenPipe→499 client_closed
- **DEF-002** 🟡 — 循环检测大修：移除 LOOP_CONSECUTIVE 双重计数（tail 扫描替代全量继承）+ 新增 Level 3（强制纯文本）+ Level 2 多工具移除 + `_LOOP_SESSION_STATE` 跨请求持久化
- **DEF-003** ✅ — re_read_rate 公式修正：`re_read_files / cleared_files * 100`，cap 100%。新增 `pipeline.re_read` 指标 + 5 个单测
- **DEF-004** ✅ — 工具过滤 recent 扫描验证非 bug：增强 observability（recent_tools 名称列表 + scanned_assistant 轮数）
- **DEF-005** 🟡 — OOM 缓解：`PROXY_OOM_SAFE_TOKENS=60000`，pipeline 后二次 token 检查（含 system prompt），超限强制 FIFO 截断
- **DEF-006** 🟡 — Kernel Panic 防御：`manage.sh` 启动前 sanity check，`--gpu-memory-utilization >0.85` 拒绝启动，`>0.80` 警告
- **DEF-007** ✅ — Chat template 工具化：`manage.sh fix-template <dir>` 一键修复 + 启动时自动检测 HuggingFace 缓存中的模板

#### P1-High（3 项完全修复 + 1 项部分修复）
- **DEF-101** ✅ — BrokenPipe/ConnectionResetError → 499 (client_closed)，不再返回 500
- **DEF-102** ✅ — fifo 策略确认为有意配置（利于 prefix cache 稳定，Plan 2D）
- **DEF-106** 🟡 — 非流式路径 JSON 修复：force_stopped 时回溯原始 tool_calls，调用 `_repair_truncated_json()`
- **DEF-108** ✅ — Blocker 触发修复：Pipeline 顺序修正（blocker detection 移到 tool-result clearing 之前），清除操作不再覆盖错误标记

#### P2-Medium（5 项修复 + 2 项缓解 + 1 项设计限制）
- **DEF-201** ✅ — re-read 检测改为仅扫描最后一次 assistant 消息，消除历史累积假阳性
- **DEF-202** ✅ — Bash dedup 跳过 `[cleared:...]` 内容，防止已清空结果反复触发 Jaccard 匹配
- **DEF-203** 🟡 — 工具过滤后按名字母排序，稳定 prefix cache 命中
- **DEF-204** ✅ — `/status` 端点不再产生日志噪音（GET /status 跳过 header logging）
- **DEF-205** ✅ — 请求去重：`_check_dedup()` 基于 body hash + `PROXY_DEDUP_WINDOW`(2s) 窗口，重复请求返回 429 + Retry-After
- **DEF-206** ⚪ — A/B 实验为运维决策工具，标记为设计限制
- **DEF-207** 🟡 — 新增 `./manage.sh watchdog` 命令，自动检测性能衰减并重启
- **DEF-104** 🟡 — 工具过滤日志增加 `filtered_out` 字段（被移除的工具名称排序列表），提升可观测性
- **DEF-107** 🟡 — 截断丢弃率 > 85% 时注入 `[System: Context severely truncated]` 通知

#### P3-Low（2 项修复）
- **DEF-302** ✅ — `_mask_sensitive()` 自动脱敏 `Authorization` / `X-Api-Key` 日志（首8+末4字符）
- **DEF-305** ✅ — `manage.sh start-cloud` 新增云 API `/models` 健康检查

### Added
- **Promptfoo 回归测试** (`promptfooconfig.yaml` + `tools/promptfoo_eval.sh`): 9 个固定 prompt 自动验证核心能力，集成到 `test/run_tests.sh --promptfoo`
- **pre-commit 分层触发**: 代理运行时自动跑 Promptfoo 快速模式（5 个核心测试），未运行时仅跑 unit tests
- **Langfuse sidecar** (`langfuse/docker-compose.yml`): 旁路观测基础设施就绪

### Changed
- **单元测试**: 78 → **124 cases**（+46），全部通过
- **DEFECT-LIST.md**: 更新修复状态统计（12 已修复 / 9 部分修复 / 8 未修复 / 1 设计限制）

### Metrics (snapshot at 0.5.1-progress)

| 指标 | 数值 |
|------|------|
| 总 commits | 39 (+6) |
| 代码行数 (`anthropic_proxy.py`) | ~3,700 (+~90) |
| 单元测试 | 124 cases (+46) |
| 缺陷修复 | 12 项（7 完全 + 5 部分） |
| Promptfoo 回归测试 | 9 cases |

---

## Future Releases

### [Unreleased] - target v0.6.0

**主题**: P0 遗留验证 + Langfuse 上线 + 剩余 P1/P2 修复

待办 (来自 DEFECT-LIST):
- ~~DEF-001~~ 🟡 部分修复 — 需生产验证 500 错误率 < 2%
- ~~DEF-002~~ 🟡 部分修复 — 需生产验证 loop_injected < 20%；Write 内容相似度检测未实施
- ~~DEF-003~~ ✅ 已修复
- ~~DEF-004~~ ✅ 已验证
- ~~DEF-005~~ 🟡 已缓解 — 需生产验证 OOM 减少
- ~~DEF-006~~ 🟡 已缓解 — 建议升级 rapid-mlx v0.6.71
- ~~DEF-007~~ ✅ 已修复
- 部署 Langfuse sidecar (3000 端口)
- 实施 `docs/proxy-prefix-cache-design.md` Phase 1-2
- 修复 DEF-107 (high_drop_ratio 干预)
- 修复 DEF-104 (白名单自动扩展)
- ~~DEF-204~~ ✅ 已修复
- ~~DEF-205~~ ✅ 已修复
- ~~DEF-302~~ ✅ 已修复
- ~~DEF-305~~ ✅ 已修复

### [Unreleased] - target v1.0.0

**主题**: llama_defender 库化 + 业务转型

- LiteLLM 替换协议转换 (~1200 LOC → ~100 LOC)
- 提取 `llama_defender` Python 库 (核心壁垒 6 模块)
- DSPy/GEPA 提示词优化模板
- 单元测试覆盖率 ≥ 80%
- 全部 30 项缺陷修复
- GitHub stars 1000+ 目标

---

[0.5.0-baseline]: #050-baseline---2026-06-06
