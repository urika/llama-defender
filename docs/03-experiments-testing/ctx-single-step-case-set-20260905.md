<!-- ctx-case 案例集规格：每案例五字段（测试环节/环节作用/正确行为/输入/期望输出） -->

# ctx-case 案例集规格 v1（TC01-TC15）

- 日期：2026-09-05
- 状态：评审中（对应框架设计见 [ctx-single-step-case-debug-design-20260905.md](ctx-single-step-case-debug-design-20260905.md)，实现为 `tools/ctx_cases.json` + `tools/ctx_case_runner.py`）
- 阅读方式：每案例六字段——**测试环节**（流程位置）、**环节作用**（为什么存在）、**正确行为**（契约，断言的依据）、**输入**（字面报文+env）、**期望输出**（断言点，字面）、**现状预判**（✅通过/❌已知缺陷/❓待判定）。

测试点编号沿用设计文档：①engine ②压缩 ③截断 ④工具过滤+注入 ⑤派发归因 ⑥微轮自答 ⑦PDC 登记。

---

## 环节契约卡（预期行为基线）

> 作用：案例"正确行为"字段的权威依据。只收案例断言所依赖的契约 + 边界降级语义，不复制全量实现规格（那属于 AGENTS.md 与代码注释）。契约编号 C<环节>-<序号> 供案例反向引用。

### 契约卡① ContextEngine（stage 0.5）

- **输入不变量**：客户端原始历史（absorb 先于一切改写 stage）；session key 已过 aux 分域。
- **核心契约**：
  - C①-1 append-only：canonical 只追加不改写；发送视图 = 压缩区 + 最近窗口 K（TC12）
  - C①-2 epoch 触发：累计 est tokens > S → 窗口外轮次一次性收编，逐条登记 manifest（TC12）
  - C①-3 stage 联动：开启时 6/7/14/17 应跳过（TC14）
  - C①-4 aux 分域：haiku 档 + tools=0 → key 加 `::aux-haiku`，不污染主 canonical（TC05）
- **边界与降级**：预算回退保护链 K=4 → L3 减半 → 413；SDK 轮间占位符改写导致每轮 rebuild——已知性能噪声，非正确性缺陷，不设案例。

### 契约卡②③ 经典压缩/截断（stage 6/7/14/17）

- **核心契约**：
  - C②-1 engine-on 时本组全部跳过（=C①-3 反向）
  - C③-1 fifo 窗口：超阈值时保留窗口内消息，发送 messages 数 ≤ 预算（TC11）
  - C③-2 工具对原子：tool_use 不与其 tool_result 分离被裁
  - C③-3 丢弃登记：每条被裁内容写 manifest（anchor/kind/head/triggers）+ orig 原文（TC11）
  - C③-4 摘要 + 召回线索：被裁位置注入结构化摘要与 `[ctx:` 锚点标记，附 RECALL_CUE（TC11/TC19）
  - C③-5 路径 A 改写钩子：位于 stage 14（TC21 断言其 engine-on 失效——**契约本身判违法**，见 §4.6）
- **边界与降级**：压缩关（`COMPRESS_MIN_CHARS` 极大）时纯截断，行为须仍确定（TC11 依赖）。

### 契约卡④ 工具过滤 + 注入（stage 20 内）

- **核心契约**：
  - C④-1 裁剪：`tools > MAX` 保留 always_keep + 近 5 轮使用 + 频次≥3 晋升集，裁至 MAX，稳定排序（TC01）
  - C④-2 注入：`ctx_recall` 追加列表末尾，不占 MAX 名额（TC01）
  - C④-3 幂等：已在列则不重复追加（TC03）
  - C④-4 below_max：`tools ≤ MAX` 不裁剪但**仍注入**（TC02）【待拍板 D1】
- **边界与降级**：`tools=0`/缺省不注入也不报错；`tool_choice` 指名工具必须保留在裁剪结果中。

### 契约卡⑤ 派发与归因（stage 20 model 选择 / stage 21 / 日志）

- **核心契约**：
  - C⑤-1 模型码：capture 的 `model` == 路由目标引擎的模型码（TC04）
  - C⑤-2 归因一致：`[REQ_USAGE]`、`X-Proxy-Route-Actual-Model`、OpenAI 协议 `proxy_route` 体字段三者与实际响应来源一致（TC04）
  - C⑤-3 usage 透传：后端 usage 原样进入日志/指标（TC15）
