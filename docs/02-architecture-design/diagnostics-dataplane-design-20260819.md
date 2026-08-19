# llama-defender 诊断数据面设计（R13-R16：缓存 / 会话 / 行为复盘观测）

> 状态：设计 v1.0（2026-08-19，基于需求评估与代码现状探索定稿）
> 需求来源：[llama-defender-integration-requirements.md §3.2](../llama-defender-integration-requirements.md)（R13-R16）
> 上游设计：[llama-defender-context-engineering-design.md §10](../llama-defender-context-engineering-design.md)（数据面缺口与分层）
> 目标：为上下文工程改造（append-only + epoch 压缩）提供**观测先行**的数据面：缓存命中率 / 延迟分档 / 会话台账 / 压缩后行为复盘四域的结构化数据源，全部经「响应头 + 端点 + jsonl」供 agent_go / 批跑 harness 消费。
> 边界：本设计**不改动任何请求处理行为**——诊断数据面是纯旁路观测层；上下文工程本体（canonical history / epoch 状态机 / 写入期压缩）不在本文档范围。

---

## 0. 背景与动机（必要性论证）

### 0.1 问题链条

上下文工程设计文档确认的三个症状，当前可观测性全部缺失：

| 症状 | 实证 | 当前可观测性 |
|------|------|-------------|
| ~3min/轮（66 轮 / 100K 上下文） | 批跑实测 | ❌ 只有代理侧 duration，不知时间花在 prefill 还是生成 |
| 41 任务批跑预期 7-12 天 | 成本模型推演 | ❌ token 数为字符 ÷4 估算（`admin_server.py:2695`），无真实 usage |
| 搜索兔子洞 45min 无自愈（748f534） | 形态学观察 | ❌ 重复行为无结构化记录，靠人读转录 |

根因假设「每轮语义改写 → KV 缓存全量失效」的验证公式 `n_tokens/n_past ≈ 1.0`，**今天代理拿不到这个数**：流式解析虽预留 `timings.prompt_n` 分支（`anthropic_proxy.py:1148`），解析后既不落盘也不回传。假设坐实（Phase 0）与治疗验收（Phase 1）都卡在此处。

### 0.2 必要性结论

> R13-R16 本身不治任何病——治病的是上下文工程本体。R13-R16 解决「看不见」：改造的假设无法验证、效果无法度量、副作用无法归因。必要性由「测不准就改不对」支撑；其中两项（R14、R13 注入标记）另有独立于改造的价值。

| 层级 | 需求项 | 判断依据 |
|------|--------|---------|
| **① 独立价值**（不改造也值得做） | R14 台账；R13 的 `Feedback-Injected` | 兔子洞轮级检测与 bench 口径去污染和压缩策略无关，是存量问题的存量解 |
| **② 改造先决**（不做则主线受阻） | timings 采集；R16 落盘；R13 的 `Prompt-Processed-N` | Phase 0 无法启动、Phase 1 无法验收（增量占比 >90% / 延迟分档 P90）、A/B 无法出数 |
| **③ 改造配套**（落地后价值放大，可条件推迟） | R15 archive（尤其 canonical_view）；`Epoch-Count` 头；props/slots 透传 | R15 依附形态学复盘是否进 A/B；props/slots 在 `--max-num-seqs 1` 单会话部署下优先级最低 |

**反证（不做会怎样）**：Phase 0 假设无法坐实 → 改造没有前提；Phase 1 效果无法量化 → 上线决策靠感觉；A/B 四臂中臂 1 无数据、干预臂无法标注 → 结论不可信；改造后兔子洞若复发，无法区分「压缩丢证据」还是「模型本身如此」；**6 处既有合成消息注入继续隐形 → bench 口径长期带毒**。

**成本对照**：D-Phase 0/1/2 合计约 2 天，全部为薄层改动（透传 + 端点 + jsonl），风险与 R8-R12 同档；对照上下文本体 2.5-3.5 天改造，其可验证性 100% 依赖这 2 天。

---

## 1. 需求评估：六个实现级缺口（G1-G6）

对 R13-R16 原始需求文本的评估结论：方向正确（§10.2② 的忠实转写），但按代码现状存在六个实现级缺口，本方案逐一补齐。

