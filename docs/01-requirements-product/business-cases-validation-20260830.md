# 业务案例与系统解决方案推演

> **状态**：产品验证文档（v1.0）｜**日期**：2026-08-30
> **来源**：三层架构规范 + 五协议设计 → 业务场景验证 → 确认架构可行性
> **关联**：[三层架构规范](../02-architecture-design/three-layer-architecture-spec-20260830.md) · [认知策略编排器](../02-architecture-design/cognitive-strategies-orchestrator-design-20260830.md) · [exp-1 实验报告](../04-analysis-diagnostics/amnesia-experiment-report-20260830.md) · [分层理论框架](../02-architecture-design/layered-theory-framework-20260830.md)
> **一句话**：五个业务案例逐一推演三层架构+五协议的端到端解决方案——覆盖防死循环/wiki 维护/会话监控/代码修复/压缩评估，验证架构可行性并识别最高优先建设件。

---

## 1. 用户画像与核心痛点

**目标用户**：在 48GB Apple Silicon 上运行本地 35B/9B 模型的开发者/研究者，用 Claude Code/opencode 等 agent 做 wiki 维护、代码修复、研究综合。

| # | 痛点 | 已发生的实例 | 用户原话 |
|---|---|---|---|
| 1 | **任务死循环浪费资源** | exp-2 v1：734 次重复 Read，0.9 tok/s，45 分钟无产出 | "本地模型常常陷入错误循环" |
| 2 | **不知道会话何时开始退化** | exp-1：ILE 积累 130 次，resolved 率降 10pp，但无预警 | "会话为什么死了不可知" |
| 3 | **截断丢的信息找不回来** | Claude Code compact、fifo 截断、epoch 折叠 | "compact 后模型忘了之前的上下文" |
| 4 | **不知道何时该信任本地、何时升级** | text 任务本地可做，code 任务死循环 | "哪些任务本地能做？" |
| 5 | **模型"自信地说错话"无法检测** | D_ledger 首跑：85% 全忆但 15% 全忘 | "怎么知道模型是不是在胡说" |

---

## 2. 案例一：防死循环（anti-spiral）

### 用户故事
> 作为系统运维者，我希望系统能在模型陷入重复读取循环的 5 次以内检测并打破循环，而不是等到第 734 次。

### 场景
模型处理一个 code 任务（修改 3 个文件+更新测试），keep=12 截断导致它忘记已读过 file_a.py，于是反复读取同一文件。

### 验收标准
- ✅ 在第 3-5 次重复读取同一文件时触发检测
- ✅ 系统自动召回"你已读过此文件"的信息
- ✅ 如果召回后仍循环 → 自动分解任务为更小步骤
- ✅ 整个过程不需要人工干预
- ❌ 不允许：等到 max_turns=150 才停止

### 系统解决方案推演

**Signal Layer 检测**：
```
reread_pressure (Tier-0):
  第1次读 file_a.py → pressure=0
  第2次读 file_a.py → pressure=1
  第3次读 file_a.py → pressure=2 ← 触发阈值(≥2)
```

**P5 Escalate 决策**：
```
决策表匹配: reread_pressure ≥ 2 AND attempt < 2 → action="reload"
输出: EscalationDecision(action="reload", query=RecallQuery("file_a.py"))
```

**P4 Recall 召回**：
```
ctx_recall.lookup(session_key, "file_a.py")
→ ManifestLine(anchor="u:t1", handle="/src/file_a.py")
→ "你已在 turn 3 读取过 /src/file_a.py (2,340 chars)"
→ 召回结果注入工作集
```

**P2 Execute（带召回上下文）**：
```
模型看到: "[已读过 /src/file_a.py, 2,340 chars, turn 3]" + 当前任务
→ 不再重读，直接基于已有信息执行
如果仍失败(第2次 attempt) → P5 action="split" → P1 拆成更小任务
```

**数据流经的契约**：
`SignalSnapshot(reread_pressure) → EscalationDecision → RecallQuery → RecallResult(ManifestLine) → SubTask(refined) → Patch → Verdict`

**预计效果**：从第 3 次重读到纠正 ≈ 20s（原死循环 734 次 × 5s = 61 分钟，**效率提升 ~180x**）

---

## 3. 案例二：Wiki 综述更新（wiki-reconcile）

### 用户故事
> 作为 wiki 维护者，我希望系统在源论文更新后自动核对综述页中的每一条论断是否仍然成立，并生成带引用的修正补丁。

### 场景
wiki 有 1 个综述页（syntheses/mmpo-review.md）引用了 6 篇源论文。其中 2 篇有更新。综述中有 15 条论断需要逐一核对。