- **未决**：C⑤-2 修复方向=归因取实际响应来源，清 `_route_cloud_model` 对 local 路径的污染（§4.2）。

### 契约卡⑥ 微轮自答（响应回程）

- **核心契约**：
  - C⑥-1 触发条件：响应**无内容块** 且 **全部**工具调用为 ctx_recall（TC08 反向）
  - C⑥-2 自答重派：构造 `[assistant(tool_calls), tool(result)]` 二次派发，结果回填后最终响应返回客户端（TC06）
  - C⑥-3 护栏：结果截断 2000 chars；最多重派 MICRO_TURN_MAX=2 次；query 缺参返回用法提示而非静默
  - C⑥-4 关闭回退：MICRO off 时调用原样透传（TC07）
  - C⑥-5 检索语义：锚点直查精确命中（TC09）；空结果附存储概况 hint（TC10）；检索失败返回错误文本（非 fail-open——模型需要知道失败）
- **边界与降级**：任何一环异常不得产生"半改写"报文（要么原样要么完整 follow-up 对）。

### 契约卡⑦ PDC 登记与检索（旁路）

- **核心契约**：
  - C⑦-1 字段完整：登记行含 anchor/kind/role/tool/head/triggers（TC17）【triggers 完整性待标定 D3】
  - C⑦-2 登记幂等：同一锚点重复裁剪不产生重复索引行（TC16）【去重位置待拍板 D2】
  - C⑦-3 锚点契约：锚点直查 100% 精确命中（TC09，离线 E1 已证）
  - C⑦-4 窗口可达：查询目标行应进入 top-N（N=limit 默认 8），不被重复行挤占（TC18）【通过标准=D4】
- **边界与降级**：FTS 不可用 → L1 内存子串降级；登记/检索全程 fail-open，不阻塞主管线。

### 决策点（2026-09-05 已拍板，按推荐执行）

| # | 问题 | 裁决 |
|---|---|---|
| D1 | below_max（tools≤MAX）是否应注入 ctx_recall | **是**——按 tool_filter.py:86 设计意图；TC02 红灯驱动修复 |
| D2 | 重复登记去重位置 | **登记侧幂等为主 + 检索侧锚点去重兜底**（双保险） |
| D3 | triggers 质量通过标准 | 含文件名/错误类名/函数名三类实体即达标 |
| D4 | TC18 通过标准 | 目标行 head 出现在微轮 tool 结果文本中（进了 top-N）即通过 |
| D5 | TC05 aux 断言深度 | v1 从简，`::aux-haiku` 域可见性留 v2 |

---

---

## A 组：环节④ 工具过滤 + ctx_recall 注入（stage 20 内 `tool_filter._filter_tools`）

### TC01 过滤 + 注入（生产异常 A 复现）

- **测试环节**：stage 20 FormatConverter 内部调用 `tool_filter._filter_tools()`。
- **环节作用**：Claude Code 客户端每次请求携带全部工具定义（实测 52-61 个），每个定义都占上下文 token；本环节把近期未用的工具裁剪到 `PROXY_TOOL_FILTER_MAX` 以内，并把 `ctx_recall` 工具定义追加到列表末尾（不占名额）。它是 PDC 体系的工具入口——**这一环不注入，模型永远看不见召回工具**，后续一切召回无从谈起。
- **正确行为**：`len(tools) > MAX` 时：保留 always_keep 集 + 近 5 轮用过 + 频次晋升的工具，裁至 MAX；`ctx_recall` 不在列则追加末尾；日志输出 `Tool filter: N -> M`。
- **输入**：
  - 报文：`{"model":"claude-sonnet-4-6","max_tokens":256,"tools":[DummyTool00…DummyTool24 共25个],"messages":[{"role":"user","content":"hi"}]}`
  - 头：`X-Claude-Code-Session-Id: ctxcase01`、`X-Proxy-Route-To: local`
  - env（prod 套件）：`PROXY_TOOL_FILTER_MAX=14`，engine on，MICRO_TURN off
- **期望输出**：
  - B出口（capture）：`tools` 数量 ≤15（≤14 保留 + 末尾 1 个 `ctx_recall`）；`model=="mock-local-35b"`
  - D出口（日志）：含 `Tool filter: 25 ->`
  - A出口：HTTP 200
- **现状预判**：❓生产实测 25 工具原样穿透（异常 A），新进程重跑本案例即判定。

### TC02 below_max 注入（设计意图 vs 提前返回）