**G1 流式时序矛盾（R13 硬伤）**：现有流式路径在读到任何后端 chunk 之前就发出全部 HTTP 头（`anthropic_proxy.py:1049-1065`）——R8 Cost 头只能做「预估」的原因（`pipeline.py:2146` docstring 自述）。而 `Prompt-Processed-N` 要等 prefill 完成后的终块 `timings` 才知道（`anthropic_proxy.py:1147-1155`），**流式响应中物理上不可能作为 HTTP 头携带**。「复用 R8 头模式」只对非流式成立 → 需求文档已回写修正。

**G2 依赖错位**：`Epoch-Count` / `Feedback-Injected` 依赖上下文工程 Phase 1/2（epoch 状态机、合成负反馈）——均未实现（全仓无 epoch 命中）。头字段必须定义为**可选、功能未启用时不出现**（fail-open，集成契约 §4）。但 `Feedback-Injected` 不必等 Phase 2：**现有管线已有 6+ 处合成消息注入**从未被计量，先行落地有即时价值。

**G3 台账/档案与 Phase 1 解耦（排期修正）**：上游设计 §10.3 把 R14/R15 排在 Phase 2。但代理每轮都收到客户端全量历史（无状态 API），dup/last_dup_turn/材料清单可**每请求派生**，不需要 canonical history 先行。修正排期：R13-R16 全部先行落地，为 A/B 四臂的**臂 1（现状基线）**提供同一套观测面——否则臂 1 无数据、四臂对比口径不一致。

**G4 后端能力矩阵缺失**：`timings`/`/props`/`/slots` 是 llama-server 原生；当前 active 是 rapid-mlx（`configs/active.conf:13`，prefix cache 开、`--max-num-seqs 1`，无 slot 概念）。`LLAMA_BACKEND` 只在启动轴存在（`manage.sh:883-895`），运行时代理不感知（唯一区分方式是 MODEL_NAME 子串，`lifecycle.py:186`）。需要能力探测 + 降级链。有利条件：流式解析已预留 timings 分支；`tools/bench_mtp.py:322-325` 有从响应取 timings 的先例——rapid-mlx 是否真返回 timings 属 D-Phase 0 首项实测。

**G5 会话发现缺失**：R14/R15 是 `/api/session/<key>/...` 形态，但 agent_go 没有枚举 key 的端点；且现有 session key 是 `X-Claude-Code-Session-Id[:8]` 截断值（`anthropic_proxy.py:528-533`），无头时回退 `md5(ip:ua:date)` 会**跨会话合并**。需补 `GET /api/sessions` + key 契约文档化。

**G6 R16 与现有 metrics 关系未定义**：`proxy_metrics.jsonl`（schema `_METRICS_V1_FIELDS`，`proxy_state.py:326-333`）是 per-request 全端点记录（含错误/队列路径），token 为字符估算；R16 要的是成功推理轮的**真实 token 深度记录**。两者应并行 + `request_id` 关联。另发现可顺带兑现的资产：`lifecycle_events.jsonl` 路径已定义但**无 writer 死路径**（`proxy_state.py:422`）——正是集成契约 R7（P2 可选事件通知）+ epoch 事件的落点。

### 1.1 可复用资产盘点

| 资产 | 锚点 | 价值 |
|------|------|------|
| R8 头暂存-发送机制 | `pipeline.py:2141-2165` → `_route_response_headers`；发送点 `anthropic_proxy.py:1474-1477`（非流式）/ `1056-1059`（流式）/ `1330-1333`（OpenAI 透传） | R13 非流式直接复用 |
| 流式 timings/usage 解析 | `anthropic_proxy.py:1147-1155` | `processed_n` 来源，需扩展 ms 字段并落盘 |
| session key + sticky | `anthropic_proxy.py:528-533`；`pipeline.py:611,653-697` | key 契约 |
| `_SESSION_REQUEST_COUNT` | `lifecycle.py:90-96` | turn 计数直接复用 |
| `_DEDUP_CACHE` / `_SESSION_TOOL_FREQ` | `loop_detection.py:66-69` / `pipeline.py:1995-1996` | 去重/频率雏形 |
| `common_prefix_ratio` 算子 | `pipeline.py:1804-1835` | 台账增量 vs 全量重建判据（→ `canonical_mismatch`） |
| 合成消息注入点 ×6 | loop L1-L3 `loop_detection.py:288-397`、blocker `pipeline.py:997-1001`、reread `pipeline.py:1466-1477`、route notice `pipeline.py:867-870`、high-drop `pipeline.py:1662-1675`、截断摘要 `truncation.py:1084-1094` | `Feedback-Injected` 的 instrumentation 对象 |
| metrics 写入链 | `anthropic_proxy.py:799-822` → `_finalize_metrics`（`admin_server.py:2656-2715`）→ `log_metrics`（`proxy_logging.py:47-67`，10MB 轮转） | R16 jsonl 写入器仿此模式 |
| `/session?sid=` 端点先例 | `anthropic_proxy.py:498`，`admin_server.py:1456` | 带参数端点挂载参照 |
| 离线缓存分析 | `tools/cache_analyzer.py`、`tools/analyze_backend_perf.py`（rapid-mlx `cache_fetch`/`tokens_to_prefill` 正则） | D-Phase 0 兜底（不进代理，守上游 §10.4 原则） |
| mock backend | `test/integration/mock_backend.py:43-77` | 需加 `timings` 字段支撑集成测试 |

