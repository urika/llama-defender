# 三层架构正式规范：分层定义、协议、数据契约

> **状态**：架构规范（v1.0）｜**日期**：2026-08-30
> **方法论**：OSI→TCP/IP 式简化——理论五层压缩为落地三层，17 种策略砍至 5 个核心协议
> **关联**：[分层理论框架](layered-theory-framework-20260830.md)（理论五层）· [PDC](progressive-disclosure-context-serving-design-20260829.md) · [IFC](information-fidelity-control-design-20260829.md) · [认知策略编排器](cognitive-strategies-orchestrator-design-20260830.md)
> **实现约束**：Python 3.9 stdlib only；与现有 proxy/pipeline 兼容；每协议独立可测

---

## 1. 分层定义

### 1.1 Signal Layer（信号层）

```
职责: 测量模型信念质量与上下文信息质量,产出类型化信号
不职责: 做决策(阈值判断归 Protocol Layer)、存储内容(归基础设施)
已实现: ✅ ifc_metrics.py + diagnostics.py + hbe_probe.py + memory_stores.py
```

| 接口 | 输入 | 输出 | 来源 |
|---|---|---|---|
| `view_summary(messages)` | 消息列表 | `ViewSummary`（锚集合/字符量/类型分布） | `ifc_metrics.py` |
| `diff_views(prev, cur)` | 两个 `ViewSummary` | `DiffResult`（丢弃/收缩/重置） | `ifc_metrics.py` |
| `reconcile(answer, ledger_paths)` | 探针答案 + 台账材料 | `ReconcileResult`（D_ledger/命中/总数） | `ifc_metrics.py` |
| `config_fingerprint()` | — | `Fingerprint`（参数快照 + hash） | `diagnostics.py` |
| `build_ifc_section(prev, cur, actions, manifest_count)` | 上述结果 | `IFCSection`（完整 ifc 段） | `ifc_metrics.py` |

### 1.2 Protocol Layer（协议层）

```
职责: 编排思考过程——分解、执行、验证、召回、升级
不职责: 测量(归 Signal)、理解任务语义(归 Application 的领域知识)
已实现: 部分——execute/verify 基础版在 harness 中;recall 核心在 ctx_recall.py(未挂载);
        decompose/escalate 设计完成未实现
```

### 1.3 Application Layer（应用层）

```
职责: 领域知识的承载——wiki 维护、代码修复、研究综合
不职责: 思考过程的编排(归 Protocol)、信号测量(归 Signal)
实现: 外部(swe-eval runner / opencode / 人工)
```

### 1.4 基础设施（跨层共享，非独立层）

```
manifest 存储:  memory_stores.ManifestStore
台账:           session_ledger.LedgerStore
档案:           session_ledger.ArchiveStore
git:            外部进程
SQLite FTS5:    ctx_recall.py 内部
```

---

## 2. 五个核心协议规范

### P1: Decompose（分解协议）

```python
def decompose(task: Task, budget: int, theta_h: float) -> List[SubTask]:
    """将大任务分解为可独立执行的子任务队列。

    前置条件:
        task.cognitive_load > budget  OR  probe_entropy(task) > theta_h

    停止条件（递归终止）:
        子任务输入集 ≤ budget  AND  探针 H_BE ≤ theta_h
        （两者同时满足才停止分解）

    升级退出（无法分解）:
        熵不随分解下降 → 模型能力边界 → 返回 EscalationDecision(route_cloud)

    后置条件:
        ① 每个 SubTask.input_files 总 token ≤ budget
        ② 依赖关系构成 DAG（无环）
        ③ 所有子任务输出的合并 ≡ 原任务输出
    """
```

**决策树**:
```
task 到来
  ├─ cognitive_load ≤ budget AND H_BE ≤ θ → 直接 Execute（不需要分解）
  ├─ cognitive_load > budget → 按引用图切分 → 递归检查每个子任务
  ├─ H_BE > θ（模型读不懂）→ 精炼纠缠论断 → 递归
  └─ 分解后熵不降 → Escalate(route_cloud)
```

