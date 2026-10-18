# swe 失败测试会话行为分析与 harness 改进（2026-10-18）

> 状态：分析归档 + 守门机制已落地｜日期：2026-10-18
> 样本：swe-eval 批次近 100 run（46 resolved / 47 failed / 3 regressed / 4 watchdog-killed 无 verdict）
> 证据源：`results/runs.jsonl`、`results/patches/`、`results/timeouts.jsonl`、代理侧
> `logs/diag/ledger|manifest|index` 与 S-17 录制分片（`archive-req/*.R|Rp.jsonl`）。
> 前作：[`swe-empty-patch-rootcause-20260903.md`](swe-empty-patch-rootcause-20260903.md)
> （fifo 窗口截断根因，已由 ctx_engine epoch 接管修复）——本篇是**其后新失败形态**的分析。

## 1. 失败面扫描

| 维度 | 发现 |
|------|------|
| verdict 分布 | resolved 46 / failed 47 / regressed 3 / None(killed) 4 |
| **路由对照** | local(splash35) resolve **60%**(27/45) vs cloud **35%**(19/55)——cloud 承载 36/47 失败 |
| **重试黑洞** | 11 实例失败≥2 次；前 3 名全 ansible（10×+8×+8×=**26 跑零产出**） |
| **超时** | watchdog 45min 硬墙：2 例 killed+backfill；mockito-1018 深跑 44.8min 被杀（无 verdict/无 patch/agent log 0 字节） |
| **空 patch** | 3 例（pr3701-1017 / pr638 / mockito-1017）全是 **Edit=0 纯探索会话** |

## 2. 失败行为画像（四模式）

### 模式 A｜纯探索零编辑 → 空 patch（3 例）

| 会话 | 轮数 | 动作数 | 工具分布 | Edit |
|------|------|------|------|------|
| swe58bca057 (pr638) | 77 | **1800** | Read 816 + Grep 427 + Glob 257 + Bash 245 + Agent 39 | **0** |
| swe7eb3887e (mockito) | 39 | 803 | Grep 319 + Read 287 + Glob 154 + Agent 18 | **0** |
| swe180d94e3 (pr3701) | 83 | 922 | Read 349 + Grep 310 + **SendMessage×26** + Agent 23 | **0** |

共性：子代理研究风暴（Agent×18-39）+ 每轮并行工具批量高达 23 个，全程零写入，
end_turn 于探索中途（尾部动作仍是 Read/Grep）。patch 提取无过错（git add -A +
diff --cached，零改动走合法空 patch 分支）。

### 模式 B｜编辑迟到撞 45min 墙（swe388be5f2 mockito-1018，167 轮）

```
turn 0-51   MockMaker.java 重读风暴(17 次/10 轮) → auto_recall 注入@51
turn 52-97  换 StackWalkerChecker.java 继续重读 → dup 召回注入@98
turn 98-105 仍重读(99/105)                    → stuck_escalate 注入@106
turn 106+   工具面切向 Bash×46+Edit×8（Edit 首现@126）
turn 167?   否——44.8min 时被 45min 墙硬杀，死在动手阶段
```
代理侧上下文栈全程正常（hit_ratio 0.96-0.99、6 次 epoch 折叠、canonical 稳定），
**死因 = 预算被探索烧光 + 硬墙无收尾宽限**。

### 模式 C｜regressed ×3
f2p 修复但 p2p 破坏——patch 落盘前无 pass_to_pass 回归守门。

### 模式 D｜对照（resolved 画像）
14-21 轮、**早编辑**（turn 6-15 首次 Edit）、快速收尾。

### 模式 B 追加结论：auto-recall 行为差分（人工口径）

swe388be5f2 三次注入的行为曲线（台账逐轮 Read 计数）：

| 注入 | 轮 | target | 注入后同目标再读 | 判定 |
|------|-----|--------|------|------|
| #1 dup | 51 | MockMaker.java（此前 17 次） | 7 次（68→123 散点，爆发停止） | 部分 |
| #2 dup | 98 | StackWalkerChecker.java | 4 次（含下一轮 99） | 无效 |
| #3 stuck_escalate | 106 | 同上 | 3 次，**但工具面切向 Bash/Edit** | 模式切换相关 |

机械层 100% 执行；行为抑制不完全；分层防线（召回→升级）的价值在 #3 兑现。
公平性保留：台账不记 Read offset/limit，注入后复读可能是不同区间合法读取。

## 3. 已落地守门（2026-10-18，run_agent.py，env 门控缺省关）

| 守门 | 触发 | 动作 | 门控 |
|------|------|------|------|
| **空 patch 守门** | 正常收尾（rc=0）且 patch 0 字节且非文本任务 | 复用 delivery_gate 的 `gate_instruction` 续跑通道（同 worktree fresh `claude -p`，改动持久）补一轮实现提示，重新提取 patch；记录 `rescue_round` 字段；非递归至多一轮 | `SWE_EMPTY_PATCH_RESCUE=1` |
| **预算收尾宽限** | watchdog 墙钟超预算 | 先经 admin-inject 注入收尾指令（停止探索/立即写入），给 `wrapup` 宽限窗自终；超窗才硬杀 | `SWE_WATCHDOG_WRAPUP_MIN` 或 targets.yaml `watchdog.run_timeout_wrapup_min`（建议 5） |

两者均 fail-open、缺省关（冻结批不受影响）；下一验证批开启。

## 4. harness 环节改进建议（未落地，按 ROI）

| # | 环节 | 建议 | 依据 |
|---|------|------|------|
| 1 | 编辑里程碑提醒 | ≥60% 轮数零 Edit/Write → 注入提醒（复用 admin-inject；watchdog 已有 repo-edit stall 检测雏形，targets.yaml `repo_edit_stall_turns` 门控——**建议对 splash target 启用**） | 模式 A 全员 0 编辑 |
| 2 | 工具风暴治理 | 每轮并行工具批量上限（≤8）；loop 治理对批量风暴形态升级（loop_l1×31 未止住 1800 动作） | pr638 每轮 23 动作 |
| 3 | 子代理研究产出 | Agent 子代理结果要求落盘 notes 文件（进可折叠域），防主线程摘要碎片化 | Agent×18-39/会话 |
| 4 | 路由策略重审 | local resolve 60% vs cloud 35%（差 25pp）——按实例分层对照排除难度混淆后，批期路由向 local 倾斜 | §1 路由对照 |
| 5 | p2p 守门 | patch 提取前跑 pass_to_pass 子集，回归→反馈一轮 | regressed ×3 |
| 6 | 可观测性 | ① 0 字节 agent log 修复（mockito-1018 等）② watchdog kill 必须落 run 记录（mockito-1018 无 verdict 无记录） | 模式 B |
| 7 | 9B 命中率复测 | haiku 路由生产流量积累后重读 `rapid_mlx_prefix_cache_*`（当前 19.8% 被评测流量稀释） | 性能基线 2026-10-17 |

## 5. 代理侧状态（排除项）

失败根因在 harness 行为层，**不在上下文供给层**：窗口截断根因（9-3）已由
ctx_engine epoch（S=70K）接管修复；本轮失败会话 hit_ratio 0.96-0.99、
epoch_collapse×25、auto_recall 注入×17、manifest +53/48h 全部按设计触发。
遗留摩擦点：swe-eval 客户端改写历史致 canonical_mismatch 25-82 次/会话
（引擎以快照重建兜住，但代表客户端与 append-only 假设的摩擦，是下一个
上下文效率杠杆）。
