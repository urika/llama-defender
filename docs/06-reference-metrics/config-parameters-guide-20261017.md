# 配置参数指南（原 AGENTS.md §11）

> **出处**：自 AGENTS.md 拆出（2026-10-17）。权威数据源始终是 [`proxy_config.py`](../../proxy_config.py) 的 `CONFIG_REGISTRY`；
> 本文件是其「推荐组合」人读视图。改默认值请改 CONFIG_REGISTRY + manage.sh，再同步本表。

> 完整参数注册表见 [`proxy_config.py`](proxy_config.py) 的 `CONFIG_REGISTRY`（约 100+ 参数）。
> 以下仅列出最常用的参数及其推荐值。

### 11.1 压缩 Profile 推荐组合

`proxy_config.py` 定义了三种预设组合，通过 `PROXY_COMPRESSION_PROFILE` 切换：

| 参数 | balanced（默认） | aggressive | conservative |
|------|-----------------|------------|--------------|
| `PROXY_COMPRESS_MODE` | `smart` | `aggressive` | `conservative` |
| `PROXY_COMPRESS_MIN_CHARS` | 3000 | 1500 | 5000 |
| `PROXY_COMPRESS_TARGET_RATIO` | 0.40 | 0.25 | 0.55 |
| `PROXY_COMPRESS_LLM_CHUNK` | 4000 | 3000 | 6000 |
| `PROXY_COMPRESS_LLM_ENABLED` | `true` | `true` | `false` |
| `PROXY_COMPRESS_CLEAR_ENABLED` | `true` | `true` | `false` |
| `PROXY_CTX_TRUNCATE_STRATEGY` | `fifo` | `fifo` | `rounds` |
| `PROXY_CTX_KEEP_MESSAGES` | 40 | 30 | 50 |
| `PROXY_LOOP_THRESHOLD` | 5 | 4 | 6 |
| `PROXY_OOM_SAFE_TOKENS` | 60000 | 50000 | 70000 |

**选择建议**：
- **balanced**: 日常 coding 任务，兼顾质量与性能
- **aggressive**: 长上下文 agentic 场景，优先控制 token 消耗
- **⚠ 云端路由不再建议 aggressive**（2026-09-07 per-route 定案）：云端由客户端自管上下文，代理压缩造成双重管理错乱；压缩仅限本地路由（engine-on 已接管）。参考 ctx-case 设计文档 §10.1 per-route 豁免
- **conservative**: 质量敏感任务（代码审查、文档生成），优先保留上下文完整性

### 11.2 本地模式关键参数

| 参数 | 默认值 | 推荐值 | 说明 |
|------|--------|--------|------|
| `--hybrid-cache-entries` | `0`（rapid-mlx） | `8` | **hybrid 模型（Qwen3.8/3.6/Ornith）必配**：trim-free 前缀复用；0=禁用→每轮全量冷 prefill。rapid-mlx `--enable-prefix-cache` 会自动设 8，显式写 8 仅保明确。**越大≠越好**：2026-09-21 实测 32→8 反而 active/peak -3.7GB、pressure-evict 177→0、墙钟 -32%（条目少→recurrent 快照占用低），见 runbook §19.3 |
| `--cache-memory-mb` | `12288`（12GB） | `12288` | prefix cache 上限；**勿盲目调大**：12→16GB 实测反引入 Metal pressure（cache 上限与 Metal cap 打架），已回退（runbook §18.4） |
| `PROXY_MAX_CONCURRENT` | `1` | `1` | 48GB Mac 上推荐 1，OOM 风险 |
| `PROXY_CTX_TRUNCATE_STRATEGY` | `fifo` | `fifo` | 当前生产策略，prefix cache 友好 |
| `PROXY_CTX_KEEP_MESSAGES` | `40` | `40` | fifo 窗口大小 |
| `PROXY_OOM_SAFE_TOKENS` | `60000` | `60000` | OOM 安全阈值，约 120K chars |
| `PROXY_PRE_TRUNCATE_CHARS` | `400000` | `400000` | 请求体预截断阈值 |
| `PROXY_MAX_REQUEST_BYTES` | `512000` | `512000` | 请求体硬上限（500KB） |
| `PROXY_CLEAR_ENABLED` | `false` | `false` | 本地后端建议关闭，避免 Wasted call 循环 |
| `PROXY_TOOL_FILTER_ENABLED` | `true` | `true` | 本地模式默认开启 |
| `PROXY_TOOL_FILTER_MAX` | `20` | `20` | 超过此数量触发过滤 |
| `PROXY_TOOL_AUTO_PROMOTE_THRESHOLD` | `3` | `3` | 使用 ≥3 次自动加入 keep 集 |
| `PROXY_LOOP_THRESHOLD` | `5` | `5` | 循环检测阈值 |
| `PROXY_LOOP_LEVEL3` | `9` | `9` | Level 3 触发阈值（移除全部工具） |
| `PROXY_DYNAMIC_MAX_TOKENS_SATURATION` | `2048` | `8192` | saturation 档输出预算（长上下文收紧，防生成超时；见 `docs/02-architecture-design/agent-output-budget-design-20260827.md`） |
| `PROXY_DYNAMIC_MAX_TOKENS_OOM` | `4096` | `4096` | oom_danger/pre_trunc 档输出预算（极端上下文兜底；agent 连续会话由 continuation 短路归入 saturation，此档主要首轮兜底） |
| `PROXY_TIMEOUT_MARGIN_S` | `30` | `30` | 非流式主动 504 余量：后端超时钳为 `客户端超时−30s`，保证错误在客户端断连前送达（消除 `CRITICAL: failed to send error`） |
| `PROXY_STREAM_IDLE_TIMEOUT_S` | `30` | `30` | 流式 chunk 空闲看门狗：首 token 后无 chunk 超此秒数即中止中继并取消后端在途生成 |
| `PROXY_COMPRESSION_PROFILE` | `balanced` | `balanced` | 压缩策略预设组合 |