---

## 2. 设计原则

| # | 原则 | 来源 |
|---|------|------|
| P0 | 诊断数据采集责任全归代理，agent_go 只消费结构化接口；不解析 llama-server 文本日志；不把客户端转录当压缩后行为依据 | 上游 §10.4 原文 |
| P1 | **时序诚实**：值产生于何时就在何时携带——决策前已知的走 HTTP 头（路由、注入标记），响应完成后才知道的走 SSE 尾注（prefill 数）；永不发占位假值 | 补 G1 |
| P2 | **先观测后改造**：诊断面独立于上下文工程 Phase 1 交付，四臂 A/B 共用同一观测面，臂 1 也有数据 | 补 G3 |
| P3 | **后端无关降级**：字段缺失 ≠ 错误；`null` 语义（「此来源不可用」）+ 消费方 fail-open | 补 G4 |
| P4 | 代理核心 stdlib-only、全状态有界（TTL + FIFO + 磁盘上限）、线程安全（仿 `_metrics_lock` 模式） | 仓库约定 |

---

## 3. 总体架构

### 3.1 组件与数据流

```text
Request ──► RequestParser(0) ──► [旁路] LedgerScanner ──► ... 21 stages ... ──► BackendDispatcher(21)
              │                    │  扫描客户端原始历史           │ dispatch 前抓 openai_body
              │                    ▼                             ▼
              │              session_ledger.py              sent_view 落盘
              │              (LedgerStore: actions/         (logs/diag/archive/<sid>.jsonl)
              │               dup/materials/turn)                    │
              ▼                                                    ▼
Response ◄── anthropic_proxy.py 响应路径 ◄──── DiagRecorder ◄── 后端终块 timings/usage
   │             │                        （processed_n / hit_ratio / 注入标记聚合）
   │  非流式: HTTP 头                        │
   │  流式:   SSE 尾注行                     ├──► logs/diag/sessions.jsonl    (R16)
   ▼                                         └──► logs/lifecycle_events.jsonl (激活死路径, 兼 R7)
admin_server.py builders: /api/sessions, /api/session/<key>/{ledger,archive,metrics}, /api/backend/{props,slots}
```

新增 2 个模块（`session_ledger.py`、`diagnostics.py`），修改 5 个现有文件，**不新建 PipelineStage 类**——台账扫描与诊断记录分别是「RequestParser 的旁路读者」和「响应路径的旁路记录者」，不是消息变换；做成 stage 会被迫参与 ctx 传递与管线编号，违背「阶段=可测试的消息处理单元」语义。旁路模块由单元测试直接覆盖。

### 3.2 关键决策（D1-D10）

**D1 流式携带通道：SSE 注释行尾注（解 G1）**
- Anthropic 流：在 `message_stop` 事件之前（message_delta/usage 发射点 `anthropic_proxy.py:1299-1304` 之后）插入：
  `: x-proxy-diag {"prompt_processed_n":412,"prompt_sent_n":98347,"hit_ratio":0.9958,"epoch_count":3,"feedback_injected":["loop_l1"]}`
- OpenAI 透传流：在 `data: [DONE]` 之前插入同格式注释行（`anthropic_proxy.py:1323-1354`）。
- **选注释行而非自定义 `event:` 的理由**：SSE 规范规定注释行（`:` 开头）被所有解析器忽略（claude CLI / Anthropic SDK / OpenAI SDK 均安全）；自定义 event 类型依赖各 SDK 的未知事件容忍度，风险面大。
- metering 消费：agent_go 本来就在读流，`api.py:156` 的 R8 解析扩展为「头 + 注释行」双来源。

