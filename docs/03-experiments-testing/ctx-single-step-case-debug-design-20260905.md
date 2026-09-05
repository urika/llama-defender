<!-- 上下文管理与召回·单步报文调试框架设计（ctx-case） -->

# 上下文管理与召回：单步报文调试框架与案例集设计

- 日期：2026-09-05
- 状态：设计评审中（未实施）
- 范围：llama-defender 代理层（`anthropic_proxy.py` / `pipeline.py` / `ctx_recall.py` / `tool_filter.py` / `context_engine.py` / `memory_stores.py`）的上下文管理、PDC 召回、上下文质量三类关键交互
- 关联：`docs/04-analysis-diagnostics/swe-empty-patch-rootcause-20260903.md`、`docs/03-experiments-testing/compression-quality-metrics-20260903.md`、IFC/PDC 设计文档

---

## 1. 背景与动机

2026-09-03 ～ 09-05 的 swe 真实会话调试确认了空 patch 三条件根因，并把生产恢复到 35B + engine-on + aux 隔离。但调试过程暴露三类问题，现有测试体系（unit/integration/promptfoo/e2e）都无法在**报文级**复现和回归：

1. **离线发现**：ctx_recall 检索质量在大会话上存在关键词查询 recall@8 仅 11%、manifest 重复登记 3 倍膨胀等缺陷（§4.4）；
2. **在线异常 A**：真实请求携带 25 个工具经生产代理后**未被过滤、ctx_recall 未注入**，原样到达本地后端（input=1562 tokens 实证），与 `PROXY_TOOL_FILTER_MAX=14` 的预期矛盾，且静态排查未能定论（§4.1）；
3. **在线异常 B**：请求实际路由本地，但 `[REQ_USAGE]` 日志把模型记成云端 `glm-5.3-flash-cn`——归因缺陷（§4.2）。

这些问题的共性：**都发生在"代理对报文的加工"这一层**（过滤、注入、改写、登记、归因），而现有 e2e 只看最终回答，integration 只看单点功能。需要一套**单步报文调试**设施：构造确定的输入报文，只断言代理侧可观测的中间结果点（发给后端的报文、响应头、注入、登记、日志事件），不关心 agent 最终行为。

## 2. 目标与非目标

**目标**

- G1 建立可重复的单步报文测试框架：确定输入 → 断言代理输出点，同一 commit 下结果稳定；
- G2 沉淀案例集：覆盖上下文管理（过滤/截断/压缩/引擎/折叠）、PDC 召回（注入/触发/自答/登记）、质量（归因/计量）三条线，**包含本次发现的问题报文**作为回归用例；
- G3 对异常 A 给出受控判定（新进程下过滤+注入是否正常），对异常 B / below_max 缺口固化为红绿可见的失败案例；
- G4 零生产干扰：不碰 4000/8081 生产栈，不触真实云端 API，不污染生产 diag 数据。

**与单元测试的分层关系**：现有 1516 条单测已覆盖 ①-⑦ 全部环节的**零件逻辑**（直接 import stage、构造 ctx、调 process()）。本框架不做它们的重复——每个案例同样"一次锁一个点"，但被测对象是**真进程中的组装行为**：stage 挂载、should_run 门控、env 参数可达性、跨 stage 的 ctx 传递。异常 A 即活证：过滤逻辑单测绿灯，真进程里却未执行。定位是"组装级单点回归"；每个红灯应反向沉淀为一条真正的单测，形成两层闭环。

**非目标**

- 不评估模型回答质量（那是 promptfoo / swe-eval 的职责）；
- 不复现多轮 agent 行为（每案例单请求，最多由微轮自答产生代理内部第二次派发）；
- 不覆盖 cloud 真实链路（路由决策的云端语义属 agent_go 联调范围，框架内强制 local）。

## 3. 信息收集清单（观测矩阵）

每案例采集四类信息，作为断言与差异分析的原始材料。

