# Jev 调研交叉分析补充（Kimi 报告 × 本仓调研，20261013）

> 状态：参考资料（非设计文档）。对象：外部调研报告
> `/Users/jinsongwang/Documents/kimi/tasks/2026-10-13/13-43-23-dd157983/Jev模型调研报告.md`
> 与本仓 2026-10-13 调研的交叉分析，只收录**增量素材**与**证据等级标注**。
>
> 本仓已有文档：使用策略与开源选型 → `docs/06-reference-metrics/jev-mcp-tool.md`
> §使用策略；级联流程设计 →
> `docs/02-architecture-design/decision-cascade-rules-jev-escalation-20261013.md`。
> 本文不重复其内容，只补充。

## 1. 交叉验证结论（两份独立调研一致的部分）

两份调研独立得出、可直接采信（引用时无需再注出处争议）：

| 结论 | Kimi 报告 | 本仓调研 |
|------|----------|---------|
| 三原语 + 概率输出，代码持有最终控制权 | ✅ | ✅ |
| 与 LLM 是协作不是替代（高频闭集判断 vs 开放生成） | ✅ | ✅ |
| **高风险必须保留确定性规则与人工回退，Jev 输出只是分支条件之一** | §7.3 | 级联设计 Tier 0 + 抽审计 |
| 校准（ECE/Brier）比 top-1 准确率更重要 | §7.2 | 使用策略「另一半」 |
| 无关内容不进 state；一题一问；先自有数据实测再谈替换 | §5.3 | L0 判据工程 + 本地校准 |
| 生态规模：161+（typesafe-jev.com 目录）/ 155（awesome-jev）/ 2,170 公开项目（arXiv 调研） | ✅ | ✅ |
| 开源选型看训练集多样性与校准证据，不是接口兼容 | —（弱提） | 使用策略第三条 |

## 2. Kimi 报告的增量素材（本仓文档此前未覆盖）

### 2.1 RSI-Jev：递归自我改进的完整范式 ⭐（对两仓最有迁移价值）

> **验证结果（2026-10-13 检索，已核 HF 模型卡与 GitHub）**：项目实存
> （Shanghua-Gao/RSI-Jev + shgao/rsi-jev-v6.1-vl-{4b,27b}）。
> **纠错：底座是 `Qwen/Qwen3.8-27B`，不是 Kimi 报告所写的 Qwen3.5-Base**；
> 27B 答案头在第 48/56/64 层（4B 为 16/20/32，未逐一核对）。

Shanghua Gao 的 RSI-Jev：13 天 8 个版本、496 个实验 Arm 全公开（含全部失败）、
`pip install "rsi-jev[fast,vision]"` 起本地 Jev 兼容服务。
**已核实**：v6.1-VL-27B 在 Decision Index 0.3 公开集 65.64（`--effort auto`，
140,178 请求、coverage 1.0、训练集重叠行剔除后分数不变）——**官方完整评分
submission pending**；权重为两个 Qwen3.8-27B 微调的权重平均（印证原则⑧）；
27B 较 4B 各集更准但校准略差（ECE 0.064 vs 0.048）。
**Decision Index 对照口径已可互证**：社区榜（eesel 等转述）Jev 57.9、Clef
自报 61.2——与 Kimi 报告「官方 Jev 57.96」一致，但 **RSI-Jev 65.64 超 Jev
系作者自报，尚无第三方复测**。证据强度高的原因是可复算（权重+实验全公开），
而非独立复测。

**环路四机制**（可迁移范式）：

1. **EDITABLE / PROTECTED 分离**：模型/数据/训练代码可改（EDITABLE）；
   评测代码/指标/测试数据 PROTECTED——「Agent 可以改模型，但不能改考试规则」
2. **预注册文化**：假设先登记预期提升再开跑，证据说话时亲手退役自己的冠军
3. **Guard Metric + 留出集**：达标进 Champion 路线，失败逐个写明原因归档
4. **失败回流**：上一轮（含失败）实验结果影响下一轮研究方向

**496 个实验沉淀的 8 条训练原则**（对建模者；④⑤ 对评估设计直接可用）：

