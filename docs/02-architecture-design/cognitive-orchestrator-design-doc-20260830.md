# 设计文档：认知编排器详细设计

> **版本**: v1.0 ｜ **日期**: 2026-08-30
> **上游**: PRD v4.0 (R11-R15) + 三层架构规范 v1.0
> **范围**: 模块设计 + 类/函数接口 + 数据流 + 依赖关系 + 部署视图

---

## 1. 模块清单与依赖关系

### 1.1 新建模块（按依赖顺序）

```
signal_types.py          ← 叶子(零依赖)
    ↑
protocol_types.py        ← 依赖 signal_types
    ↑
contract_registry.py     ← 依赖 signal_types + protocol_types (仅引用名称)
    ↑
idempotency.py           ← 依赖 protocol_types
    ↑
decompose.py             ← 依赖 protocol_types + signal_types
    ↑
escalate.py              ← 依赖 protocol_types + signal_types
    ↑
protocol_orchestrator.py ← 依赖上述全部 + ctx_recall + memory_stores
```

### 1.2 修改的现有模块

| 模块 | 修改内容 | 原因 |
|---|---|---|
| `memory_stores.py` | ManifestLine 显式化为 TypedDict | 契约显式化 |
| `ctx_recall.py` | RecallQuery/RecallResult 显式化 | 契约显式化 |
| `proxy_config.py` | 注册 PROTOCOL_* 配置项 | R12 配置需求 |

### 1.3 依赖关系图（无环验证）

```
signal_types.py (0 deps)
    ↑
protocol_types.py (1: signal_types)
    ↑                    
contract_registry.py (2: signal_types, protocol_types)
    ↑                    
idempotency.py (1: protocol_types)
    ↑                    
decompose.py (2: protocol_types, signal_types)
    ↑                    
escalate.py (2: protocol_types, signal_types)
    ↑                    
protocol_orchestrator.py (7: 上述全部 + ctx_recall + memory_stores)
```

---

## 2. 模块详细设计

### 2.1 signal_types.py

```python
"""Signal Layer 的数据契约——Signal 生产者与 Protocol 消费者之间的接口。"""

from typing import List, Optional, Dict, Any

# ===== ViewSummary（视图摘要——差分计算的基线）=====
class ViewSummary(TypedDict, total=False):
    n_msgs: int
    total_chars: int
    rationale_chars: int
    units: Dict[str, Dict]  # anchor → unit dict
    tool_names: List[str]

# ===== DiffResult（视图差分——ILE 判定的输入）=====
class DiffResult(TypedDict, total=False):
    dropped_units: int
    dropped_chars: int
    dropped_rationale_chars: int
    shrunk_units: int
    shrunk_chars: int
    added_units: int
    prev_total_chars: int
    prev_rationale_chars: int
    view_reset: bool

# ===== ReconcileResult（台账对账——D_ledger）=====
class ReconcileResult(TypedDict, total=False):
    d_ledger: float
    hit: int
    total: int
    extras: int

# ===== SignalSnapshot（信号聚合——Protocol 决策输入）=====
class SignalSnapshot(TypedDict, total=False):
    """某时刻的完整信号快照——P1 分解判据 + P5 升级触发的输入。

    由 Signal Layer(ifc_metrics) 生产，Protocol Layer 消费。
    所有字段 Optional——Signal 层故障时 Protocol 层以默认值运行(fail-open)。
    """
    contract_version: int  # =1
    # 信念质量
    h_be: Optional[float]
    h_be_trend: Optional[float]
    d_ledger: Optional[float]
    # 上下文质量
    retention: Optional[float]
    rationale_ratio: Optional[float]
    ile: bool
    ile_kinds: List[str]
    view_reset: bool
    # 行为信号
    reread_pressure: int
    action_diversity: Optional[float]
    # 综合估计
    cognitive_load: float
    # 环境标记
    config_fingerprint: str
    session_key: str
    turn: int
```

### 2.2 protocol_types.py