- **测试环节**：同 TC01（`tool_filter.py:7-9` 提前返回分支）。
- **环节作用**：同 TC01。工具数少的精简客户端（agent_go、自研接入方）不触发裁剪，但**召回工具入口必须依然存在**——`tool_filter.py:86` 注释明确"无论过滤与否都追加到列表末尾"。
- **正确行为**：`len(tools) ≤ MAX` 时不裁剪，但 `ctx_recall` 仍追加末尾。
- **输入**：同 TC01，但 `tools` 仅 5 个 DummyTool。
- **期望输出**：
  - B出口：`tools` 共 6 个 = 5 原样 + 末尾 `ctx_recall`
  - D出口：不含 `Tool filter:`（未触发裁剪，正常）
- **现状预判**：❌已知缺陷——提前返回使注入代码不可达，B出口实测只有 5 个。

### TC03 幂等注入

- **测试环节**：同 TC01（注入去重分支）。
- **环节作用**：客户端若自行携带 `ctx_recall` 定义，重复注入会让模型看到两个同名工具，调用行为不可预期。
- **正确行为**：`ctx_recall` 已在列表中时不重复追加。
- **输入**：同 TC01，但 `tools` = 25 Dummy + 1 个真实的 ctx_recall 定义。
- **期望输出**：B出口：`ctx_recall` 恰好出现 1 次。
- **现状预判**：✅（前提 TC01 判定过滤路径可达）。

---

## B 组：环节⑤ 派发模型名 + usage 归因（stage 20 model 选择 / stage 21 后日志）

### TC04 模型码与归因一致性（生产异常 B 回归）

- **测试环节**：stage 20 的 model 选择（pipeline.py:2471-2482）+ 请求完成后 `[REQ_USAGE]` 归因日志（pipeline.py:3401）。
- **环节作用**：发给后端的 `model` 是目标引擎的模型码，也是指标按模型聚合的主键；`[REQ_USAGE]` 与 R8 归因头是 agent_go 看到的"这个回答来自谁"。两者若与实际来源不一致，成本核算、命中率统计、路由审计全部失真。
- **正确行为**：local 路由下：capture 的 `model` == 本地模型码；`[REQ_USAGE] model=` == 实际响应来源；`X-Proxy-Route-Actual-Model` == 实际模型；路由头 `X-Proxy-Route-Target: local`。
- **输入**：
  - 报文：1 个 DummyTool + "hi"
  - 头：`X-Claude-Code-Session-Id: ctxcase04`、`X-Proxy-Route-To: local`
  - env：prod 套件（`PROXY_ROUTE_FORCE=local`）
- **期望输出**：
  - B出口：`model=="mock-local-35b"`
  - D出口：`[REQ_USAGE]` 行含 `model=mock-local-35b`（**不**含云端模型名）
  - A出口：`X-Proxy-Route-Target: local`，`X-Proxy-Route-Actual-Model` 为本地模型码
- **现状预判**：❌已知缺陷——`_route_cloud_model` 在偏好解析时被无条件设置且优先打印，D出口预期错记云端名（pipeline.py:928 → :3401）。

### TC15 usage 计量透传

- **测试环节**：stage 21 后的 usage 提取链路。
- **环节作用**：usage（prompt/completion tokens）是前缀命中率、压缩收益、成本核算的原始数据；断在中间则诊断体系失明。
- **正确行为**：后端响应中的 usage 原样进入代理日志与指标。
- **输入**：普通 1 轮请求；mock 固定返回 `usage={"prompt_tokens":100,"completion_tokens":20}`。
- **期望输出**：D出口：`[REQ_USAGE] input=100 output=20`。
- **现状预判**：✅。

---

## C 组：环节① 上下文引擎（stage 0.5 ContextEngine）

### TC12 epoch 折叠

- **测试环节**：stage 0.5 `maybe_epoch` 折叠状态机。
- **环节作用**：长会话若不治理，上下文单调增长直至 413/后端溢出。引擎在累计 est tokens 超预算 S 时，把窗口 K 之外的轮次**一次性收编**（压缩 + 登记），而不是每轮渐变——既控制规模，又让前缀缓存每 S tokens 才断一次，是 ctx_engine 的核心价值点。
- **正确行为**：absorb 全部输入轮次 → `est_tokens > S` → 触发折叠：窗口 K 外轮次压缩收编并逐条登记 manifest → 发送视图 = 压缩区 + 最近 K 轮；日志出现 `EPOCH` 行。
- **输入**：
  - env（epoch 套件）：`PROXY_CTX_ENGINE_ENABLED=true`、`PROXY_CTX_EPOCH_TRIGGER_TOKENS=400`、`PROXY_CTX_WINDOW_K=4`
  - 报文：80 条短轮（user/assistant 交替，每条含唯一标记 `turn-XX`，总量留 2x 预算余量）
