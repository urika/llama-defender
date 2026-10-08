# llama-defender 上下文工程增强需求（v1 需求稿，2026-10-08）

> 状态：**需求稿 v1**（待评审；R-CM-* 编号为本文档私域，勿与集成契约 `llama-defender-integration-requirements.md` 的 R1-R19 混用）
> **交付更新（2026-10-08）**：**P0 已落地** —— R-CM-1 跨会话记忆（`memory_store.py`，stdlib sqlite3+FTS5，`MemoryInjectStage` 0.61，`/api/memory`·`/admin/memory` 端点）与 R-CM-2 自动交接（`handoff.py` 规则化生成，`HandoffInjectStage` 0.62，`/api/handoff`·`/admin/handoff`，`./manage.sh handoff`）。均默认关（`PROXY_MEMORY_ENABLED`/`PROXY_HANDOFF_ENABLED=false`）、reloadable、append-only、fail-open。LLM 富化生成与 TTL 驱逐为后续增强。
> **另（2026-10-08）**：**R-CM-6 输出预算自适应已交付**——由既有 L-73（2026-10-07）实现（长载荷工具不降压 + 工具轮次下限兜底），本次补齐 `PROXY_DYNAMIC_MAX_TOKENS_FLOOR`/`_WRITE_NO_REDUCE` 的 reloadable 接线与文档/矩阵对齐。P1/P2 剩余（R-CM-3/4/5/7）待排期。
> 关联：`ctx-recall-auto-closed-loop-20260905.md`（ctx_recall 自闭环）、`../02-architecture-design/progressive-disclosure-context-serving-design-20260829.md`（PDC 渐进披露）、`../02-architecture-design/context-architecture-evolution-20260829.md`、集成契约 `llama-defender-integration-requirements.md`（R13-R19 诊断/信号数据面）
> 目标项目：`/Users/jinsongwang/APP/llama.cpp`（llama-defender）
> 背景：2026-10-08 复盘「小窗口（≤256K）时代 vibe coding 如何实现」并调研开源项目后确认——本仓已具备**取数（ctx_recall）/ 压缩（fifo·epoch·BM25）/ 缓存（append-only + prefix cache）/ 注入（admin/inject）**四条主干，但相对业界成熟方案存在三块结构性空白与两项可强化点。本需求把这些空白转化为可交付契约。

---

## 0. 结论速览

**目标**：在**不引入第三方运行时依赖**（代理核心 stdlib-only）、不破坏 append-only 前缀纪律、fail-open 的前提下，补齐跨会话记忆与交接、门控式压缩、符号级检索三条能力线，并强化预取与输出预算。

**优先级**：R-CM-1（跨会话记忆）、R-CM-2（自动交接）为 **P0**；R-CM-3（门控折叠）、R-CM-4（符号检索）、R-CM-6（输出预算）、R-CM-7（召回度量）为 **P1**；R-CM-5（预取推送）为 **P2**。

**非目标**：不改客户端（Claude Code 等）配置；不替换后端引擎；不实现向量嵌入/训练；不承诺 f2p 提升（先度量后承诺）；代理核心不引入 numpy/tree-sitter/torch 等依赖。

---

## 1. 能力地图（现状 vs 目标）

| 能力线 | 现状（2026-10-08） | 缺口 | 本文档需求 |
|---|---|---|---|
| 取数（检索） | `ctx_recall`：L1 子串 + L2 FTS5 + archive 全文恢复；manifest 可寻址索引 | 无**符号级**检索（定义/引用）；召回真命中率低（历史 ~8%） | R-CM-4、R-CM-7 |
| 压缩 | `truncation.py`（fifo/smart）+ `context_engine` epoch 折叠 + `content_compressor` BM25 | epoch 触发为**纯 token 阈值**，无"闭单元/非卡住"结构判据 | R-CM-3 |
| 缓存 | append-only + `PROXY_CTX_VIEW_STABLE_ENABLED` + LCP snapdown；实测命中 99% | 位置无关 KV 复用（跨 RAG）未探索 | （非目标，登记观察） |
| 提示注入 | `AdminInjectStage`（Stage 0.6，ADR-013）、frozen 系统段 | 无**自动**基于会话状态的头/尾注入 | R-CM-1、R-CM-2 |
| 记忆 | `session_ledger`（会话内）、`diagnostics`/archive（落盘、TTL 驱逐） | **无跨会话持久记忆** | R-CM-1 |
| 交接 | 无 | **无自动 handoff 摘要**（会话重启需人工复述） | R-CM-2 |
| 预取 | 设计 D3 列 `PROXY_PD_PREFETCH_ENABLED`，未实装 | 无"需求信号→预取推送" | R-CM-5 |
| 输出预算 | `PROXY_DYNAMIC_MAX_TOKENS_SATURATION=8192` 全局 | 写文件轮次被 8192 截断（文件写一半） | R-CM-6 |
| 度量 | `sessions.jsonl` per-turn、`ifc_metrics` 五指标、L-61 复发计数 | 无"召回采纳率"闭环指标 | R-CM-7 |