### 3.1 模型类型与路由归因

| 观测点 | 来源 | 说明 |
|---|---|---|
| 客户端请求模型 | 报文 `model` 字段 | 决定 smart router 偏好 |
| 路由目标 | 响应头 `X-Proxy-Route-Target` | `local\|cloud\|local_forced` |
| 路由理由 | 响应头 `X-Proxy-Route-Reason` | header_override / forced / zero_margin 等 |
| 实际模型 | 响应头 `X-Proxy-Route-Actual-Model` | R8 契约 |
| 后端收到的 model | mock capture `body.model` | **报文级真相**（本地 rapid-mlx 忽略 model 名，唯独 capture 能证明） |
| usage 归因模型 | 代理日志 `[REQ_USAGE] model=` | 异常 B 的发生点（`_route_cloud_model or ctx.model`） |

### 3.2 是否走代理与代理参数

代理参数在框架内**由 env profile 构造即真相**（直接 `python3 anthropic_proxy.py` 启动，不 source conf，不依赖 SIGHUP 后的运行时漂移）。关键参数：

`PROXY_CTX_ENGINE_ENABLED`、`PROXY_TOOL_FILTER_ENABLED`/`_MAX`、`PROXY_PD_ENABLED`、`PROXY_PD_MICRO_TURN_ENABLED`、`PROXY_CTX_KEEP_MESSAGES`、`PROXY_CTX_EPOCH_TRIGGER_TOKENS`/`_WINDOW_K`、`PROXY_OOM_SAFE_TOKENS`/`PROXY_PRE_TRUNCATE_CHARS`、`PROXY_COMPRESS_MIN_CHARS`、`PROXY_ROUTE_FORCE`、`PROXY_QUEUE_ENABLED`、`PROXY_HBE_ENABLED`。

每个 suite（§5.3）固化一组参数，案例报告必须记录所属 suite 的 env，杜绝"参数不明"导致的误判（生产异常 A 排查的首要教训）。

### 3.3 上下文要素与事件

| 事件类 | 观测点 | 来源 |
|---|---|---|
| stage 轨迹 | `[stage_name] completed` 有/无 | 代理日志（engine-on 应无 `[content_compressor]` 等） |
| 引擎事件 | `[context_engine] msgs=/est_tokens=/epochs=/usage/hit` | 代理日志 |
| 折叠事件 | `EPOCH` 行、折叠后窗口消息数 | 代理日志 + capture |
| 截断/压缩 | fifo 触发、`[ctx:` anchor 标记、摘要注入 | capture 报文 + 日志 |
| PDC 登记 | `logs/diag/manifest/<sid>.jsonl` 行数与字段 | 影子 diag 目录 |
| 原文寄存 | `logs/diag/orig/<sid>.jsonl` | 影子 diag 目录 |
| 微轮自答 | `[MICRO_TURN]` 行 + 第二次 dispatch 报文（含 `role=tool` 结果） | 日志 + capture |
| 注入类 | blocker/re_read/`[System: …]` 提示 | capture 报文 |
| 边界防护 | 413 状态、队列拒绝 | 响应状态码 |

### 3.4 报文级真相（capture）

mock backend 记录每次收到的 `POST /v1/chat/completions` 完整 body + 关键 header 到 capture JSONL。**一切"代理发给后端什么"的断言以 capture 为准**，不以代理日志为准——日志会说"Forwarding to local"，capture 才证明 25 个工具原样穿透（异常 A 的教训）。

## 4. 已知差异与根因分析（目标 vs 实际）

### 4.1 异常 A：25 工具未过滤、ctx_recall 未注入【已定案：护栏分支误伤注入】