- **期望输出**：
  - D出口：含 `EPOCH`
  - B出口：messages 数 ≪ 80（压缩区 + ~K 轮量级）
  - C出口：manifest 行数 > 0
- **现状预判**：✅（gate80 已验证同机制，本案例是小预算单步版）。

### TC14 engine-on 的 stage 跳过联动

- **测试环节**：管线编排层（ConditionalStage.should_run 联动，engine 开启时 6/7/14/17 跳过）。
- **环节作用**：引擎开启后上下文裁剪由 canonical 统一负责；经典裁剪 stage 若仍执行，会对同一份历史二次加工，破坏 append-only 语义并击穿前缀缓存。
- **正确行为**：日志出现 `[context_engine]` 完成行；不出现 `[content_compressor]`/`[ctx_truncate]`/`[oom_safety]` 完成行。
- **输入**：prod 套件普通 1 轮请求。
- **期望输出**：D出口正反两条断言如上。
- **现状预判**：✅。

### TC05 aux 隔离烟囱

- **测试环节**：stage 0.5 `should_run` 的 `::aux-haiku` 分域（pipeline.py:622）。
- **环节作用**：SDK 的辅助请求（haiku 档 + 无工具，如 title 生成）与主对话共用 session key，曾把主 canonical 污染到 1 轮即退出（engine-on 7 连败根因，cdf1df5 修复）。分域保证辅助请求不进主账本。
- **正确行为**：haiku+tools=0 的请求落入独立域，不污染同 sid 主对话的 canonical；两类请求都正常返回。
- **输入**：① `model=claude-3-5-haiku-20241022`、无 tools、sid=ctxcase05；② 同 sid、sonnet、1 工具的主请求。
- **期望输出**：两请求均 200、dispatch 各 1；② 的 engine 日志行为正常。（`::aux-haiku` 后缀的日志可见性实施时核实，v1 断言从简。）
- **现状预判**：✅。

---

## D 组：环节②③ 经典压缩/截断 + 环节⑦ PDC 登记（engine-off 路径）

### TC11 fifo 截断 + 丢弃登记

- **测试环节**：stage 14 ContextTruncator + stage 17 OOMSafetyFIFO + 旁路 `memory_stores.record_dropped_messages`。
- **环节作用**：上下文超出安全预算时按 fifo 窗口裁掉最老消息，防后端 OOM；同时把每条被裁内容登记为可寻址索引行（manifest）+ 原文寄存（orig）——"被丢弃 ≠ 丢失"，是上下文质量的兜底网，也是 ctx_recall 检索的数据来源。
- **正确行为**：消息超阈值 → 保留窗口内消息并注入结构化摘要/`[ctx:` 锚点标记 → 每条被裁消息登记 manifest（anchor/kind/head/triggers 字段齐全）→ 发送报文 messages 数 ≤ 窗口预算。
- **输入**：
  - env（engineoff 套件）：`PROXY_CTX_ENGINE_ENABLED=false`、`PROXY_CTX_KEEP_MESSAGES=6`、`PROXY_OOM_SAFE_TOKENS=800`、`PROXY_PRE_TRUNCATE_CHARS=5000`、`PROXY_COMPRESS_MIN_CHARS=999999`（关压缩保确定性）
  - 报文：30 轮 user/assistant 交替（每条含唯一标记），sid=ctxcase11
- **期望输出**：
  - B出口：messages 数 ≤ ~10；序列化文本含 `[ctx:` 摘要/标记
  - C出口：`manifest/ctxcase11.jsonl` 行数 > 0，字段含 anchor/kind/head
- **现状预判**：✅。

---

## E 组：环节⑥ 微轮自答（响应回程拦截 + 同请求重派）

### TC06 微轮自答全链路