1. 在基准自己的训练集上微调会被「拒绝」（刷分损伤通用知识）——补 15% 通用知识回放解毒
2. 「冻结」不如「慢学习率」：底部 8 层 LR 降到 1/10，两头兼顾
3. 列表级重排奖励（NDCG@5 + KL 锚定）是第一个真正起作用的 RL——重排 R@1 +60%
4. **让「自信的错误」付出 4 倍于「胆小的正确」的代价**——准确率与校准同时改善
5. **想让它会说「不知道」，数据里就得有该说不知道的题**（歧义题 0.679 → 0.891）
6. 读出细节是隐藏大分：选项池化自己的 token 而非分隔符（0.383 → 0.753）
7. 短而干净胜过长而脏；分布内调好的策略会过早停
8. 同底座双微调权重平均（0.5/0.5）胜过两个母模型（4B/27B 均成立）

**多层答案头 + effort 分档**的架构洞见：决策能力在 Transformer **浅层就已成形**
（多数任务第 16 层已不输第 32 层）——「简单任务快、困难任务深」。

### 2.2 LLM2Jev：免训练改造路线（与 RSI-Jev 相反的哲学）

> **验证结果**：论文实存——arXiv:2610.02076《LLM-as-Jev: LLMs Are Already
> Jev-Style Decision Models—When and How to Fine-Tune Them》。

论文 + 开源实现。架构不变、免训练优先——Qwen3.5-4B 免训练即可**匹配同底座
社区 Jev 式模型**（论文口径）；提供 training-free 推理配方 + 微调目标
（tree-factorized listwise loss + KL 锚定）；已支持多模态零样本。
「够用线可低至 81.4%」的**具体数字未在论文摘要/检索片段中直接见到**，
引用前需回原文核对。适合快速把现有 LLM 决策接口化的对照实验。
与本仓 SemIf/Rizzo/AnyJev 同属「零训练读 logits」路线（AnyJev 已达 1.1k★）。

### 2.2b 验证期间发现的重大生态事件（Kimi 报告仅列为目录条目，实为重要）

| 事件 | 日期 | 要点 |
|------|------|------|
| **Microsoft-Decision-1** | 2026-10-09 | 微软入场：Qwen3.5-9B 后训练，Microsoft Foundry + OpenRouter 可用，$0.042/M（与 Jev 同价）、32K 上下文；微软自报 36 基准 83.5% 准确率/85ms，**声称超过 Jev 1.13**（vendor claim，基准为微软自选） |
| **Cloudflare Clef / Clef-flash** | 2026-09 下旬 | 开放权重 **Apache-2.0**（HF 可得）、**Jev API 完全兼容**、原生图像输入（Jev 仅文本）；Workers AI 托管 $0.24/$0.09 每百万输入 token；JevBench 0.762；威胁情报团队用于网站分类（2.2s vs gpt-oss-120b 4.7s）；官方坦承单遍设计「复杂问题显著弱于推理模型」 |
| OpenAI Decisions API | 传闻中 | 已被多方提及但未定价发布 |

**选型格局变化**：「托管高精度（Jev）vs 开源自托管（Clef/RSI-Jev/Kev）」之上
新增「大厂托管低价（Microsoft-Decision-1）」一档；接口兼容已成行业事实标准
（Jev/Clef/Microsoft/RSI-Jev/Ollaya 均说 `/v1/systemone` 方言）——
**换底座成本趋零，本仓使用策略第三条（看训练集多样性与校准证据）更加成立**。

### 2.3 官方四大架构模式 + 落地经济性

> **验证结果**：四模式命名已核实——datawhalechina/jev-cookbook 第 3 章明确
> 覆盖 speculative fan-out / confidence gating / composite scoring / intent routing
> 四模式。批问经济性（12.2×/10×）与重排数字（5%→18%）为官方配方口径，
> 未见第三方复测，引用保持 vendor claim 标注。

| 模式 | 说明 | 本仓对应 |
|------|------|---------|
| 置信度门控路由 | >90% 自动 / 60–90% 调 LLM / <60% 人工 | 级联设计 Tier 1 三带 |
| **投机式扇出（Speculative Fan-out）** | 一次并行问多题，高置信直接用 | swe-eval C 通道批问的理论背书 |
| 组合评分 | 多 Score/Noul 组成复合决策，组合逻辑在代码 | —（新增） |
| 意图路由 | 请求 → 工具/工作流映射 | —（新增） |

**批问经济性实测**（官方配方，vendor claim）：13 个问题打包一次调用，便宜
12.2×、快 10.0×——并行提问几乎不加时延（本仓 jev_quota 台账同向：单次
≈0.000037 credit）。**工具规模法则**（社区经验，未验证出处）：<15 工具 LLM
函数调用够用；15–50 加嵌入路由；>50 需专门分类器——Jev 填这层。

### 2.4 数据分析决策流的可复用抽象

