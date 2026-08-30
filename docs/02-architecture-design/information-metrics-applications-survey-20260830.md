# 信息面度量的推广与应用：从代理到 harness、wiki 与后训练（调研与设计综合）

> **状态**：设计研究记录（未实施）｜**日期**：2026-08-30
> **来源**：exp-1-amnesia 实验窗口期间的理论推演 + 两轮业界调研（主流 agent 上下文工程 / harness+RL 后训练）。
> **关联**：[IFC](information-fidelity-control-design-20260829.md) · [PDC](progressive-disclosure-context-serving-design-20260829.md) · [架构演进总览](context-architecture-evolution-20260829.md) · [熵改善研究计划(对话定稿,IFC-6)](docs/07-project-board/v0.7.0-information-plane.md)
> **一句话**：本会话建成的度量体系（ILE/retention/H_BE/D_ledger/压缩汇率）不止服务于本代理——它是 harness 算法的通用审计仪、任务分解的停止判据、以及 RL 后训练的奖励组件空位；本文记录这条推广线的推理与业界对证。

---

## 1. 框架对既有场景的解释力

### 1.1 wiki + opencode = 路线 D 的现实实现

| 框架概念 | wiki 对应物 |
|---|---|
| Cold 层（全保真） | `sources/` 原文页 |
| Warm 层（派生压缩） | `syntheses/` 综述页 |
| Hot 层 | agent 会话加载的几页 |
| **manifest** | `[[wikilink]]` 链接图 + 文件名 + MOC |
| ctx_recall | 读文件 / grep |

**wiki+agent 成功而"全库塞单会话"失败的原因被框架精确解释**：manifest 完整 + Cold 全保真 + 模型自决拉取三者齐备。跨会话（wiki/git）与会话内（本代理数据面）两层拼合才是完整记忆架构——恰好补上 PRD 搁置的 U1。

框架预测的四个 wiki 失效模式与处方：**综述漂移**（递归摘要噪声→git 为 canonical、综述为可重建派生层）；**陈旧综述低熵陷阱**（清晰地错→D_ledger 式定期对账）；**锚腐**（断链→manifest 覆盖率审计）；**更新时预算溢出**（opencode 天然按需读=自带披露）。

### 1.2 为什么云端 1M 有效而本地陷入错误循环

云端不是更强的同一游戏，而是**退出了有损域**（V=H，ILE 结构性为零）。本地错误循环的四机制：**①证据存活性**（错误的证据滑出窗口→无法自纠→重推导同错）；**②迭代替代容量→发散**（有损投影下不动点迭代不收敛——对摘要改摘要是发散形态）；**③验证经济学**（云端重扫 50 万 token 是秒级，验证成本改变理性策略）；**④能力×任务乘性放大**（框架外变量，诚实标注）。exp-1 的轮次膨胀观测（治疗 15+ 轮 vs 对照 8-9）即机制①②的直接证据。

## 2. 本地 harness 完成 wiki 任务的可行性设计

**核心原理：无损小步 + 结构性全局**——harness 不让本地模型模仿 1M，而是让每个操作的输入集完整进视图（操作级 V=H），全局一致性靠结构（链接图+引用锚+git）。

| 失效机制 | harness 对策 |
|---|---|
| 证据不存活 | 状态外置 git（append-only canonical），锚永存 |
| 有损迭代发散 | 编辑 ground truth 永远是原文（quote 级引用+机械校验），迭代信息随 commit 单调增长 |
| 验证太贵 | patch 式编辑 + diff/quote 匹配验证（零模型成本） |
| 能力差距 | 任务分级路由（下表） |

**任务分级**：机械可验证操作（断链/格式/索引）✅ 优于人工；范围化对账（论断×源引文）✅ 可行；局部综合 🟡 质量折扣；**原创跨源综合 ❌ 留给云端/人**（能力边界，harness 造不出洞察）。最小验证路径从断链修复+引用审计起步（零风险即刻有价值）。

## 3. 度量驱动的任务分解与算法设计

