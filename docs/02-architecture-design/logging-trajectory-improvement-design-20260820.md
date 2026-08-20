# 代理层日志体系评估与改进设计（对标 DSH 轨迹视图四层模型）

> 日期：2026-08-20
> 作者视角：产品经理（现状审计 + 差距分析 + 分阶段改进路线）
> 参考基准：DSH（DeepSeek Harness）Trajectory 视图——事件溯源（append-only 事实源）+ 结构化轨迹树（动作投影）+ 语义层 + 系统性能层的"单一事实源，多层投影"架构
> 关联设计：[`diagnostics-dataplane-design-20260819.md`](diagnostics-dataplane-design-20260819.md)（R13-R16 诊断数据面）、[`llama-defender-context-engineering-design.md`](llama-defender-context-engineering-design.md)（上下文工程，epoch 字段消费方）
> 结论一句话：**R13-R16 已搭出正确的"最小轨迹系统"，缺的不是架构重构，而是三块地基（台账持久化 / 请求流关联字段 / 主日志轮转）和一把消费扳手（离线投影与 diff 工具）。**

---

## 1. 现状盘点：已有 7 类日志/档案

| # | 数据面 | 内容 | 持久化 / 治理 | 对应 DSH 层级 | 成熟度 |
|---|---|---|---|---|---|
| 1 | `logs/anthropic_proxy.log` | 人类可读主日志（REQ_SUMMARY、管线决策、错误） | **无轮转，实测 510MB 持续增长** | 事件层（文本态） | ★★☆☆☆ |
| 2 | `logs/proxy_requests.jsonl` | 请求级摘要（method/path/model/chars/status/duration） | **无轮转；无 session_id/request_id** | 事件层 | ★★☆☆☆ |
| 3 | `logs/proxy_metrics.jsonl` | 24 管线阶段深度指标（lifecycle/router/compress/truncate/loop/…） | 10MB 轮转 ✓ | 事件层（结构化） | ★★★★☆ |
| 4 | `logs/diag/sessions.jsonl`（R16） | per-turn 深度记录（token 事实、hit_ratio、注入清单、canonical_mismatch），request_id 与 #3 关联 | 10MB 轮转 ✓ | 事件层（结构化） | ★★★★☆ |
| 5 | `logs/diag/archive/<sid>.jsonl`（R15） | sent_view：每轮实际发给后端的完整 payload（"模型实际所见"唯一权威） | 单轮 400KB 截断 + 200MB 总量 + TTL ✓（77 会话在档） | 事件层（事实源） | ★★★★☆ |
| 6 | R14 台账（`session_ledger.LEDGER`） | 动作轨迹：tool_use/result 配对 → action 序列 + dup 计数 + 材料清单 | **仅内存**：TTL 180min / 64 会话 FIFO，重启即失 | 动作层 | ★★☆☆☆ |
| 7 | `/metrics/history`、`/api/session/<key>/metrics`、admin `/status` | 分位/时序聚合、epoch 字段（Phase 1 前为 null） | 从 #3/#4 派生 | 性能层 | ★★★★☆ |

**架构方向判定：已对标 DSH 的正确形态。**
- sent_view ≈ DSH append-only 事实源（"模型可见 = 已记录"合规口径已具备）；
- 台账 ≈ 动作层投影（dup/last_dup_turn 即循环死锁检测的输入信号）；
- loop/blocker/reread/lifecycle/canonical_mismatch + `feedback_injected` ≈ 语义层信号源；
- **语义评分按契约归 agent_go**（"采集归代理、理解归 agent_go"），边界比 DSH 更清晰——这是优点，不是差距。

## 2. 差距清单（全部有本周实证）