```
原始数据 → Jev 初筛/分类（高置信自动通过）
 → 中置信：聚合后人工抽样复核
 → 低置信/新颖样本：升级 LLM 或分析师
 → 全部结果落库（概率、阈值、版本、真实结果）→ 反哺阈值调优
```

带实测锚点：RAG 重排 top-1 5%→18%、top-10 38%→62%；引用核查发现幻觉；
SEC 年报 75 行业组按置信度上报细类/回退粗类；pg-jev 在 Postgres SQL 内做
语义判断。此抽象与级联设计 Tier 结构一致，**增量在于「落库反哺」环节的显式化**
——概率/阈值/版本/真实结果四元组必须持久化，否则阈值调优与漂移监控无米下锅。

### 2.5 其他增量

- **采用速度（已核实，多源一致）**：上线 24 小时触达 Vercel AI Gateway
  近 13% 付费团队（GPT-5.6 家族 2×、Fable 5.1 6×，其余模型 24h 均 <7%），
  18 小时破 10%——**重要口径注记：窗口期内 Jev 在网关上免费（促销至
  9/25），该数字含促销因素**。附带发现：Vercel 自家把 Luna 5.6 安全分类器
  换成 Jev 后提速 5–18×（厂商口径）；三天内 Cloudflare/LangChain/Langfuse
  完成接入。
- **中文学习路径（已核实）**：`datawhalechina/jev-cookbook`（Datawhale，
  2026-09-20 建仓，11 章 Notebook + 18 篇实战配方 + 模型评测 + 用 RLCD
  微调 Laya 中文全流程）——自训路线门槛最低的入口。
- **SDE 级联模式**：mini 模型初抽 → Jev 验证 → reasoning 模型复核
- **代码评审分流 / CI/CD 决策门**：PR 并行判断（安全敏感/缺测试/变更类别）；
  构建失败原因 Choice 分类决定重试/回滚/升级——与 agent_go 落地点 §4.2 同构

## 3. 证据等级标注（引用纪律，已按 2026-10-13 检索更新）

| 素材 | 验证结果 | 引用时注意 |
|------|---------|-----------|
| RSI-Jev 权重/实验/训练原则 | **实存，可复算自报**（HF 模型卡+GitHub 已核） | 引用注明「作者口径，官方评分 pending」；**底座是 Qwen3.8-27B（Kimi 报告写 Qwen3.5 有误）** |
| Decision Index 0.3：RSI-Jev 65.64 vs Jev 57.9 | 双方分数均可互证（HF 卡 + 社区榜转述 Jev 57.9）；**65.64 超 Jev 系作者自报** | 与本仓 OneJev「89.3 vs 89.0（354 官方题）」**是不同基准，勿混算** |
| LLM2Jev | **实存**（arXiv:2610.02076），免训练路线论文确认 | 「81.4%」具体数字未在检索片段直接见到，回原文核对后再引 |
| Microsoft-Decision-1 | **实存**（2026-10-09 发布，Qwen3.5-9B 后训练，Foundry+OpenRouter） | 83.5%/超 Jev 为微软自选基准的 vendor claim |
| Clef / Clef-flash | **实存**（Cloudflare，Apache-2.0 开放权重，Jev API 兼容，图像输入） | 61.2/0.762 等为自报或社区榜，官方坦承复杂问题弱于推理模型 |
| Eve / StartLux / Gero-4B | **仍未独立验证**（typesafe-jev.com 目录条目，本轮未逐一核 GitHub） | 引用前先核实存与许可证；另有 Strands Decider 2B（开源，Kimi 报告未提）可关注 |
| Vercel 13% 采用率 | **多源核实**（Forbes/TNV/daily.dev 等一致） | **窗口期网关免费（促销至 9/25），数字含促销因素**，须一并注明 |
| 重排 5%→18%、批问 12.2×/10×、SEC 75 行业组 | 官方配方口径，未见第三方复测 | 标注 vendor claim |
| 工具规模法则（<15/15–50/>50） | 出处未定位 | 标注「社区经验，未验证出处」 |

## 4. 对 swe-eval / agent_go 落地的增量启示

1. **EDITABLE/PROTECTED 与既有治理的映射**（Kimi 报告最大启发）：
 - swe-eval 已有**预注册文化**（`jev-efficacy-preregistration` / `jev-slots-preregistration`）
 与冻结基线（ARS-1/AC），agent_go 已有 `eval metric-freeze` 不可变 KPI
 ——RSI-Jev 的「改模型不改考试规则」是同一纪律的独立验证，**不需新造概念，
 只需在 C 通道/评审链路落地时显式引用**；
 - 级联设计 §4 的 shadow-then-effective 纪律与 PROTECTED 同源：阈值/抽审比例/
 评测集都属 PROTECTED 面。
