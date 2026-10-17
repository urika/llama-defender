# Ornith-1.5-9B 能力调研：还能做什么（2026-10-15）

> **调研范围**：本机辅助引擎 `ornith-ai/Ornith-1.5-9B-MLX-4bit`（:8084，rapid-mlx，262K ctx，`--no-mllm` 纯文本，`max-num-seqs=1` 串行，稳态 ~100 tok/s）。
> **来源**：HF 官方 model card（ornith-ai/Ornith-1.5-9B，MIT）、Ollama registry、本仓文档（`ornith-15-integration-20260826.md` / `local-stack-and-training-feasibility-20260830.md` / `protocol-layer-ownership-review-20260902.md`）、本仓质量门实测（commit `62de9d0`）。
> **结论先行**：我们目前只用到它「知识工人」的一面（已验收），官方真正的牌面是「**终端里的编码 agent**」——待验证的增量在 agentic 编码与 tool calling。

---

## 一、官方定位与能力画像

Ornith-1.5 是「端到端自我改进」家族（9B / 35B-A3B / 397B，联合优化任务生成 + scaffold 构建 + solution rollouts），9B 是**最轻成员，定位单卡部署 + 移动端边缘**（另有 Mobile 量化变体；Ollama 367K 下载，Q4_K_M 6.6GB + CLIP 视觉塔 922MB）。

三个关键能力（含本机验证状态）：

1. **Reasoning 模型**：默认开 ```think``` 块，`reasoning_content` 独立字段——本机 rapid-mlx 已配 `qwen3` reasoning parser；**thinking 开/关的质量-延迟 A/B 尚未测**。
2. **原生 tool calling**：`qwen3_xml` 工具解析器 → OpenAI 风格 `tool_calls`。**2026-10-15 实测 :8084 支持 `tools` 参数**：`finish_reason=tool_calls`、参数 JSON 正确解析、单请求正确拆出两个调用（天气+温控）。
3. **上游多模态**：HF/Ollama 版带 CLIP 视觉塔（image-text-to-text，qwen3_5 架构）——**本机 MLX 构建 `--no-mllm` 无视觉塔**；换带视觉的构建是可选升级路径。

### 官方 benchmark（model card 口径）

| 基准 | **Ornith-1.5-9B** | Qwen3.5-9B | Qwen3.6-35B-A3B | Gemma-4-31B |
|------|------|------|------|------|
| SWE-bench Verified | **70.6** | 53.2 | 73.4 | 52 |
| Terminal-Bench 2.1 (Terminus-2) | **46.2** | 21.3 | 52.5 | 42.1 |
| Terminal-Bench 2.1 (Claude Code) | **47** | 18.9 | 49.2 | – |
| HLE (with tools) | 30.5 | 24.5 | 28.9 | 26.5 |
| MCP-Atlas / Toolathlon-Verified | 54.2 / 41.2 | – | 62.8 / 41.7 | – |
| GPQA Diamond | 86.4 | 81.7 | 86 | 84.3 |
| WideSearch / BrowseComp | 59.5 / 56.4 | 53.6 / 41.5 | 60.1 / 62 | 54.2 / – |

> 口径注意：SWE-bench / Terminal-Bench 为官方 scaffold 配套成绩，非裸模型直跑；但**相对同级模型的优势方向明确——终端 / 编码 / 工具型任务**（对比 Qwen3.5-9B 接近翻倍）。

本机另册事实：`qwen3_5` 架构**在 mlx_lm 注册表、开箱可训**（`local-stack-and-training-feasibility-20260830.md`，权重 4.7GB 已在本机）——它同时是**可改造资产**（LoRA/蒸馏回灌路径已立项）。

## 二、「还能做什么」分层清单

### A. 已验证，可直接承诺（质量门 62de9d0，32 条金标）

| 能力 | 实测 | 备注 |
|------|------|------|
| 结构化抽取 | 8/8 = 100% | 中英/日志/房源多形态字段全中 |
| 接地问答 + 防幻觉拒答 | 6/6（unanswerable 2/2 红线守住） | 无答案时老实拒答不编造 |
| 多轮指令精炼 | 4/4 = 100% | 表格/压缩/编号/提取 |
| 封闭标签打标 | 7/8 = 87.5% | 唯一失手为两可情感题（金标偏严） |
| 要点覆盖式摘要 | 覆盖 100% | **硬字数不承诺**（超字 14–65%，硬化 prompt 0/4 可修） |

### B. 官方强项，待验证（最有价值的增量）

1. **轻量 agentic 编码**：haiku 路由（`haiku→local-9b prefer_local`）正是为此而设——commit message、报错解释、测试草稿、命令生成、小补丁。SWE-bench 70.6 的同族实力远未用上。→ **质量门 coding 类**
2. **Tool calling 中介**：意图→工具 JSON（实测通过）；MCP-Atlas 54.2。→ **质量门 tool_call 类**
3. **Thinking A/B**：开/关 think 的质量-延迟曲线（parser 已就位，未测）。

