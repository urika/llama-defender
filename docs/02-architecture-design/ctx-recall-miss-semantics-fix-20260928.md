# ctx_recall miss 语义修复（L-53）——设计规格与激活说明

> **状态**：代码完成（2026-09-28），**待代理重启激活**（批期红线 1 禁重启；排 EXP-8 收官窗）
> **归属**：D3 上下文管理（召回交互层）；登记=swe-eval roadmap **L-53**（P1）
> **出典**：2026-09-27 EXP-8 6cc97447 会话（run 202432）recall-miss 事故——
> 用户 sent_view 取证 + swe-eval 会话 `s-20260927-c98dab` 独立复核（内容自 turn 24 起
> **连续 119 轮在视图**；模型 ctx_recall 重复调用 7 次〔同 query dup 1/2/3〕+
> 同目标重读 55 次〔Grep×35 / Bash grep×20〕，miss 后零回头翻窗）。

---

## 1. 问题（三层，源码级定位）

| # | 层 | 事实 | 定位 |
|---|---|---|---|
| 1 | 窗口可见性 | 241K chars / 145 条消息的含视图，注意力稀释下模型找不到目标，**误判"被折叠"**（理性误判——它无法验证） | 结构性问题，非本次修复范围 |
| 2 | 提示词契约 | `RECALL_CUE` 旧文写 **"Recall first; re-read only if ctx_recall returns nothing"**——priming 召回为第一反应，且 miss 后重读是 cue **明文授权**的 fallback；同 cue 内 IFC-11 又写 "if recall finds nothing, say explicitly that the information is no longer available"——**两条 post-miss 指令互斥**，另一种解读会产出"假不可得" | `ctx_recall.py` `RECALL_CUE` |
| 3 | miss 文案 | "该信息可能从未被丢弃，或在本会话开始前"——hedged 猜测、无可执行指引；而 ctx_recall **只检索已折叠单元**（`memory_stores.MANIFEST`），miss 的严格语义（=从未被折叠）本可判定 | `format_recall_result()` 空分支 |

## 2. 修复

### P1 契约/文案（本件）
- **`RECALL_CUE` 重写**：删 "Recall first"（订单反转）→ **"Order of effort: (1) 先查当前上下文（未折叠内容仍在窗口）；(2) ctx_recall（窗口缺失的内容）；(3) 重读文件仅作最后手段"**；IFC-11 加前置条件——**"仅当 recall 未命中 且 窗口核验确认不在当前上下文"**才可宣称不可得（消除矛盾，保留防编造意图）。
- **miss 文案三段式去 hedge**：按 `window_hint` 三态——
  - 命中窗口：`**该内容仍在你的当前上下文中**（第 N 条消息附近／共 M 条）——请直接使用上下文中的原文，勿重读文件。`
  - 确认不在：`**不在你的当前上下文中**——折叠库亦未命中：先换关键词重试一次 recall；仍无再重读/检索（或该信息确实不可得，勿编造）。`+ 存储概况线索（保留 S1）
  - 不可判（无视图）：`ctx_recall 只检索**已折叠**内容——未命中说明它从未被折叠：…先在窗口内查找，勿直接重读文件。`+ 存储概况线索

### P2 窗口在场核验（本件）
- 新纯函数 **`window_lookup(messages, query)`**：查询 token（`_query_tokens`，≥3 字符）**过半命中**同一消息即计命中；返回 `{found, index, total, hits}` 或 `None`（不可判）。
- **数据源=折叠后视图**：engine-on 路径中 recall 结果改写发生在 MessageConverter stage（`pipeline.py` ~2924），而 ContextEngine stage 0.5 已把折叠后视图写回 `ctx.messages`（`pipeline.py:712`）——**调用现场的 `messages` 即模型将看到的同一视图**，无需读档案；engine-off（truncation 路径）同理取截断后视图。
- 接线：`content_compressor.rewrite_ctx_recall_results()` 在 `results` 为空时计算并传入 `window_hint`；`format_recall_result(..., window_hint=None)` 保持向后兼容（其余调用方零改动）。

## 3. 验证与测试

- 新增 7 例（`test/unit/test_ctx_recall.py::TestWindowLookupL53`、`test/unit/test_recall_cue.py::TestCueContractL53`）：
  命中/不在/不可判三态、三段式文案、**回归锚**（`"可能从未被丢弃"` 不得复现、`"Recall first"` 不得复现、IFC-11 前置条件在位）。
- 全量单测 **1783 passed / 0 failed**（2026-09-28）。
- **待激活后实证**（验收挂钩 swe-eval 侧）：同参数重读率（roadmap L-15 v2 指标）在同类会话上下降；miss 后"翻窗优先"行为可于 run 台账核对。

## 4. 激活与约束

- **激活=代理重启**（本件为 Python 代码，`manage.sh reload` 不重载模块；批期红线 1 禁重启）。
- 落地走**三件套**（AGENTS.md 红线 1 增补条款）：公告 + OBSERVATION 落档 + 一次窗一次戳。
- 行为类变更（层间接口契约 §5.x）：激活后同批/后续批如做对照，按 A/B 纪律；`RECALL_CUE` 变更同时改变 A/B 两路注入文本（唯一源头，字节一致不变）。
- 不做（本次范围外）：给模型做"内容在窗口哪一条"的稳定寻址语言（当前只给序号提示）；把窗口核验扩展成通用检索服务。