2. **原则⑤直接改进拒答设计**：校准集里必须混入「该说不知道」的歧义题
 ——本仓级联设计 §3.3 的三率监控可据此补一条：**校准集构成要含歧义/新颖
 样本，否则拒答带标定失真**。
3. **原则④可操作化**：损失函数视角（自信错误 4× 代价）在不训模型的场景
 的等价物 = **抽审预算向高置信带倾斜**（假通过的代价 >> 假拒答，抽审 ρ
 对 p≥0.95 带更高而非均匀）。
4. **投机式扇出**为 swe-eval C 通道批问与 agent_go review 初筛（diff +
 验证结果多题并行）提供官方模式名与经济性数据（12.2×/10×）。
5. **落库反哺四元组**（概率/阈值/版本/真实结果）应写入两仓落地验收：
 agent_go `review` 初筛、swe-eval 归因台账都要持久化这四元组，
 支撑后续阈值调优与漂移监控。

## 5. 大厂入场全景（2026-10-13 补充，均已核实至官方/一线报道）

| 时间 | 厂商 | 产品 | 关键事实 | 来源等级 |
|------|------|------|---------|---------|
| 2026-09-15 | TypeSafe AI | **Jev**（首个 System One 决策模型） | $0.042/M 输入、输出免费、70–500ms、RLCD 校准训练 | 官方 |
| 2026-09-29 | **OpenAI** | **Decisions API**（DevDay 2026 发布） | GPT-6 Luna 驱动；用户自定义问题+有限预定义答案；文本/图像输入；~150ms/决策、比 Responses API 快 ~10×（报道口径）；10-07 转 Public Beta。The New Stack 评述：系对 Jev 走红的快速反应 | 官方 recap（openai.com/index/devday-2026-recap）+ OpenAI Devs 官方推文 + 多家一线报道 |
| 2026-10-09 | **Microsoft** | **Microsoft-Decision-1** | Qwen3.5-9B 后训练（自建数据配方非蒸馏）；Foundry+OpenRouter 入口，$0.042/M 输入输出均免费；自报 83.5% 超 Jev（自选基准 vendor claim）；官方明言将重训到 MAI/OpenAI 自家模型上 | 官方博客（commandline.microsoft.com） |
| 2026-10 月中 | **Cloudflare** | **Clef / Clef-flash** | Apache-2.0 开放权重；Jev API 兼容 + **图像输入**；自报 JevBench 0.762；Workers AI 推理 $0.24/M 输入 $0.09/M 输出；官方坦承复杂问题弱于推理模型 | 官方/多源 |
| 2026-09 下旬 | **Vercel** | AI Gateway 接入 Jev | 发布 24h 近 13% 付费团队用 Jev 路由（GPT-5.6 的 2×、Fable 5.1 的 6×；**窗口期网关免费促销至 9/25 须注明**）；自家 Luna 5.6 安全分类器换 Jev 提速 5–18×（厂商口径） | 多源核实（Forbes/TNV/daily.dev 等） |

格局判读：一个月内 OpenAI/Microsoft/Cloudflare 三家一线全部入场，**「问题+有限选项→概率输出」的接口形态已成事实标准**（三家与 Jev 均同构）；竞争分层为——托管闭源（OpenAI/TypeSafe）、厂商开放权重（Microsoft/Cloudflare）、社区自训（RSI-Jev/LLM2Jev/Laya 等 161+）。对本仓含义：接口层押注「Jev 兼容三问型」是低风险选择（Cloudflare/Microsoft 均已做兼容实现，可平替）；更高层的双阈值/抽审/升级逻辑属于我们自己的治理资产，不绑死任何一家。

## 6. 学术论文证据（独立第三方，2026-10-13 补充）