- 现象：生产实测 25 工具原样穿透（无 `Tool filter:` 行、input=1562 tokens 实证）；影子环境（磁盘代码）完整复现 → **当前代码缺陷，非进程漂移**。
- 根因（进程内单步定位）：`tool_filter._filter_tools` 的 keep 集 = ALWAYS_KEEP(21 个标准工具名) ∪ 近 5 轮使用 ∪ 频次晋升。客户端工具名与该集几乎无交集时 `kept=0 < 5` → `too_few_after_filter` 分支**整体放弃过滤**——DEF-203 前缀稳定护栏把"放弃裁剪"扩大成了"放弃裁剪+放弃 ctx_recall 注入"；`below_max` 早退分支同病（§4.3）。注入承诺（"无论过滤与否都追加"）在两个早退分支下均不可达。
- 影响面：生产 claude-cli 流量不受影响（日志 824 次 `Tool filter:`，Read/Bash 等命中 ALWAYS_KEEP）；**工具名不在标准集的客户端**（自研 agent、测试装置）无过滤无召回入口。
- 修复方向：注入逻辑从两个早退分支中拎出（早退只放弃裁剪，不放弃注入），或上移至 FormatConverter。
- 判定：TC01 红灯固化为回归案例。

### 4.2 异常 B：REQ_USAGE 归因错记云端模型【已定案，另发现无 key 变体】

- 根因链：SmartRouter 在偏好解析时无条件设置 `ctx._route_cloud_model`（pipeline.py:928）→ `[REQ_USAGE]` 打印 `getattr(ctx,'_route_cloud_model','') or ctx.model`（pipeline.py:3401，流式孪生 anthropic_proxy.py:1591）。
- 生产形态（云 key 在）：local 路由被记成 `glm-5.3-flash-cn`。
- 影子形态（无云 key）：`_route_cloud_model` 为空 → 回退打印 `ctx.model` = **客户端请求模型名**（claude-sonnet-4-6），同样不是实际响应引擎——两个变体，同一缺陷：**归因从不指向实际响应来源**。
- 后端实际收到本地模型码（pipeline.py:2471-2482 local 分支），路由与推理正确，纯归因污染。`X-Proxy-Route-Actual-Model` 与 `proxy_route` 体字段是否同源受污染 → TC04 一并覆盖。
- 修复方向：usage 归因取实际响应来源（dispatch 处已知的 backend 模型码）。

### 4.3 below_max 注入缺口【设计意图 vs 实现矛盾，已定根因】

- `tool_filter.py:7-9`：`tools ≤ MAX` 直接提前返回 `below_max`；ctx_recall 注入代码（:86 注释"**无论过滤与否都追加到列表末尾**"）位于提前返回之后，永远不可达。
- 实测影响面：claude-cli 生产会话 52-61 工具（>20）**不受影响**；精简客户端（agent_go、工具数 < MAX 的任何接入方）拿不到 ctx_recall —— 与"四次真实会话 ctx_recall 零触发"的观测部分相关。
- 处置：TC02 按注释的设计意图编写期望（≤MAX 也注入），当前代码下红灯；修复后转绿。

### 4.4 离线召回质量评估结论（tools/eval_ctx_recall.py，2026-09-05）

| 维度 | 结果 | 结论 |
|---|---|---|
| E1 锚点直查 | 4 会话 100%（1207/1207） | "锚点可精确取回"契约成立 |
| E2 任务成功口径（query=内容前缀，top8 存在同内容行） | swef5653 99%、swedaaac/cli_877f 100% | 现实查询可达性优秀 |
| E2 精确锚点@8 | swef5653 76%，其余 100% | 大会话被重复行拥挤 |
| E2 关键词式（triggers 前 3 词） | 大会话 11% | **弱项**：triggers 质量差（cli_2566 18/18 行为空） |
| E3 archive 全文恢复 + E4 offset 分页 | 全部命中、前缀保真 100%、分页逐字对齐 | 恢复链路可靠 |
| 登记膨胀 | swef5653 919 条 r: 行仅 306 唯一锚点（~3x） | 同内容跨轮重复登记挤占 top-N |

