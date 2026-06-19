# 05 计划：分阶段落地路线图

## 5.1 总体节奏

| 阶段 | 周期 | 主题 | 主要产出 |
|------|------|------|----------|
| 第一阶段 | 1–2 周 | 基线与快速收益 | 修复关键指标、稳定 Cache Aligner 雏形 |
| 第二阶段 | 2–3 周 | 语义压缩与防御增强 | JSON/代码压缩器、Read 指纹、循环升级 |
| 第三阶段 | 2–3 周 | 资源与观测护栏 | 动态并发、内存预测、完整 metrics |
| 第四阶段 | 持续 | 配置治理与调参工具 | 配置模板、A/B 实验自动化、文档化 |

## 5.2 第一阶段：基线与快速收益（已实施）

### 5.2.1 目标

- 建立可信任的上下文优化 metrics 基线。
- 实现 Cache Aligner 最小可行版本（MVP）。
- 修复已知的可观测性错误。

### 5.2.2 任务清单

| 任务 | 负责人 | 验收标准 | 状态 |
|------|--------|----------|------|
| 1.1 修复 `re_read_rate` 公式 | 开发 | `test/unit/test_proxy_fallback.py` 新增断言通过 | ✅ 已完成 |
| 1.2 在 metrics 中新增 `common_prefix_ratio` 字段 | 开发 | 每条请求记录前后与上一请求的共同前缀比例 | ✅ 已完成 |
| 1.3 实现 `cache_aligner_mvp`：固定前 N 条消息 | 开发 | `PROXY_CACHE_ALIGN_ENABLED` 开关，关闭时不影响现有逻辑 | ✅ 已完成 |
| 1.4 工具定义过滤后排序 | 开发 | 不同请求相同工具集产生相同顺序，metrics 记录 `tools_count`/`tools_filtered` | ✅ 已完成 |
| 1.5 隔离 mid-conversation system 消息 | 开发 | Qwen chat template 不报错，信息不丢失 | ✅ 已完成 |
| 1.6 更新集成测试覆盖 | 开发 | `test/integration/` 新增 cache-align 场景 | ✅ 已完成 |
| 1.7 文档化第一阶段结果 | PM/开发 | 更新 `05-plan.md` 与 `CHANGELOG.md` | ✅ 已完成 |

### 5.2.3 实施详情

- 新增辅助函数：`_compute_re_read_rate`、`_compute_common_prefix_ratio`、`_message_stable_hash`、`_normalize_system_messages`、`_apply_cache_aligner`。
- `TOOL_ALWAYS_KEEP` 由 `set` 改为有序 `tuple`，`_filter_tools` 按白名单固定顺序 + 最近使用 + 字母顺序排序。
- `_handle_messages` 中集成：system 消息规范化 → cache aligner 拆分 → dynamic zone 压缩 → 重组 → common_prefix_ratio 计算与缓存。
- 新增集成测试 `test/integration/test_cache_align_integration.sh`，实测两请求间 `common_prefix_ratio` 可达 **0.87**。

### 5.2.3 预期收益

- `common_prefix_ratio` 可观测。
- 相邻请求共同前缀从 35% → 60%+。
- `re_read_rate` 指标正确。

## 5.3 第二阶段：语义压缩与防御增强

### 5.3.1 目标

- 用语义保留压缩替代粗暴 clearing，降低 token 量并减少 re-read。
- 增强循环/异常行为防御。

> 本阶段中 TokenSieve 借鉴点的详细开发计划见 `07-tokensieve-implementation-plan.md`。

### 5.3.2 任务清单

