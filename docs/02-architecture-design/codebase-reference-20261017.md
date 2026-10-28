# 代码库参考（Codebase Reference）

> **出处**：自 AGENTS.md 拆出（2026-10-17，拆分动因=参考级明细内联导致 agent 引导文件膨胀至 700 行）。
> 本文件是 **AGENTS.md §3/§4.3 的完整展开**：模块清单、请求管线与旁路系统、配置文件明细、工具清单、Admin API 端点。
> 修订规则：改这里，AGENTS.md 只留指针——两处不要同时维护。权威数据源：参数默认值=`proxy_config.py` CONFIG_REGISTRY；管线实现=`pipeline.py`。

## 一、核心文件清单（原 AGENTS.md §3.1）

### 3.1 核心文件

| 文件 | 用途 |
|------|------|
| [`manage.sh`](../../manage.sh) | 服务管理器：start/stop/restart/reload/switch/status/watchdog/wizard，以及本地/云端路由强制切换 |
| [`anthropic_proxy.py`](../../anthropic_proxy.py) | HTTP 代理入口：`ThreadingHTTPServer` + `Handler`（~1480 行）；流式/非流式响应处理、双协议端点；请求处理逻辑已全部下沉到 `pipeline.py` |
| [`pipeline.py`](pipeline.py) | 管线抽象：25 个可独立测试的 `PipelineStage`，把 `_handle_messages` 拆成 RequestParser、LifecycleClassifier、SmartRouter、ContentCompressor、ContextTruncator、BackendDispatcher 等阶段 |
| [`proxy_state.py`](proxy_state.py) | 单一真相源：所有 `PROXY_*` / `LLAMA_*` 常量、共享可变状态、线程本地上下文、`_RELOAD_SPEC`、模型别名、路由状态 |
| [`proxy_config.py`](proxy_config.py) | `CONFIG_REGISTRY`：每个环境变量的默认值（区分 local/cloud）、类型、作用域、文档说明；配置统一阶段一起为默认值唯一权威（`get_default()` / `validate_startup()` / `write_defaults_sh()`） |
| [`backend_strategy.py`](../../backend_strategy.py) | `BackendStrategy` / `LocalStrategy` / `CloudStrategy`：把 38+ 处 `if IS_CLOUD` 收敛为策略类 |
| [`reload_config.py`](../../reload_config.py) | SIGHUP 热重载实现：重新解析 `configs/active.conf` + `configs/secret.local.conf`，更新 `proxy_state` 与主模块；同时重载模型目录并重建 `MODEL_ROUTE_PREFERENCES` |
| [`model_registry.py`](../../model_registry.py) | 模型目录注册表：加载/校验 `configs/models.json`（providers/models/routes 三段，对齐 agent_go 三层设计），`$env`/`$default` 引用、fallback chain、坏文件拒绝热替换、`catalog_hash`；目录文件缺失时自动合成等价目录（与旧硬编码行为一致），`MODEL_ROUTE_PREFERENCES` 与 `get_model_aliases()` 均由其派生 |
| [`message_converter.py`](../../message_converter.py) | Anthropic ↔ OpenAI 消息与工具格式双向转换（含 `convert_openai_request_to_anthropic`，双协议端点入口）、token 估算 |
| [`tool_parser.py`](tool_parser.py) | XML ↔ JSON 工具参数解析、`<tools>` 内容块 fallback、流式工具提取器 |
| [`content_compressor.py`](../../content_compressor.py) | TokenSieve 语义压缩 + BM25 相关性驱动压缩（TS-1）：JSON / 代码 / 日志 / 文本的分层压缩。TS-4（2026-08-18 日志分析落地）：BM25 drop 分支改为类型感知结构化压缩（`_structured_compress`，json/code/log 保结构），结果超 `PROXY_BM25_DROP_TARGET_RATIO`（默认 0.45）再截断封顶；`PROXY_BM25_DROP_THRESHOLD` 默认 0.5→0.1；客户端断连（BrokenPipe）按 499 记账不再计 500 |
| [`compression_types.py`](../../compression_types.py) | TS-3 统一压缩结果类型契约：`CompressionResult` / `CompressionSubResult` TypedDict（JSON 可序列化，兼容 Py3.8+） |
| [`session_ledger.py`](../../session_ledger.py) | R14/R15 诊断存储：会话台账（action 轨迹 / dup / last_dup_turn / 材料清单，客户端原始历史增量扫描 + 前缀失配全量重建 → `canonical_mismatch`）+ sent_view 档案（`logs/diag/archive/<sid>.jsonl`，MB 上限，TTL 驱逐）+ **台账增量落盘**（`logs/diag/ledger/<sid>.jsonl`，A3 2026-08-20：R14 端点内存优先、档案兜底，跨重启/驱逐可查——agent_go 轮级看门狗不失忆） |
| [`request_archive.py`](../../request_archive.py) | **S-17/BR-4 录制面**（`PROXY_REQUEST_ARCHIVE_ENABLED` 默认关，2026-10-13）：逐请求档案 append-only（`s17-archive/v1`，schema 契约=swe-eval `s17-recording-surface-design` §六，消费方 `mirror_replay.py`）。按 view 分片 `logs/diag/archive-req/<sid>.R.jsonl`（改写前公平口径）+ `<sid>.Rp.jsonl`（改写后敏感性口径，Anthropic 形状可重放）；snapshot/delta 增量（msg_hash 前缀比对，客户端改写/epoch 折叠自动 snapshot 兜底）、response 行（非流式全量、流式由中继尾部聚合变量 materialization——v1.1 2026-10-15，均超长保形截断）、重启扫片续号、配额驱逐以会话为单位 + L-18 活跃保护。挂点全旁路 fail-open，不改管线主链/log_request/流式事件序列 |
| [`context_engine.py`](../../context_engine.py) | R8.1-R8.3 上下文工程引擎（Phase 1 2026-08-21，**默认关**）：append-only canonical 会话（客户端历史写入期压缩后冻结，永不回溯改写——修复前缀缓存击穿）+ epoch 状态机（超 S 预算一次性收编 K 窗口外轮次入压缩区，回退保护 K=4→L3 减半→413 硬上限）；开启时管线 6/7/14/17 四 stage 联动跳过 |
| [`diagnostics.py`](../../diagnostics.py) | R13/R16 诊断记录器：per-request 累积（7 处注入登记 / timings 能力探测 / token 事实）、`X-Proxy-Diag-*` 响应头、SSE 尾注 `: x-proxy-diag {...}`、`logs/diag/sessions.jsonl` per-turn 深度记录（request_id 与 proxy_metrics 关联）、lifecycle 事件（R7 兑现） |
| [`truncation.py`](truncation.py) | 上下文截断：char / rounds / fifo / smart 策略、单遍合并压缩（L2 清除 + L4 thinking 剥离）、工具对原子保护（TS-2）、关键词索引、摘要缓存 |
| [`lifecycle.py`](../../lifecycle.py) | 生命周期阶段分类（init/growth/expansion/saturation/oom_danger/pre_trunc）与动态 token 预算 |
| [`loop_detection.py`](../../loop_detection.py) | 工具循环、文本输出循环、阻塞模式检测与干预 |
| [`tool_filter.py`](tool_filter.py) | 动态工具定义过滤，降低长工具列表的 token 开销 |
| [`admin_server.py`](../../admin_server.py) | `/status` 状态页、`/api/*` 结构化 JSON 端点、系统内存/进程信息、指标聚合与 `/metrics/history`、请求快照清理、并发统计 |
| [`proxy_logging.py`](proxy_logging.py) | 结构化 JSONL 日志、敏感头脱敏 |
| [`hbe_probe.py`](hbe_probe.py) | H_BE shadow 探针（2026-08-29，`PROXY_HBE_*` 均 reloadable，active 配置已开）：成功本地响应后搭 prefix cache 便车追加双探针锚定提问，top_logprobs（默认 20）截断熵估计信念熵，落盘 `logs/diag/hbe.jsonl`（10MB 轮转）。只测不动——不改任何路由/截断/压缩决策；仅本地路径触发；引擎锁等待有界（`LOCK_WAIT_S` 超时自动放弃），fail-open。冒烟实测：cache 便车 16K tokens 探针仅 1.2s |
| [`unit_model.py`](../../unit_model.py) | 消息原子化统一词汇表（叶子模块，**禁止 import 其他仓库模块**）：Handle（TypedDict `{"type","value"}`）词汇表唯一源头（对齐 context_engine/session_ledger/pipeline 三处漂移）、`msg_hash`/`msg_text_hash` 指纹、`text_str`/`iter_blocks`/`result_text` 内容原子化遍历。后续 `unit_id` 分配器（IFC 数据架构 §3.1 Unit 实体）在此扩展 |
| [`ifc_metrics.py`](../../ifc_metrics.py) | IFC Tier-0 结构指标（信号层，`PROXY_IFC_ENABLED` 默认开，纯函数 + 有界会话基线）：**锚点差分法**——相邻两轮发送视图差分 + 台账动作序列派生 `retention`/`rationale_ratio`/`action_div`（工具序列归一化 bigram 熵，双端检测循环坍缩/游走）/`reread_pressure`。不触碰管线 stage（无 per-stage 钩子），见 `docs/02-architecture-design/information-fidelity-control-design-20260829.md` |
| [`memory_stores.py`](../../memory_stores.py) | PDC 披露清单（manifest）存储：被丢弃上下文单元的可寻址索引行（会话页表，`R10.1`「索引永不丢」），`logs/diag/manifest/<sid>.jsonl` 增量落盘 + MB 上限 + 跨批隔离（`SESSION_GAP_SECONDS` 轮转 `.prev`）。写入方：truncation fifo/OOMSafetyFIFO 与 context_engine epoch 折叠 |
| [`ctx_recall.py`](../../ctx_recall.py) | PDC 渐进披露检索核心（`R10.2`，已挂载）：L1 内存子串过滤 + L2 FTS5 trigram 全文（`logs/diag/index/<sid>.db`）+ archive 全文恢复（`recover_full_content`）+ **IFC-3 微轮自答**（`build_follow_up_messages` 同请求自答 ctx_recall 并重派，客户端透明）；`TOOL_SCHEMA` 工具定义；三级穿透检索（整句短语→token 聚合 r: 内容行→全量） |
| [`memory_store.py`](../../memory_store.py) | **R-CM-1 跨会话持久记忆**（`PROXY_MEMORY_ENABLED` 默认关，2026-10-08）：项目级 stdlib `sqlite3`+FTS5（不可用则 LIKE 回退）记忆库（`MEMORY` 单例），`add/search/list/forget/stats/export_markdown`；`MemoryInjectStage`（0.61）会话首请求注入一次（append-only；engine-on 进 canonical）；端点 `GET /api/memory`/`POST /admin/memory`/`DELETE /admin/memory/<id>`；分域头 `X-Proxy-Project`。fail-open |
| [`handoff.py`](handoff.py) | **R-CM-2 自动交接摘要**（`PROXY_HANDOFF_ENABLED` 默认关，2026-10-08）：从 `session_ledger` 规则化抽取结构化 handoff（goal/files_touched/tools_used/recent_actions/repeated_targets）；`store`（`logs/diag/handoff/<sid>.jsonl`+`.md`）/`latest`/`render_block`/`distill_memory`；`HandoffInjectStage`（0.62）下一会话首请求注入；端点 `GET /api/handoff`/`POST /admin/handoff`，CLI `./manage.sh handoff`。P0 为规则化生成，LLM 富化接口预留 |
| [`queue_manager.py`](../../queue_manager.py) | 请求优先级队列：按 total_chars 分桶（interactive/standard/large/huge），heapq 最小堆，叠加在 `_llama_lock` 之上；`acquire(timeout)` 排队超时出队；`stats()` 供 `/api/queue`。默认关 `PROXY_QUEUE_ENABLED=false`（active 配置已开） |
| [`signal_types.py`](../../signal_types.py) | Signal Layer 数据契约（叶子）：`ViewSummary`/`DiffResult`/`ReconcileResult`/`IFCSection` 4 个类型；字段全 Optional（fail-open：信号层故障时协议层以默认值运行） |
| [`protocol_types.py`](protocol_types.py) | Protocol Layer 数据契约：五协议（P1-P5）+ 编排器 8 个核心类型（`Task`/`SubTask`/`VerificationRule`/`Citation`/`Patch`/`Verdict`/`EscalationDecision` 等），依赖 signal_types |
| [`contract_registry.py`](../../contract_registry.py) | 契约注册表（叶子元数据）：所有数据契约的元数据管理 + 运行时验证（fail-open 返回错误列表，不抛异常）；`join_keys`/`sla`/`producer`/`consumers` 描述数据血缘 |
| [`idempotency.py`](../../idempotency.py) | 幂等管理器：对 decompose/verify/recall 三类纯计算做内容寻址缓存（线程安全 LRU + TTL），同任务同会话不重复计算 |
| [`decompose.py`](../../decompose.py) | P1 分解协议：双判据递归分解（容量判据按 token 预算拆文件等），将大任务拆为输入集在预算内的原子子任务 |
| [`verification_chain.py`](../../verification_chain.py) | P3 验证协议：三级验证链（职责链 CoR）L1 机械 → L2 语义 → L3 台账 |
| [`escalate.py`](../../escalate.py) | P5 升级协议：验证失败后的策略切换决策（决策表 + 幂等闸 + 熔断器 + `TaskState` 有限状态机） |
| [`post_governance.py`](post_governance.py) | 后治理基础版 + 数据质量监控：入场层校准（`EntryLayerCalibrator` 按历史推荐入场层 0-3）、模式编译（`PatternCompiler` 成功模式 ≥3 次下沉为 Skill）、数据质量 SLA（`DataQualityMonitor`）；对齐 L0-rules/L1-prompts/L2-memory/L3-control 分层语义 |
| [`protocol_orchestrator.py`](protocol_orchestrator.py) | 五协议编排器（P1 Decompose→P2 Execute→P3 Verify→P4 Recall→P5 Escalate 闭环）：`TaskState` 状态机跟踪 + 非法转换抛异常 + 幂等 + 死循环防御（全局迭代上限 + 单子任务重试上限）；执行器/召回器为注入 callable（Phase 1 stub / Phase 2 接 pipeline 真实模型调用） |
| [`autotune.py`](../../autotune.py) | BVR 自调优闭环 Phase 1（`bvr-autotune-loop-design-20260916`）：失败事件 → 提案提取器规则引擎（loop/amnesia/prefix_break/oom_evict/dup_reread/off_track）→ 兑现闸门（失败面绑定/预言/限速/扩能力降权）→ 分级签名（low auto / medium・high human）→ 审计落盘 `logs/diag/autotune/`；纯旁路 CLI（`./manage.sh autotune`），不进入 proxy 运行路径 |