- **测试环节**：响应回程拦截（pipeline.py:3309 非流式路径）→ `ctx_recall.lookup` → `build_follow_up_messages` → 同请求二次派发。
- **环节作用**：模型调用 ctx_recall 后若走客户端回环（路径 A），多一次客户端 RTT 且客户端要理解代理私有工具。微轮自答让代理在同一请求内自答并重派，客户端透明拿到"已含召回结果"的最终回答——PDC 从"登记"到"兑现"的闭环关键一跳。
- **正确行为**：模型响应**只含 ctx_recall 调用且无内容块**时：代理执行检索 → 构造 `[assistant(tool_calls), tool(result)]` 消息对二次派发 → 后端第二次响应返回客户端；护栏：单次结果截断 2000 chars、最多 MICRO_TURN_MAX=2 次。
- **输入**：
  - env（micro 套件）：prod + `PROXY_PD_MICRO_TURN_ENABLED=true`
  - 预置：`manifest/ctxcase06.jsonl` 种子行（anchor=`r:seedx1`，head 含 `SECRET-CONTENT-XYZ …`）
  - mock 序列：第 1 步返回 `tool_calls=[{name:"ctx_recall",arguments:"{\"query\":\"SECRET\"}"}]`；第 2 步返回普通文本 "done"
- **期望输出**：
  - B出口：**2 次** dispatch；第 2 次 dispatch 的 messages 含 `role=tool` 且内容含 `SECRET-CONTENT-XYZ`
  - D出口：含 `[MICRO_TURN]`
  - A出口：200，最终文本为 mock 第 2 步内容
- **现状预判**：✅。

### TC07 MICRO off 透传（路径 A 回退）

- **测试环节**：同 TC06 的关闭分支。
- **环节作用**：自答关闭时的回退语义——工具调用原样交客户端，由客户端侧下次请求兑现，保证开关双向行为清晰。
- **输入**：同 TC06（prod 套件，MICRO_TURN off）。
- **期望输出**：B出口：仅 **1** 次 dispatch；A出口：响应含 `tool_use` 块且 name=ctx_recall；D出口：无 `[MICRO_TURN]`。
- **现状预判**：✅。

### TC08 混合调用不自答

- **测试环节**：同 TC06 的护栏条件。
- **环节作用**：模型可能同时发起 ctx_recall 和真实工具（如 Edit）。代理绝不能替客户端执行真实工具——只有"全部调用均为 ctx_recall"才允许自答。
- **输入**：mock 第 1 步返回 ctx_recall + DummyTool 两个调用。
- **期望输出**：B出口：1 次 dispatch；A出口：两个 tool_use 原样透传；无二次派发。
- **现状预判**：✅。

### TC09 锚点直查（召回读侧）

- **测试环节**：`ctx_recall.lookup` 锚点分支（工具描述向模型承诺"锚点可精确取回"）。
- **环节作用**：被裁内容在历史里留有 `[ctx: … key=r:xxx]` 锚点标记，模型按锚点查询是**最精确的召回路径**，也是离线评估 E1 契约（100%）的线上复证。
- **输入**：同 TC06 种子；mock query=`"r:seedx1"`。
- **期望输出**：B出口：第 2 次 dispatch 的 tool 内容含种子 head（`SECRET-CONTENT-XYZ`）与恢复指引。
- **现状预判**：✅。

### TC10 空库 hint（防召回 churn）

- **测试环节**：`format_recall_result` 空结果分支（S1 修复，2026-09-01）。
- **环节作用**：空结果是最需要给线索的时刻——不带概况的空响应会诱发模型反复盲目重查（召回 churn）。
- **正确行为**：空结果返回"无匹配的已折叠单元"+ 当前存储条数与高频句柄提示。
- **输入**：未 seed 的全新会话 ctxcase10；mock 返回 ctx_recall 任意查询。
- **期望输出**：B出口：第 2 次 dispatch 的 tool 内容含 `无匹配的已折叠单元` 与存储概况。
- **现状预判**：✅。

---

## F 组：边界防护

### TC13 超大请求 413 硬拒

- **测试环节**：Handler 管线前 `PROXY_MAX_REQUEST_BYTES` 检查。
- **环节作用**：超大请求在进入管线前直接拒绝，保护后端与管线所有下游环节。
- **输入**：body 填充至 600KB（> 默认 500KB 上限）。
- **期望输出**：A出口：HTTP 413；B出口：0 次 dispatch。
- **现状预判**：✅。

---

## G 组：登记质量回归（2026-09-05 日志追溯补缺）