**D2 `Prompt-Processed-N` 语义与来源链**
- 语义：本轮后端**实算 prefill token 数**（缓存未命中部分）。
- 来源优先级：`timings.prompt_n`（响应终块）→ 无 timings 时该字段缺省（不发假值，P1 原则）。`prompt_sent_n` 取 `usage.prompt_tokens`。
- `hit_ratio = 1 − prompt_n / prompt_tokens`（上游 §10.2① 公式）；任一分母缺失 → `null`。
- 能力探测：进程级单次探测 + 缓存（首个含 `timings` 的响应置 `backend_timings_supported=true`）。

**D3 台账派生：增量 + 前缀校验（解 G3，对齐上游 §4.7）**
- 每请求在 stage 0 之后（messages 尚为客户端原始 Anthropic 格式、任何压缩/注入之前）扫描：`assistant.tool_use` 与 `user.tool_result` 配对 → action 记录。
- 规范化 key：`tool_name + 参数签名`——search 类取 query（小写、压空白）、read 类取路径、fetch 类取 URL、其余取首参数字符串；`md5(key)[:12]`。
- 增量策略：LedgerStore 记录已收编消息数 `n`；新请求若前 `n` 条与前缀一致（复用 `common_prefix_ratio` 判据）→ 只处理增量；不一致（客户端侧裁剪/微压缩）→ 全量重建并记 `canonical_mismatch` 事件（上游 §4.7.4 监控项，先行落地）。
- dup/`last_dup_turn`：确定性计算（规范化 hash 对比），无 LLM 参与（上游 §4.2 规则 2）。
- 材料清单（保守启发式，Phase 2 增强）：`write/edit` 的 `file_path`、bash 输出中 `saved|created|written` 模式匹配的路径。

**D4 turn 定义**：turn = 代理所见该会话的**请求序号**，复用 `_SESSION_REQUEST_COUNT`（`lifecycle.py:90-96`）。一次请求内的多工具调用同 turn。写入 R14 响应与 agent_go 对接文档（消解轮级看门狗对齐歧义）。

**D5 session key 契约（解 G5）**：沿用 `X-Claude-Code-Session-Id[:8]`；两条规则：① 批跑 harness/agent_go **必须显式发送该头**（否则 md5(ip:ua:date) 回退 key 按天合并所有无头会话，台账污染）；② `manage.sh route-force-*` 的 8 字符截断不对称问题（manage.sh 传参不截断，内部 key 截断）一并文档化。新增 `GET /api/sessions` 提供发现，条目带 `key_source`（header/fallback）。

**D6 `Feedback-Injected` 先行语义（解 G2 的可先行半）**：定义为「**本请求代理向 prompt 注入了任何合成内容**」，值为 kind 数组。现有 6 处注入点全部纳入：`loop_l1/loop_l2/loop_l3/text_loop/blocker/reread_hard/route_notice/high_drop_notice/truncation_summary`；Phase 2 负反馈落地后加 `negative_feedback`；复述块加 `recitation`。实现：新 helper `mark_injected(msg, kind)` 统一打标（写入 msg 的内部 sidecar dict，不进后端 payload），DiagRecorder 聚合。**这是对上游 §4.6「开启时必须在转录中标注」的直接兑现。**
`Epoch-Count`：Phase 1 前不出现；落地后由 epoch 状态机供给（字段名现在定死，消费方代码可先写好）。

**D7 R15 三视图（视角正确性核心）**：

| 视图 | 内容 | 产生时机 |
|------|------|---------|
| `client_view` | 客户端发来的原始 messages | stage 0 后快照（只存引用计数/长度，正文不重复落盘） |
| `sent_view` | **实际发给后端的最终 payload**（含全部注入块，逐块带 kind 标注） | BackendDispatcher dispatch 前（`pipeline.py:2529` 构造后） |
| `canonical_view` | 代理派生历史 | Phase 1 后点亮，此前 501 明示未启用 |

`sent_view` 是「模型实际所见」的唯一权威（上游 §10.1 视角正确性），形态学复盘以此为准。落盘 `logs/diag/archive/<sid>.jsonl`（每轮 append `{turn, ts, payload, injections[]}`），受 `PROXY_DIAG_ARCHIVE_MAX_MB` 总量上限 + 会话 TTL 清理。