### P2: Execute（执行协议）

```python
def execute(subtask: SubTask, working_set: List[FileContent],
            tools: List[Tool]) -> Patch:
    """在一个子任务上运行模型，产出带引用的补丁。

    前置条件:
        working_set 已逐字加载（操作级 V=H）
        subtask.input_files 的内容全部在 working_set 中

    核心约束:
        模型输出的每条引用必须指向 working_set 中的文件
        输出格式为统一 diff + 引用列表

    后置条件:
        Patch.diff 是可应用的 unified diff
        Patch.citations 中每个 (file, quote) 在 working_set 中可验证存在
    """
```

### P3: Verify（验证协议）

```python
def verify(patch: Patch, rules: List[VerificationRule],
           ledger_facts: Optional[LedgerFacts]) -> Verdict:
    """机械校验补丁的正确性。

    三级验证（成本递增，精度递增）:
        Level 1 (机械, 零模型成本):
            - citation_existence: 每条引用的 quote 在 cited file 中存在
            - diff_validity: patch 可干净地应用到 base files
            - format_compliance: 输出格式符合预期 schema

        Level 2 (语义, 模型成本, 按比例抽样):
            - content_accuracy: patch 内容与任务要求一致
            - side_effect_check: patch 不破坏无关功能
            ⚠️ 相关性失败风险(Self-Refine, 2023): 语义验证用同模型
               → 评论者与生成者共享偏见 → 错误可能不触发警报。
               应优先使用 L1 机械校验; L2 仅作补充, 不能替代 L1。
               缓解: D_ledger 对账提供外部 ground truth, 不受此偏见影响。

        Level 3 (对账, 台账成本):
            - d_ledger_check: patch 中声称的事实与台账一致
            → 不受相关性失败影响(外部真值, 非模型自评)

    验证策略: Level 1 全量执行; Level 2 按 PROXY_VERIFY_SEMANTIC 比例抽样;
              Level 3 在有台账数据时执行

    后置条件:
        Verdict.passed=True → patch 可 commit
        Verdict.passed=False → Verdict.errors 非空,供 Escalate 使用
    """
```

### P4: Recall（召回协议）

```python
def recall(query: RecallQuery, manifest: ManifestStore) -> RecallResult:
    """从 manifest 中检索被截断的信息单元。

    检索层级:
        L1: 内存子串匹配（快, 短查询兜底）
        L2: SQLite FTS5 trigram 全文检索（准, ≥3 字符）

    后置条件:
        无匹配 → 返回空列表（不是错误——信息可能从未被丢弃）
        有匹配 → 按相关性排序,每条含稳定 anchor 可供后续定位
    """
```

### P5: Escalate（升级协议）

```python
def escalate(failed_patch: Patch, signals: SignalSnapshot,
             attempt_count: int) -> EscalationDecision:
    """验证失败后的策略切换决策。

    决策表（attempt_count 从 0 开始）:
        ┌────────────────┬──────────────────────┬────────────────┐
        │ 条件            │ 动作                  │ 产出           │
        ├────────────────┼──────────────────────┼────────────────┤
        │ attempt < 2    │ retry                │ 重构 prompt    │
        │ AND 错误可修复  │                      │                │
        ├────────────────┼──────────────────────┼────────────────┤
        │ attempt < 2    │ reload (recall+重执行) │ 重载工作集     │
        │ AND 上下文过期  │                      │                │
        ├────────────────┼──────────────────────┼────────────────┤
        │ 负荷 > 硬限     │ split                │ 更细的子任务   │
        ├────────────────┼──────────────────────┼────────────────┤
        │ H_BE 持续高     │ route_cloud          │ 云端路由       │
        ├────────────────┼──────────────────────┼────────────────┤
        │ attempt ≥ 2    │ hard_escalate        │ 人工/标记放弃   │
        └────────────────┴──────────────────────┴────────────────┘

    不变式（幂等闸）:
        同一子任务的升级路径不会产生第三次同层重试

    后置条件:
        所有升级决策记录到 attempt_log（供后治理分析）
    """
```