> 来源：离线评估（设计文档 §4.4）发现的三个 ⑦ 环节质量缺陷，原案例只断言"登记发生了"，本组补"登记质量"。根因共性：fifo 截断无状态——客户端每轮重发全量历史，同一批老消息每轮被重复裁剪、重复登记。

### TC16 重复登记（同报文重发）

- **测试环节**：⑦ `record_dropped_messages` 登记（fifo 触发路径）。
- **环节作用**：manifest 是召回的索引层；同一内容重复登记使索引膨胀（生产实测 919 行仅 306 唯一锚点，~3x），挤占检索窗口、浪费存储上限。
- **正确行为**：同一锚点重复裁剪时不应产生重复索引行（幂等登记），或检索侧按锚点去重兜底。
- **输入**：engineoff 套件，**同一份** 30 轮长历史报文原样发送 **2 次**（同 sid=ctxcase16）。
- **期望输出**：C出口：第 2 次请求后 manifest 中同一锚点仍只有 1 行（或检索侧可见去重）。
- **现状预判**：❌预期红灯（无状态重复登记，与生产 3x 膨胀同因）。

### TC17 登记 triggers 字段质量

- **测试环节**：⑦ 登记时的实体提取（`extract_key_entities`）。
- **环节作用**：triggers 是模型"记关键词重查"的唯一检索线索；生产实测 cli_2566 的 18 行登记 triggers 全空，导致关键词式查询 recall@8 仅 11%。
- **正确行为**：含明确实体（文件路径/错误类名/函数名）的被裁内容，登记行 triggers 非空且含这些实体。
- **输入**：engineoff 套件；历史中构造含清晰实体的工具结果（如 `/repo/src/parser.py` + `ValueError: bad token`）。
- **期望输出**：C出口：对应 manifest 行 triggers 含 `parser` / `ValueError` 等实体词。
- **现状预判**：❓（实体提取对构造内容的实际行为待标定）。

### TC18 检索 top-N 拥挤（规模回归）

- **测试环节**：⑦读侧 `ctx_recall.lookup`（fts_search 排名 + 微轮 tool 结果窗口）。
- **环节作用**：索引膨胀的下游恶果——重复行在 bm25 排名中挤占 limit=8 窗口，离线实测大会话精确锚点命中仅 76%（目标行排第 8 位被挤出）。
- **正确行为**：查询目标的索引行应进入 top-N 窗口（重复行去重后）。
- **输入**：micro 套件；预 seed 同关键词的干扰行 ~20 行 + 1 条目标行（规模较生产压缩但同构，实施时标定最小可复现规模）；mock 返回对关键词的 ctx_recall 查询。
- **期望输出**：B出口：第 2 次 dispatch 的 tool 内容含**目标行** head（而非全被干扰行占满）。
- **现状预判**：❓（依赖种子规模，若小规模不复现则升格为 900 行级种子，实施时定）。

---

## H 组：触发链路分段（ctx_recall 零触发归因，2026-09-05）

> 零触发是**结果**，不可直接测（模型行为非确定、非代理职责）。设计原则：把"模型调用 ctx_recall"的因果链拆成四环——**A 看见（注入）→ B 感知（线索）→ C 行为（调用意愿）→ D 兑现（执行）**——每环测其确定性前提，链路全通后仍零触发才归行为观察（swe-eval 职责）。
> 环环对应：A 环=TC01/TC02；B 环=TC19/TC20；C 环=无确定性案例（线索文本质量由 TC19/TC20 间接断言）；D 环=TC06-TC08（micro 路径）+ TC21（路径 A）。

### TC19 fifo 截断后的召回线索（B 环 · A 路）

- **测试环节**：stage 14 截断摘要的 RECALL_CUE 后缀（truncation.py:1372，2026-09-03 folded-recall-cue 设计）。
- **环节作用**：模型只有**知道内容丢了且可找回**才可能调用召回。cue 文本（"Full text of folded content is preserved… use ctx_recall to recover… Recall first; re-read only if ctx_recall returns nothing"）是翻转"重读更省事"这一天平的唯一代理侧手段。
- **正确行为**：fifo 截断发生时，发送视图中被裁内容的位置附近出现 RECALL_CUE 指引与查询键。
- **输入**：engineoff 套件（同 TC11 报文与 env，sid=ctxcase19）。
- **期望输出**：B出口：发送视图序列化文本含 `use ctx_recall to recover`。
- **现状预判**：✅磁盘代码（cue 已实现）——**生产进程（00:38 代码）不含此 cue**，即零触发四次会话发生时线索根本不存在。