改进候选（本框架之外，登记为后续项）：检索结果按锚点去重（等价于 top-N 窗口扩 3 倍）；登记端 triggers 提取增强与同锚去重。

### 4.5 生产进程代码滞后【运维项】

生产代理 00:38:41 启动；磁盘代码 10:07-10:11 被并行会话修改（+204 行未提交，PipelineContext/RereadDetector 区）。本框架跑的是**磁盘代码**（≈ 下一版），与生产进程行为可能不同——这正是需要单步框架的原因之一：磁盘代码的报文行为应该由框架先验证，再择窗重启生产，避免"重启即上线未验证行为"。

### 4.6 engine-on 下路径 A 改写钩子失效【零触发的自增强因素，已证实】

`rewrite_ctx_recall_results`（MICRO off 时把客户端回的 ctx_recall error tool_result 改写为真实检索结果）的调用点在 `truncation.py:1159`——**stage 14 内部**。engine-on（生产现值）时 stage 14 被跳过 → 钩子永不执行 → 即使模型调用了 ctx_recall，也会收到永不兑现的 error result → 模型弃用该工具 → **强化零触发（自增强死锁）**。TC21 双路径对照实测证实：engine-off 变体改写成功（✅），engine-on 变体 error result 原样透传（❌）。修复方向：改写钩子上移至管线公共路径或 engine 发送视图组装处。

### 4.7 stage 17 OOM 内联裁剪路径缺 PDC 登记与摘要【框架首发新发现】

真实执行的 OOM 迭代裁剪在 `pipeline.py:2305`（stage 17 内联循环，日志 `OOM safety (iter N)`），该路径**既无 manifest 登记、也无结构化摘要/RECALL_CUE**；登记与 cue 的调用点全部位于 truncation.py（:1321 fifo、:1683 另一 OOM 实现、:1372 cue）——**实际走到的路径与登记/摘要代码不在同一处**。后果：以 OOM 相位裁掉的内容"丢了且模型不知道丢了"，直接削弱召回数据来源（触发链 B 环断）。TC11/TC17/TC19 红灯固化。修复方向：pipeline.py OOM 循环内补 record_dropped_messages + 摘要/cue（或收敛到 truncation.py 统一路径）。

### 4.8 框架建设过程中的实现侧发现（防复发）

- 代理存在 **2 秒 body-hash 请求去重层**（429 duplicate_request），单步测试同/近同报文需间隔或 nonce；
- 会话 key 取 `sid[:8]`——**测试会话命名必须在前 8 字符内唯一**，否则引擎 canonical 跨案例串账（首轮 t13 600KB body 曾毒化同 key 后续案例）；
- `ManifestStore._rotate_stale_file` 按**末行 ts** 判批次新旧，种子数据 ts 过旧会在首次读取时被轮转清空；
- 413 大小门在 `_do_dispatch`（管线后）而非文档所称"管线前"——引擎（stage 0.5）先于该门处理报文，超大报文会先溢出 500；
- lifecycle 相位由 **chars 阈值阶梯**驱动（PROXY_CLEAR_THRESHOLD→…→PROXY_CHARS_OOM_DANGER），`PROXY_OOM_SAFE_TOKENS` 不影响相位判定。

### 4.9 折叠纯文本消息不可恢复【TC22/TC23 快速验证的附带发现，2026-09-05】

engine-on 折叠把**纯文本消息**登记为 `h:`（内容指纹）锚——实测该类锚：① `recover_full_content` 只支持 `r:` 锚（h: 直接返回 None），**无全文恢复路径**；② 索引行 head 为空，召回结果只显示 "87 chars | h:xxx"，模型无法判断内容相关性；③ 同锚行重复登记 ×2。对照：**tool_result 内容（r: 锚）恢复链路完好**（archive 兜底 + 微轮回填，TC23 全绿）。后果：折叠掉的**非工具类文本**（用户结论、计划、中间推断）实际处于"登记了但取不回"状态。修复方向：h:/u: 锚接入 archive 全文检索，或折叠登记时为 text 单元补 head/orig。TC22/TC23/TC24 已固化（快速验证套件）。

