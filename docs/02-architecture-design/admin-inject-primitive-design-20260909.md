# Admin 注入原语设计（/admin/inject + DEF-311 钉住扩展）

> **状态**：已接受（Track A 实现依据）
> **日期**：2026-09-09
> **上游契约**：swe-eval `docs/adr/ADR-013-local-cloud-combo-exp6.md` §9.0（T0 冻结稿）
> **任务编号**：ADR-013 T1（注入原语）+ T9（钉住正则扩展），Track A / llama.cpp 仓
> **本文档相对 T0 契约的补强**：评审发现 §9.0 四处缺口（注入持久化路径未点破、
> admin 鉴权无既有模式、无投递确认、无注入卫生约束），本文档给出冻结决策，
> 差异见 §8 与 ADR-013 的对照，需 Track B 知悉但不需要契约重冻结（均为代理内部
> 实现语义与防御性约束，端点请求/响应形态不变）。

---

## 1. 需求与边界

ADR-013 的 M2/M3（卡死升级/验证触发）需要 harness 在 run 进行中向指定会话注入
云端处方。代理侧职责（机制，无任务语义）：

- 提供 `POST /admin/inject`：按 session_key 排队注入块，该会话下一次请求转发前
  把文本以 `<{tag}>…</{tag}>` 包裹追加到 user 尾部；
- `once=true`（v1 唯一支持值）：注入块写入发送视图/canonical 后清除队列项；
- 失败语义 fail-open：会话不存在 → 404；代理重启 → 队列静默清空；
- DEF-311 钉住正则扩展 `<cloud-consult>`（T9），注入块跨折叠存活。

**非目标**：不承载 consult 策略（预算/触发/处方解析全在 swe-eval）；不支持
广播/多会话注入；不做注入内容的 LLM 加工。

## 2. 端点契约（与 T0 一致，括号内为补强）

```
POST /admin/inject
{"session_key": "<全量 key>", "tag": "cloud-consult",
 "text": "<处方全文>", "once": true}
```

- 200 `{"ok": true, "queued": <该会话待注入数>, "session_key": ...}`
- 400 缺字段 / tag 非法 / text 非字符串
- 401 `PROXY_ADMIN_TOKEN` 已配置且请求未携带匹配凭证（见 §4，**新增**）
- 404 会话未知（引擎/台账均无此 key；调用方按 fail-open 处理）
- 413 text 超 `PROXY_INJECT_MAX_CHARS`（默认 4000；**不截断、拒绝**——截断的
  处方比没有更危险，调用方重发精简版）

注入在 `do_POST` 中的位置：`/admin/reload` 同款前置分支（`_check_dedup` 之前）——
重复注入相同处方是合法用法，不应被去重窗口 429。

## 3. 注入语义（评审问题 #6 的冻结决策）

### 3.1 双路径

| 引擎状态 | 行为 | 持久性 |
|---|---|---|
| engine-on（EXP-6 口径） | 注入块 append 进 canonical（`_proxy_injected` 标记的冻结消息），本轮视图尾部同现 | **跨轮存活**，折叠时经 T9 钉住延续 |
| engine-off（生产默认） | 仅 append 到本轮 `ctx.messages` 尾部（view-only） | 单轮有效，下轮客户端历史不含即消失 |

engine-off 不退化为错误：view-only 注入对"下一动作即生效"的处方已够用；
跨轮持久是 engine-on 的增量价值。ADR-013 补一句口径说明即可。

### 3.2 canonical 写入的连带处理

注入消息**不进** `sent_set`/`sent_order`（客户端永远不会重发它）：

- `absorb` 尾部失配检查（`context_engine.py:336`）不受影响——`sent_order[-1]`
  仍是最后一条客户端消息指纹，无假 mismatch WARN；
- `_sync_order_after_collapse`（`:502`）的 keep 计数排除 `_proxy_injected`
  消息，保持 sent_order 尾部对齐口径不被注入块稀释；
- 注入点在 stage 0.6（`AdminInjectStage`，紧随 ContextEngineStage），在
  `maybe_epoch` 之后 append——不参与本轮容量判定（注入有 4000 字符上限，
  过冲有界），但保证落在 K 窗口尾部、本轮视图与 canonical 顺序一致。

### 3.3 消息形态

末条已是 user（tool_result 轮）时**合并进末条 user 消息的 content 列表**
（追加 text block），否则新建 user 消息——避免 consecutive user 触发后端
模板/配对异常。AutoRecallStage 的尾注先例（`pipeline.py:2187`）同构。

### 3.4 drain 时机与 once 语义

队列项在**写入发送视图/canonical 成功时**清除（BackendDispatcher 之前，确定性
时点），不等后端成功——后端失败导致客户端重试时，engine-on 路径注入已在
canonical 自然重放；engine-off 路径注入消失属 §3.1 已声明语义，调用方凭
投递确认（§5）决定是否重发。

### 3.5 隔离

- `::aux` 后缀会话（aux-haiku / aux-strict 隔离域）不注入——辅助请求不吃处方；
- 每会话待注入队列上限 8 条（超出 429），全局会话数上限 128（FIFO 驱逐，
  复用 `_AUTO_RECALL_STATE` 治理模式）；
- 代理重启队列清空（内存态，不持久化——T0 已声明静默清除）。

## 4. 鉴权（评审问题 #3 的冻结决策）

现状：全部 admin 端点仅靠 127.0.0.1 绑定，**无 token 先例**。注入端点能直接
操纵模型行为，风险高于 reload，决策：