**D8 R16 数据模型（解 G6）**：新文件 `logs/diag/sessions.jsonl`，per-turn 深度记录，与 `proxy_metrics.jsonl` 并行、`request_id` 关联（metrics 侧追加 `request_id` 字段，向后兼容）。schema v1：

```json
{"schema_version": 1, "ts": "...", "request_id": "...", "session_key": "a1b2c3d4",
 "turn": 42, "route_target": "local", "actual_model": "mlx-community/Qwen3.8-27B-4bit",
 "backend": {"type": "local", "name": "rapid-mlx", "timings_supported": true},
 "prompt_sent_tokens": 98347, "prompt_processed_tokens": 412, "hit_ratio": 0.9958,
 "generation_tokens": 1024, "ttft_ms": 850.2, "prompt_eval_ms": 610.0, "gen_ms": 15400.0,
 "epoch_count": 3, "epoch_triggered": false, "is_epoch_turn": false,
 "feedback_injected": ["loop_l1"], "canonical_mismatch": false,
 "compression": {"mode": "smart", "ratio": 0.42}}
```

`is_epoch_turn` 是延迟 P90 **分档统计**（上游 §6.1：非 epoch 轮 <15s / epoch 轮 <60s）的前提。聚合端点按 session 出时序 + 分位数。

**D9 后端能力矩阵（解 G4）**：

| 后端 | timings | /props /slots | hit_ratio | 处理 |
|------|---------|---------------|-----------|------|
| llama-server | ✅（终块） | ✅ | ✅ | 全功能 |
| rapid-mlx | ❓ D-Phase 0 实测 | ❌ | 实测定 | 有 timings→全功能；无→字段 null + 离线 `tools/cache_analyzer.py` 兜底（不进代理） |
| cloud | ❌（仅 usage） | ❌ | null（云端缓存语义不归本代理） | 只记 token/成本 |

`LLAMA_BACKEND` 运行时可见性：manage.sh source conf 后已 export，proxy 侧 `os.environ.get("LLAMA_BACKEND", "unknown")` 读入 `proxy_state` 并进 `/api/status`（顺带修掉「运行时唯一区分方式是 MODEL_NAME 子串」的脆弱现状）。`/api/backend/props|slots` = 代理对 `{LLAMA_BASE 去掉 /v1}/props|slots` 的只读反向代理（rapid-mlx 下返回 501 + `{"supported": false}`，agent_go fail-open）。

**D10 bench 口径标注数据源（补充需求）**：上游 §9 P0-1 要求批次 manifest 记录压缩模式/注入开关/S/K——目前 agent_go 只能读文件。本方案在 `/api/status` 增 `ctx_config` 段（仿 R11 `route_config` 先例）：`{"compression_mode", "feedback_injection_enabled", "epoch_S", "window_K", "diag_enabled"}`。S/K 在 Phase 1 前为 null。已回写需求文档记入 R16。

---

## 4. 接口契约

### 4.1 R13：诊断归因（双通道）

**非流式（两协议）— HTTP 头**，复用 `_route_response_headers` 暂存机制（`pipeline.py:2141` 模式）：

| 头 | 类型 | 何时出现 |
|----|------|---------|
| `X-Proxy-Prompt-Processed-N` | int | 后端返回 timings 时 |
| `X-Proxy-Epoch-Count` | int | Phase 1 落地后 |
| `X-Proxy-Feedback-Injected` | csv（如 `loop_l1,blocker`） | 本请求有注入时 |
| `X-Proxy-Diag-Request-Id` | str | 总是（与 R16 jsonl / metrics 关联） |

**流式（两协议）— SSE 尾注注释行**（D1 格式），随最后一个数据事件发出。Anthropic 协议云端透传路径（`_handle_anthropic_stream_passthrough`）同样在流尾部追加（该路径代理只透传，需在转发循环末尾补一行 write）。

**OpenAI 协议非流式**：响应体 `proxy_diag` 字段（与 `proxy_route` 并列，`pipeline.py:2557-2580` 同点注入）。

### 4.2 R14：`GET /api/session/<key>/ledger`