## 二、数据流与旁路系统（原 AGENTS.md §3.2）

### 3.2 数据流

```
Client POST /v1/messages（Anthropic）或 POST /v1/chat/completions（OpenAI，经 convert_openai_request_to_anthropic 转换）
  → anthropic_proxy.py:Handler.do_POST()
  → _llama_lock 获取并发许可
  → Handler._handle_messages()
  → InstrumentedPipeline 依次执行 25 个 stage（编号为代码注释中的历史层号）
       0. RequestParser
       0.5. ContextEngine (上下文工程引擎, 默认关; 开启时 6/7/14/17 跳过)
       0.6. AdminInjectStage (admin 注入原语, ADR-013 T1; /admin/inject 队列非空才运行)
       0.61. MemoryInjectStage (R-CM-1 跨会话记忆注入, 默认关; 会话首请求注入一次)
       0.62. HandoffInjectStage (R-CM-2 自动交接注入, 默认关; 会话首请求注入一次)
       1. LifecycleClassifier
       2. DynamicMaxTokens
       2.5. SmartRouter (local/cloud 路由决策)
       2.6. RouteNotification
       3. ErrorTranslator
       4. BlockerDetector
       5. SystemNormalizer
       6. CacheAligner
       7. ContentCompressor
       8. ToolLoopDetector
       9. TextLoopDetector
      10. SessionLoopState
      11. LoopIntervention
      12. RereadDetector
      12.5. AutoRecallStage (ctx_recall 自闭环 auto-recall, 默认关; 台账 dup 检测→manifest 折叠确认→召回尾部注入)
      13. DateNormalizer
      14. ContextTruncator
      15. HighDropRatioNotice
      16. MessageHashDebug
      17. OOMSafetyFIFO
      18. PrefixRatioComputer
      19. ToolPairingRepair
      20. FormatConverter
      21. BackendDispatcher
  → 转发到后端（local 后端走 OpenAI chat completions 格式；cloud 直接透传；OpenAI 端点请求 `_openai_mode` 原样返回 OpenAI 格式响应）
  → 流式或非流式响应返回 Client
```

