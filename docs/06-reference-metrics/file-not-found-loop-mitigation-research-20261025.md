# file_not_found 重复探测问题：调研与解决方案

> 日期: 2026-10-25 · 状态: 调研完成，方案待实施
> 触发: 批跑会话 swe71877 对同一路径重试 22 次（18 个 file_not_found），全批单日 6,179 次 file_not_found
> 关联: DEF-313（会话工具冻结）、loop_detection Blocker、`tool_filter._translate_tool_result_errors`

---

## 1. 问题现象（实测数据）

| 维度 | 数据（2026-10-25 批跑日） |
|------|--------------------------|
| 全批 file_not_found 错误转译 | **6,179 次**（1,378 个转译事件） |
| 全批 wasted call | 2,024 次 |
| 单会话最重（swe71877） | 同一路径 `/…/work/lib/ansible/cli/config.py` 重试 **22 次**；累计 18 个 file_not_found |
| 该会话上下文 | 467,630 chars（~150K token），turn 122 重放 3 次（每次 ~60–100s prefill） |
| Blocker 注入 | `Bash failed (file_not_found) 3-4 times in a row` ×3 次以上，**未阻止** |
| canonical_mismatch | 文档初稿 304 次；**swe-eval 评审时点实测 455+ 且在涨**（1025-1）——判读按实测披露 |

**代价**：每次重复探测 ≈ 1 个 turn（模型生成 tool_use + 后端往返 + 结果回灌）。按 60% 为重复探测折算，每天 ~3,700 个 turn 被浪费，并推高长会话的 TTFT 与 canonical 压力。

---

## 2. 现有两层防线为何失效

### 2.1 错误转译（`tool_filter._translate_tool_result_errors`）

当前注入的提示（R5.1/R5.2 时代实现）：

```
[System: 文件不存在。请先用 Bash ls 或 find 命令确认项目结构，
然后使用正确的文件路径。]
```

三个失效点：

1. **无答案性信息**——只说"不存在"，不告诉模型"那目录里到底有什么"。模型要跳出循环所需的恰是替代路径，而提示让它再花 turn 自己去找。
2. **每次提示一模一样**——没有"你已失败 N 次"的累积压力，模型第 22 次看到的与第 1 次完全相同。
3. **模型的路径不是瞎猜**——上游 ansible 仓库确实存在 `cli/config.py`；worktree 的 PR 分支里被删除/移动。模型按自己的"仓库记忆"访问，不认为该放弃。

### 2.2 Blocker 注入（`loop_detection._build_blocker_message`）

```
[BLOCKER] The tool 'Bash' has failed with the same error (file_not_found)
3 times in a row. Do NOT call 'Bash' again with similar arguments.
Either switch to a different tool, change your approach, or report this
blocker to the user.
```

失效点：英文泛化指令（"switch tool or approach"），**无具体替代行动**；与错误转译提示各自为政（一中一英、内容不呼应）。注入 3 次以上仍未阻止。

---

## 3. 行为学定性

这类问题在长程 agent 失败研究中有明确定位：

- **step repetition 占 agent 全部失败的 17.1%，为最大单项**（2025 年 100 例真实失败研究）。
- 模型不是"看不到错误"，而是**缺少跳出循环所需的信息**——错误只说"不存在"，没说"正确的是什么"。
- swe71877 的输出本身佐证：模型会自我诊断（见 §5 语义抽样），但缺信息时只能继续试。

---

## 4. 业界参照

| 来源 | 机制 | 启示 |
|------|------|------|
| **SWE-agent**（NeurIPS 2024，ACI 设计） | edit 命令 lint 失败 → **自动回滚**，反馈含具体错误并明说 *"DO NOT re-run the same failed edit"* | 错误反馈必须**具体 + 含禁令**；接口层要**替模型挡住重复犯错**，不靠提示自觉 |
| **OpenAI Harness Engineering**（2026） | "give Codex **a map**, not a 1,000-page instruction manual"——仓库知识必须可发现 | 与其教模型怎么找，不如**把目录结构直接给它** |
| **aider repo-map** | 会话前置分层仓库结构图 | 从源头减少路径猜测 |
| **FutureAGI 失败图谱**（2026） | cycle detector：**同一工具 + 相似参数 >3 次**即强制干预 | Blocker 已计数到 3-4 次，但干预是"软"的（只注入提示），应升级为确定性动作 |
| **Why Multi-Agent Systems Fail**（2025） | 确定性循环检测 + 明确终止条件是对抗 step repetition 的有效手段 | 同上 |