```json
{"session_key": "a1b2c3d4", "turns_seen": 66, "updated_at": "...", "canonical_mismatch_count": 1,
 "actions": [{"turn": 42, "tool": "search", "target": "github ansible pull 80376",
   "target_hash": "e3f0...", "result_chars": 654, "dup": 4, "first_turn": 42, "last_dup_turn": 48,
   "handle": {"type": "query", "value": "..."}}],
 "dup_queries": [{"target": "...", "count": 8, "first_turn": 21, "last_turn": 48}],
 "materials": [{"path": "work/upstream_pkg_mgr.py", "turn": 43, "via": "fetch"}]}
```

支持 `?limit_turns=N`。未启用/未知 key → 404 JSON；key 曾存在已被驱逐 → 410 + `evicted_at`。

### 4.3 R15：`GET /api/session/<key>/archive`

`?view=sent|client|canonical&turn=N&limit=50&offset=0&include_payload=true|false`。默认 `view=sent&include_payload=false`（只回索引：每轮 turn/ts/chars/injections/token 数），拉正文才带 payload（单轮可达 100KB+）。`canonical` 视图 Phase 1 前返回 501 + `{"enabled": false}`。与 `logs/snapshots/` 的关系：snapshots 是**失败时**的 before/after（保留不动），archive 是**常态每轮** sent_view。

### 4.4 R16：会话维度指标

- 落盘：`logs/diag/sessions.jsonl`（D8 schema，10MB 轮转 + 保留 1 备份，仿 `proxy_logging.py:52-63`）。
- `GET /api/session/<key>/metrics`：内存聚合（LedgerStore 侧滚动窗口）：

```json
{"turns": 66, "hit_ratio_series": ["..."], "hit_ratio_p50": 0.99, "hit_ratio_p90": 0.95,
 "ttft_p50_ms": 800, "ttft_p90_ms": 1500,
 "latency_by_kind": {"normal_turn": {"p90_ms": 14000}, "epoch_turn": {"p90_ms": 55000}},
 "epoch_count": 3, "injection_counts": {"loop_l1": 4}, "canonical_mismatch_count": 1}
```

- `GET /metrics/history?session=<key>`：现有端点加过滤参数（读侧小改 `anthropic_proxy.py:1599-1699`）。

### 4.5 辅助端点

| 端点 | 用途 |
|------|------|
| `GET /api/sessions` | 活跃会话列表：`[{key, key_source, turns, last_seen, route, hit_ratio_p90, evict_in_min}]`（解 G5） |
| `GET /api/backend/props` / `GET /api/backend/slots` | llama-server 原生端点只读反代；不支持时 501 结构化降级（D9） |
| `GET /api/status` 扩展 | `backend.name` + `ctx_config` 段（D9/D10） |

### 4.6 错误与兼容语义

统一遵守集成契约 §4：非 2xx + JSON `{"error", "state"}`；agent_go 对缺失端点/字段 fail-open；所有新字段**只增不改名**；`api_version` 维持 R9 版本位递增策略（新增端点 → `"2"`）。

---

## 5. 代码落点（实现清单）

| 文件 | 改动 | 锚点 |
|------|------|------|
| `session_ledger.py`（新） | LedgerStore + ArchiveStore：扫描/规范化/增量 diff/dup 计算/材料启发式/TTL+FIFO 驱逐/jsonl 落盘/线程锁 | stdlib only |
| `diagnostics.py`（新） | DiagRecorder：timings 能力探测、processed_n/hit_ratio 计算、SSE 尾注序列化、sessions.jsonl 写入、lifecycle_events 激活、`mark_injected()` | stdlib only |
| `proxy_state.py` | 新常量 + `_SESSION_LEDGER`（有界）+ `__all__` + `_RELOAD_SPEC` 追加（`proxy_state.py:834-958` 格式） | |
| `proxy_config.py` | CONFIG_REGISTRY 新条目（§6 表） | `proxy_config.py:46+` |
| `pipeline.py` | ① stage 0 后调 LedgerScanner（客户端原始视图，`pipeline.py:379-386` 附近）；② dispatch 前抓 `ctx.openai_body` 落 sent_view（`pipeline.py:2529` 后）；③ 6 处注入点包 `mark_injected`；④ 非流式 usage 预读处（`pipeline.py:2560-2562`）扩 timings 解析 + 头注入 | |
| `anthropic_proxy.py` | ① 流式循环 timings 扩展解析（`anthropic_proxy.py:1147-1155` 加 `prompt_ms/predicted_ms`）+ 尾注发射（`1299-1304` 后）；② OpenAI 透传流补 usage/timings 捕获（`1343-1354` 现在完全不解析）+ `[DONE]` 前尾注；③ 非流式 `_respond_json` 发诊断头（`1474-1477` 同点）；④ `do_GET` 加 `elif self.path.startswith("/api/session/")` 与 `/api/sessions`、`/api/backend/*` 分支（`anthropic_proxy.py:413-525` elif 链，参照 `:498` 写法）；⑤ mc 加 `request_id` | |
| `admin_server.py` | 新 builder：`_build_sessions_list` / `_build_session_ledger` / `_build_session_archive` / `_build_session_metrics` / `_build_backend_props`，加入 `__all__`（`admin_server.py:2722-2756`）；`_api_status` 加 `ctx_config` 与 `backend.name` | |
| `test/integration/mock_backend.py` | 响应加 `timings`（`MOCK_TIMINGS_PROMPT_N` 等环境变量控制） | `mock_backend.py:43-77` |