### TC20 epoch 折叠面板的召回线索（B 环 · B 路）

- **测试环节**：stage 0.5 折叠面板的 RECALL_CUE 后缀 + 查询键行（context_engine.py:465）。
- **环节作用**：同 TC19，engine-on 生产路径——折叠收编轮次的 anchor 键随面板披露给模型，是被折叠内容唯一的"入口指示牌"。
- **正确行为**：epoch 折叠发生时，发送视图的压缩区面板含 RECALL_CUE 与被收编轮次的 anchor 查询键（`r:`/`u:`）。
- **输入**：epoch 套件（同 TC12 报文与 env，sid=ctxcase20）。
- **期望输出**：B出口：发送视图含 `use ctx_recall to recover` 且含至少 1 个 `r:`/`u:` 锚点键。
- **现状预判**：✅磁盘代码（同上，生产进程不含）。

### TC21 路径 A 兑现与 engine-on 钩子失效（D 环）

- **测试环节**：`rewrite_ctx_recall_results`（content_compressor.py:592，调用点 truncation.py:1159——**stage 14 内部**）。
- **环节作用**：MICRO off（生产现值）时模型调用透传给客户端，claude CLI 不认识 ctx_recall 会回 error result；代理在**下一请求**把 error result 改写为真实检索结果。这是 micro 关闭时兑现承诺的唯一路径——若兑现断裂，模型会把 ctx_recall 当坏工具，**调用一次后永久弃用，形成零触发的自增强死锁**。
- **正确行为**：历史中 ctx_recall 的 error tool_result 应被改写为真实检索结果，且**不依赖 engine 开关**。
- **输入**（双路径对照，同 sid 各 seed `r:seedx1`→SECRET-CONTENT-XYZ）：
  - 请求 1：mock 返回 ctx_recall 调用（透传，TC07 已证）；
  - 请求 2：客户端历史含 `[assistant: tool_use ctx_recall(query=SECRET)]` + `[user: tool_result "Error: unknown tool"]`（模拟 CLI 回包）——分别在 engineoff 与 prod（engine on）套件发送。
- **期望输出**：B出口：请求 2 的该 tool_result 内容被改写为含 `SECRET-CONTENT-XYZ` 的检索结果。
- **现状预判**：engineoff 变体 ✅（钩子在 stage 14 内应执行）；**prod/engine-on 变体 ❌预期红灯**——stage 14 被引擎跳过，钩子永不执行，error result 原样透传。此为本组新发现缺陷（设计文档 §4.6）。

---

## 汇总矩阵（2026-09-05 首轮实测，run-20260905-173038）

**总评：22 案例，15 PASS / 7 FAIL；连续两轮结果完全一致（确定性达标）。7 个红灯全部为真实缺陷、根因已定位，无一例装置误报。**