## 5. 框架设计

### 5.1 组件与数据流

```
ctx_case_runner.py（单文件编排器）
  ├── 影子环境 build_shadow(tmp)：symlink 仓库根 *.py + configs/models.json，
  │      真实 logs/ 子目录 → 代理的 _SCRIPT_DIR/_DIAG_DIR/logs 全部落影子，与生产隔离
  │      ※ 不放 secret.local.conf、不 source 任何 conf → 云端物理不可达
  ├── 内嵌 mock backend（ThreadingHTTPServer 线程，:8100）
  │      POST /v1/chat/completions → 按 control steps 顺序返回（text / tool_calls），
  │      每次调用追加 capture JSONL（headers+body）
  │      GET  /v1/models → mock-local-35b
  ├── 受控代理（subprocess，:4100，stdout 重定向到套件日志文件）
  │      env = suite profile（§5.3）+ 案例级覆盖
  ├── 断言引擎：案例 expect → 对响应/headers/capture/影子 diag/日志逐条断言
  └── 报告器：per-case PASS/FAIL + 断言级 期望vs实际 表；失败案例归档问题报文
```

### 5.2 断言词汇表（报文结果点）

| 断言键 | 对象 | 说明 |
|---|---|---|
| `resp_status` | HTTP 状态 | 413 等边界 |
| `resp_header.<name>` | 响应头 | 路由归因四件套、队列头 |
| `resp_stop_reason` | 响应体 | tool_use / end_turn |
| `resp_tool_use_names` | 响应体 content | 透传的工具调用名（MICRO off 路径） |
| `resp_text_contains` | 响应体 text 块 | 最终回答包含召回线索等 |
| `backend_dispatch_count` | capture 条数 | 微轮=2，普通=1，413=0 |
| `backend_tools_last.contains/exact/max_count` | 最后一次 capture 的 tools | 过滤与注入的核心断言点 |
| `backend_model_last` | 最后一次 capture 的 model | 本地模型码正确性（异常 B 回归） |
| `backend_msg_count_last.max/exact` | 最后一次 capture 的 messages 数 | 窗口深度/折叠收缩 |
| `last_dispatch_contains` | 最后一次 capture 序列化文本 | anchor 标记、`[System: …]`、tool 结果内容 |
| `log_contains / log_not_contains` | 代理 stdout 日志 | stage 轨迹、`[MICRO_TURN]`、EPOCH、Tool filter 行 |
| `manifest_count_min.<sid>` | 影子 manifest 行数 | PDC 登记发生 |

### 5.3 套件（env profile）

| suite | 关键 env | 用途 |
|---|---|---|
| `prod` | ENGINE=on, PD=on, MICRO_TURN=off, FILTER_MAX=14, ROUTE_FORCE=local, QUEUE=off, HBE=off | 复刻生产参数基线 |
| `micro` | prod + `PROXY_PD_MICRO_TURN_ENABLED=true` | 微轮自答链路 |
| `engineoff` | ENGINE=off, KEEP=6, OOM_SAFE_TOKENS=800, PRE_TRUNCATE=5000, COMPRESS_MIN_CHARS=999999 | 经典截断/登记路径（关压缩保确定性） |
| `epoch` | ENGINE=on, EPOCH_TRIGGER_TOKENS=400, WINDOW_K=4 | 小预算触发折叠 |

代理按 suite 重启（每次 ~1.5s），suite 内案例顺序执行、会话 key 互异（`ctxcase<TC>`）。

### 5.4 隔离与副作用控制