---

## 6. 配置项（CONFIG_REGISTRY 新增）

| 键 | 默认 | 类型/作用域 | 说明 |
|----|------|------------|------|
| `PROXY_DIAG_ENABLED` | `true` | bool / reloadable | 诊断数据面总开关 |
| `PROXY_DIAG_SSE_TAIL` | `true` | bool / reloadable | 流式 SSE 尾注（关=纯头模式） |
| `PROXY_DIAG_SESSION_TTL_MIN` | `180` | int / reloadable | 台账/档案会话 TTL |
| `PROXY_DIAG_SESSION_MAX` | `64` | int / reloadable | 内存台账会话数上限（FIFO） |
| `PROXY_DIAG_ARCHIVE_ENABLED` | `true` | bool / reloadable | sent_view 常态落盘 |
| `PROXY_DIAG_ARCHIVE_MAX_MB` | `200` | int / reloadable | archive 总量上限（超限删最老会话目录） |
| `PROXY_DIAG_JSONL_PATH` | `logs/diag/sessions.jsonl` | str / module | R16 落盘路径 |
| `PROXY_DIAG_TIMINGS_SOURCE` | `auto` | str / reloadable | `auto`（响应体探测）\| `off` |

manage.sh 无需手工同步（`write_defaults_sh` 自动生成默认值、`config-lint` 自动校验）。

---

## 7. 实施计划（对上游 §10.3 的修正排期）

| 阶段 | 内容 | 验收门禁 |
|------|------|---------|
| **D-Phase 0**（0.5 天） | timings 采集+探测、R13 头+尾注最小集（Processed-N / Feedback-Injected / Request-Id）、R16 最小落盘、`/api/status` 的 backend.name | 双协议流式/非流式均能拿到 Processed-N（后端支持时）；6 处既有注入全部可计量；**坐实或证伪 rapid-mlx timings 可用性**（为上游 Phase 0 闸门供数） |
| **D-Phase 1**（1 天） | R14 LedgerStore + ledger 端点 + `/api/sessions`；R15 sent_view 落盘 + archive 端点；`canonical_mismatch` 事件 | 台账 dup 计数与人工核对一致；60 轮会话 archive 可完整回放 sent_view；TTL/上限驱逐生效 |
| **D-Phase 2**（0.5 天） | session metrics 聚合端点、`/metrics/history?session=`、`/api/backend/props\|slots` 反代、lifecycle_events 激活（R7 兑现）、`ctx_config` 段 | hit_ratio 时序 + epoch/非 epoch 分档出数（配合上游 Phase 1 验收） |

依赖关系：**三个 D-Phase 全部不依赖上下文工程 Phase 1/2**；反向依赖只有一处——上游 Phase 1 验收门禁 1/2（增量占比 >90%、延迟分档）消费 D-Phase 0/2 的数据。即：先诊断面、后改造，四臂 A/B 口径从第一天统一。

---

## 8. 测试计划