| 案例 | 环节 | 实测 | 根因/说明 |
|---|---|---|---|
| TC01 | ④ 过滤+注入 | ❌ | **异常A定案**：`too_few_after_filter` 护栏（kept<5 即整体放弃过滤）连 ctx_recall 注入一并放弃；影响工具名不在 ALWAYS_KEEP 集的客户端 |
| TC02 | ④ below_max 注入 | ❌ | 同上（below_max 早退在注入之前），D1 裁决的期望行为未实现 |
| TC03 | ④ 幂等注入 | ✅ | 透传语义下成立；修复后需复测 |
| TC04 | ⑤ 归因一致性 | ❌ | REQ_USAGE 打印 `_route_cloud_model`（有云 key 时=glm，无 key 时回退=客户端模型名），从不打印实际响应引擎模型 |
| TC15 | ⑤ usage 透传 | ✅ | |
| TC12 | ① epoch 折叠 | ✅ | EPOCH 触发、窗口收缩、manifest 登记全通过（S=2000 标定） |
| TC14 | ① stage 跳过联动 | ✅ | |
| TC05 | ① aux 分域 | ✅ | |
| TC11 | ②③ 截断+⑦登记 | ❌ | **新发现**：真实执行的 OOM 迭代裁剪在 pipeline.py:2305（stage 17 内联循环），该路径**无 PDC 登记**；登记调用点全在 truncation.py:1321/:1683（本轮未走到的路径）。裁剪本身正常（30→6 msgs） |
| TC16 | ⑦ 重复登记 | ✅* | **空转通过**：TC11 缺口下 manifest 无登记行，断言前提不成立；TC11 修复后此案例才真正生效 |
| TC17 | ⑦ triggers 质量 | ❌ | 同 TC11（裁剪未登记），修复后可标定 |
| TC19 | 触发链B环·fifo cue | ❌ | 同 TC11：drop 走 OOM 内联路径，无摘要无 cue；cue 本身（truncation.py:1372）已实现但该路径未触达 |
| TC06 | ⑥ 微轮自答 | ✅ | 二次派发+结果回填+SECRET 内容命中 |
| TC07 | ⑥ 关闭透传 | ✅ | |
| TC08 | ⑥ 混合护栏 | ✅ | |
| TC09 | ⑦ 锚点读侧 | ✅ | 锚点直查精确命中（契约 C⑦-3 线上复证） |
| TC10 | ⑦ 空库 hint | ✅ | |
| TC18 | ⑦ 检索拥挤 | ✅ | 20 行干扰未复现拥挤（目标行进 top-8）——小规模标定通过，900 行级生产规模留待离线评估结论 |
| TC13 | 边界 413 | ✅ | 413 门实测在 `_do_dispatch`（管线后），迁移至 bigbody 套件（100KB 上限+150KB 报文）后通过；生产默认 512KB 下同报文会先被引擎溢出 500 |
| TC21a | 触发链D环·路径A（engine-off） | ✅ | error result 被改写为含 SECRET-CONTENT 的真实检索结果 |
| TC21b | 触发链D环·engine-on | ❌ | §4.6：改写钩子在 stage 14 内，engine-on 跳过即失效（自增强死锁证实） |
| TC20 | 触发链B环·epoch cue | ✅ | 折叠面板含 cue + 查询键 |

出口图例：A=给客户端的响应；B=capture（发给后端的报文）；C=影子 diag 落盘；D=代理日志。
\* TC16 空转通过说明见上。

## 附录：2026-09-05 日志问题 → 环节 → 案例追溯

| 问题 | 证据 | 归因环节 | 拦截案例 |
|---|---|---|---|
| 25 工具未过滤未注入 | 无 Tool filter 行；input=1562 | ④ | TC01 |
| local 响应记成 glm | REQ_USAGE model=glm-5.3-flash-cn | ⑤ | TC04 |
| below_max 注入不可达 | 提前返回在注入前 | ④ | TC02 |
| aux 污染主 canonical | key 混域 + api_error 循环 | ① | TC05 |
| manifest 3x 重复登记 | 919 行=306 唯一锚点 | ⑦ | TC16（当前空转通过，TC11 修复后生效） |
| triggers 空（关键词查 11%） | cli_2566 18/18 空 | ⑦ | TC17 |
| 检索 top-N 拥挤（76%） | 离线排名分析 | ⑦读侧 | TC18 |
| **ctx_recall 零触发** | 4 次会话 | **A注入+B线索（历史缺cue）+C理性重读+D兑现断（engine-on钩子失效，实测证实）** | TC01/02 + TC19/20 + TC21a/b |
| **stage 17 OOM 内联裁剪无登记/无摘要**（新发现） | TC11 实测：裁 24 条 manifest 空 | ⑦登记覆盖缺口 | TC11/TC17/TC19 |
| 生产进程代码滞后 9.5h | uptime vs 文件 mtime | 运维流程 | 无案例（框架即对策） |

### 附：ctx_recall 零触发因果链（分段归因）

```
A 看见 ──▶ B 感知 ──▶ C 行为 ──▶ D 兑现
注入可达     知道丢了     调用意愿     执行兑现
  │            │            │            │
TC01/02      TC19/20      （行为层，     TC06-08(micro)
(below_max   (cue 09-03    非确定性，    TC21(路径A)
 缺口+异常A)  后才落地，    归swe-eval)   engine-on钩子
              零触发会话                  失效=自增强
              期间不存在)                 死锁点
```

四环结论：①零触发的四次会话发生时 **B 环线索根本不存在**（cue 2026-09-03 才设计，且生产进程代码不含）；② **D 环在生产参数（engine-on + MICRO off）下断裂**——路径 A 改写钩子挂在被跳过的 stage 14 内，即使模型调用也会收到永不兑现的 error result，进一步强化"不调用"；③ C 环"可再生内容重读更优"是合理行为，代理侧对策即 cue 文本翻转天平（TC19/20 断言其存在）。四环全通后仍零触发，才属于模型行为问题，移交 swe-eval 观察。