| # | 问题 | 实证 | 影响 | 严重度 |
|---|---|---|---|---|
| G1 | **动作层不持久** | 2026-08-19 qwen3.8 编程能力评估时，SWE 批跑（08-17/18）的动作轨迹只能对文本日志 grep 重演；台账早已 TTL 驱逐 | 批跑复盘成本高；click-06 类死循环（1205 请求/4h19m）无法事后量化 dup 演化 | **P0** |
| G2 | **requests.jsonl 缺关联字段** | 08-12 性能分析 191 个 500 错误按会话归因全部失败（session_id 全为 `?`） | 最基础的请求流无法与会话流/诊断流 join，事件层"断链" | **P0** |
| G3 | **主日志无轮转** | `anthropic_proxy.log` 实测 509MB 且持续增长 | 磁盘风险；grep 一次分钟级，故障定位慢 | **P0** |
| G4 | **7 类 JSONL 无统一事件契约** | schema 各异、轮转策略不一（#2/#3/#4 三种口径）；消费方各自解析 | 脆弱：新增字段/文件时 tools 与 agent_go 两侧静默破裂 | P1 |
| G5 | **有档案无消费工具** | sent_view 已常态落盘，但无离线投影器；click-06 修复前后对比（pflash 96K 验证）只能手搓脚本 | 数据在攒、分析靠手；档案价值无法兑现 | **P1** |
| G6 | 导出层空白 | langfuse compose 为可选项、从未接线；无 OTLP/Loki 通道 | 长期存储与跨系统分析缺位 | P2（暂缓） |

## 3. 改进路线（按 ROI 排序）

### Phase A — 补地基（P0，估 1-2 天）

| 项 | 内容 | 验收 |
|---|---|---|
| A1 | `proxy_requests.jsonl` 补 `session_id`/`request_id` 两字段 + 10MB 轮转（对齐 #3 策略） | 新记录两字段非空；`logs/` 下出现 `.1` 轮转文件 |
| A2 | `anthropic_proxy.log` 按大小轮转（50MB × 3 份，`RotatingLogHandler` 或等价） | 主日志 ≤50MB；grep P95 < 5s |
| A3 | **台账持久化**：`LedgerStore.record_request` 增量 append 到 `logs/diag/ledger/<sid>.jsonl`（复用 archive 的 MB 上限/TTL 治理模式）；`/api/session/<key>/ledger` 优先读档案、内存做缓存 | 重启后 ledger 端点仍可查询历史会话；磁盘有界。**契约验收（2026-08-20 补，见 §3.1）**：跨重启 5 分钟内 R14 端点对既有会话可查（内存驱逐 ≠ 档案删除），agent_go 轮级看门狗不因代理重启/SIGHUP 失忆 |

### Phase B — 投影工具（P1，估 2-3 天）

| 项 | 内容 | 直接消费者 |
|---|---|---|
| B1 | `tools/trace_query.py`：跨流统一查询 CLI——按 session/turn/status/request_id join `proxy_metrics` + `diag/sessions` + `diag/ledger`，输出 JSON/表格 | 故障定位（G2 修复后天然可用）、批跑复盘 |
| B2 | `tools/trace_replay.py`：sent_view → 每轮时间线 + 动作树 + 注入标记的离线投影（JSON/HTML），支持 **A/B diff**（两会话或同会话两时段） | click-06/rich-11 重跑验证、TS-4 压缩副作用分析、上下文工程 epoch 轮分析（epoch 字段已预留，台账持久化后自然可查） |

### Phase C — 按需（P2，明确暂缓）

- C1 导出通道：文件 → 外部 shipper（vector/alloy）→ Loki/Tempo。**红线：不引入进程内 SDK**（代理核心 stdlib-only 约束，AGENTS.md §6.2）；langfuse 仅在需要会话级 UI 时再接线。
- C2 语义评分（目标完成度/意图漂移）：**不进代理**——按集成契约归 agent_go，代理只保信号采集完整。

### 3.1 agent_go 消费方需求核对（2026-08-20 补）

集成契约（[`llama-defender-integration-requirements.md`](../llama-defender-integration-requirements.md) §3.2 + [`llama-defender-context-engineering-design.md`](../llama-defender-context-engineering-design.md) §9）已把日志数据列为 agent_go 的采集/分析/决策三级输入，本节核对本改进与其对应关系：