### 验收标准
- ✅ 每条论断独立核对（不是整页重写）
- ✅ 每条修正附引用（指出源论文的具体段落）
- ✅ 无法核对的论断标记"待人工审查"而非强行修改
- ✅ 全程在本地 9B 模型上完成
- ✅ 总耗时 ≤ 30 分钟

### 系统解决方案推演

**P1 Decompose**：按论断分解为 15 个 SubTask，每个 SubTask 只含综述段(200 chars) + 对应源论文(8K chars) → 每个子任务 V=H。

**P2 Execute**：每个 SubTask 输出带 Citation 的 Patch：
```python
Patch(
    diff="- r=-0.684\n+ r=-0.684 (p<0.001, n=200)",
    citations=[Citation(
        file="mppo-paper.md", quote="Pearson correlation r = -0.684",
        line_range=[412, 412], context="验证原文数值")])
```

**P3 Verify**：L1 机械校验——引用存在性(quote 在源论文中可找到) + diff 可应用性。

**预计耗时**：15 × 6s = 90s（本地 9B），远低于 30 分钟限制。

**9B 能力验证**：SWE-bench 70.6 的模型做"论断核对"（严格易于 coding）；双轴准入 H_BE 低 + D_ledger 低 → 本地可执行。

---

## 4. 案例三：会话健康监控（ctx-monitor）

### 用户故事
> 作为系统运维者，我希望在长会话（30+轮）退化到不可用之前收到预警，并自动获得降级建议。

### 场景
研究任务已运行 28 轮，上下文从 20K 涨到 80K chars，经历 22 次 ILE。熵趋势从下降转为上升。再过 5 轮任务将失败。

### 验收标准
- ✅ 在第 25-28 轮（失效前 3-5 轮）触发预警
- ✅ 预警包含具体信号值 + 建议动作
- ✅ 可配置为自动路由（切云端）或仅告警
- ✅ 预警有明确的假阳性/假阴性率

### 系统解决方案推演

**Signal Layer 趋势采集**：
```
turn 20: h_be=0.45, retention=0.95 → 正常
turn 22: h_be=0.52, retention=0.82, ile=true → 开始退化
turn 24: h_be=0.61, retention=0.71, ile=true
turn 26: h_be=0.78, retention=0.58, ile=true ← 触发预警
turn 28: h_be=0.91, retention=0.44, ile=true → 接近失效

趋势: h_be_slope=+0.023/turn, retention_slope=-0.063/turn
```

**预警条件**（双条件 AND 控制假阳性）：
- `h_be_slope > 0.015/turn` AND 持续 ≥ 5 轮
- OR `retention < 0.5`
- OR `cumulative_ile > 15`

**假阳性率（exp-1 对照组数据）**：
- 单条件(hbe_slope): 7/20 = 35%（假阳性偏高）
- 双条件(AND retention): 0/20 = 0%（假阳性受控）
- 灵敏度(治疗组): 8/13 = 62%（可检测到多数退化）

**响应动作**：
- 方案 A（自动路由）：P5 → action="route_cloud" → 剩余任务切云端
- 方案 B（任务分解）：P1 → 拆成小子任务 → 避免继续膨胀
- 方案 C（接受降级）：继续本地，记录降级原因

---

## 5. 案例四：多文件代码修复（code-fix）

### 用户故事
> 作为开发者，我希望系统能修复一个跨 3 个文件的 bug，并在提交前验证一致性。

### 场景
Bug：A.py 的 process() 返回类型从 str 改为 dict。影响：B.py 的 3 处调用需适配。测试：C_test.py 的 2 个用例需更新。

### 验收标准
- ✅ 三个文件作为一个原子变更集（全改或全不改）
- ✅ 提交前机械验证跨文件一致性
- ✅ 模型在修改 B.py 时可通过召回获取 A.py 的新签名
- ✅ 本地 35B 可完成

### 系统解决方案推演

**P1 Decompose（按依赖分解）**：
```
SubTask 1: 修改 A.py(process 函数)
SubTask 2: 适配 B.py 调用 (依赖 ST1 的新签名)
SubTask 3: 更新 C_test.py 断言 (依赖 ST1)
依赖: ST1 → {ST2, ST3}(可并行)
```

**P4 Recall（跨文件信息传递）**：
```
ST2 执行时模型需要 A.py 的新返回类型
→ 如果截断丢失了 → P4 Recall("A.py process 返回类型")
→ ManifestLine: "u:t-a, handle=A.py, head=def process"
→ 模型看到新签名 → 正确适配 B.py 的调用
```