### 11.3 云端模式关键参数

| 参数 | 默认值 | 推荐值 | 说明 |
|------|--------|--------|------|
| `PROXY_MAX_CONCURRENT` | `5` | `5` | 云端并发高，API 无 OOM 风险 |
| `PROXY_TOOL_FILTER_ENABLED` | `false` | `false` | 云端不触发工具过滤 |
| `PROXY_CLEAR_ENABLED` | `true` | `true` | 云端可开启 tool result 清理 |
| `PROXY_COMPRESSION_PROFILE` | `balanced` | `aggressive` | 云端按 token 计费，建议激进压缩 |

### 11.4 路由参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_ROUTE_SENSITIVE_PATTERNS` | `""` | 匹配的请求强制走本地（逗号分隔关键字） |
| `PROXY_ROUTE_FORCE` | `""` | 强制路由模式：`local` 或 `cloud` |
| `PROXY_CLOUD_COOLDOWN` | `300` | 云端失败后的冷却时间（秒） |
| `PROXY_CLOUD_MAX_RETRY` | `2` | 云端重试次数 |

### 11.5 请求队列参数（Phase 1）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_QUEUE_ENABLED` | `false` | 启用优先级请求队列；默认关闭 = 原信号量行为（`qwen3.8-27b-4bit` 与 `ornith-oq4e` 已开启；后者为缓解 haiku 强制本地后长会话饿死小请求） |
| `PROXY_QUEUE_TIMEOUT_SECONDS` | `300` | 排队超时，超时返回 503 + `Retry-After` |
| `PROXY_QUEUE_LARGE_THRESHOLD_CHARS` | `80000` | large bucket 阈值（低优先级排队） |
| `PROXY_QUEUE_HUGE_THRESHOLD_CHARS` | `200000` | huge bucket 阈值：不入本地队列，按 `HUGE_ACTION` 处理。**engine-on 会话豁免（2026-09-07 TC28）**：先按引擎视图 est_tokens 复检，视图≤折叠预算 S 则 waive 放行（原始 chars 不再代表后端负载） |
| `PROXY_QUEUE_HUGE_ACTION` | `cloud` | huge 处理：`cloud`（强制路由云端，需 `PROXY_ROUTE_ENABLED=true`）/ `reject`（413） |