度量是三个控制器，不是仪表盘：

**① 分解的双停止判据**（递归拆解伪码）：容量判据（操作输入集 ≤ 预算 → 操作级 ILE=0）+ **熵判据**（操作前探针 H_BE > θ → 论断纠缠，再拆；熵随分解不降 → 撞能力边界，转路由）。

**② 双轴准入四象限**：H_BE 低×D_ledger 低 → ✅ 本地执行；低×高 → **清晰地错**（最危险象限）硬升级人工；高×低 → 再分解；高×高 → 云端。这张表就是"能力边界的经验发现算法"。

**③ Tier-0 触发器→恢复动作表**：reread_pressure↑ → 中止+无损重载重试一次；action_div 坍缩 → 幂等闸升级不三试；轮次膨胀超类基线 → 反馈调分解参数。批层：操作类别失败率 → 路由权重自校准（拉后即弃同款频率计数）。

**与 E2 的依赖**：θ_H 的可信度取决于 exp-1 判定——熵信号有效则其当分解判据主角，否则重心移向 D_ledger/机械验证。

## 4. harness 算法的审计与优化：损失空间坐标系

**任何上下文工程算法 = 损失空间一点**（丢什么：内容/锚 × 何时：主动/被动 × 可恢复性：无/有 manifest）。七家定位见 §5 表。由此推出的能力：

- **通用审计协议**（失忆实验的推广）：治疗变量换成任意算法对（compact 开关/摘要风格/子代理 vs 内联），统一测机制保真/信念响应(ΔH_BE)/结局，外加**压缩汇率**（每省 1K token 付出多少比特信念熵/D_ledger 偏差）——经验化率失真曲线，行业尚无。
- **免费审计已到手**：客户端 auto-compact 在本代理数据面呈现为 view_reset（今晨 53 次）——无需改客户端即可测各家 compact 的时机/损失量/前后信念变化/rationale 破坏。
- **三级优化闭环**：参数级（响应曲线调参，exp-1 即是）→ 选择级（生命周期条件化算法选择）→ 组合级（算法应用前后各探一次，运行时选信念代价最低变体）。
- **设计级**：度量梯度即改进方向——rationale_ratio 指保护对象、拉后即弃率直接测丢弃算法的假阴性代价（→保留先验）、D_ledger 高分内容进钉集合。

## 5. 业界调研 I：主流 agent 上下文工程落地（2026-08-30）

| Agent | 机制 | 框架坐标判定 |
|---|---|---|
| Claude Code | auto-compact + 服务端 compaction + JIT + 子代理 + 4 上下文工具 + /doctor | 路线 C，无 manifest |
| Codex | scratchpad+handoff 自动压缩，不可关不可回看 | 路线 C 封闭版 |
| opencode | 隐藏 compaction agent 生成结构化 checkpoint；子代理回摘要 | **结构化 checkpoint 是全行业离 manifest 最近的一步** |
| pi (badlogic) | 不压缩优先：精确控制+全程可检视进入上下文的一切 | **路线 D 唯一雏形**（控制输入而非压缩输出） |
| OpenClaw | 工作区目录记忆 + Active Memory（转录索引+混合检索注入） | 跨会话路线 D；矛盾：全量载入吃 60-80% 窗口 |
| Hermes (Nous) | 双压缩+prompt caching，85% 阈值中段摘要 | 路线 C + 成本视角亮点 |
| DeepSeek Harness | 全插件化（context 是插件）、local-first | manifest 可插入的开放接口 |

**三个发现**：①全行业压倒在路线 C，无一实现会话内锚保真+可召回；②**阈值之争（20%/85%/90%）是无度量的症状**——压缩汇率是行业缺失的量；③子代理交接损失无人量化。

**本框架可测性三级**：走 Anthropic 兼容 API 的全部客户端 = **全维度可测**（view_reset/ILE/ΔH_BE/D_ledger/verdict——今晨 53 次 view_reset 即首次被动审计的实证）；Codex（指向代理时）= 效果面可测机制黑箱；**pi+本框架 = 理念同源的最佳组合**（它面向人透明，我们面向算法透明）。