**共识**：提示升级只是止损；**确定性短路 + 直接喂答案**才是根治（"接口挡错误"与"给地图"是同一思想的两种实现）。

---

## 5. 方案矩阵

| # | 方案 | 做法 | 效果 | 成本/风险 |
|---|------|------|------|----------|
| **A** | **升级提示** | 会话级记录 `(path, 失败次数)`；≥3 次时提示升级为：「**该路径已确认不存在（第 N 次）。禁止再尝试此路径。** 已列出父目录内容：…」 | 中——措辞更强，仍是"提示自觉" | 极低（会话状态 dict）；文本变更一次性，注意前缀缓存 |
| **B** | **最近邻建议** | 首次失败时有界执行 `ls <父目录>` / `find -name <basename>`（按 `(dir,basename)` 缓存），把**真实候选文件**注入提示：「相近文件: [x.py, y.py]」 | **高**——模型当场拿到答案，无需自花 turn | 每错误一次有界子进程（缓存后趋零） |
| **C** | 目录快照 | 首次某目录下 file_not_found → 注入限深 `ls` 列表 | 中高 | 上下文 token 成本 |
| **D** | **确定性短路（skip-exec）** | 会话级 known-missing 表：模型再对已知不存在路径发起 Read/Grep/Glob → **不派发后端**，直接合成 tool_result（含计数 + 候选） | **最高**——零延迟、零后端往返、确定性 | 需在工具循环加合成点（可复用 MICRO_TURN 自答管道）；"路径后来出现"边界（TTL/逃逸口） |
| **E** | 仓库地图前置 | 会话首请求注入 repo 顶层树 | 中（源头减少猜测） | 上下文成本；改动面大 |
| **F** | Blocker 措辞升级 | 注入消息含具体路径信息、中文、与错误转译呼应 | 低-中 | 极低 |

---

## 6. 推荐组合与实施草图

### 一期（低风险高收益）：A + B，改 `tool_filter.py`

```
_translate_tool_result_errors 增强:
1. 从 tool_result 原始错误提取 path（正则 "File does not exist: <path>"）
2. 会话级 _MISSING[path] = count（沿用 DEF-313 _SESSION_TOOL_FREEZE 的
   会话状态模式，cap 512）
3. count == 1: 现提示 + （异步）取父目录候选
   count >= 3: 升级提示 A：
   "该路径已确认不存在（第 N 次）。禁止再次尝试。已为你列出父目录内容：
    <ls 截断>。请改用以上真实存在的文件，或先向用户确认。"
4. 候选获取：子进程 ls（timeout 2s），按 (dir) 缓存；**目标限定在 worktree 根内，超界即不取候选**（评审第 5 点：收安全面、免泄漏面变化）
```

覆盖面：Read / Grep / Glob（显式 file_path 参数）。Bash 命令里的路径难以可靠解析，一期靠 A 的提示覆盖。

**前置量测门槛（评审第 2 点，硬性）**：「60% 为重复探测」是假设不是测量。落地前必须先从 1,378 个转译事件量测 **(path, 次数) 分布**（≥2/≥3/≥5 次占比）——该分布决定 A+B 是否已够、D 的短路阈值定几、收益估计是否成立。
**量测可行性结论（llama.cpp 侧实测）**：代理日志**不含路径级数据**——完整请求体不落盘（仅 51 条小探测行）、错误块已被转译改写（原始路径消失）、ctx_recall query 不落盘。因此 (path,次数) 只能从 **swe-eval 运行工件**（claude-cli 轨迹）测，swe-eval 已认领量测脚本（零外发零改动）；备选：批后临时开 body 采样。

### 二期（确定性短路）：D，复用 MICRO_TURN 自答管道