**旁路观测/信号层（不改变主数据流，只读或失败静默）**：
- **H_BE 探针**（`hbe_probe.py`）：成功本地响应后，搭 prefix cache 便车追加双探针锚定提问，`top_logprobs` 截断熵落盘 `logs/diag/hbe.jsonl`；只测不动。
- **IFC 信号层**（`ifc_metrics.py` + `diagnostics.py` + `memory_stores.py`）：相邻轮发送视图差分 + 台账动作序列派生 `retention`/`rationale_ratio`/`action_div`/`reread_pressure`；无 per-stage 钩子。
- **PDC 披露召回**（`ctx_recall.py` + `memory_stores.py`）：被 fifo/epoch 丢弃的单元写入 manifest（可寻址索引行），模型经 `ctx_recall` 工具查询（L1 内存子串 + L2 FTS5 trigram + archive 全文恢复），微轮自答同请求重派。**自闭环 auto-recall**（stage 12.5，`PROXY_AUTO_RECALL_ENABLED` 默认关，2026-09-05 设计）：台账 `dup_queries` 检测同 Read 目标 ≥3 次 + manifest 确认已折叠 → 代理代答取回并以 `[System: AUTO-RECALL]` user 尾消息注入（每轮至多 1 条、每会话限 5 次、同目标去重；阈值/上限/注入长度均 reloadable）——实测模型主动召回 0 调用后的主动兜底，详见 `docs/01-requirements-product/ctx-recall-auto-closed-loop-20260905.md`。**墓碑召回**（`PROXY_TOMBSTONE_RECALL_ENABLED` 默认关，2026-09-06）：客户端历史改写把旧 tool_result 丢成悬空调用（工具结果单轮生命周期，EXP-2 实锤 transcript 级证据），stage 20 配对补洞注入的墓碑附 ctx_recall 取回提示（被动）+ AutoRecallStage 按悬空 call_id 直查 manifest 寄存并代答回填（主动，末条 assistant 未决调用豁免）——写入期压缩实测已寄存 ~40% 墓碑化内容。2026-09-07 增强（DEF-310/seq5 落地）：①auto-recall 注入升级为**结构感知摘录**（AST 骨架+focus 命中块，修复 Read 行号前缀致 ast 失效的上游 bug）+ **卡死型升级转径**（已注入目标仍重读达 阈值+STUCK_EXTRA 时改注入换路线提示）；②**关键事实钉住**（IFC-12）：折叠收编时提取 token/key=值 行常驻台账头；③**断言值核对**（IFC-13）：模型断言值与登记事实矛盾时下轮注入 VALUE-CHECK 更正；④**反编造禁令**（IFC-11）进 RECALL_CUE。EXP recall-r2 机制实证 6 次注入；治理曲线见 DEF-312。
- **队列**（`queue_manager.py`）：`PROXY_QUEUE_ENABLED` 时叠加在 `_llama_lock` 之上按 interactive/standard/large/huge 分桶排队。
- **录制面**（`request_archive.py`，S-17/BR-4，2026-10-15）：逐请求档案 append-only（`s17-archive/v1`，schema 契约=swe-eval `s17-recording-surface-design` §六）——R（改写前公平口径）/R′（改写后敏感性口径）双视图 snapshot/delta（msg_hash 前缀比对，客户端改写/epoch 折叠自动 snapshot 兜底）＋response 行（非流式全量、流式由中继尾部聚合变量成形——v1.1）；五挂点全旁路（入口暂存/capture_sent_view/anthropic 块/响应完形/do_POST 收尾），不改 log_request 本体与管线主链。消费方=swe-eval `mirror_replay.py`（反事实镜像重放，批活守卫）；分片命名与 mirror 用法的实作级补充见联合文档 1015-1/1015-2。`PROXY_REQUEST_ARCHIVE_ENABLED` 默认关，激活=批界窗 conf + reload。