- 新增 `PROXY_ADMIN_TOKEN`（默认 `""`，reloadable）；
- **默认空 = 与既有 admin 端点对齐（localhost-only），行为零变化**；
- 配置后 `/admin/inject` 要求 `Authorization: Bearer <token>` 或
  `X-Admin-Token: <token>` 匹配，不匹配 401；
- v1 只罩 `/admin/inject`（新机制最小面）；是否推广到既有 admin 端点另议。

## 5. 投递确认（评审问题 #10 的冻结决策）

注入落视图时调用 `diagnostics.record_injection("admin_inject", detail={
"tag", "chars", "engine_persisted"})`——复用 R13 既有归因通道：

- 非流式：`X-Proxy-Feedback-Injected: admin_inject` 响应头即投递凭证；
- 流式：SSE 尾注 `: x-proxy-diag` 同理携带；
- R16 `sessions.jsonl` 落 `injection_details.admin_inject`（tag/chars/是否
  进 canonical），swe-eval 的 cloud_consult 口径字段可与之交叉核验。

200 仅代表入队；投递凭证以响应头/R16 落盘为准。无新增查询端点。

## 6. 注入卫生（评审问题 #9 的冻结决策）

云端处方是不可信输入，注入前消毒：

1. 长度上限 `PROXY_INJECT_MAX_CHARS`（默认 4000）——超限 413 拒绝（§2）；
   保护 DEF-311 钉住预算（8000 字符）不被单个巨段挤占；
2. 标签消毒：text 内的 `</?{tag}>`、`</?system-reminder>`、`</?test_env>`
   序列的尖括号替换为全角 `＜＞`——防包裹解析断裂与钉住去重污染；
3. tag 白名单校验：`^[a-z][a-z0-9-]{1,31}$`（契约内 tag=cloud-consult；
   其他合法 tag 可用但只有 cloud-consult 进 T9 钉住正则——钉住是标签级
   白名单而非通配，防任意标签蹭钉住预算）。

## 7. T9：DEF-311 钉住正则扩展

`context_engine.py:632-635` 的 `_pat` 增加 `|<cloud-consult>.*?</cloud-consult>`。
去重/预算/跨折叠延续语义全部复用，无新机制。注意钉住提取面是 canonical
被收编轮次——只有 engine-on 路径（§3.1）的注入块进入该面，与预期一致。

## 8. 与 ADR-013 §9.0 的差异对照（需 Track B 知悉）

| 项 | T0 契约 | 本设计 | 影响 |
|---|---|---|---|
| 鉴权 | "复用 admin 既有鉴权模式" | 既有模式不存在；新增可选 PROXY_ADMIN_TOKEN，默认空=localhost-only | Track B 默认无感 |
| 投递确认 | "凭返回码决定是否重试" | 200=入队；投递凭证=X-Proxy-Feedback-Injected 头 / R16 落盘 | Track B 可升级为凭证驱动 |
| 超长 text | 未规定 | 413 拒绝（不截断） | Track B 自控处方长度 ≤4000 |
| 注入持久性 | "DEF-311 钉住保证跨折叠存活" | 仅 engine-on 路径；engine-off=单轮 view-only | EXP-6 引擎开，无影响 |
| once 清除时点 | "注入后清除队列" | 写入视图/canonical 即清除（不等后端成功） | 语义明确化 |

## 9. 实现映射

| 文件 | 改动 |
|---|---|
| `proxy_state.py` | `_ADMIN_INJECT_QUEUE`（dict[key, list]）+ `_ADMIN_INJECT_LOCK` + 上限常量；`PROXY_ADMIN_TOKEN` / `PROXY_INJECT_MAX_CHARS` 读取 |
| `proxy_config.py` | CONFIG_REGISTRY 注册上述两个变量（reloadable） |
| `context_engine.py` | T9 正则扩展；`CanonicalSession.append_injected(text, tag)`；`_sync_order_after_collapse` 排除 `_proxy_injected` |
| `pipeline.py` | `AdminInjectStage`（stage 0.6，ConditionalStage）：队列非空才运行；消毒/包裹/合并/双路径注入/record_injection |
| `anthropic_proxy.py` | `POST /admin/inject` 前置分支（dedup 前）+ `_handle_admin_inject()`（鉴权/校验/404/429/413）；管线注册 0.6 |
| `test/unit/test_admin_inject.py` | 新增：入队/once/404/413/429/鉴权/消毒/合并/engine-off view-only/aux 跳过 |
| `test/unit/test_context_engine.py` | 扩展 DEF-311 测试：cloud-consult 段跨折叠钉住存活 |
| `AGENTS.md` / `CLAUDE.md` | 端点表 + 配置参数 + 管线 stage 0.6 同步 |

预估：代理侧实现 ~150 行（不含测试），符合 ADR-013 任务表。

## 10. 验收

- T1 单测：注入 → 下一发送视图含块 →（engine-on）fold 后仍存 → once 清除；
- T9 单测：`<cloud-consult>` 段折叠后出现在 `[policy pinned]` 块、去重生效；
- `bash test/run_tests.sh --unit` 全绿；`--signature` / `--snapshot` 无漂移
  （新 stage 属新增节点，快照按工具流程更新）；
- **红线 1 兼容**：代码化合入但不重启代理；EXP-3 v2 批次期集成验收顺延至
  收官重启窗口（T10 联调）。

## 11. 风险

1. engine-off 单轮语义被误用为跨轮承诺——文档与 ADR 对照表已声明；
2. 注入块占钉住预算与政策段竞争——4000 上限 + 钉住区 8000 预算双闸门，
   极端情形以 DEF-313 WARN 观测面兜底；
3. `PROXY_ADMIN_TOKEN` 默认空意味着任何本机进程可注入——与既有 admin 面
   同级风险，生产如敏感由部署方配置 token。