---

## 2. 需求条目

> 通用约束（所有条目适用）：**append-only**（禁改历史前缀，仅头部 frozen 区或尾部追加）；**fail-open**（任一环失败 → 主链路照常转发，仅记日志）；**stdlib-only**（外部能力走 subprocess / sidecar MCP / HTTP，不得 import 进 `anthropic_proxy.py`·`pipeline.py`·`proxy_state.py`）；所有开关 **SIGHUP reloadable** 且默认 **off**（灰度）；新增配置必须在 `proxy_config.py` 的 `CONFIG_REGISTRY` 注册并在 `manage.sh` 设默认。

### R-CM-1 跨会话持久记忆（P0）

**目标**：代理侧维护一个**项目级**持久记忆库，跨会话/跨进程留存"事实 / 决策 / 回归 / 偏好"，供后续会话注入。

- **存储**：`logs/memory/`（stdlib `sqlite3` + FTS5；按项目根或 session_key 前缀分域）。人可读导出为 markdown（对齐 `basic-memory` 的 disk-source-of-truth 模型）。
- **写入**：会话结束（`SessionEnd` 语义，可由空闲 TTL 或显式端点触发）从 `session_ledger` + `sessions.jsonl` + archive **蒸馏**结构化条目；蒸馏调用后端模型（可复用现有 backend 分发），无模型时退化为规则抽取（fail-open）。
- **读取/注入**：新会话首个请求，按相关性（BM25/关键词，stdlib）挑 top-k 注入**头部 frozen 区或尾部**，受 `PROXY_MEMORY_INJECT_MAX_CHARS` 封顶；注入登记 `diagnostics.record_injection("memory")`。
- **生命周期**：每条带 `provenance`（来源 session/turn）、`confidence`、`status`（active/superseded）、`verify_after`；支持衰减与去重（对齐 `memory-mcp` / `claude-mem`）。
- **端点**：`GET /api/memory`（列表/检索）、`POST /admin/memory`（写）、`DELETE /admin/memory/<id>`（删）。
- **配置**：`PROXY_MEMORY_ENABLED=false`、`PROXY_MEMORY_INJECT_MAX_CHARS=2000`、`PROXY_MEMORY_TOP_K=5`、`PROXY_MEMORY_TTL_DAYS=90`、`PROXY_MEMORY_DB_PATH=logs/memory/memory.db`。

**验收**：① 会话 A 记录决策 → 新进程冷启会话 B 首请求头部可见该决策（sent_view 档案取证）；② 关开关时零行为变化；③ DB 损坏时 fail-open；④ 注入不破坏前缀（首轮即可，稳定前缀由 frozen 区保证）。

**借鉴**：`mem0`、`getzep/graphiti`、`letta/memgpt`、`thedotmack/claude-mem`、`demostenex/ai-memory`、`yucx-go/agent-knowledge`（BM25+RRF 零向量路线尤其契合 stdlib 约束）。

### R-CM-2 自动交接摘要（P0）

**目标**：会话边界或上下文将满时，自动生成结构化 **handoff** 并以 append-only 方式落盘 + 下轮注入，消除"重启需人工复述"。