### C. 工程化潜力（栈内角色扩展）

4. **级联 Tier 2 首选**：Jev 灰区（0.4–0.6 带）升级的第一级廉价复核（级联设计文档既定位置）。
5. **云端降级臂**：models.json fallback 链已含 local-9b——零外发、零边际成本。
6. **262K 长文档吞吐**：prefix cache 实测有效（24K 重发 26.3s→1.63s）；wiki/finwiki/docs 语料全库字段归并、分块抽取。
7. **数据工程**：金标候选起草（人只做校对）、弱标注、合成数据；配合「开箱可训」→ 35B/云端难例蒸馏回灌 9B（`local-stack` 文档已立项）。
8. **lenient 摘要流水线**：先出要点全覆盖长稿，硬字数由程序截断/二次压缩（绕开已证短板）。

### D. 做不了 / 不该做

- 硬字数摘要（已证 0/4 可修，结构性偏好「宁保内容不删事实」）
- embedding / rerank（另用 embedgemma2 :8083）
- 高风险决策（留给规则 + Jev + 人）
- 视觉/OCR（本构建无塔；上游有，见升级路径）
- 高并发（`max-num-seqs=1` 串行，~100 tok/s；多 agent 并发须代理队列治理）

## 三、建议验证路线（按性价比排序）

1. **扩质量门**：`coding`（8 条，安全沙箱执行评分）+ `tool_call`（5 条，真实 tools 参数评分）——检验官方牌面，复用现框架（**本档已执行，见下节**）。
2. **haiku 分流实测**：promptfoo 核心用例的 haiku 档切 local-9b 对比 flash。
3. **Thinking A/B**：子样本 × 开/关 think 质量-延迟曲线。
4. 视觉升级调研（可选）：若有 OCR/截图理解需求，评估带 mmproj 的构建。

## 四、验证执行记录（2026-10-15）

- **tool_call 支持实测**：:8084 `tools` 参数生效，单请求正确产出 `get_weather{city:北京}` + `set_thermostat{temperature:26}` 双调用。
- **质量门 v2 扩展**：金标 32→45 条，新增 `coding`（8：安全沙箱执行断言 5 + 关键词 3）与 `tool_call`（5：单工具+参数/表单填充/三选一/不该调用/枚举完整性）两类；阈值 coding 0.625 / tool_call 0.8。评分器：`score_coding`（import 白名单 + 危险内建黑名单扫描后 `python3 -I` 子进程跑断言）+ `score_tool_call`（真实 tool_calls 解析，`no_tool` 反例要求零调用+content_must）。
- **全量执行结果**（`logs/knowledge_gate/gate-20261015-061028.json`，overall 39/45 = 86.7%）：

| 类别 | v2 实测 | 线 | 判 |
|------|---------|-----|-----|
| **tool_call** | **5/5 = 100%** | 80% | ✓ 官方牌面兑现：工具选择、参数填充、三选一、不该调不调、枚举约束全对 |
| **coding** | **7/8 = 87.5%** | 62.5% | ✓ 唯一失手 COD04（版本号正则漏 `v1.2.3`——`\b` 在字母前无边界），真实边缘 bug 非金标误伤 |
| extraction | 8/8 = 100% | 75% | ✓ |
| grounding | 6/6 = 100% | 66.7% | ✓（unanswerable 防幻觉红线 2/2 守住） |
| refine | 4/4 = 100% | 50% | ✓ |
| tagging | 7/8 = 87.5% | 87.5% | ✓（压线） |
| summary | 2/6 = 33.3% | 66.7% | ✗ 硬字数失守，与 v1 一致（结构性短板，非本次回归） |

- **门判词：FAIL**——唯一未达标类别仍是 summary 硬字数（既有结论：9B 不承诺硬字数摘要，应走 lenient 流水线）。**官方牌面两项（coding/tool_call）均以高分通过**，验证路线第 1 项收官。诚实门语义保持：未事后调阈值。

## 五、haiku 档分流实测（2026-10-16）

**动机**：models.json 中 `claude-haiku-4-5` 路由为 `prefer_local`，但 `local_model=local-default`（= 35B 主引擎）——验证「haiku 档本地兜底下沉到 9B」的可行性。

**方法**：31 条金标双臂对比（TC1-9 直译自 promptfoo 核心用例 + 质量门 tagging/extraction/summary 子集 22 条，scorer 复用 gate 模块）；A 臂 9B(:8084)、B 臂 `glm-5.3-flash`（zhipu-cn anthropic 协议直连，即 haiku 路由云侧首选）；temperature=0。工具：`tools/haiku_tier_compare.py`，报告 `logs/haiku_tier/compare-20261016-225231.json`。

