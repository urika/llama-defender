# 开发 Spec 文档：单任务开发输入（5 份拆分）

> **上游**: PRD v4.0 + 设计文档 v1.0 ｜ **日期**: 2026-08-30
> **原则**: 每个 spec 是一个可独立开发和测试的任务单元，有明确的输入/输出/验收标准
>
> **交付状态**（2026-08-30 更新）:
> - ✅ Spec-A 数据契约 + 契约注册表（signal_types / protocol_types / contract_registry, 22 对齐测试）
> - ✅ Spec-B ctx_recall 挂载(MVP-2 已端到端验证) + P5 Escalate（escalate.py: 状态机/熔断/决策表, 21 测试）
> - ✅ Spec-C P1 Decompose + ProtocolOrchestrator + IdempotencyManager（decompose.py / idempotency.py / protocol_orchestrator.py, +72 用例, commit 58bd2a0）
> - ✅ Spec-D 设计模式 + 并发 + 性能预算（verification_chain.py 三级职责链 / PROTOCOL_BUDGETS / 并发与预算测试, commit 58bd2a0; 性能预算暂为模块常量, proxy_config.py 释放后迁 CONFIG_REGISTRY）
> - ✅ Spec-E 后治理 + 运维监控（post_governance.py: EntryLayerCalibrator / PatternCompiler / PostGovernance / data_quality_report, +31 用例；admin_server.py 并行占用, /api/data-quality 端点接线延后——data_quality_report() 已为纯函数可直接挂载）

---

## Spec A: 数据契约模块 + 契约注册表

### 任务描述
创建 `signal_types.py`、`protocol_types.py`、`contract_registry.py` 三个模块，将 14 个隐式数据契约显式化为 TypedDict，并建立契约注册表。

### 输入
- 三层架构规范 §3 数据契约定义（14 个 TypedDict）
- 现有 `ifc_metrics.py` 中的隐式返回类型

### 输出文件
```
signal_types.py      (~80 行) — ViewSummary, DiffResult, ReconcileResult, SignalSnapshot
protocol_types.py    (~150 行) — Task, SubTask, VerificationRule, Patch, Citation,
                                 Verdict, CheckResult, EscalationDecision, SessionState
contract_registry.py (~120 行) — CONTRACT_REGISTRY, validate_contract,
                                 get_consumers, get_producer_contracts
test/test_contract_alignment.py (~80 行)
```

### 关键接口
```python
# signal_types.py
class SignalSnapshot(TypedDict, total=False):
    h_be: Optional[float]
    d_ledger: Optional[float]
    retention: Optional[float]
    reread_pressure: int
    ile: bool
    cognitive_load: float
    config_fingerprint: str
    session_key: str
    turn: int
    # ... 完整字段见三层架构规范 §3

# contract_registry.py
def validate_contract(data: dict, contract_name: str,
                      strict: bool = False) -> List[str]
def get_consumers(contract_name: str) -> List[str]
def get_producer_contracts(protocol_name: str) -> Dict[str, List[str]]
```

### 依赖
- `typing`（标准库）
- 零外部依赖（叶子模块）

### 验收标准
- [ ] 所有 14 个 TypedDict 定义且字段与三层架构规范 §3 一致
- [ ] `validate_contract` 对空 dict 返回空错误列表（total=False 所有字段可选）
- [ ] `get_consumers("SignalSnapshot")` 返回包含 "P1_decompose" 和 "P5_escalate"
- [ ] `get_producer_contracts("P2_execute")` 的 produces 包含 "Patch"
- [ ] 单测通过（`bash test/run_tests.sh --unit`）
- [ ] `import signal_types; import protocol_types; import contract_registry` 无报错

### 预计工作量
半天

---

## Spec B: ctx_recall 挂载 + P5 Escalate 实现

### 任务描述
将已实现的 `ctx_recall.py` 检索核心挂载到管线（方案 A：次请求改写），并实现 P5 Escalate 升级决策器。

### 输入
- `ctx_recall.py`（已实现，未挂载）
- `ctx_recall.TOOL_SCHEMA`（工具定义已就绪）
- 三层架构规范 §2 P5 协议规范

### 输出文件
```
escalate.py              (~100 行) — TaskState, TaskCircuitBreaker, Escalator
test/test_escalate.py    (~80 行)
# 修改: pipeline.py (挂载点) — 等 pipeline.py 空出后
# 修改: tool_filter.py (工具注入钩子)
```