- **触发**：①会话空闲超阈值；②窗口将满（epoch 折叠前）；③`./manage.sh handoff` / `POST /admin/handoff` 显式触发。三触发均幂等（内容寻址，参考 `idempotency.py`）。
- **生成**：把待交接区（不常驻的折叠段 + 台账动作序列）喂给后端模型，产出结构化字段：`{goal, done, decisions, open_questions, next_steps, files_touched, anchors}`；无模型时规则化降级。
- **落盘**：`.agents/checkpoints/<session>.md` 或 `logs/diag/handoff/<sid>.jsonl`（明文、可跨 agent 携带，对齐 `agent-handoff` / `session-continuity`）。
- **注入**：下一会话作为 frozen 头部或尾部 `[System: HANDOFF]` 块，长度封顶。
- **配置**：`PROXY_HANDOFF_ENABLED=false`、`PROXY_HANDOFF_TRIGGER_CHARS=...`、`PROXY_HANDOFF_MAX_CHARS=3000`、`PROXY_HANDOFF_DIR=logs/diag/handoff`。

**验收**：① 长会话（≥50 轮）触发后，新会话首请求含 handoff 块且字段完整；② 与 epoch 折叠不重复冲突（折叠在压缩区，handoff 在尾部/头部）；③ 幂等——同会话重复触发不产生多份。

### R-CM-3 门控式 epoch 折叠（P1）——✅ 已交付（2026-10-08）

**目标**：epoch 触发从"纯 token 阈值"升级为 **token 阈值 × 结构判据**，避免在"推导中途/卡住"时折叠丢失关键中间结果（对齐 SelfCompact 的 rubric）。

- **判据**（全部满足才折叠）：①**闭单元**——本轮最后一步为已完成的 tool 结果而非悬空调用；②**非卡住**——`loop_detection` 未命中且 `ifc_metrics.reread_pressure` 低于阈值；③**有进展**——较上次折叠有新 material（台账 material 清单增量非空）。
- **卡住时**：不折叠，改走诊断注入/换路线提示（复用 `[System: ...]` 尾消息通道）。
- **配置**：`PROXY_CTX_EPOCH_RUBRIC_ENABLED=false`、`PROXY_CTX_EPOCH_RUBRIC_STUCK_MAX=...`。
- **验收**：① 构造"中途折叠"反例，开启 rubric 后不再触发；② 正常闭单元轮次折叠行为与旧版一致或更优；③ 与 `_RELOAD_SPEC`/`ctx_config` 对齐。

**借鉴**：SelfCompact（arXiv 2606.23525）+ `tianjianl/selfcompact`。

### R-CM-4 符号级检索增强（P1）

**目标**：给 `ctx_recall` 增加**符号索引层（L1.5）**——命中符号名（函数/类/变量）时，返回定义点 + 引用点，替代纯文本子串。

- **实现**：外部 `ctags`（universal-ctags）或 `tree-sitter` **sidecar**（subprocess/HTTP），代理侧仅消费结构化输出；无外部工具时自动退化为现有 L1/L2（fail-open）。禁止在核心 import tree-sitter。
- **检索语义**：`query` 命中符号 → `format_recall_result` 追加"定义位置 + top-N 引用位置"（含行号与短上下文）。
- **配置**：`PROXY_RECALL_SYMBOL_ENABLED=false`、`PROXY_RECALL_SYMBOL_TOOL=ctags`、`PROXY_RECALL_SYMBOL_CMD=`、`PROXY_RECALL_SYMBOL_MAX_REFS=8`。
- **验收**：① 对含定义/引用的目标，召回转出符号定位段；② 无 ctags 环境 fail-open 不报错；③ 召回结果仍 ≤ `MICRO_TURN_RESULT_MAX_CHARS` 护栏（必要时分页锚点）。

**借鉴**：`Aider-AI/aider`（tree-sitter + PageRank repo map）、`oraios/serena`（LSP `find_symbol`/`find_referencing_symbols`）、`DeusData/codebase-memory-mcp`、`cocoindex-io/cocoindex`。

### R-CM-5 预取 / 推送披露（P2）

**目标**：实装 `PROXY_PD_PREFETCH_ENABLED`——当需求信号（重读目标、高熵游走、显式问题）出现时，用 BM25 从 manifest 预取相关折叠单元，**主动推送**到尾部，减少一轮往返。