### 11.6 诊断数据面参数（R13-R16＋S-17 录制面，全部 reloadable）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_DIAG_ENABLED` | `true` | 诊断数据面总开关（头/尾注/台账/档案/落盘） |
| `PROXY_DIAG_SSE_TAIL` | `true` | 流式 SSE 尾注 `: x-proxy-diag`（关 = 纯头模式） |
| `PROXY_DIAG_SESSION_TTL_MIN` | `180` | 台账/档案会话 TTL（分钟） |
| `PROXY_DIAG_SESSION_MAX` | `64` | 内存台账会话数上限（FIFO 驱逐，驱逐后端点 410） |
| `PROXY_DIAG_ARCHIVE_ENABLED` | `true` | sent_view 常态每轮落盘（`logs/diag/archive/`） |
| `PROXY_DIAG_ARCHIVE_MAX_MB` | `200` | archive 磁盘总量上限（超限删最老会话文件） |
| `PROXY_DIAG_LEDGER_ENABLED` | `true` | 台账增量落盘 `logs/diag/ledger/`（R14 端点档案兜底；关 = 仅内存） |
| `PROXY_DIAG_LEDGER_MAX_MB` | `100` | ledger 磁盘总量上限（超限删最老会话文件） |
| `PROXY_REQUEST_ARCHIVE_ENABLED` | `false` | **S-17/BR-4 录制面**（`request_archive.py`）：逐请求档案 append-only（`s17-archive/v1`，`logs/diag/archive-req/` 按 view 分片 `.R` 改写前/.`Rp` 改写后）；默认关=重启行为中立，激活=conf 置 true + reload（批界窗执行） |
| `PROXY_REQUEST_ARCHIVE_DIR` | `logs/diag/archive-req` | 档案分片目录（相对路径锚定 `_SCRIPT_DIR`） |
| `PROXY_REQUEST_ARCHIVE_MAX_MB` | `2048` | 档案总量上限；驱逐以会话为单位成对删（拆散=mirror 拒收档案）＋L-18 活跃保护窗内不驱逐、全保护接受溢出 |
| `PROXY_REQUEST_ARCHIVE_RESPONSE_MAX_CHARS` | `65536` | response 行序列化上限（保形截断，`truncated:true`；流式已由尾部聚合变量 materialization——v1.1） |
| `PROXY_CTX_ENGINE_ENABLED` | `false` | R8 上下文工程引擎（append-only + 写入期压缩 + epoch；开启时 6/7/14/17 跳过） |
| `PROXY_CTX_EPOCH_TRIGGER_TOKENS` | `0` | epoch 预算 S（tokens）；0=auto → min(65%×ctx/4, 70K) |
| `PROXY_CTX_WINDOW_K` | `0` | epoch 重切保留最近 K 轮；0=auto → 24 |
| `PROXY_DIAG_TIMINGS_SOURCE` | `auto` | prefill 数来源：`auto`（响应体 timings 探测，无则字段缺省）/ `off` |
| `PROXY_CACHE_LCP_SNAPDOWN` | `1` | LCP snap-down（GDN 线性态检查点续算，Gate A 验证 29.7×）；生产画像见 `tools/lcp_trace_analyzer.py` 归档 |

### 11.7 循环干预与 fnf 缓解参数（fnf 一期 F1/F2/F3'，2026-10-26，全部 reloadable，缺省关＝零行为变化）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_FNF_GUIDE_ENABLED` | `false` | **F1**：file_not_found 会话账本＋分级升级提示——同路径第 N 次失败（默认 N≥3）注入「已确认不存在（第 N 次）＋禁止再试＋替代行动」；清零口＝同路径后续成功读取。激活=空窗一次戳（conf sha256＋窗前后 /metrics 快照）→ swe-eval 探针 A/B |
| `PROXY_FNF_CANDIDATES_ENABLED` | `false` | **F2**（F1 子旗标）：最近邻候选——首败时 `ls -1 <父目录>`（2s 超时、512 chars、每会话每目录至多一次）注入真实存在的相近文件；安全边界＝仅**已证工作区根**内（根集＝同会话成功 Read 的 file_path 公共前缀 ≥2 层），根外不取 |
| `PROXY_LOOP_ESCALATION_MEMORY_ENABLED` | `false` | **F3'**：会话级升级记忆——阶梯触发带会话级累积计数（注入「第 N 次」）；L2 sticky＝同错误形态在限制窗内重现 → 维持摘除并升级提示（打破「干预饱和但无记忆无升级」） |
| `PROXY_LOOP_STICKY_TURNS` | `3` | F3' sticky 窗宽（轮）——L2 摘除后同错误形态在此轮数内重现即维持摘除 |
| `PROXY_LOOP_BASH_PATH_KEYS_ENABLED` | `false` | **F3**（2026-10-26 v0.1 起降级为兜底面）：Bash 计数键修复——从 tool_result 错误行派生 `Bash:fnf:<path>` 键；注意 1025-5 实测阶梯本已重度触发（变体逃逸前提失效） |