**P3 Verify（跨文件一致性）**：
```
L1 机械: B.py 中无 .strip() 等 str 方法调用(新返回是 dict)
L2 语义: 模型判断"是否有遗漏的调用点"
```

**预计耗时**：3 × 10s + 验证 + commit ≈ 35s

---

## 6. 案例五：压缩成本评估（compress-cost）

### 用户故事
> 作为架构师，我希望量化"截断 1K chars 损失多少信息"的经验值，以便为不同任务类型选择最优截断策略。

### 场景
同一批 30 个任务，分别用 keep=12 / 24 / 600 运行，对比信号、结局、性能。

### 验收标准
- ✅ 输出压缩汇率（bits per K chars）
- ✅ 输出性能指标（轮次膨胀比/缓存命中率）
- ✅ 输出结局指标（resolved 率变化）
- ✅ 给出推荐截断阈值

### 系统解决方案推演

**实验协议**（复用 exp-1 基建）：
```
对照: keep=600, 30 任务
治疗: keep=24, 30 任务 (--force)
台账: experiment_ledger.begin()
指纹: config_fingerprint(keep=24)
```

**分析命令**（一条命令出全部指标）：
```bash
python3 tools/trace_query.py ifc \
  --verdicts ~/APP/swe-eval/results/runs.jsonl \
  --cohort keep_messages=24 --limit 200
```

**exp-1 已有数据**：
- 压缩汇率：对数模型 ΔH=−0.207+0.029·ln(x+1), R²=0.25
- 死亡螺旋：keep=12 + code 任务 = 734x Read + 0% cache
- resolved 降幅：keep=12 时 −10pp（方向正确但不显著）

---

## 7. 五案例协议覆盖矩阵

| 案例 | P1 Decompose | P2 Execute | P3 Verify | P4 Recall | P5 Escalate | Signal |
|---|---|---|---|---|---|---|
| **防死循环** | △(split 兜底) | ✅(reload 后) | — | ✅(核心) | ✅(触发) | ✅(reread) |
| **Wiki 更新** | ✅(按论断分) | ✅(15 次) | ✅(引用校验) | — | △(retry) | △(准入) |
| **会话监控** | △(分解选项) | — | — | — | ✅(路由) | ✅(核心) |
| **代码修复** | ✅(按文件分) | ✅(3 次) | ✅(跨文件) | ✅(签名召回) | △(retry) | △(监测) |
| **压缩评估** | — | — | — | — | — | ✅(核心) |

**结论**：
- **P4 Recall + Signal** 覆盖最多案例（4/5）→ **最高优先建设**
- **P2 Execute + P3 Verify** 是所有案例的基础 → 已有基础实现
- **P5 Escalate** 在防死循环和会话监控中不可替代 → 需实现
- **P1 Decompose** 在 wiki/code 场景是质量关键 → 需实现

---

## 8. 系统解决方案验证状态

| 原理 | 案例验证 | 实验数据 | 状态 |
|---|---|---|---|
| 验证优势(P3) | Wiki/Code | B1 信度 0.995; 机械校验可靠 | ✅ 已验证 |
| 锚充分性(P4) | 防死循环/代码修复 | manifest 100% 覆盖; recall 待挂载 | 🟡 机制✅ 效果待测 |
| 可测升级(P5) | 防死循环/会话监控 | reread_pressure 与死循环相关 | 🟡 信号✅ 阈值待调 |
| 无损分解(P1) | Wiki/代码修复 | wiki+opencode 实践; exp-2 反面 | 🟡 实践✅ 正式实验待做 |
| 压缩汇率 | 压缩评估 | exp-1: R²=0.25 对数模型 | 🟡 首版已拟合 |

---

## 9. 对路线图的影响

| 优先级 | 行动 | 验证案例 | 依赖 |
|---|---|---|---|
| **最高** | ctx_recall 挂载（P4 上线） | 防死循环 + 代码修复 | pipeline.py 空出 |
| **高** | P1 Decompose 实现 | Wiki + 代码修复 | signal_types.py |
| **高** | P5 Escalate 实现 | 防死循环 + 会话监控 | protocol_types.py |
| **中** | 会话监控预警 | 会话监控 | Signal 趋势计算 |
| **中** | 跨 harness 审计 | 压缩评估(跨 harness) | 现有 trace_query |
| **低** | wiki 9B pilot | Wiki 更新 | P1+P2+P3 全上线 |

---

*关联实验数据：[exp-1 报告](../04-analysis-diagnostics/amnesia-experiment-report-20260830.md) · B1 噪声底 · 压缩汇率拟合*