- **信号**：台账 `dup_queries`（已有）、`ifc_metrics` 游走熵（已有）、AutoRecallStage 检测点（已有）。
- **执行**：`content_compressor` BM25 打分 → manifest 取正文（`recover_full_content`）→ `[System: PREFETCH ...]` 尾部注入；与 R-CM-2 的 auto-recall 合并去重（每轮 ≤1 条）。
- **配置**：`PROXY_PD_PREFETCH_ENABLED=false`、`PROXY_PD_PREFETCH_TOP_K=3`、`PROXY_PD_PREFETCH_MAX_CHARS=2000`。
- **验收**：① 触发后目标重读率下降（对照 auto-rec单一臂）；② 不重复注入；③ 与 append-only 前缀纪律兼容。

### R-CM-6 输出预算自适应（P1）——✅ 已交付（L-73）

**目标**：消除"写文件轮次被 `saturation` 档 8192 输出上限截断"（`cli_41fedbd5` 实证：大文件在 ~23813 字符处截断、残缺内容仍落盘报成功）。

**实现（L-73，2026-10-07；本次补 reloadable 接线）**：
- **判定**：本请求工具集含**长载荷工具**（`Write`/`Edit`/`MultiEdit`/`NotebookEdit`/`Bash`，见 `pipeline.py:_LONG_PAYLOAD_TOOLS`）→ **完全不降压**（保留客户端原始 `max_tokens`，低内存也不降）。
- **其余带工具轮次**：下限不低于 `PROXY_DYNAMIC_MAX_TOKENS_FLOOR`（且不超过客户端原始请求）——防工具参数被切断。
- **纯文本轮次**：保留原降压语义（截断文本可见、可重试，不造成静默落盘损坏）。
- **配置**（reloadable）：`PROXY_DYNAMIC_MAX_TOKENS_WRITE_NO_REDUCE=true`、`PROXY_DYNAMIC_MAX_TOKENS_FLOOR=32768`（0=关兜底）。

**验收**：`test/unit/test_lifecycle.py::TestComputeDynamicMaxTokens` 7 例覆盖（no-reduce / 下限兜底 / Bash 入列 / 关闭豁免 / FLOOR=0 / 不超客户端请求）。

### R-CM-7 召回采纳率度量（P1，支撑 R-CM-1/2/4/5）——✅ 已交付（2026-10-08）

**目标**：建立"注入 → 是否被采纳"的闭环指标，作为上述所有能力的疗效口径。

- **指标**：`recall_hit_rate`、`recall_lead_lag`（注入后该目标 `last_dup_turn` 是否推进 = 未再重读 → 采纳）、`handoff_lead_lag`、`memory_reuse_count`。
- **落点**：`sessions.jsonl` per-turn 字段 + `/api/session/<key>/metrics` 聚合 + `X-Proxy-Diag-*`（可选）。
- **验收**：① 指标可从端点读出且与 sent_view 对账；② 复用 L-61 复发-交错计数，不新增重型统计。

---

## 3. 配置项汇总（新增，均 reloadable + 默认 off）

| 配置 | 默认 | 作用域 | 归属需求 |
|---|---|---|---|
| `PROXY_MEMORY_ENABLED` | false | local/cloud | R-CM-1 |
| `PROXY_MEMORY_INJECT_MAX_CHARS` | 2000 | 全局 | R-CM-1 |
| `PROXY_MEMORY_TOP_K` | 5 | 全局 | R-CM-1 |
| `PROXY_MEMORY_TTL_DAYS` | 90 | 全局 | R-CM-1 |
| `PROXY_MEMORY_DB_PATH` | logs/memory/memory.db | 全局 | R-CM-1 |
| `PROXY_HANDOFF_ENABLED` | false | 全局 | R-CM-2 |
| `PROXY_HANDOFF_TRIGGER_CHARS` | （待定） | 全局 | R-CM-2 |
| `PROXY_HANDOFF_MAX_CHARS` | 3000 | 全局 | R-CM-2 |
| `PROXY_HANDOFF_DIR` | logs/diag/handoff | 全局 | R-CM-2 |
| `PROXY_CTX_EPOCH_RUBRIC_ENABLED` | false | local | R-CM-3 |
| `PROXY_RECALL_SYMBOL_ENABLED` | false | local | R-CM-4 |
| `PROXY_RECALL_SYMBOL_TOOL` | ctags | local | R-CM-4 |
| `PROXY_RECALL_SYMBOL_MAX_REFS` | 8 | local | R-CM-4 |
| `PROXY_PD_PREFETCH_ENABLED` | false | local | R-CM-5 |
| `PROXY_PD_PREFETCH_TOP_K` | 3 | local | R-CM-5 |
| `PROXY_DYNAMIC_MAX_TOKENS_WRITE_NO_REDUCE` | true | 全局 | R-CM-6 |
| `PROXY_DYNAMIC_MAX_TOKENS_FLOOR` | 32768 | 全局 | R-CM-6 |