> **认知编排器 / Protocol Layer（P1-P5，`post_governance.py`/`escalate.py`/`decompose.py`/`verification_chain.py`/`idempotency.py`/`protocol_orchestrator.py`/`protocol_types.py`/`signal_types.py`/`contract_registry.py`）**：按三层架构规范（`docs/02-architecture-design/three-layer-architecture-spec-20260830.md`）与认知编排器设计（`cognitive-orchestrator-design-doc-20260830.md`）实现，**当前为 agent_go 集成契约 §3.3 R17/R18/R19 的参考实现冻结版**。Signal 层（ifc_metrics/diagnostics/hbe_probe/memory_stores/ctx_recall）已生产挂载；Protocol 层（decompose/verify/escalate/recall）为编排闭环的待接线部分，其任务工程 / 工具形态 / 是否接入运行时 pipeline 的决策归 agent_go。本仓库不再主动扩展编排器本体，仅吸收可拆解到已有组件的增强（如 PDC-L1/L2、ifc_metrics 信号口径）。


## 三、配置文件明细（原 AGENTS.md §3.3）

### 3.3 配置文件

配置放在 `configs/*.conf`，是 Bash 可 source 的 `KEY="value"` 文件。

| 文件 | 说明 |
|------|------|
| `configs/active.conf` | 指向当前激活配置的符号链接（当前 → `splash-qwen36-35b-optiq.conf`） |
| `configs/kapitsa-qwen36-35b-4bit.conf` | **Kapitsa 0.2.24**（`LLAMA_BACKEND=kapitsa`，manage.sh 原生分支）+ `mlx-community/Qwen3.6-35B-A3B-4bit` + DFlash2 草稿器 + MTPLX MTP head：KFlash 树验证叠加 MTP 级联投机 → 纯 decode **148-150 tok/s**（splash+OptiQ 为 116-132），质量门 91.9%（4bit 档上限；生产 OptiQ 93.5%）。**速度优先档**；模型目录须含 `dflash2/` 与 `mtp.safetensors`（来源 `Youssofal/Qwen3.6-35B-A3B-MTPLX-Optimized-Speed`），见 [`flint-kapitsa-evaluation-20261029.md`](../05-operations-changelog/flint-kapitsa-evaluation-20261029.md) §六 |
| `configs/splash-qwen36-35b-optiq.conf` | **（当前激活）Inco Splash 1.4.0** + `mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit`（MoE 3B 激活，混合精度敏感层 8bit）+ DFlash2 草稿：`--max-context 256K --state-format bf16 --language-only --persistent-cache`，**thinking on + reasoning low + token budget 2048**、`--idle-release 120m`（2026-10-21 45min 墙根因修复，dbcdb47）。实测（409 请求窗口）：decode **132 tok/s** / prefill 1,010 / TTFT p50 2.2s / state 命中 **94.6%** / draft 接受率 0.687 / 内存 ~33GB。conf DESC 写 1.3.2，运行时实际 1.4.0（Homebrew 已升级） |
| `configs/ornith-oq4e.conf` | **已于 2026-10-21 归档** → `configs/archived/ornith-oq4e.conf`，35B oQ4e 权重已删除（Ornith 家族仅保留 9B）。历史：rapid-mlx + Ornith-1.5-35B-A3B-oQ4e-fixed-mtp（20.1GB imatrix 混合精度，Qwen3.5 hybrid，`--hybrid-cache-entries 8`，无投机解码，rapid-mlx 0.14.3 + bf16 KV，稳态 decode ~84 tok/s）；见 runbook §17/§19 |
| `configs/ornith-9b.conf` | rapid-mlx + Ornith-1.5-9B-MLX-4bit（dense hybrid ~5GB，262K 原生上下文，文档处理/轻量任务定位；`--no-mllm` 纯文本——该 MLX 构建无视觉塔；无投机解码：2026-08-29 实测 llama.cpp DFlash 对 9B 档无净收益；`--hybrid-cache-entries 8` 必配，thinking off）。**当前 :8084 下线**（2026-10-20 实测 9B/35B 同驻触发全局换页 55.8→10.6 tok/s，详见 `docs/04-analysis-diagnostics/ornith-9b-memory-pressure-20261020.md`）；haiku 路由 local_model 已置空回落主引擎 |
| ~~`configs/minicpm5-2b.conf`~~ | **已于 2026-10-21 删除**（conf 与权重 `~/models/MiniCPM5-2B-MLX-4bit` 1.3GB 均清除）。删除原因=质量门 v3 裁决 FAIL 44/62=71.0%（overall 线 75%）：**tool_call 0/7**（无工具调用形态，正文替代）为硬伤，tagging 80%/summary 57.1% 亦未达标；速度极快（均值 681ms/条，62 条 42s）但质量不足以承载轻任务档。历史：rapid-mlx + MiniCPM5-2B-MLX-4bit（本地路径，dense 2.5B，1.35GB，131K ctx，唯一可与 35B 共存的 LLM；DSpark 草稿用途因仅 SGLang 支持而未落地）。**轻任务档结论：走 35B/云**（路由 local_model 置空回落主引擎即为终态）；未来具备工具调用形态的小模型候选可直接复用质量门 v3 即插即测 |
| `configs/dflash-35b.conf` | dflash-mlx + Qwen3.6-35B-A3B-4bit + z-lab DFlash（DFlash 投机解码 ~117 tok/s，thinking off，drafter 需 config 补丁，见 docs/05-operations-changelog/dflash-mlx-integration-20260826.md） |
| `configs/qwen3.8-27b-4bit.conf` | rapid-mlx **0.14.3**（`.venv-rapidmlx-0143`，2026-09-21 #12 升级；EXP-8 LCP off 臂前置）+ Qwen3.8-27B 4bit（hybrid，`--hybrid-cache-entries 8` + gpu-mem 0.80 + pflash，prefix cache 增量轮秒级）。**KV 量化已定案 bf16**（2026-09-23：0.14.3 对 hybrid GDN 拒绝量化 fail-closed 实测，`KV_QUANTIZATION=false`） |
| `configs/splash-qwen36-35b-mlx.conf` | **Inco Splash 1.3.2**（1.3.1 起 Splash 包不再加载）+ `mlx-community/Qwen3.6-35B-A3B-4bit` + `incoai/Qwen3.6-35B-A3B-DFlash2`：`--max-context 256K --state-format bf16 --language-only --persistent-cache`；M5 Pro 48GB 实测 冷 prefill 30.4K **2122 tok/s** / decode **123–140**（短 prompt 335）/ 热 TTFT **0.27s** / state 34 MiB·条 / Metal 21.9GB。**日常快档** |
| `configs/splash-qwen38-27b-mlx.conf` | **Inco Splash 1.3.2** + `mlx-community/Qwen3.8-27B-4bit` + `incoai/Qwen3.8-27B-DFlash2`：同套参数；冷 prefill 450–488 tok/s / decode 65–71 / 热 TTFT 0.53–0.56s / state 80.6 MiB·条 / Metal 19.0GB。**能力档（agentic 63.3）**。两档互斥单引擎（switch+restart，约 60–90s），选型依据见 `docs/06-reference-metrics/splash-model-capability-research-20261020.md` |
| ~~`configs/splash-qwen36-35b.conf`~~ | **DEPRECATED（2026-10-20）**：Splash 1.0.2 时代的 `incoai/Qwen3.6-35B-A3B-Splash` 专有包，1.3.1+ 无法加载（#335）。历史数据：散文 117 / 代码 264 tok/s |
| ~~`configs/splash-qwen38-27b.conf`~~ | **DEPRECATED（2026-10-20）**：`incoai/Qwen3.8-27B-Splash` 专有包，1.3.1+ 无法加载。历史数据：代码 78–86 tok/s |
| `configs/ornith-oq4e-256k.conf` | **已于 2026-10-21 归档** → `configs/archived/ornith-oq4e-256k.conf`，35B oQ4e 权重已删除。历史（256K 非交互档）：禁头截断 + bf16 KV（5.0GiB@256K）+ 原文寄存；冷 prefill 7–11min、暖轮 10–30s；KV 量化在 0.14.3 对 hybrid fail-closed（ArraysCache） |
| `configs/embedgemma2.conf` | llama-server + **EmbeddingGemma 2 嵌入引擎**（`ggml-org/embeddinggemma-2-GGUF:Q8_0` 310MB + 可选 `mmproj` 多模态塔）：**辅助引擎**，独立端口 8083，`start-aux/stop-aux` 独立启停、与主后端共存。LLM 无关的纯向量服务（OpenAI `/v1/embeddings`，768d，MRL 支持 512/256/128，纯文本 + 可选 图/音/视频 共用同一向量空间）；`--embeddings --pooling mean -ngl 0`（CPU，实测索引 12.4 块/s、单条 p50 14.6ms）；**必须 `LLAMA_EMBEDDING_PROBE=true`**——`--embeddings` 下 `/v1/chat/completions` 恒 500，就绪探针须改打 `/v1/embeddings`；模型与投影器已落在 `tools/bin/`（`EMBEDGEMMA2_SERVER_BIN` / `EMBEDGEMMA2_MMPROJ=1` 可覆盖，`_EG2_ROOT` 亦可）；**CONFIG_AUTOSTART_AUX 默认随 `manage.sh start` 自动拉起**（2026-10-21） |
| `configs/qwen3.6-27b-4bit.conf` | rapid-mlx + Qwen3.6-27B dense 4bit（tool clearing 关闭） |
| `configs/gemma4-26b.conf` | rapid-mlx + Gemma-4-26B，并发 2，数据处理优化 |
| `configs/deepseek-chat.conf` | 云端 DeepSeek OpenAI 兼容端点（`deepseek-v4-flash`） |
| `configs/qwen25-coder-7b-8bit.conf` | llama-server + `mlx-community/Qwen2.5-Coder-7B-Instruct-8bit`（8.5GB）：**IDE FIM 补全档**，独立 8085（8083 让位 embedgemma2 默认启动）；未常驻，按需启动 |
| `configs/spark-x2.5-llama-server.conf` | llama-server + Spark-X2.5-4B.gguf（本地路径），独立 8082（minicpm5-2b 归档后端口已释放） |
| `configs/models.json` | 模型目录（providers/models/routes），`model_registry.py` 加载、SIGHUP 热重载；**删除后自动合成等价目录**，新增云端模型只需加条目（Phase A+） |
| `configs/secret.local.conf` | **git-ignored**，存放真实 API Key（含分提供商 `ZHIPU_API_KEY`/`KIMI_API_KEY`，由目录 `key_env` 引用） |
| `configs/archived/` | 归档的历史配置（qwen3-8b、rapid-mlx-9b、rapid-mlx-35b、thinkingcap-…-mtp 等） |