### 关键接口
```python
# escalate.py
class TaskState(Enum):
    PENDING, EXECUTING, VERIFYING, PASSED,
    FAILED_RETRYABLE, FAILED_SPLITTABLE, FAILED_ROUTE,
    ESCALATED, COMMITTED

class TaskCircuitBreaker:
    def __init__(self, threshold: int = 3, cooldown_s: int = 300)
    def can_execute(self, task_type: str) -> bool
    def record_failure(self, task_type: str) -> None
    def record_success(self, task_type: str) -> None

class Escalator:
    def __init__(self, max_retries: int = 2,
                 breaker: TaskCircuitBreaker)
    def escalate(self, patch, verdict, signal, attempt,
                 task_type) -> EscalationDecision
```

### ctx_recall 挂载点（方案 A：次请求改写）
```python
# 在 pipeline.py 的请求入口(stage 0 附近):
# 当检测到客户端返回的 tool_result 是 ctx_recall 的 error result 时，
# 代理将该 tool_result 改写为真实检索结果
# 复用 compress_tool_result 的改写路径

# 在 tool_filter.py 的工具过滤处:
# 注入 ctx_recall.TOOL_SCHEMA 到工具列表
```

### 依赖
- Spec A（protocol_types.py 中的 EscalationDecision, SignalSnapshot）
- `ctx_recall.py`（已存在）
- `memory_stores.py`（已存在）
- pipeline.py 需空出（并行工作提交后）

### 验收标准
- [ ] ctx_recall 工具注入后模型可调用
- [ ] 方案 A 改写：ctx_recall 的 error result 被替换为真实检索结果
- [ ] Escalator 决策表所有分支有单测覆盖
- [ ] 幂等闸：attempt≥2 时返回 "human"
- [ ] 熔断器：同类任务失败 3 次后 can_execute 返回 False
- [ ] 集成测试：端到端 ctx_recall → 模型看到"已读过"信息
- [ ] 单测通过

### 预计工作量
3 天（含 pipeline 挂载调试）

---

## Spec C: P1 Decompose + ProtocolOrchestrator

### 任务描述
实现 P1 分解器（双判据递归）和 ProtocolOrchestrator 编排器（五协议串联入口）。

### 输入
- 三层架构规范 §2 P1 协议规范 + §4 编排器伪码
- Spec A 的 protocol_types.py（Task, SubTask）
- Spec B 的 escalate.py（Escalator）

### 输出文件
```
decompose.py              (~100 行) — DecompositionCriterion, CapacityCriterion,
                                       EntropyCriterion, Decomposer
protocol_orchestrator.py  (~150 行) — ProtocolOrchestrator
idempotency.py            (~80 行)  — IdempotencyManager
test/test_decompose.py    (~60 行)
test/test_orchestrator.py (~80 行)
test/test_idempotency.py  (~40 行)
```

### 关键接口
```python
# decompose.py
class CapacityCriterion(DecompositionCriterion):
    def __init__(self, budget_tokens: int = 30000)
    def should_split(self, task, signal) -> bool  # est_tokens > budget
    def split(self, task) -> List[SubTask]        # 按引用图切分

class EntropyCriterion(DecompositionCriterion):
    def __init__(self, theta_h: float = 0.9)
    def should_split(self, task, signal) -> bool  # h_be > theta

class Decomposer:
    def __init__(self, criteria: List[DecompositionCriterion])
    def decompose(self, task, signal) -> List[SubTask]

# protocol_orchestrator.py
class ProtocolOrchestrator:
    def __init__(self, generator, verifier, decomposer,
                 escalator, idempotency, model_semaphore)
    def solve(self, task: Task) -> SolveResult

# idempotency.py
class IdempotencyManager:
    def decompose_idempotent(self, task, decomposer, signal) -> List[SubTask]
    def verify_l1_idempotent(self, patch, rules, signal) -> Verdict
    def recall_idempotent(self, query, manifest_count) -> RecallResult
```

### 依赖
- Spec A（全部类型）
- Spec B（Escalator）
- `threading`（模型信号量）

### 验收标准
- [ ] Decomposer 对松耦合任务正确分解为独立子任务
- [ ] Decomposer 对紧耦合任务（熵不降）返回升级建议
- [ ] ProtocolOrchestrator.solve() 端到端可运行（mock generator）
- [ ] 编排器中 P4 recall 在 reload 时被正确调用
- [ ] 幂等缓存：同一 task hash 重复分解返回缓存结果
- [ ] 模型锁在 Execute 步骤正确获取/释放
- [ ] 单测 + 编排器端到端测试通过

### 预计工作量
3 天

---

## Spec D: 设计模式落地 + 并发 + 性能预算