| 维度 | local-9b (:8084) | glm-5.3-flash | 判读 |
|------|------|------|------|
| **总体质量** | 26/31 = 83.9% | 28/31 = 90.3% | flash +2（B06 两可 + summary 字数 2 条） |
| tc（promptfoo 核心 9 条） | **9/9** | 9/9 | **打平**——回归基线无损 |
| extraction | **8/8** | 7/8 | flash 失手为日期格式归一化（金标偏严类） |
| tagging | 7/8 | **8/8** | 9B 唯一失手 B06（两可情感，金标偏严） |
| summary | 2/6 | **4/6** | 9B 结构性短板（硬字数）在此处兑现为差距 |
| **p50 延迟** | **3.3s** | 6.5s | 9B 快 ~2×；p95 打平（44s，长生成尾巴） |
| 边际成本 | 0 | 0（订阅） | 打平 |
| 引擎占用 | **独立 :8084** | 云 | 9B 不占 :8081——35B 主引擎容量全数释放 |

**过程修正**：首跑 flash 5 个「失手」全为空响应 artifact——anthropic 协议下 `thinking` 块计入 max_tokens，1024 在长题被思考耗尽；修正 4096 后 flash 真实质量还原。**方法论教训：跨协议对比必须核空响应。**

**结论**：**9B 可承接 haiku 档**——质量与 flash 差距仅 2 条（且全落在已知的金标偏严/硬字数两类），promptfoo 回归基线打平，p50 快 2×，零成本，且把 haiku 从 35B 主引擎剥离（现状 local-default 路由白白消耗 :8081 容量）。**建议**：haiku 路由 `local_model` 改指 ornith-9b；flash 保留为 fallback（严字数任务可按 route 敏感词升级）。切换动作 = models.json 一行 + reload（属生产变更，走批后窗口）。

> **执行记录（2026-10-17）**：批次收官后已切换（models.json hash `24bcf509`，`/api/route/policies` preferences 确认 + :4000 实测转发 `engine=ornith-9b`）。顺带修掉实测踩坑的报头口径 bug（pipeline.py `X-Proxy-Route-Actual-Model` 此前对 local 目标无条件回显 35B，现与派发侧 openai_body_model 口径一致；生效待下次 proxy 重启）。splash-35B 第三臂对照待跑。

### 5.1 splash-35B 第三臂对照（2026-10-17，:8081 空闲窗口）

31 条同金标直连 :8081（`incoai/Qwen3.6-35B-A3B-Splash`，DFlash2，热 prefix cache）。**结果推翻了「splash p50 会被队列拖高」的空载预测**：

| 臂 | 质量 | p50 | p95 | 定位 |
|----|------|-----|-----|------|
| **splash-35B** | **29/31 = 93.5%** | **300ms** | 1.4s | 质量延迟双冠（空载） |
| glm-flash | 28/31 = 90.3% | 6.5s | 44s | 云首选 |
| 9B | 26/31 = 83.9% | 3.3s | 44s | 隔离引擎 |

splash 仅失手 C01/C06（summary 4/6，与 flash 同类）；tagging 8/8 全对（含 9B 栽的 B06），单条 140-150ms。

**裁决（维持 9B 默认，论据修正为负载形态而非引擎速度）**：
- splash 的 300ms 是**空载数**——实测窗口 :8081 无并发。生产形态下 :8081 是单槽串行（proxy 重启以来 ~1240 次转发：sonnet 本地 fallback + 主流量；EXP 批期饱和），haiku 请求要排在在途重型轮次后面（无队列分桶，裸信号量），300ms→数十秒的尾部不可控，且反向偷主流量槽位。
- 9B 的 3.3s 是**负载无关数**（独立引擎，含批期），haiku 档任务（标题/摘要/分类）对 3s 完全不敏感；质量语义=flash 够用档（tc 9/9 打平）。
- **重审触发条件**（满足任一即翻回 splash 重验）：① haiku 档出现延迟关键型工作流且 :8081 占空比低；② strict-length 摘要类任务成为 haiku 档主力（9B summary 短板放大）；③ :8081 升级多槽/队列分桶。



## 参考来源

1. HF model card（官方，benchmark 表与 serving 配方）：https://huggingface.co/ornith-ai/Ornith-1.5-9B
2. HF MLX-4bit 卡（本机部署同源）：https://huggingface.co/ornith-ai/Ornith-1.5-9B-MLX-4bit
3. Ollama registry（家族/视觉塔/下载量）：https://ollama.com/library/ornith-1.5
4. 本仓 `docs/05-operations-changelog/ornith-15-integration-20260826.md`（MLX 版本清单）
5. 本仓 `docs/02-architecture-design/local-stack-and-training-feasibility-20260830.md`（开箱可训结论）
6. 本仓质量门（commit `62de9d0`）：`test/fixtures/knowledge_gate_9b.json` + `tools/knowledge_gate_9b.py`
7. 级联设计（Tier 2 定位）：`docs/02-architecture-design/decision-cascade-rules-jev-escalation-20261013.md`