---

## 3. 数据契约（关键类型定义）

### 3.1 跨层流动的核心类型

```python
# ===== Signal Layer → Protocol Layer =====

class SignalSnapshot(TypedDict):
    """某时刻的信号快照——Protocol 层的决策输入"""
    h_be: Optional[float]          # 信念熵（bits），None=无探针
    h_be_trend: Optional[float]    # 会话级熵趋势斜率
    d_ledger: Optional[float]      # 台账对账偏差 [0,1]
    retention: Optional[float]     # 视图保留率 [0,1]
    rationale_ratio: Optional[float]  # 动机存活率 [0,1]
    reread_pressure: int           # 重读压力（最近N轮重复读取次数）
    action_diversity: Optional[float]  # 动作多样性 [0,1]
    ile: bool                       # 本轮是否发生信息损失
    ile_kinds: List[str]            # ["unit_drop", "compress_drop"]
    cognitive_load: float           # 综合认知负荷估计
    config_fingerprint: str         # 当前配置 hash（10 位）


# ===== Protocol Layer 内部 =====

class Task(TypedDict):
    """待执行的任务——Application 层创建,Protocol 层消费"""
    id: str                         # 任务唯一标识
    description: str                # 任务描述（自然语言）
    input_files: List[str]          # 需要的文件路径列表
    expected_output_type: str       # "patch" | "text" | "analysis"
    constraints: Dict[str, Any]     # 领域特定约束
    parent_task_id: Optional[str]   # 父任务 ID（分解产物）
    depth: int                      # 分解深度（0=原始任务）


class SubTask(Task):
    """分解后的子任务——Decompose 的输出, Execute 的输入"""
    verification_rules: List[VerificationRule]
    dependencies: List[str]         # 依赖的其他 SubTask.id
    budget_tokens: int              # 本子任务的 token 预算


class VerificationRule(TypedDict):
    """验证规则——每条对应一个机械检查"""
    name: str                       # 规则名（如 "citation_existence"）
    check_type: str                 # "string_match" | "diff_apply" | "format" | "semantic"
    parameters: Dict[str, Any]      # 规则参数


class Patch(TypedDict):
    """Execute 的输出——带引用的补丁"""
    task_id: str
    diff: str                       # unified diff 格式
    citations: List[Citation]       # 引用列表
    model_confidence: float         # 模型自评置信度 [0,1]
    generation_metadata: Dict[str, Any]  # token 用量、延迟等


class Citation(TypedDict):
    """单条引用——Verify P3 的校验对象"""
    file: str                       # 被引用文件路径
    quote: str                      # 被引用的具体内容片段
    line_range: Tuple[int, int]     # 行号范围
    context: str                    # 引用上下文（为什么引用这段）


class Verdict(TypedDict):
    """Verify 的输出——补丁是否通过"""
    patch_id: str
    passed: bool
    checks: List[CheckResult]       # 每条规则的执行结果
    verify_level: str               # "mechanical" | "semantic" | "ledger"
    total_cost_ms: int              # 验证耗时


class CheckResult(TypedDict):
    """单条验证规则的结果"""
    rule_name: str
    passed: bool
    detail: str                     # 失败原因（供 Escalate 分析）


# ===== Recall 协议 =====

class RecallQuery(TypedDict):
    """召回查询"""
    session_key: str
    query: str                      # 关键词/路径/工具名
    kind: Optional[str]            # "tool_use" | "tool_result" | "text"
    limit: int                      # 返回条数上限


class RecallResult(TypedDict):
    """召回结果"""
    query: RecallQuery
    matches: List[ManifestLine]     # 匹配的索引行
    search_method: str              # "fts5" | "substring"
    latency_ms: int


# ===== Escalate 协议 =====

class EscalationDecision(TypedDict):
    """升级决策"""
    task_id: str
    action: str                     # "retry" | "reload" | "split" | "route_cloud" | "human"
    reason: str                     # 触发原因描述
    signals_at_decision: SignalSnapshot  # 决策时的信号快照
    attempt_count: int
    refined_task: Optional[SubTask]  # split/reload 时的新任务


# ===== Manifest（基础设施,支撑 Recall）=====

class ManifestLine(TypedDict):
    """manifest 索引行——每个被丢弃单元一条"""
    turn: int                       # 丢弃发生的轮次
    reason: str                     # "fifo_drop" | "oom_drop" | "epoch_collapse"
    anchor: str                     # 稳定单元 ID（如 "u:t-ifc1"）
    kind: str                       # "tool_use" | "tool_result" | "text"
    role: str                       # "user" | "assistant" | "system"
    tool: str                       # 工具名（如 "Read"）
    handle: Optional[Handle]        # 统一句柄 {"type": "path", "value": "/src/a.py"}
    size_chars: int                 # 被丢弃内容的字符数
    head: str                       # 前 120 字符（可检索体）
    ts: str                         # ISO 时间戳


# ===== Session State（跨协议共享）=====

class SessionState(TypedDict):
    """会话级状态——所有协议共享"""
    session_key: str
    turn: int
    config: Dict[str, Any]          # config_fingerprint 的展开
    manifest: ManifestStore          # manifest 存储实例
    ledger: LedgerStore              # 台账实例
    signal_history: List[SignalSnapshot]  # 信号历史（趋势计算用）
    task_queue: List[SubTask]        # 待执行子任务队列
    attempt_log: List[EscalationDecision]  # 升级历史（后治理用）
    baseline_view: Optional[ViewSummary]    # 上一轮视图（retention 计算）
```