- **unit**（`test/unit/test_session_ledger.py` / `test_diagnostics.py` 新增，约 30 用例）：规范化 hash 稳定性、dup/last_dup_turn、前缀 diff 增量 vs 全量重建、材料启发式、hit_ratio 数学（含分母 0/null）、TTL/FIFO/MB 上限驱逐、SSE 尾注序列化、`mark_injected` 聚合、schema 向后兼容（旧字段补 null）。
- **integration**：mock_backend 注入 `timings`（流式终块 + 非流式）→ 验证头/尾注/jsonl 三通道一致；ledger/archive/metrics 端点 200/404/410/501 矩阵；`PROXY_DIAG_ENABLED=false` 时零开销零字段。
- **signature/snapshot**：`_finalize_metrics`、`_api_status` 等签名变更需重跑 `--signature` + `--snapshot`；预提交钩子自动覆盖 `--unit`。
- 涉及 `anthropic_proxy.py`/`pipeline.py` → 提交前 `bash test/run_tests.sh --all`（仓库 §5.3 硬性要求）。

---

## 9. 风险与对策

| 风险 | 对策 |
|------|------|
| SSE 尾注被某客户端拒绝（理论外） | 注释行是规范保证的忽略语义；`PROXY_DIAG_SSE_TAIL` 一键关；promptfoo 回归验证 claude CLI 实际行为 |
| rapid-mlx 无 timings → R13/R16 半瘫 | D-Phase 0 首项即实测；缺失时字段 null（P3）+ 离线 `tools/cache_analyzer.py` 兜底（不进代理）；向上游提 issue 路径保留 |
| 无头客户端 session key 按天合并 → 台账污染 | 文档化「harness 必须发 `X-Claude-Code-Session-Id`」；`/api/sessions` 暴露 `key_source` 便于发现 |
| archive 磁盘增长（60 轮 × 100KB × 多会话） | `include_payload=false` 默认索引模式 + `ARCHIVE_MAX_MB` LRU 删最老 + logs/ 已 git-ignore |
| LedgerStore 内存 | 会话数上限 + 每会话 action 数软上限（>2000 触发上游 §4.2 规则 3 降级聚合：同目标合并保计数） |
| 双协议 × 流式/非流式 × local/cloud × anthropic-protocol 云端组合爆炸 | DiagRecorder 单一实现、四条响应路径只做「取值+发射」薄封装；集成测试取 6 个代表组合 |
| 存量 bug 顺带修 | ① metrics 读取相对路径 vs 写入绝对路径不一致（`anthropic_proxy.py:1495` vs `proxy_state.py:652`）→ 统一 `_SCRIPT_DIR`；② 流式 `last_chunk` 残留变量（`anthropic_proxy.py:1120/1130`）→ 清理；③ `route-force-*` 8 字符截断不对称 → 文档化（不动行为） |

---

## 10. agent_go 对接清单

| # | 事项 | 落地点 |
|---|------|--------|
| 1 | metering 解析扩展：`api.py:156` 的 R8 头解析模式扩展为「HTTP 头 + SSE 注释行 `: x-proxy-diag`」双来源 | agent_go `api.py` |
| 2 | metering.jsonl 增字段：`prompt_processed_n / hit_ratio / epoch_count / feedback_injected[] / diag_request_id` | agent_go metering |
| 3 | 轮级看门狗：轮询 `GET /api/session/<key>/ledger`，dup ≥3 → rabbit_hole 事件（上游 §9 P1-4） | agent_go `subtask.py` |
| 4 | 批跑 harness **显式发送 `X-Claude-Code-Session-Id`**（key 契约，见 D5） | agent_go harness |
| 5 | bench manifest 口径标注数据源：读 `/api/status` 的 `ctx_config` 段（上游 §9 P0-1） | agent_go `batch_governance.py` |
| 6 | 形态学复盘以 `GET /api/session/<key>/archive?view=sent` 为准，弃用客户端转录 | agent_go `eval.py` |

---

## 11. 需求文档回写记录（2026-08-19 已执行）

对 `docs/llama-defender-integration-requirements.md` §3.2 的 5 处修订：
1. R13 修正「复用 R8 头模式」表述 → 非流式 HTTP 头 + 流式 SSE 尾注 `: x-proxy-diag {...}` 双通道。
2. 补 `X-Proxy-Diag-Request-Id` 头与 `request_id` 关联字段（metering ↔ R16 jsonl 对齐键）。
3. 补 `GET /api/sessions` 发现端点与 turn 计数定义。
4. `/api/status` 的 `ctx_config` 段记入 R16（bench 口径标注数据源）。
5. agent_go 侧对接注意（双来源解析、harness 显式会话头）补入边界节。