1. **JEVal 基准（arXiv:2610.03935，复旦等，2026-10-02）**《General Decision Models: Benchmarking and Insights Beyond Jev》：11,257 实例/36 数据集/10 域/25 模型配置，目前最全面的独立评测。三条与本仓级联设计直接相关的发现：
 - **能力边界**：证据可解的决策最强；需专业知识或诚实不确定性估计时变弱——**「常选对最可能答案但大幅高估其概率」**。这为级联设计 §3 前提①（confident-wrong 盲区）与 §3.3 三率监控（尤其校准误差）提供了独立实证：**不能只看 top-1 对错，必须核验概率值本身**。
 - **系统级错误累积（τ-bench）**：更快的局部决策缩短单轮时延，但长轨迹上决策错误累积**降低任务成功率**——单点便宜 ≠ 系统便宜，直接支撑级联设计的抽审（Tier 1 ρ 审计）与分层升级（Tier 2）不可省略；也是 swe-eval C 通道「影子先行、灰区升级」的文献依据。
 - **InnerJev-4B/27B**（Reasoning-to-Readout Self-Distillation）：把开源 LLM 自身推理蒸馏为单遍首 token 决策，27B 在 JEVal 与 Jev 打平、~0.1s/问——**免训练数据配方路线**（与 LLM2Jev 同族），为未来降外部依赖提供了比自训 RLCD 更现实的路径。
2. **《Evaluating and Benchmarking the System One Model Jev》（arXiv:2609.37647，同作者组早前工作）**：Jev 在 27 个数据集上胜同底座推理模型 Qwen3.8-27B、9 个数据集落后（WinoGrande +15.1/MMLU +10.1）；**校准按数据集平均后与 Qwen 持平**——Jev 的校准优势并非全域成立，再次印证「必须在自己的分布上做校准核验」（本仓 jev-mcp-tool.md 使用策略 L1）。
3. **《Just Ask Jev: RLCD as a Zero-Shot Detector of AI Alignment Failures》（arXiv:2609.29429）**：RLCD 作为零样本对齐失败探测器——TypeSafe 训练方法（RLCD）的独立延伸应用，佐证该方法族在「检测类判断」任务上的泛化性；与 swe-eval 用 Jev 做归因/残差登记的思路同构。
4. 引用纪律：以上论文均为 arXiv 预印本（未经同行评审）；JEVal 结论为第三方复旦团队，可信度高于 vendor claim，但 InnerJev 自报分数仍属作者口径。

## 7. 来源清单

**大厂官方文档/公告**
- OpenAI DevDay 2026 Recap（Decisions API 权威描述）：https://openai.com/index/devday-2026-recap
- OpenAI Developers 官方推文（2026-09-29 limited preview 公告）：https://x.com/OpenAIDevs/status/2105003318917697873
- The New Stack：OpenAI answers TypeSafe's Jev with a Decision API built on Luna（2026-09-29）：https://thenewstack.io/openai-decision-api-luna
- Microsoft-Decision-1 官方博客（2026-10-09）：commandline.microsoft.com（Microsoft Decision 1 发布公告）
- OpenAI 社区：Decisions API Public Beta 公告（2026-10-07）：https://community.openai.com/t/devday-2026-announcements-and-developer-resources/1402006
- Cloudflare Clef：Apache-2.0 开放权重公告（多源转述：techfabric.ai / flaviocopes.com / the-decoder.de）
- Vercel 采用率报道：Forbes / TechNovice / daily.dev（2026-09 下旬，含促销窗口注记）

**学术论文（arXiv 预印本）**
- arXiv:2610.03935 — General Decision Models: Benchmarking and Insights Beyond Jev（JEVal，复旦等，2026-10-02）：https://arxiv.org/abs/2610.03935
- arXiv:2609.37647 — Evaluating and Benchmarking the System One Model Jev：https://arxiv.org/abs/2609.37647
- arXiv:2610.02076 — LLM-as-Jev（免训练路线）：https://arxiv.org/abs/2610.02076
- arXiv:2609.29429 — Just Ask Jev: RLCD as a Zero-Shot Detector of AI Alignment Failures：https://arxiv.org/abs/2609.29429
- arXiv:2502.09054 — 双阈值拒答设计（级联设计 §3 引用）

**开源项目**
- Shanghua-Gao/RSI-Jev + HF shgao/rsi-jev-v6.1-vl-27b（底座 Qwen3.8-27B，Decision Index 0.3 自报 65.64）
- datawhalechina/jev-cookbook（中文学习路径，11 章+18 配方+RLCD 微调 Laya）
- typesafe-jev.com 生态目录（161+ 条目；Eve/StartLux/Gero-4B 等未逐一核实）

**本仓/外部报告**
- 外部报告：`/Users/jinsongwang/Documents/kimi/tasks/2026-10-13/13-43-23-dd157983/Jev模型调研报告.md`（2026-10-13）
- 本仓：`jev-mcp-tool.md`（使用策略）、`decision-cascade-rules-jev-escalation-20261013.md`（级联设计）、`jev-quota.md`（额度台账）