| 任务 | 负责人 | 验收标准 | 状态 |
|------|--------|----------|------|
| 2.1 实现 `JSONCompressor`（TokenSieve `sieve` 借鉴） | 开发 | JSON 数组结构化摘要：保留 key、截断 value、前 N 项 + 计数，失败回退 | ✅ 已完成 |
| 2.2 实现 `CodeCompressor` | 开发 | 删除不影响语法的空白/注释，保留代码 | ✅ 已完成 |
| 2.3 实现 `LogCompressor`（TokenSieve `log_compressor` 借鉴） | 开发 | 聚合重复日志行，保留错误行 | ✅ 已完成 |
| 2.4 实现 `ANSIScrubber`（TokenSieve `scrubber` 借鉴） | 开发 | 去除 Bash 输出中的 ANSI 颜色码与不可见字符 | ✅ 已完成 |
| 2.5 实现 `ContentRouter`（TokenSieve `router`/`pvfn` 借鉴） | 开发 | 按 JSON/代码/日志/文本选择压缩策略 | ✅ 已完成 |
| 2.6 实现 `CompressionAuditor`（TokenSieve `auditor` 借鉴） | 开发 | 压缩后校验 JSON 语法/代码平衡，失败回退 | ✅ 已完成 |
| 2.7 压缩器统一注册与阈值触发 | 开发 | 仅当 tool_result 长度 > `PROXY_COMPRESS_THRESHOLD` 时启用 | ✅ 已完成 |
| 2.8 实现标量 `Deduper`（TokenSieve `deduper` 借鉴） | 开发 | 长字符串首次出现后去重，默认关闭，aggressive 模式启用 | ✅ 已完成 |
| 2.9 Read 结果指纹 + re-read 检测 | 开发 | 跨请求识别未变化的重复读取，metrics 记录 | ⏸ 延期至 Phase 3 |
| 2.10 错误语义升级：Wasted call → 结构化提示 | 开发 | 模型收到 "文件自上次读取未变化，无需重读" 等提示 | ⏸ 延期至 Phase 3 |
| 2.11 Blocker 升级：强制换工具建议 | 开发 | Read 失败 2 次后建议 Bash，测试通过 | ⏸ 延期至 Phase 3 |
| 2.12 文本循环检测增强 | 开发 | n-gram + Jaccard 双重检测，误伤率 < 5% | ⏸ 延期至 Phase 3 |
| 2.13 集成测试与 benchmark | 开发/QA | `bench_agent.py` 运行 30 分钟无死循环 | 🟡 单测/集成测试通过，长会话 benchmark 待补 |

### 5.3.3 实施详情

- 新增辅助函数：`_scrub_ansi`、`_detect_content_type`、`_sieve_json`、`_compress_code`、`_compress_log`、`_compress_text`、`_dedupe_scalars`、`_audit_compression`、`compress_tool_result`。
- 在 `_compress_content_pass` 中新增 Phase 1b 语义压缩层：仅处理 Cache Aligner 划分出的 dynamic zone，threshold 默认 4096，mode 默认 `semantic`。
- 压缩器按内容类型路由：`json` → `_sieve_json`，`code` → `_compress_code`，`log` → `_compress_log`，`text` → `_compress_text`。
- 审计器默认开启：JSON 压缩后必须能 `json.loads`，代码压缩后必须保持括号平衡，失败则回退原内容。
- 新增集成测试 `test/integration/test_compress_integration.sh`，实测大 JSON tool_result 从 12480 字符压缩至 2442 字符（ratio ≈ 0.20）。

### 5.3.4 预期收益

- tool_result token 量下降 20%–40%（TokenSieve 式 JSON 摘要贡献主要收益）。
- `wasted` 错误率下降或归零。
- 长会话（30min+）稳定性提升。

## 5.4 第三阶段：资源与观测护栏

### 5.4.1 目标

- 降低 OOM 风险。
- 建立完整的上下文优化看板。

### 5.4.2 任务清单

| 任务 | 负责人 | 验收标准 | 状态 |
|------|--------|----------|------|
| 3.1 改进 token 预估模型 | 开发 | 按中英文、代码类型动态调整 ratio | ✅ 已完成 |
| 3.2 动态并发控制 | 开发 | 根据后端响应延迟/错误率调整 semaphore | ✅ 已完成 |
| 3.3 内存压力主动拒绝 | 开发 | 当 `/status` 显示可用内存 < 阈值时返回 503 + Retry-After | ✅ 已完成 |
| 3.4 输出 token 动态限制 | 开发 | 根据后端类型自动设置合理的 `max_tokens` 兜底 | ✅ 已完成 |
| 3.5 请求失败快照 | 开发 | 大请求失败时写入前后对比 JSON | ✅ 已完成 |
| 3.6 `/status` 增强 | 开发 | 显示 prefix ratio、压缩率、循环计数、最近 blocker | ✅ 已完成 |
| 3.7 统一 metrics schema | 开发 | 定义 v1 schema，所有指标字段固定 | ✅ 已完成 |