| agent_go 需求 | 契约位置 | 数据源 | 本改进对应 | 状态 |
|---|---|---|---|---|
| metering 归因（成本/路由/缓存命中率） | R13（已交付） | R8/R13 头 + SSE 尾注，`api.py:156` 解析 → metering.jsonl | 无需改动（采集链路已通） | ✅ 不依赖 |
| 形态学复盘（兔子洞） | R15 | `archive?view=sent` | 档案已在常态落盘；B2 投影为代理侧自用 | ✅ 不依赖 |
| 时间线复盘 / canonical_mismatch 监控 / A/B 出数 | R16 | `/api/session/<key>/metrics`、`/metrics/history?session=` | 无需改动 | ✅ 不依赖 |
| **轮级无进展看门狗**（dup ≥3 → rabbit_hole 事件，kill 或标注） | 上下文工程 §9 P1-4（agent_go 侧待做）：`subtask.py:131/364` 轮询 `GET /api/session/<key>/ledger` | **R14 台账** | **A3**——台账现仅内存（TTL 180min/64 会话 FIFO），代理重启/SIGHUP 后进行中任务的 ledger 端点返回空 → **看门狗失忆** | ⚠️ **A3 是其隐性依赖** |

**结论**：
1. **A3 从"内部便利"升级为契约相关**：agent_go 只消费 HTTP 端点（不读日志文件），R14 端点跨重启可查是看门狗可靠性的前提——验收标准已回写 A3 行（跨重启 5 分钟内可查）。
2. B1/B2 工具（trace_query/trace_replay）为代理侧开发/运维自用，**不进集成契约**；G4 统一事件契约的价值在代理内部与 tools，不需为 agent_go 扩面。
3. 看门狗消费模式下 `/api/session/<key>/ledger` 的轮询频率预计为轮级（每次模型请求一轮、秒到分钟级间隔）——A3 落地时读路径应以内存优先、档案兜底，避免轮询打满档案 IO（写入仍在请求热路径之外）。

### 明确不做（对照 DSH 的取舍）

| DSH 能力 | 本仓库决策 | 理由 |
|---|---|---|
| Resume / Fork / Replay 全量重放 | 不做请求级重放；仅离线投影（B2） | 代理是无状态转发层，会话状态归客户端；重放属 harness（swe-eval/agent_go）职责 |
| 事件溯源重构（状态=事件投影） | 不做 | 现有"请求级快照 + per-turn 记录"已满足审计口径，重构收益不抵风险 |
| 进程内 OTLP/Langfuse SDK | 不做 | stdlib-only 红线；文件 + 外部 shipper 等价且解耦 |

## 4. 验证与回灌

- Phase A 落地后：重跑 click-06/rich-11（qwen3.8 配置），用 B2 diff 修复前后动作轨迹 → 回灌 `BENCHMARK.md`；
- 台账持久化验收后，R14 端点 `410 session_evicted` 语义保留（内存驱逐 ≠ 档案删除）；
- 本文档状态行随各 Phase 推进更新，完成后在 `docs/README.md` 索引登记。

---

> 状态：**Phase A 已落地（2026-08-20）**——A1 requests.jsonl 补 session_id/request_id + 10MB 轮转；A2 主日志 copytruncate 轮转（PROXY_LOG_ROTATE_MB=50 × KEEP=3）+ wrapper 单一写入方（stdout→/dev/null，stderr 仍落文件）；A3 台账增量落盘 `logs/diag/ledger/`（PROXY_DIAG_LEDGER_ENABLED/MAX_MB，R14 端点内存优先档案兜底，FIFO/TTL 驱逐后仍 200）。新增配置已注册 CONFIG_REGISTRY + _RELOAD_SPEC；单测 1121 全绿，签名快照重生成。Phase B（trace_query/trace_replay）待排期。
> 风险：A3 需注意 ledger 档案写入在请求热路径上——沿用 archive 的"写失败静默 + 有界"模式，不新增阻塞点；agent_go 看门狗轮询 R14 端点（轮级频率）依赖读路径内存优先