```python
"""Protocol Layer 的数据契约——五协议 + 编排器的公共词汇。"""

# ===== Task =====
class Task(TypedDict, total=False):
    contract_version: int  # =1
    id: str
    description: str
    input_files: List[str]
    expected_output_type: str  # "patch" | "text" | "analysis"
    constraints: Dict[str, Any]
    parent_task_id: Optional[str]
    depth: int

# ===== SubTask =====
class SubTask(Task):
    verification_rules: List[VerificationRule]
    dependencies: List[str]
    budget_tokens: int

# ===== VerificationRule =====
class VerificationRule(TypedDict):
    name: str
    check_type: str  # "string_match" | "diff_apply" | "format" | "semantic"
    parameters: Dict[str, Any]

# ===== Patch =====
class Patch(TypedDict, total=False):
    contract_version: int
    task_id: str
    diff: str
    citations: List[Citation]
    model_confidence: float
    generation_metadata: Dict[str, Any]

# ===== Citation =====
class Citation(TypedDict):
    file: str
    quote: str
    line_range: Tuple[int, int]
    context: str

# ===== Verdict =====
class Verdict(TypedDict, total=False):
    contract_version: int
    patch_id: str
    passed: bool
    checks: List[CheckResult]
    verify_level: str  # "mechanical" | "semantic" | "ledger"
    total_cost_ms: int

# ===== CheckResult =====
class CheckResult(TypedDict):
    rule_name: str
    passed: bool
    detail: str

# ===== EscalationDecision =====
class EscalationDecision(TypedDict, total=False):
    contract_version: int
    task_id: str
    action: str  # "retry" | "reload" | "split" | "route_cloud" | "human"
    reason: str
    signals_at_decision: Dict[str, Any]  # SignalSnapshot 的子集
    attempt_count: int
    refined_task: Optional[SubTask]

# ===== SessionState（黑板）=====
class SessionState(TypedDict, total=False):
    session_key: str
    turn: int
    config: Dict[str, Any]
    signal_history: List[Dict[str, Any]]
    task_queue: List[SubTask]
    attempt_log: List[Dict[str, Any]]
    baseline_view: Optional[Dict]
```

### 2.3 contract_registry.py

```python
"""契约注册表——所有数据契约的元数据管理与运行时验证。"""

CONTRACT_VERSION = 1

CONTRACT_REGISTRY: Dict[str, Dict[str, Any]] = {
    "SignalSnapshot": {
        "version": 1, "module": "signal_types", "typed_dict": "SignalSnapshot",
        "producer": ["ifc_metrics.build_ifc_section"],
        "consumers": ["P1_decompose", "P5_escalate", "post_governance"],
        "join_keys": ["session_key", "turn"],
    },
    "Task": {
        "version": 1, "module": "protocol_types", "typed_dict": "Task",
        "producer": ["application_layer"],
        "consumers": ["P1_decompose", "P2_execute"],
        "join_keys": ["task_id"],
    },
    "SubTask": {
        "version": 1, "module": "protocol_types", "typed_dict": "SubTask",
        "producer": ["P1_decompose"],
        "consumers": ["P2_execute", "P3_verify"],
        "join_keys": ["task_id", "id"],
    },
    "Patch": {
        "version": 1, "module": "protocol_types", "typed_dict": "Patch",
        "producer": ["P2_execute"],
        "consumers": ["P3_verify", "git_commit", "post_governance"],
        "join_keys": ["task_id"],
    },
    "Verdict": {
        "version": 1, "module": "protocol_types", "typed_dict": "Verdict",
        "producer": ["P3_verify"],
        "consumers": ["P5_escalate", "post_governance"],
        "join_keys": ["patch_id"],
    },
    "EscalationDecision": {
        "version": 1, "module": "protocol_types", "typed_dict": "EscalationDecision",
        "producer": ["P5_escalate"],
        "consumers": ["post_governance", "circuit_breaker"],
        "join_keys": ["task_id"],
    },
    "ManifestLine": {
        "version": 1, "module": "memory_stores", "typed_dict": None,
        "producer": ["truncation_fifo_hook", "context_engine_collapse_hook"],
        "consumers": ["P4_recall", "E1_audit", "trace_query"],
        "join_keys": ["session_key", "anchor"],
    },
    "SessionState": {
        "version": 1, "module": "protocol_types", "typed_dict": "SessionState",
        "producer": ["protocol_orchestrator._init_state"],
        "consumers": ["所有协议"],
        "join_keys": ["session_key"],
    },
}

def validate_contract(data, contract_name, strict=False) -> List[str]:
    """fail-open 验证——返回错误列表(空=通过)。"""
    ...

def get_consumers(contract_name) -> List[str]: ...
def get_producer_contracts(protocol_name) -> Dict[str, List[str]]: ...
def evolve_contract(name, changes) -> None: ...
```

### 2.4 decompose.py（P1）