### 3.2 类型间的关系图

```
Application 创建
     │
     ▼
   Task ──── Decompose(P1) ────→ List[SubTask]
                                        │
                                        ▼
                              Execute(P2) ──→ Patch
                                               │
                                               ▼
                              Verify(P3) ──→ Verdict
                                               │
                              ┌────────────────┤
                              │ passed         │ failed
                              ▼                ▼
                           commit         Escalate(P5)
                                              │
                              ┌─────────┬─────┴────┬─────────┐
                              │ retry   │ reload   │ split   │ route
                              ▼         ▼          ▼         ▼
                           Execute  Recall(P4)  Decompose  external
                              │         │          │(P1)
                              │    ManifestStore   │
                              │         │          │
                              └─────────┴──────────┘
                                        │
                                 Signal Layer
                              (H_BE/ILE/D_ledger/retention)
                              → SignalSnapshot → 驱动 P1/P5 决策
```

---

## 4. 协议编排器（Protocol Orchestrator）

```python
class ProtocolOrchestrator:
    """五协议的编排入口——Protocol Layer 的唯一公开接口"""

    def solve(self, task: Task) -> SolveResult:
        state = self._init_state(task)

        # P1: 分解（如果需要）
        subtasks = self._decompose_if_needed(task, state)

        # 逐子任务执行
        results = []
        for sub in self._topological_order(subtasks):
            result = self._solve_subtask(sub, state)
            results.append(result)

        return self._aggregate(results, task)

    def _solve_subtask(self, sub: SubTask, state: SessionState) -> Patch:
        for attempt in range(self.max_retries + 1):  # 幂等闸
            # P2: 加载工作集 + 执行
            working_set = self._load_verbatim(sub, state)
            patch = self._execute(sub, working_set)

            # P3: 验证
            verdict = self._verify(patch, sub, state)

            if verdict.passed:
                self._commit(patch, state)
                return patch

            # P5: 升级决策
            signals = self._snapshot_signals(state)
            decision = self._escalate(patch, signals, attempt)

            match decision.action:
                case "retry":
                    continue  # 重构 prompt 后重试
                case "reload":
                    # P4: 召回补全上下文
                    recall_result = self._recall(decision.query, state)
                    self._augment_working_set(recall_result, state)
                    continue
                case "split":
                    # 递归分解
                    subtasks = self._decompose(decision.refined_task, state)
                    return self._solve_subtasks_sequentially(subtasks, state)
                case "route_cloud" | "human":
                    return self._external_route(decision, state)

        return self._hard_escalate(sub, state)  # 不应到达此处（幂等闸保证）
```