```
模型 tool_use(Read, path ∈ _MISSING 且 count >= 3)
  → 不派发后端，直接合成 tool_result（与 ctx_recall 微轮自答同管道）
  → 结果含：确认不存在 + 候选文件 + 失败计数
逃逸口（评审第 6 点修正）：模型对父目录执行 ls 后**只重置短路态、累计计数保留**——否则 ls 完又猜同一路径会重新放行 2 次真实探测
```

### 边界与风险

- **文件后来被创建**（agent 自己 touch）→ 逃逸口 + TTL；且 count<3 前仍真实执行
- **会话隔离** → 沿用 DEF-313 会话状态模式，cap 512，不跨会话污染
- **前缀缓存** → 更正（评审第 7 点）：升级提示含「第 N 次」，N 递增使注入文本**逐次变化**；但注入位于 tool_result 位置，**不作用于 system 前缀**，无需担心前缀缓存

---

## 7. 预期收益

```
今日: 6,179 file_not_found / 1,378 个转译事件
假设 60% 为重复探测（≥2 次）→ ~3,700 次/天可被短路或当场纠正
每次浪费 ≈ 1 个 turn（tool_use 生成 + 后端往返 + 结果回灌）
收益: 每天节省数千 turn 的生成与回灌 → 批跑墙钟缩短
     + 长上下文会话的 canonical_mismatch 压力下降（今日 304 次）
     + swe71877 类"僵尸探测会话"不再拖到 467K chars
```

---

## 8. 附录：相关代码位置

| 组件 | 位置 |
|------|------|
| 错误转译（一期改造点） | `tool_filter.py` `_translate_tool_result_errors()` |
| Blocker 注入（F 项改造点） | `loop_detection.py` `_build_blocker_message()`，调用点 `pipeline.py:1852` |
| 会话状态模式参考 | `tool_filter.py` DEF-313 `_SESSION_TOOL_FREEZE` |
| MICRO_TURN 自答管道（二期复用） | `pipeline.py` `_pd_micro_turn` / `_micro_recall_dispatch` |
| 今日数据出典 | `logs/anthropic_proxy.log`（1,378 转译事件）、`logs/lifecycle_events.jsonl`（304 canonical_mismatch） |


---

## 9. 评审采纳（swe-eval 1025-1，联合协调 docs/joint-coordination-llamacpp.md）

swe-eval 侧对本档完成事实核查与方案评审（其 commit f76b791，协调文档 §五 1025-1、§三 待办 #21）。七点意见与本档采纳状态：

| # | 评审意见 | 采纳 |
|---|----------|------|
| 1 | **红线 1 窗口纪律**：触发出典是在飞 confirmatory 批的会话，代理侧任何改动（连提示文案）必须等空窗；行为类改动按 J6 强制 A/B | ✅ 一期/二期均在空窗后实施；A/B 前不上生产 |
| 2 | **前置量测**：「60% 重复探测」是假设；先量 (path,次数) 分布再定 A+B/D 阈值 | ✅ 已列入 §6 前置门槛；实测结论：代理日志无路径数据，须由 swe-eval 运行工件测（对方认领） |
| 3 | **判读可比性分段**：D 短路改变 turn 消耗与 loop 干预计数，启用前后 S1/S2/loop intervention 读数不可直比，按启用时刻分段 | ✅ 判读方案采纳（dup 指标按重启分段的既有先例） |
| 4 | **A/B 判据含协同面**：不只 file_not_found 下降——resolve 不劣化、S1/S2 触发率迁移、escalate 处方消费（adoption）都在判据 | ✅ 判据组采纳 |
| 5 | **B 的 ls 安全边界**：限定 worktree 根内，超界不取候选 | ✅ 已写入 §6 |
| 6 | **逃逸口语义**：只重置短路态，累计计数保留（避免 ls 后再猜重新放行 2 次真实探测） | ✅ 已修正 §6 |
| 7 | **前缀缓存措辞更正**：注入在 result 位置不作用于 system 前缀；「第 N 次」使文本逐次变化但不影响 system 前缀缓存 | ✅ 已更正 §6 |