### 5.4.3 实施详情

- 新增辅助函数：`_estimate_tokens_dynamic`、`_classify_content_for_ratio`、`_should_reject_for_memory`、`_compute_dynamic_max_tokens`、`_record_request_for_concurrency`、`_adjust_concurrency`、`_write_request_snapshot`、`_cleanup_snapshots`、`_get_context_optimization_stats`。
- 新增 16 个环境变量（`PROXY_TOKEN_RATIO_*`、`PROXY_MEMORY_REJECT_THRESHOLD`、`PROXY_DYNAMIC_MAX_TOKENS_*`、`PROXY_DYNAMIC_CONCURRENT_*`、`PROXY_SNAPSHOT_*`）。
- 在 `do_POST` 中接入内存压力检查、失败快照、并发窗口记录与 `_adjust_concurrency`。
- 在 `_handle_messages` 中接入 `_compute_dynamic_max_tokens`，并记录 `max_tokens_original`/`max_tokens_dynamic`/`used_pct`。
- OOM safety 迭代截断同时考虑字符数与动态 token 估算。
- `_finalize_metrics` 使用动态 ratio 估算 token，并保证 `_METRICS_V1_FIELDS` 固定字段全部存在。
- `/status` 新增「Context Optimization」卡片；`/metrics` 返回 schema v1 与 Phase 3 聚合字段。
- 新增单元测试 22 个；新增 memory-reject、status 集成测试各 1 套。

### 5.4.4 预期收益

- OOM 导致的 503 下降。
- 调参从"拍脑袋"变为"看数据"。

## 5.5 第四阶段：配置治理与调参工具

### 5.5.1 目标

- 降低配置复杂度。
- 让 A/B 实验可自动化运行。

### 5.5.2 任务清单

| 任务 | 负责人 | 验收标准 | 预计工时 |
|------|--------|----------|----------|
| 4.1 定义 3 套配置模板 | PM/开发 | `local-balanced`、`local-aggressive`、`cloud` 可切换 | 1d |
| 4.2 启动时配置校验 | 开发 | 冲突参数打印警告，关键参数缺失报错 | 4h |
| 4.3 A/B 实验 runner 增强 | 开发 | `./tools/run_experiment.sh --compare cache_align` 自动对比指标 | 1.5d |
| 4.4 参数影响文档 | PM | 每个新参数说明默认值、推荐值、风险 | 1d |
| 4.5 预提交 hook 扩展 | 开发 | 每次修改代理后跑 unit + integration | 4h |

## 5.6 里程碑与验收标准

| 里程碑 | 时间 | 关键指标 | 验收方式 |
|--------|------|----------|----------|
| M1：基线可测 | 第 2 周末 | `common_prefix_ratio` 可采集，`re_read_rate` 正确 | 代码 review + 单元测试 |
| M2：Cache Aligner MVP | 第 4 周末 | 相邻请求共同前缀 ≥ 60%，prefix cache 命中率 > 0% | 30 分钟 benchmark |
| M3：语义压缩上线 | 第 7 周末 | tool_result token 下降 ≥ 20%，wasted 不增加 | A/B 实验对比 |
| M4：完整护栏 | 第 10 周末 | OOM 下降，metrics 完整，配置模板可用 | 生产环境 24h 观察 |

## 5.7 风险与应对

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| Cache Aligner 改动引入新 template 错误 | 中 | 高 | 默认关闭，cloud 模式先测 |
| 语义压缩破坏 JSON/代码语法 | 中 | 高 | 压缩后解析验证，失败回退 |
| 动态并发导致请求排队过长 | 中 | 中 | 设置排队上限，超时返回 503 |
| 指标采集增加日志量 | 高 | 低 | 采样 + 日志轮转 |
| 维护者时间不足 | 高 | 中 | 拆分小 PR，优先做 M1/M2 |

## 5.8 成功后的下一步

1. 将验证有效的策略沉淀为默认配置。
2. 探索跨会话的轻量级记忆层（如 Read 文件索引缓存）。
3. 考虑将代理核心逻辑拆分为模块化 Python 包，降低单文件维护压力。