> 旗标名与 swe-eval `configs/capability-flags.yaml` 的 `fnf_guide_kit` 册条逐字一致
> （`PROXY_FNF_GUIDE_ENABLED` / `PROXY_FNF_CANDIDATES_ENABLED` / `PROXY_LOOP_ESCALATION_MEMORY_ENABLED`）。

### 11.8 admin 注入原语参数（ADR-013 T1，reloadable）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_ADMIN_TOKEN` | `""` | `/admin/inject` 鉴权；默认空 = localhost-only（同既有 admin 端点） |
| `PROXY_INJECT_MAX_CHARS` | `4000` | 注入 text 上限，超限 413（不截断） |

### 11.9 BVR 自调优参数（bvr-autotune-loop-design-20260916，reloadable）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_AUTOTUNE_ENABLED` | `false` | 自调优总开关；默认关 = 纯旁路 CLI（`autotune.py` 不进入 proxy 运行路径） |
| `PROXY_AUTOTUNE_DAILY_LIMIT` | `5` | 每日晋级提案上限（T4 限速，gate 返回 `over_daily_limit`） |
| `PROXY_AUTOTUNE_SESSION_LIMIT` | `1` | 单会话晋级提案上限（gate 返回 `over_session_limit`） |

> **2026-10-05 裁决（hub 0925-2①，域负责人拍板"分工"）**：autotune **降格为纯提案器**——`generate/list` 保留，`apply/rollback` 冻结（提案一律进 swe-eval E 期周闸门人审，采纳后经 goal.yaml 管控的 knob 路径生效）；参数白名单唯一权威＝`configs/goal.yaml`（version 递增），白名单外提案拒绝。**批期禁 autotune apply**（同批期红线 1 族）。生效链路（overrides.conf/reload 钩子）如需接通，先按契约 §六登记（roadmap #15）。

### 11.10 跨会话记忆 + 自动交接（R-CM-1/R-CM-2，2026-10-08，reloadable）

需求：[`docs/01-requirements-product/context-engineering-enhancement-requirements-20261008.md`](../../docs/01-requirements-product/context-engineering-enhancement-requirements-20261008.md)。**均默认关**。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_MEMORY_ENABLED` | `false` | R-CM-1 跨会话记忆总开关（`memory_store.py`，stdlib sqlite3+FTS5） |
| `PROXY_MEMORY_DB_PATH` | `logs/memory/memory.db` | 记忆库路径（相对 `_SCRIPT_DIR`） |
| `PROXY_MEMORY_DEFAULT_PROJECT` | `default` | 记忆分域键；请求头 `X-Proxy-Project` 可覆盖 |
| `PROXY_MEMORY_INJECT_MAX_CHARS` | `2000` | 会话首请求注入记忆块上限 |
| `PROXY_MEMORY_TOP_K` | `5` | 记忆检索条数上限 |
| `PROXY_MEMORY_TTL_DAYS` | `90` | 保留期（P0 仅登记） |
| `PROXY_HANDOFF_ENABLED` | `false` | R-CM-2 自动交接总开关（`handoff.py`，规则化抽取） |
| `PROXY_HANDOFF_TRIGGER_CHARS` | `0` | 自动触发阈值（0=仅显式端点） |
| `PROXY_HANDOFF_MAX_CHARS` | `3000` | 注入块上限 |
| `PROXY_HANDOFF_DIR` | `logs/diag/handoff` | 落盘目录（JSONL + markdown） |

- 注入 stage：`MemoryInjectStage`（0.61）/ `HandoffInjectStage`（0.62），**会话首请求注入一次**（进程内守卫，防每轮重复破坏前缀）；engine-on 持久进 canonical、engine-off view-only；append-only + fail-open。
- 端点：`GET /api/memory`（`?project=&q=&mode=stats`）、`POST /admin/memory`、`DELETE /admin/memory/<id>`、`GET /api/handoff?session=KEY`、`POST /admin/handoff`。
- CLI：`./manage.sh handoff <session_key> [--goal/--decision/--next/--question/--distill]`。
- P0 交接为**规则化**生成（确定性、零后端依赖）；LLM 富化生成接口预留（`handoff.build(..., enrich=...)`）。

队列分桶：interactive（<16K chars，最高优先级）→ standard → large → huge；同 bucket FIFO。响应头带 `X-Queue-Bucket/Position/Estimated-Wait-Ms/Wait-Ms`，状态见 `GET /api/queue`。

> 完整参数列表及详细文档见 [`proxy_config.py`](proxy_config.py) 的 `CONFIG_REGISTRY`。

---