配置文件中必须包含元数据：`CONFIG_NAME`、`CONFIG_DESC`、`CONFIG_MEMORY`，供 `./manage.sh list` 读取。


## 四、工具与文档（原 AGENTS.md §3.4）

### 3.4 工具与文档

- `tools/`：benchmark（`bench_*.py`）、分析（`analyze_*.py`）、监控（`monitor.py`、`sysmon.sh`、`exp9_watch.py`——EXP-9 批期值守：驱动存活/turn 新鲜度/信号量审计/sessions.jsonl 轮转防护归档，2026-09-20）、需求追踪（`trace_requirements.py`）、模块提取（`extract_module.py`）、轨迹查询/投影（`trace_query.py` / `trace_replay.py` / `trace_common.py`，Phase B 2026-08-21：requests/metrics/diag/ledger/archive 五流跨查与 sent_view 离线投影、A/B diff，见 `docs/02-architecture-design/logging-trajectory-improvement-design-20260820.md`）、LCP 遥测分析（`lcp_trace_analyzer.py`，2026-09-20：后端日志切命中率/复用分布/失配分型，`--archive` 防轮转，Gate B 判读工具）、**长会话试点**（`pilot_long_session.py`，2026-09-21：append-only/多工具分叉两形态，读引擎 `/metrics` 计数器）、**重跑前置门**（`rerun_gate.py`，2026-09-21：批前 2–3 真实 run 校验驱逐/墙钟/epoch_collapse 四件套，exit 1=STOP 停批）、DFlash drafter 蒸馏（`dflash_distill_mlx.py`，dflash-mlx 0.1.8 API 适配版，2026-08-26 Ornith 实验：acceptance 2.34→2.66 但线上无增益，见 changelog）等。
- **语义检索**（`tools/semantic_search.py`，2026-10-14）：纯标准库 + EmbeddingGemma 2（`./manage.sh start-aux embedgemma2`，端口 8083）的本地向量检索。子命令 `index`（增量：按 mtime+size 跳过未变文件）/ `query` / `oneshot`（小目录不落索引）/ `info`；索引落 `.semantic/<name>/`（`meta.json` + `chunks.jsonl` + `vectors.f32`，git-ignored）。已建：`wiki`（OpenClaw wiki 1788 段 / 272 文件）、`finwiki`（199 段 / 28 文件）、`docs`（本仓库 `docs/` 4385 段 / 162 文件）。定位是给这些知识库做**换说法也能命中**的语义召回，与 MCP `wiki_search`（BM25，精确关键词/路径更快更准）互补而非替代：低词汇重合或跨语言（中↔英）query 用语义，术语/路径检索用 BM25。
- **Jev shadow**（`tools/jev_shadow.py`，2026-10-15）：规则判断点（当前 loop_l1）→ 决策模型的**双臂离线回放**（云 TypeSafe Jev / 本地 RSI-Jev `~/APP/rsi-jev` :8022），级联设计 §6 实测数据源。只读 sessions/archive 日志、fail-open、批期安全；分臂阈值 `ARM_PHI={cloud:0.80, local:0.80}`（2026-10-15 效用裁决：行动阈值宁严勿松，本地 0.50 仅为测量对齐参考）+ 记录 `arm` 字段 + `--summary` 分臂统计；落盘 `logs/diag/jev_shadow{,_local_compare}.jsonl`。实测（n=48/37）：Jev 确认规则正当 0%、choice 双臂一致 94.6%、本地概率平坦（effort 四档救不了）→ 本地臂当前=安全阀非过滤器；详见级联文档 §6。
- **9B 知识/能力质量门**（`tools/knowledge_gate_9b.py` + `test/fixtures/knowledge_gate_9b.json`，2026-10-17 v3）：aux 引擎 :8084 直连的质量门（不碰 :4000/:8081，批期安全）；金标 **62 条 / 7 类**（extraction/tagging/summary/grounding/refine/coding/tool_call），确定性评分（stdlib、无 LLM 裁判）：coding=安全沙箱执行断言（import 白名单 + 危险内建/dunder/反射黑名单→`python3 -I` 子进程，RLIMIT_CPU 6s，harness 符号后置防覆盖）、tool_call=真实 tools 参数解析（含 no_tool 防过度调用与双意图双调用反例）；**unanswerable 防幻觉红线=任一失手整门 FAIL**；exit 0/1 门语义；`--case/--cat/--limit` 过滤，报告落 `logs/knowledge_gate/`。v3 判词 FAIL 54/62=87.1% 仅因 summary 硬字数（结构性短板，硬化 prompt 已证不可修）；新暴露真弱点=接地算术（D09 88≠83）与多标签漏半（B09）；extraction 13/13（含 24.5K 长文埋点）、tool_call 7/7。
- **haiku 档分流对照**（`tools/haiku_tier_compare.py` + `test/fixtures/haiku_tier_cases.json`，2026-10-17）：31 条金标（promptfoo TC1-9 断言直译 + 质量门 tagging/extraction/summary 子集复用）多臂对比 harness——`--arm 9b`(:8084) / `flash`（zhipu-cn anthropic 协议直连）/ `splash`(:8081)，scorer 复用 gate 模块，temp=0 确定性口径。三臂实测：splash 29/31 p50=300ms 双冠（空载）、flash 28/31 6.5s、9B 26/31 3.3s；**裁决=haiku 路由→ornith-9b 已执行**（负载形态论证+三条重审触发条件），见 `docs/06-reference-metrics/ornith-9b-capability-research-20261015.md` §五/§5.1。教训：跨协议对比必须核空响应（anthropic 协议 thinking 块计入 max_tokens，1024 会在长题耗尽致空 text）。
- `docs/`：按 7 类组织（需求产品、架构设计、实验测试、分析诊断、运维变更、参考指标、项目看板 `07-project-board/`）。入口 [`docs/README.md`](../../docs/README.md)；根级关键文档含 `llama-defender-integration-requirements.md`（agent_go 集成契约）、`DEFECT-LIST.md`、`requirement-matrix.md`。
- `test/`：自动化测试，见下节。
- `logs/`：运行时日志、测试日志、metrics、快照（git-ignored）。
- `assets/chat-templates/`：Qwen chat template 修复模板。