- 影子目录使 `_DIAG_DIR`、snapshots、日志全部落在 tmp；生产 `logs/diag/` 零写入；
- 无 key、无 conf source → 云端调用物理不可能；`PROXY_ROUTE_FORCE=local` 双保险；
- mock 只监听 127.0.0.1 临时端口，runner 结束即销毁进程与线程；
- 已知残留：直启代理的 `log()` 会追加写 `/tmp/anthropic_proxy.log`（非 manage.sh 路径），与生产（repo logs/）不同文件，可接受。

### 5.5 报告与问题报文归档

- 输出 `logs/ctx_case/report-<ts>.json`：每案例的 suite env、断言期望/实际、capture 摘要；
- FAIL 案例额外落 `logs/ctx_case/fail-<tc>.json`：完整请求报文 + 最后一次 capture + 响应 + 相关日志行——即"问题报文"档案，可直接作为新案例素材回填案例集；
- 终态打印汇总表：案例 × 断言 × 期望/实际 × 红绿。

## 6. 案例集 v1

案例全规格（每案例六字段：测试环节/环节作用/正确行为/输入/期望输出/现状预判）见 **[ctx-single-step-case-set-20260905.md](ctx-single-step-case-set-20260905.md)**（TC01-TC21，含 G 组登记质量回归、H 组触发链路分段与"日志问题→环节→案例"追溯附录）。下表仅作索引。

预判列：✅预期通过；❌已知缺陷（红灯=暴露缺陷，修复后转绿）；❓TC01 判定异常 A。

| ID | suite | 场景 | 关键报文点 | 期望 | 预判 |
|---|---|---|---|---|---|
| TC01 | prod | **异常A复现**：25 个占位工具+短问句 | `log_contains "Tool filter: 25 ->"`；backend_tools last=ctx_recall 且 max_count≤15 | 过滤至 ≤14 + 末尾注入 ctx_recall | ❓ |
| TC02 | prod | **below_max 缺口**：5 个工具 | backend_tools.contains ctx_recall | 按注释意图：不过滤也注入 | ❌ §4.3 |
| TC03 | prod | 幂等注入：25 工具中已含 ctx_recall | backend_tools 中 ctx_recall 恰好 1 次 | 不重复注入 | ✅（依赖 TC01 判定） |
| TC04 | prod | **异常B回归**：header 强制 local | resp_header Target=local；backend_model_last=mock-local-35b；`log_contains "REQ_USAGE.*mock-local-35b"` | 归因=实际来源 | ❌ §4.2（REQ_USAGE 行预期错记 deepseek/glm 名） |
| TC05 | prod | aux 隔离烟囱：haiku 模型+tools=0 | dispatch=1；无异常状态码 | 不崩、正常透传（::aux-haiku 分域在日志的可见性另验证） | ✅ |
| TC06 | micro | **微轮自答全链路**：seed manifest（SECRET-CONTENT 锚点行）→ mock 第1步返回 ctx_recall 调用 | dispatch=2；第2次 dispatch 含 `role=tool` 且内容含 SECRET-CONTENT；`log_contains "[MICRO_TURN]"` | 自答→同请求重派→结果回填 | ✅ |
| TC07 | prod | MICRO off 透传：同 TC06 输入 | dispatch=1；resp_tool_use_names=[ctx_recall] | 调用原样给客户端（路径 A） | ✅ |
| TC08 | micro | 混合调用不自答：ctx_recall+Dummy 两个调用 | dispatch=1；透传 | 混有其他工具→不自答 | ✅ |
| TC09 | micro | 锚点直查：query=r:seed 锚点 | 第2次 dispatch tool 内容含种子 head | 锚点契约（E1 线上复证） | ✅ |
| TC10 | micro | 空库 hint：未 seed 的会话查任意词 | 第2次 dispatch 含"无匹配的已折叠单元"与存储概况提示 | 空结果线索防 churn | ✅ |
| TC11 | engineoff | **fifo 截断+PDC 登记**：30 轮标记性历史 | backend_msg_count ≤ ~8；manifest_count_min>0；last_dispatch_contains 摘要/anchor 标记 | 窗口生效+丢弃可寻址 | ✅ |
| TC12 | epoch | **小预算折叠**：80 短轮跨 S=400 | `log_contains "EPOCH"`；backend_msg_count < 输入轮数；manifest>0 | 一次性收编+登记 | ✅ |
| TC13 | prod | 413 边界：>500KB 报文 | resp_status=413；dispatch=0 | 管线前硬拒 | ✅ |
| TC14 | prod | engine-on stage 跳过 | `log_contains "[context_engine]"`；`log_not_contains "[content_compressor]"` | 6/7/14/17 由引擎接管 | ✅ |
| TC15 | prod | usage 计量存在 | `[REQ_USAGE]` 行存在且 input>0 | mock usage 透传 | ✅ |