---

## 5. 与现有代码的映射

| 规范组件 | 现有文件 | 状态 |
|---|---|---|
| SignalSnapshot | `ifc_metrics.build_ifc_section()` | ✅ 已实现 |
| ManifestLine | `memory_stores.record_units()` | ✅ 已实现 |
| RecallQuery/Result | `ctx_recall.lookup()` | ✅ 已实现（未挂载） |
| Execute（小步协议） | harness 设计文档 | 🟡 设计完成 |
| Verify L1（机械） | 集成测试中的断言 | 🟡 零散存在,需整合 |
| Verify L3（对账） | `ifc_metrics.reconcile()` | ✅ 已实现 |
| Decompose | `ifc_metrics` 中的熵判据概念 | 🔴 未实现 |
| Escalate | 幂等闸设计 | 🔴 未实现 |
| ProtocolOrchestrator | — | 🔴 未实现（核心待建件） |

---

## 6. 测试策略

| 协议 | 测试方法 | 关键测试用例 |
|---|---|---|
| P1 Decompose | 单元测试（mock 模型） | 松耦合任务可分解；紧耦合任务正确升级 |
| P2 Execute | 集成测试（mock 后端） | 工作集完整加载；输出含有效引用 |
| P3 Verify | 单元测试（纯函数） | 引用存在/不存在；diff 可应用/不可应用 |
| P4 Recall | 集成测试（已有 test_ctx_recall.py） | 中文 trigram 命中；短查询子串降级 |
| P5 Escalate | 单元测试（决策表驱动） | 每种触发条件的正确动作；幂等闸不突破 |
| 编排器 | 端到端测试 | 全链路: 分解→执行→验证→commit |

---

## 7. 配置参数汇总

| 参数 | 默认 | 归属协议 | 说明 |
|---|---|---|---|
| `PROXY_IFC_BUDGET_TOKENS` | 30000 | P1 | 子任务输入集 token 预算 |
| `PROXY_IFC_THETA_H` | 0.9 | P1 | 分解停止的熵阈值 |
| `PROXY_IFC_MAX_RETRIES` | 2 | P5 | 幂等闸：同层最大重试次数 |
| `PROXY_IFC_VERIFY_SEMANTIC_RATIO` | 0.2 | P3 | Level 2 语义验证的抽样比例 |
| `PROXY_PD_RECALL_LIMIT` | 8 | P4 | 召回返回条数上限 |
| `PROXY_IFC_ESCALATE_COOLDOWN_S` | 30 | P5 | 升级后冷却时间 |

---

## 8. 非功能需求

| 需求 | 约束 | 实现方式 |
|---|---|---|
| **Fail-open** | 信号层故障不阻塞协议执行 | try/except + 默认值 + warn_suppressed |
| **有界性** | 所有存储有上限,驱逐有序 | ManifestStore/LedgerStore 已有 FIFO + MB 上限 |
| **可观测** | 每步决策可追溯 | attempt_log + signal_history + config_fingerprint |
| **可逆性** | 所有 commit 可回滚 | git 作为唯一持久化层 |
| **Stdlib only** | 核心零第三方依赖 | Python 3.9 标准库 |
| **线程安全** | 多请求并发 | 继承现有 _diag_lock / _llama_lock 模式 |