**事实核查修订**：canonical_mismatch 按评审时点实测 **455+（全日，仍在涨）** 披露，不引用本档初稿的 304 快照；swe71877 确认为 P0 复现批在飞 run（session_size 464K→467K 吻合）；file_not_found 当前轮转段（07:59–21:53）1,531 次，全日 6,179 为跨轮转量级估计。

**落地路径（#21）**：空窗 → ① (path,次数) 前置量测（swe-eval 脚本）→ ② A+B 空窗 apply + 探针小批 A/B（2×N 配对实例，判据=第 4 点判据组）→ ③ 判读通过后上生产；D 二期独立 A/B 归因。本批判读对 canonical_mismatch 按实测 455+ 披露，不引用未落地方案的预期收益。


## 实现状态（2026-10-25 定稿 / 2026-10-26 v0.1 修正）

一期 F1+F2+F3' 已按 swe-eval 方案（`swe-eval/docs/fnf-loop-mitigation-solution-20261025.md`，2026-10-26 v0.1 修正版）落地，**四旗标缺省关 = 零行为变化**（批期可入库，激活=空窗 conf 戳 + reload）：

| 件 | 落点 | 旗标 |
|----|------|------|
| F1 fnf 会话账本＋分级升级提示（count≥3：计数＋禁令＋替代行动） | `tool_filter.py` `_translate_tool_result_errors_ex` / `_FNF_SEEN`(cap 512, DEF-313 模式) | `PROXY_FNF_GUIDE_ENABLED` |
| F2 最近邻候选（ls 父目录，2s 超时，512 chars，仅已证工作区根内） | 同上 `_FNF_DIR_CACHE` / `_fnf_workspace_roots`（成功 Read 公共前缀 ≥2 层） | `PROXY_FNF_CANDIDATES_ENABLED`（F1 子旗标） |
| **F3' 会话级升级记忆**（阶梯触发带会话级累积计数「第 N 次」；L2 sticky：同错误形态 N 轮内重现 → 维持摘除并升级提示） | `loop_detection.py` `_apply_loop_intervention` + `_LOOP_ESCALATED`/`_LOOP_STICKY`(cap 512) | `PROXY_LOOP_ESCALATION_MEMORY_ENABLED`（窗宽 `PROXY_LOOP_STICKY_TURNS`=3） |
| F3 Bash 计数键修复（`Bash:fnf:<path>` 派生键）—— **v0.1 起降级为兜底面** | `pipeline.py` ToolLoopDetector（`_FNF_TUID_PATH` 转译期记账） | `PROXY_LOOP_BASH_PATH_KEYS_ENABLED` |

> **v0.1 修正要点（swe-eval 1025-5 修正案，本仓同步）**：原 F3「Bash 变体逃逸致零触发」
> 前提**失效**——代理日志权威面证实阶梯工作正常且已重度触发（L1=902/L2=57/L3=8；
> swe71877 会话 49 次干预、Read 同路径重复直达 L1）。真问题＝**干预饱和但无记忆无升级**
> （L1 每次同文、L2 摘除仅当轮、下轮恢复即重新饱和）。故 F3 改写为 F3'（会话级升级
> 记忆＋L2 sticky），原 F3 降级为兜底面保留（不删，旗标仍缺省关）。
> 旗标名与 swe-eval `configs/capability-flags.yaml` 的 `fnf_guide_kit` 条目对齐
> （`PROXY_FNF_GUIDE_ENABLED` / `PROXY_FNF_CANDIDATES_ENABLED` / `PROXY_LOOP_ESCALATION_MEMORY_ENABLED`）。

- 清零口：同路径后续成功 Read → 计数清零（逃逸口，只清计数）
- L 级互斥：`_LOOP_SESSION_STATE.level>0`（上一轮干预）时转译跳过升级提示，避免双注入
- 观测：转译日志增列 `fnf_guided=N`、`fnf_candidates=hits/misses`
- 测试：`test/unit/test_fnf_guide.py`（方案 §3.1 四断言 + F3' 四断言 + 旗标关回归）；unit 2090 / signature / snapshot / trace 全绿
- 激活纪律：空窗一次戳（conf sha256 + 窗前后 /metrics 快照）→ 探针 A/B（判据预注册见方案 §4）→ PM 门后默认 on