案例集以 `tools/ctx_cases.json` 数据文件维护（payload/env 覆盖/seed/expect 全声明式），新增案例不改 runner。

## 7. 文件布局

```
tools/ctx_case_runner.py      # 编排器（影子环境/mock/代理/断言/报告）
tools/ctx_cases.json          # 案例集 v1（TC01-TC21，机器可读）
tools/eval_ctx_recall.py      # 离线召回评估（已落地，2026-09-05）
logs/ctx_case/                # 报告与问题报文归档（git-ignored）
docs/03-experiments-testing/ctx-single-step-case-debug-design-20260905.md  # 框架设计（本文档）
docs/03-experiments-testing/ctx-single-step-case-set-20260905.md           # 案例集规格（六字段/案例）
docs/03-experiments-testing/ctx-case-behavioral-extension-design-20260905.md  # 行为层扩展设计（B-suite，待评审）
docs/03-experiments-testing/system-behavior-testing-methodology-20260905.md   # 系统行为类测试评估方法论
```

## 8. 验收标准

1. `python3 tools/ctx_case_runner.py` 一条命令跑完 4 suite × 15 案例，全程 <2 分钟，结束时无残留进程/端口；
2. 生产栈（4000/8081）零请求、生产 `logs/diag/` 零写入（以生产 `/api/status` uptime 不变 + 影子目录有完整 diag 佐证）；
3. TC01 对异常 A 给出明确判定结论并回填 §4.1；
4. TC02/TC04 以红灯如实暴露 §4.3/§4.2 缺陷，报告含问题报文归档；
5. 同一 commit 重复运行结果一致（mock 序列化、会话 key、日志断言均确定性）。

## 9. 风险与边界

- **影子 `_SCRIPT_DIR` 判定**：~~依赖 abspath 不解析 symlink~~ **实测 symlink 在本机 Python 3.9 import 机制下被解析回真实路径，首轮 diag 泄漏进生产目录（已清理）；已改为复制 .py 方案**，冒烟验证影子落盘正常；
- **epoch/fifo 阈值敏感性**：est_tokens 估算与字符数相关，TC11/TC12 的 payload 规模留 2x 余量避免边界抖动；
- **`[MICRO_TURN]` 触发条件**：响应必须无内容块且全部调用为 ctx_recall——mock 第 1 步 `content=null` 严格满足；若代理对 null content 处理有分叉，案例会暴露它（也是有价值的结果）；
- **日志断言脆性**：`log_contains` 只锚定稳定标记（`[MICRO_TURN]`、`EPOCH`、`[REQ_USAGE]`、`[context_engine]`），不锚定措辞；
- **生产判读**：框架验证的是磁盘代码；生产进程 00:38 代码的行为差异（如异常 A）不能由框架直接复现，只能给出"新代码是否正确"的判定，生产进程处置（重启窗口）另行决策。

## 10. 后续扩展（非本期）