---

## 五、结构化 Admin API 端点（原 AGENTS.md §4.3 表）

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/status` | 结构化健康与就绪状态：`proxy`、`backend`、`active_profile`、`state`、`ready`、`route_config`（R11 路由配置摘要） |
| GET | `/api/route/policies` | 脱敏路由策略 + 模型目录（R9）：providers（key 只回 `key_set` 布尔）、models、preferences、defaults、`catalog_hash`（agent_go 漂移检测） |
| GET | `/api/watchdog` | watchdog 状态：`enabled`、`running`、`pid`、`restart_count_1h`、`last_failure_reason` |
| GET | `/api/queue` | 请求优先级队列状态（Phase 1 默认关闭）：`enabled`、`workers`、`waiting`、`by_bucket`、`oldest_wait_ms` |
| GET | `/api/profiles` | 可用模型配置列表：`name`、`desc`、`memory_gb`、`active` |
| GET | `/metrics[?n=N]` | 最近请求指标（JSON） |
| GET | `/metrics/history[?session=K]` | 历史指标（JSON，R16 可选会话过滤） |
| GET | `/api/sessions` | 活跃诊断会话列表（R14 发现端点）：`key`/`key_source`/`turns`/`last_seen` |
| GET | `/api/session/<key>/ledger` | 会话台账（R14）：actions（dup/last_dup_turn）/ dup_queries / materials；404 未知、410 已驱逐 |
| GET | `/api/session/<key>/archive?view=sent` | sent_view 档案（R15）：默认索引模式，`include_payload=true` 拉正文；canonical 视图 Phase 1 前 501 |
| GET | `/api/session/<key>/metrics` | 会话诊断聚合（R16）：hit_ratio 分位、epoch/非 epoch 延迟分档、per-turn 时序 |
| GET | `/api/session/<key>/signals` | 会话信号快照（R17，契约 v1 已冻结）：SignalSnapshot——IFC 五指标 + H_BE 熵/趋势，全 Optional fail-open；404/410 对齐 R14/R15 |
| GET | `/api/session/<key>/hbe` | R17 附属：H_BE shadow 探针原始记录透传（schema v2 含 `completion_budget` 等；支持 `?since=`） |
| GET | `/api/backend/props` / `/api/backend/slots` | llama-server 原生端点只读反代；后端不支持时 501 结构化降级 |
| POST | `/admin/route/force-local` / `force-cloud` | 会话级路由覆盖 |
| POST | `/admin/reload` | HTTP 热重载（R12，等效 `manage.sh reload`，含模型目录重载） |
| POST | `/admin/inject` | 会话注入原语（ADR-013 T1）：按 session_key 排队注入块，下一请求以 `<{tag}>` 块追加 user 尾部（once；engine-on 进 canonical 跨轮存活，engine-off 单轮）；错误码 400/401/404/413/429。详见 [设计文档](../../docs/02-architecture-design/admin-inject-primitive-design-20260909.md) |
| GET | `/status` | 人类可读 HTML 状态页 |

- `/api/status` 在 `state` 为 `healthy` 或 `starting` 时返回 200，否则 503（同 JSON body）。`state` 枚举：`healthy | starting | backend_down | proxy_down | model_drift | down`。
- `ready` 表示后端模型已加载、可接受推理请求（`starting` → `ready=false`），agent_go 的 `wait_ready` 以此字段为准。
- 路由响应头（R8 契约名）：每个路由响应带 `X-Proxy-Route-Target`（`cloud|local|local_forced`）、`X-Proxy-Route-Actual-Model`、`X-Proxy-Route-Reason`、`X-Proxy-Route-Cost`（预估费用，本地为 0）；OpenAI 协议非流式响应体另带 `proxy_route` 字段（实际 usage 计费）。单请求路由覆盖用请求头 `X-Proxy-Route-To: local|cloud`（无会话粘性）。
- 多提供商分发（Phase B）：按模型解析目录 provider 凭证/并发锁；路由 `fallback_chain` 跨商降级（跳过冷却中/无 key 的提供商）；分商熔断互不影响；按模型目录价格计费 + 全局/分商预算双上限。**熔断可观测（L-96，2026-10-13）**：cooldown 进入（WARN 含连续失败数）/到期/配额重置过点均有独立日志打点——此前 cooldown 态回落被误标 "Cloud API key not configured"（联合文档 1013-1 (a)）；`/api/status` `route_config.provider_cooldown` 暴露每 provider `{active, remaining_s, source, cooldown_seconds}`（source ∈ circuit_breaker/quota_reset）；cooldown 回落 route_reason 分流为 `cloud_provider_cooldown`，legacy `cloud_no_api_key` 仅指 key 缺失。
- Anthropic 协议分发（Phase D）：provider 可声明 `protocol: anthropic`（双端点双 key：`anthropic_base_url` + `anthropic_key_env`）——该 provider 的模型走 `{anthropic_base_url}/v1/messages`（Anthropic 协议，如 Z.ai Coding Plan 订阅），SSE 原样透传、非流式直返 + `proxy_route` 归因；OpenAI 协议客户端自动跳过 anthropic 候选并沿链降级。当前 zhipu=protocol anthropic（订阅路径，glm 边际成本 0）。
- **R8-R12 已全部交付（2026-08-15）**：R8 归因头四件套 `X-Proxy-Route-*`（含 Cost 与 local_forced）+ OpenAI 协议非流式 `proxy_route` 体字段；R9 `GET /api/route/policies`（脱敏目录 + `catalog_hash`）；R10 `/v1/models` 能力元数据（`real_model`/`thinking_*`/`json_compliance`/`context_chars`/`price`/`direct_capable`）；R11 `/api/status` `route_config` 段；R12 `POST /admin/reload`。CLI 侧配套 `./manage.sh models` / `models-validate`。
- **R13-R16 诊断数据面已交付（2026-08-19，设计文档 [`docs/02-architecture-design/diagnostics-dataplane-design-20260819.md`](../../docs/02-architecture-design/diagnostics-dataplane-design-20260819.md)）**：R13 诊断归因双通道——非流式 HTTP 头 `X-Proxy-Diag-Request-Id` / `X-Proxy-Feedback-Injected`（csv）/ `X-Proxy-Prompt-Processed-N`（仅后端返回 timings 时，`hit_ratio = 1 − prompt_n/prompt_tokens`），流式经 SSE 注释行尾注 `: x-proxy-diag {...}`（message_stop / [DONE] 之前，规范保证被所有解析器忽略）；现有 7 处合成内容注入全部可计量。R14 台账 + `/api/sessions` 发现。R15 sent_view 每轮落盘（「模型实际所见」唯一权威）。R16 `logs/diag/sessions.jsonl` per-turn 深度记录 + `/api/status` `ctx_config` 段（bench 口径机读源）+ `lifecycle_events.jsonl` 激活（`canonical_mismatch`）。总开关 `PROXY_DIAG_ENABLED`。



## 六、源码构建 llama-server（原 CLAUDE.md 独有段）

Brew 的 `llama-server` 版本落后于 GitHub。需要 MTP 支持时从源码构建：

```bash
git clone https://github.com/ggml-org/llama.cpp /tmp/llama.cpp
cmake /tmp/llama.cpp -B /tmp/llama.cpp/build -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=OFF
cmake --build /tmp/llama.cpp/build --config Release -j --target llama-server
# Binary: /tmp/llama.cpp/build/bin/llama-server
```

设 `LLAMA_SERVER_BIN` 环境变量或更新 `tools/bench_mtp.py` 的 `LLAMA_SERVER_BIN` 常量以使用自建二进制。