```python
"""P1 Decompose Protocol——双判据递归分解。"""

class DecompositionCriterion:
    """分解判据基类——策略模式，可插拔。"""
    def should_split(self, task: Task, signal: SignalSnapshot) -> bool: ...
    def split(self, task: Task) -> List[SubTask]: ...

class CapacityCriterion(DecompositionCriterion):
    """容量判据——est_tokens(task.input_files) > budget 时分裂。"""
    def __init__(self, budget_tokens: int = 30000): ...

class EntropyCriterion(DecompositionCriterion):
    """熵判据——signal.h_be > theta_h 时精炼纠缠论断。"""
    def __init__(self, theta_h: float = 0.9): ...

class CouplingCriterion(DecompositionCriterion):
    """耦合度判据(远期)——子问题互信息 > kappa 时分裂。"""

class Decomposer:
    """P1 分解器——多判据递归。"""

    def __init__(self, criteria: List[DecompositionCriterion]):
        self.criteria = criteria

    def decompose(self, task: Task, signal: SignalSnapshot) -> List[SubTask]:
        """递归分解直到所有子任务满足所有判据。

        返回按依赖拓扑排序的 SubTask 列表。
        如果熵不随分解下降 → 返回 EscalationDecision(route_cloud)。
        """
        for criterion in self.criteria:
            if criterion.should_split(task, signal):
                subs = criterion.split(task)
                return [s for sub in subs for s in self.decompose(sub, signal)]
        return [self._to_subtask(task)]

    def _to_subtask(self, task: Task) -> SubTask:
        """将原子 Task 转为 SubTask（添加验证规则和预算）。"""
        return SubTask(
            **task,
            verification_rules=self._default_rules(task),
            dependencies=[],
            budget_tokens=self._estimate_budget(task),
        )
```

### 2.5 escalate.py（P5）

```python
"""P5 Escalate Protocol——验证失败后的策略切换。"""

class TaskState(Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    PASSED = "passed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_SPLITTABLE = "failed_splittable"
    FAILED_ROUTE = "failed_route"
    ESCALATED = "escalated"
    COMMITTED = "committed"

class TaskCircuitBreaker:
    """同类任务熔断——连续失败 N 次后打开，冷却期内直接升级。"""
    def __init__(self, threshold: int = 3, cooldown_s: int = 300): ...
    def can_execute(self, task_type: str) -> bool: ...
    def record_failure(self, task_type: str) -> None: ...
    def record_success(self, task_type: str) -> None: ...

class Escalator:
    """P5 升级决策器——决策表驱动。"""

    def __init__(self, max_retries: int = 2, breaker: TaskCircuitBreaker):
        self.max_retries = max_retries
        self.breaker = breaker

    def escalate(self, patch: Patch, verdict: Verdict,
                 signal: SignalSnapshot, attempt: int,
                 task_type: str) -> EscalationDecision:
        """根据验证结果+信号+尝试次数决定升级动作。

        决策表:
          attempt < 2 AND 错误可修复 → retry
          attempt < 2 AND 上下文过期(reread_pressure) → reload
          负荷 > 硬限 → split
          H_BE 持续高 → route_cloud
          attempt >= 2 → hard_escalate (人工)
          熔断器打开 → route_cloud (不重试)
        """
        # 熔断检查(优先级最高)
        if not self.breaker.can_execute(task_type):
            return self._decision("route_cloud", "circuit_breaker_open")

        # 幂等闸
        if attempt >= self.max_retries:
            return self._decision("human", "max_retries_exceeded")

        # 信号驱动
        if signal.get("reread_pressure", 0) >= 2:
            return self._decision("reload", "reread_pressure_high")
        if signal.get("cognitive_load", 0) > self.hard_limit:
            return self._decision("split", "cognitive_load_exceeded")
        if signal.get("h_be_trend", 0) > 0.02:
            return self._decision("route_cloud", "hbe_trend_rising")

        # 默认: 重试
        return self._decision("retry", "verification_failed")
```

### 2.6 protocol_orchestrator.py