## 6. 业界调研 II：harness + RL 后训练方法论（五层）

1. **记忆工作流 RL**：MemAgent（ICLR'26 Oral，8K 训练→3.5M 外推<5%）、MEM1（近恒定上下文）——起点综述路线 A 成主流。
2. **上下文折叠 RL**：Context-Folding（Meta，ICML'26，FoldGRPO+密集过程奖励，10× 压缩）、Agent-Omit、AgentFold——训练模型在被折叠视图下工作。
3. **自我总结 RL**：ReSum×2、**Cursor self-summarization（首个生产级部署）**——路线 C 的训练化。
4. **API 级训练协同**：Anthropic context editing（模型专门训练以适应被清除的 tool result；单特性 +29%，+memory tool +39%）——harness 与模型共同设计的标杆。
5. **基础设施**：Kimi K2 / Tongyi DeepResearch（CPT→SFT→RL）/ MiniMax Forge / Verlog（400+ 轮）；RL 涌现上下文管理行为。

**本框架的三个位置**：①度量=这类训练缺的奖励/评估组件（H_BE 密集信号、D_ledger 精度轴防"自信压缩器"、压缩汇率为 RL 代价项）——**exp-1 的 E2 判定正在测的正是这个信号的效度**；②本代理数据面=rollout 免费仪器（过程观测随产出）；③本地 35B 的下一代组合：FoldGRPO 式后训练 + 本代理 rollout 环境 + 本度量奖励，为自有 harness 机制训练自有模型（Anthropic 范式的本地翻版）。

## 7. 路线图影响（v0.8 候选，均以 E2 判定为前提）

- 跨 harness 压缩审计实验（同一任务集横向跑 Claude Code/opencode/Hermes 的 compact，出各家压缩汇率与 D_ledger 残留）
- 子代理交接保真测量（父上下文对子工作 D_ledger）
- wiki harness 最小验证（断链修复+引用审计起步）
- 本地模型后训练评估项（若 E2 成立）

## 8. 主要参考

业界上下文工程：Anthropic [有效上下文工程](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) · [compaction](https://platform.claude.com/docs/en/build-with-claude/compaction) · [context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing) · OpenAI [compaction](https://developers.openai.com/api/docs/guides/compaction) · [badlogic 三代理压缩研究](https://gist.github.com/badlogic/cd2ef65b0697c4dbe2d13fbecb0a0a5f) · [三 CLI 对比](https://justin3go.com/en/posts/2026/04/09-context-compaction-in-codex-claude-code-and-opencode) · [opencode compaction](https://opencode.ai/v2/docs/compaction/) · [pi 构建心得](https://mariozechner.at/posts/2025-11-30-pi-coding-agent/) · [OpenClaw Active Memory](https://docs.openclaw.ai/concepts/active-memory) · [Hermes 压缩与缓存](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/context-compression-and-caching.md) · [20% vs 90% 之争](https://levelup.gitconnected.com/why-i-compact-my-ai-coding-agent-at-20-not-90-1a665c0c4e5d)

后训练：[Context-Folding](https://arxiv.org/abs/2510.11967) · [MemAgent](https://arxiv.org/abs/2507.02259) · [MEM1](https://arxiv.org/abs/2506.15841) · [ReSum-搜索](https://arxiv.org/abs/2509.13313) · [ReSum-RLVR](https://arxiv.org/abs/2606.13316) · [Cursor self-summarization](https://cursor.com/blog/self-summarization) · [Kimi K2](https://www.alphaxiv.org/overview/2507.20534) · [Tongyi DeepResearch](https://arxiv.org/html/2510.24701v1) · [MiniMax M2](https://www.alphaxiv.org/abs/2610.10304) · [Verlog](https://neurips.cc/virtual/2025/128043) · [RL 涌现行为](https://arxiv.org/abs/2510.24585)