### 任务描述
在 Spec C 的基础上应用设计模式（黑板/DI/责任链/状态机/熔断），并实现管线级并发和性能预算。

### 输入
- Spec C 的 ProtocolOrchestrator
- 三层架构规范 §6 测试策略 + §7 配置参数
- 架构讨论中的设计模式分析

### 输出
```
# 修改: protocol_orchestrator.py (应用设计模式)
# 修改: escalate.py (TaskState 状态机正式化)
# 修改: proxy_config.py (注册新配置)

verification_chain.py  (~60 行)  — MechanicalHandler, SemanticHandler,
                                    LedgerHandler (责任链)
test/test_verification_chain.py (~60 行)
test/test_concurrency.py (~40 行)
```

### 关键接口
```python
# verification_chain.py
class VerificationHandler(Protocol):
    def set_next(self, handler) -> "VerificationHandler"
    def handle(self, patch, context) -> Verdict

def build_verify_chain(config) -> VerificationHandler:
    mech = MechanicalHandler()
    sem = SemanticHandler(ratio=config.semantic_ratio)
    led = LedgerHandler()
    mech.set_next(sem).set_next(led)
    return mech
```

### 配置注册
```python
# proxy_config.py 新增
"PROTOCOL_PIPELINE_CONCURRENT": {"defaults": {"all": "3"}, "type": "int", "scope": "reloadable"},
"PROTOCOL_BUDGET_EXECUTE_S": {"defaults": {"all": "30"}, "type": "int", "scope": "reloadable"},
"PROTOCOL_BUDGET_VERIFY_S": {"defaults": {"all": "10"}, "type": "int", "scope": "reloadable"},
"PROTOCOL_BREAKER_THRESHOLD": {"defaults": {"all": "3"}, "type": "int", "scope": "reloadable"},
```

### 依赖
- Spec C（编排器已实现）

### 验收标准
- [ ] 责任链：L1→L2→L3 依次执行，L1 失败不进入 L2
- [ ] 语义验证按比例抽样（ratio=0 时不调用 L2）
- [ ] 管线并发：两个 task 的 Decompose 可并行（不占模型锁）
- [ ] 性能预算超时触发降级（Execute 超时→retry_with_reload）
- [ ] TaskState 转换表无非法转换
- [ ] 新配置在 CONFIG_REGISTRY 注册且 reloadable
- [ ] 单测通过

### 预计工作量
2 天

---

## Spec E: 后治理基础版 + 运维监控

### 任务描述
实现后治理基础版（入场层校准 + 模式编译）和数据质量监控。

### 输入
- 三层架构规范 §8 非功能需求
- 架构讨论中的后治理设计
- 现有 trace_query.py 基础设施

### 输出文件
```
post_governance.py        (~100 行) — PostGovernance, EntryLayerCalibrator,
                                       PatternCompiler
test/test_post_governance.py (~60 行)
# 修改: admin_server.py (数据质量端点)
```

### 关键接口
```python
# post_governance.py
class PostGovernance:
    def process(self, task, layer_trajectory, outcome):
        """从每次思考中提取改进信号"""
        attribution = self._attribute(layer_trajectory)
        self._adjust_entry_layer(task.type, attribution)
        if attribution.repeated_pattern and attribution.count >= 3:
            self._compile_pattern(attribution.pattern)

class EntryLayerCalibrator:
    """根据历史数据调整某类任务的推荐入场层"""
    def update(self, task_type: str, attribution: Attribution)
    def recommend(self, task_type: str) -> int  # 推荐入场层(0-3)

class PatternCompiler:
    """将层 N 的成功模式编译到层 N-1"""
    def compile(self, pattern: Dict) -> Optional[Skill]
```

### 依赖
- Spec D（编排器+验证链已完整）

### 验收标准
- [ ] 后治理记录每次 solve 的层级轨迹
- [ ] 入场层校准：同类任务历史显示总在层 2 成功 → 推荐入场层从 0 调到 2
- [ ] 模式编译：同类成功模式出现 ≥3 次后生成 Skill
- [ ] 数据质量端点返回各产品的 SLA 满足情况
- [ ] 单测通过

### 预计工作量
2 天

---

## 开发顺序与依赖关系

```
Spec A (契约) ──────→ Spec B (Recall+Escalate) ──────→ Spec C (Decompose+编排器)
                                                              │
                                                              ▼
                                                     Spec D (模式+并发+性能)
                                                              │
                                                              ▼
                                                     Spec E (后治理+运维)
```

**总计**: 5 个 spec，约 12 天工作量（可与现有 IFC-3 挂载包部分并行）