```python
"""ProtocolOrchestrator——五协议串联入口（编排器）。"""

class ProtocolOrchestrator:
    """认知编排器——Protocol Layer 的唯一公开接口。

    设计模式:
      - Blackboard: SessionState 作为共享黑板,协议不互相引用
      - Dependency Injection: generator/verifier/escalator 通过构造函数注入
      - Strategy: 分解判据和验证级别可插拔
    """

    def __init__(self,
                 generator: ModelGenerator,      # P2: 模型生成器(可 mock)
                 verifier: VerificationChain,     # P3: 验证责任链
                 decomposer: Decomposer,          # P1: 分解器
                 escalator: Escalator,            # P5: 升级决策器
                 idempotency: IdempotencyManager, # 幂等缓存
                 model_semaphore: threading.Semaphore,  # 模型锁
                 ):
        self.gen = generator
        self.verifier = verifier
        self.decomposer = decomposer
        self.escalator = escalator
        self.idem = idempotency
        self.sem = model_semaphore

    def solve(self, task: Task) -> SolveResult:
        """主入口——分解→执行→验证→升级的完整循环。"""
        state = self._init_state(task)
        signal = self._get_signal(state)

        # P1: 分解
        subtasks = self.idem.decompose_idempotent(
            task, self.decomposer, signal)
        state["task_queue"] = subtasks

        # 逐子任务执行
        results = []
        for sub in self._topological_order(subtasks):
            result = self._solve_subtask(sub, state)
            if result:
                results.append(result)

        return self._aggregate(results, task)

    def _solve_subtask(self, sub: SubTask,
                       state: SessionState) -> Optional[Patch]:
        """单子任务的 execute→verify→escalate 循环。"""
        for attempt in range(self.escalator.max_retries + 1):
            # P2: 加载工作集 + 执行(占模型锁)
            working_set = self._load_verbatim(sub, state)
            with self.sem:  # 模型级串行
                patch = self.gen.generate(sub, working_set)

            # P3: 验证(不占模型锁)
            signal = self._get_signal(state)
            verdict = self.idem.verify_l1_idempotent(
                patch, sub.verification_rules, signal)

            if verdict["passed"]:
                self._commit(patch, state)
                self.escalator.breaker.record_success(sub["task_type"])
                return patch

            # P5: 升级决策
            decision = self.escalator.escalate(
                patch, verdict, signal, attempt, sub["task_type"])
            state["attempt_log"].append(decision)

            match decision["action"]:
                case "retry":
                    continue
                case "reload":
                    # P4: 召回补全上下文
                    recalled = ctx_recall.lookup(
                        decision["query"]["session_key"],
                        decision["query"]["query"])
                    self._augment_working_set(recalled, state)
                    continue
                case "split":
                    subs = self.decomposer.decompose(
                        decision["refined_task"], signal)
                    return self._solve_subtasks_seq(subs, state)
                case "route_cloud" | "human":
                    self.escalator.breaker.record_failure(sub["task_type"])
                    return None  # 外部处理

        return None  # 不应到达(幂等闸保证)
```

---

## 3. 配置参数

| 参数 | 默认 | 层 | 说明 |
|---|---|---|---|
| `PROTOCOL_BUDGET_TOKENS` | 30000 | P1 | 子任务输入集 token 预算 |
| `PROTOCOL_THETA_H` | 0.9 | P1 | 分解停止的熵阈值 |
| `PROTOCOL_MAX_RETRIES` | 2 | P5 | 幂等闸 |
| `PROTOCOL_VERIFY_SEMANTIC_RATIO` | 0.2 | P3 | L2 语义验证抽样比例 |
| `PROTOCOL_RECALL_LIMIT` | 8 | P4 | 召回返回上限 |
| `PROTOCOL_BUDGET_EXECUTE_S` | 30 | 性能 | Execute 超时 |
| `PROTOCOL_BUDGET_VERIFY_S` | 10 | 性能 | Verify 超时 |
| `PROTOCOL_BUDGET_RECALL_S` | 2 | 性能 | Recall 超时 |
| `PROTOCOL_PIPELINE_CONCURRENT` | 3 | 并发 | 管线并发(模型仍=1) |
| `PROTOCOL_BREAKER_THRESHOLD` | 3 | P5 | 熔断阈值 |
| `PROTOCOL_BREAKER_COOLDOWN_S` | 300 | P5 | 熔断冷却期 |

---

## 4. 测试策略

| 层级 | 测试文件 | 覆盖内容 |
|---|---|---|
| 契约 | `test_contract_alignment.py` | 注册表一致性+协议对齐 |
| P1 | `test_decompose.py` | 判据组合+边界条件 |
| P2 | `test_execute.py` | MockGenerator+引用校验 |
| P3 | `test_verify.py` | 三级验证+责任链 |
| P4 | `test_ctx_recall.py` (已有) | FTS5+子串+降级 |
| P5 | `test_escalate.py` | 决策表+幂等闸+熔断 |
| 编排 | `test_orchestrator.py` | 端到端(mock 后端) |
| 集成 | `test_protocol_integration.sh` | 真实后端+完整流程 |