> 全部须登记进 `proxy_config.py` 的 `CONFIG_REGISTRY`（含类型/作用域/文档），并在 `manage.sh` 设默认；`./manage.sh config-lint` 须通过。

---

## 4. 约束与红线

1. **stdlib-only**：R-CM-4 的 tree-sitter/LSP 只能走 subprocess 或 sidecar MCP；核心三文件零新增依赖。
2. **append-only / 前缀纪律**：注入只允许 frozen 头部或尾部追加；不得回写历史（DEF-308 教训）。
3. **fail-open**：任一新增环节失败静默降级，主链路与错误率不受影响。
4. **默认关 + 灰度**：所有新能力 default off；开启走 `active.conf`。
5. **批期纪律**：实验批跑期间不得切换引擎/重启代理（红线 1），本需求的能力以 reloadable 开关形式灰度，避免重启。
6. **测试**：新增 pipeline stage 须继承 `PipelineStage` 薄封装 + `test/unit/test_pipeline_stages.py` 补测；改签名/行为须过 `--signature` + `--snapshot`。

---

## 5. 度量（DoD 与判读）

| 需求 | 主指标 | 判定 |
|---|---|---|
| R-CM-1 | 跨会话记忆复用次数、注入后重复解释率 | >0 且不劣化错误率 |
| R-CM-2 | handoff 生成成功率、重启后首轮对齐度 | ≥95% 生成成功 |
| R-CM-3 | "中途折叠"反例消除、正常折叠不退化 | 反例 0 触发 |
| R-CM-4 | 召回真命中率（≥200 chars 结果占比） | 相对现状（~8%）提升 |
| R-CM-5 | 目标重读率下降 | 对照臂显著 |
| R-CM-6 | 写轮次输出截断率 | 归零 |
| R-CM-7 | 召回采纳率（lead-lag） | 可读且与 sent_view 对账 |

---

## 6. 实施顺序（建议）

1. **R-CM-7**（度量先行，无它则后面无法判读）
2. **R-CM-6**（最小改动、直接消除已实证 DEFECT）
3. **R-CM-1 → R-CM-2**（复用 ledger/archive 原料，P0）
4. **R-CM-3**（小改，收益大）
5. **R-CM-4**（需 sidecar 接入）
6. **R-CM-5**（与 R-CM-2 合流，最后）

---

## 7. 待办 / 登记

- [x] R-CM-* 已登记入 `docs/requirements.yaml`（新 domain `R-CM`，R-CM-1~7；R-CM-1/2 有 impl+test 锚点，R-CM-3~7 声明待排期）并在 `tools/trace_requirements.py` 支持 `R-CM-<n>` 编号（ID_PATTERN + 排序键）；`--trace` 30 requirements、锚点全解析、无孤儿标签。
- [ ] R-CM-2 与 `admin-inject-primitive`（ADR-013 T1）通道仲裁：handoff 用 frozen 头部 vs 尾部，需在架构文档定稿。
- [ ] R-CM-4 的 sidecar（ctags/serena/codebase-memory-mcp）选型与部署形态（本机 vs 客户端 MCP）待定。
- [ ] 位置无关 KV 复用（LMCache/CacheBlend）作为观察项登记，暂不立项。

---

> 本文档为需求稿，供评审；编号 R-CM-* 与集成契约 R1-R19 隔离，避免与 agent_go 契约冲突。