- 检索端按锚点去重 + 登记端 triggers 增强后，将 §4.4 的关键词式查询场景补为案例（需先可注入确定性 seed）；
- auto-recall（Stage 12.5）开启后的主动注入案例（依赖 `PROXY_AUTO_RECALL_ENABLED`）；
- queue 分桶/超时、路由 fallback_chain 的报文点案例（需多 mock 后端）；
- 与 `tools/gate_test_ctx_engine.py` 合并长程验收入口（gate 80 轮 + 本框架单步，互补）。

### 10.1 per-route 上下文管理豁免（`context_managed_by: proxy | client`，实现规格已定稿）

> 来源：-p 模式分层讨论（2026-09-05）定案——按路由区分而非一刀切：云端路由透传（客户端自管，避免双重管理与簿记错乱），本地路由代理管理（窗口错位下唯一防线，Claude Code 按 200K 认知而本地实用安全区远小于名义值——52K/轮即触发 517 次驱逐的实测）。数据佐证：透传为最佳产出条件（5822B，§4 相关性分析）；压缩栈信息税有实测价码。**本项同时推翻 AGENTS.md §11.3"云端压缩 aggressive"旧建议，实现落地时同步修订。**

**stage 豁免集（精确清单）**：

| 类别 | stage | `context_managed_by: client` 时 | 理由 |
|---|---|---|---|
| 内容改写类 | 0.5 引擎 / 7 压缩 / 14 截断 / 17 OOM | **豁免（跳过）** | 客户端自管，代理不改内容 |
| 横切观测类 | 诊断数据面 / IFC / PDC 登记 / R8 归因头 / TC04 归因 | **保留** | 不改内容，云端路由同为观测受益者 |
| 工具整形类 | 20 内 tool_filter + ctx_recall 注入 | **保留**（正交于上下文管理） | 归因与召回入口与窗口管理无关 |
| 兜底类 | `_do_dispatch` 413 门 / PROXY_MAX_REQUEST_BYTES | **保留** | 物理上限保护，与簿记无关 |

**实现挂点**：`configs/models.json` provider/model 层加 `context_managed_by` 字段（model_registry capabilities 机制扩展），管线入口按 `ctx._route_target` + 目录元数据计算豁免集；解析优先级 = **per-request 头 `X-Proxy-Context-Managed-By: client|proxy`**（对齐 X-Proxy-Route-To 模式，兼作 L1 验收注入点）**>** 目录元数据 **>** 目标默认（云端 client / 本地 proxy）；agent_go 零改动。配套项：本地 worker 注入 `DISABLE_AUTOCOMPACT=1`（**前置**：L1 epoch 案例全绿 + 413 兜底分支案例覆盖——客户端压缩关掉后代理回退链是唯一防线）。

**验收案例（可先行编写，红灯即规格）**：
- TC25：provider 标 `client` 的本地协议路由 → 断言日志无 `[context_engine]`、capture 的 messages 与客户端原报文逐字一致（含超阈长历史不被裁）；
- TC26：同报文 provider 标 `proxy`（现状）→ 断言引擎照常工作（回归保护）；
- TC27：横切类不豁免——client 路由下 manifest 登记 / R8 头仍存在。

**前置依赖与时机**：
1. pipeline.py / proxy_state.py / model_registry.py 的并行会话未提交改动（RECALL_CUE 区 +204/+24 行）先落地——本项改动与这三文件强交集，混改必撞车；
2. TC04 归因修复（同文件簇，小改动）随批同做，否则开关生效后按模型聚合的指标失真；
3. 建议与 OOM 登记缺口（TC11/17/19）、注入护栏误伤（TC01/02）合并为同一个"管线修复批"——一次重启窗口全部拾取，避免多次生产重启；
4. 重启门槛：修复批落地后 L1 全量跑，红灯 ≤ 已知不可修项，方可择窗重启生产（当前生产进程仍是 09-05 00:38 代码，磁盘积压已含 aux 修复等待拾取）。
