#!/usr/bin/env python3
"""protocol_orchestrator.py — 五协议编排器（stdlib only）。

P1 Decompose → P2 Execute → P3 Verify → P4 Recall → P5 Escalate 的闭环。

设计依据: 三层架构规范 §3 / PRD v4.0 R11 / Spec-C
依赖: decompose.py + escalate.py + verification_chain.py + idempotency.py + protocol_types.py

冻结声明 (LD-6 / 2026-09-02):
- 当前为 agent_go 集成契约 §3.3 R17/R18/R19 的「参考实现冻结版」。
- 五协议闭环比价 / 任务工程 / 工具形态的接入决策归 agent_go，本仓库不再扩展。
- 可拆解到已有组件的增强（PDC-L1/L2、ifc_metrics、ctx_recall）已在 Signal/Protocol
  边界内持续迭代；编排器本体保持 Phase 1 独立模块，不主动接运行时 pipeline。

关键设计:
- 执行器/召回器为注入的 callable（依赖注入）——Phase 1 用 stub 测试,
  Phase 2 接 pipeline 的真实模型调用
- 状态机跟踪: 每个子任务经 TaskState 有限状态机, 非法转换抛异常
- 幂等: decompose/verify 结果缓存, 同任务同会话不重复计算
- 死循环防御: 全局迭代上限 + 单子任务重试上限
"""
import time
from typing import Callable, Dict, List, Optional

from decompose import Decomposer, task_hash
from escalate import Escalator, TaskState, can_transition
from idempotency import IdempotencyManager
from protocol_types import build_task, EscalationDecision, Patch, SubTask, Task, Verdict
from signal_types import SignalSnapshot
from verification_chain import build_verification_chain

# P2 执行器契约: SubTask (+ 可选召回上下文) → Patch
Executor = Callable[[SubTask], Patch]
# P4 召回器契约: (query, session_key) → 召回文本（空串 = 未命中）
Recaller = Callable[[str, str], str]

# ============================================================================
# 性能预算（Spec-D）——Phase 1 以模块常量落地; proxy_config.py 释放后迁入
# CONFIG_REGISTRY（键名已定: PROXY_PROTOCOL_*）。测试 enforce 见
# test_protocol_orchestrator.TestPerformanceBudgets。
# ============================================================================
PROTOCOL_BUDGETS = {
    # 编排自身开销上限（不含 executor 模型调用时间）
    "orchestration_overhead_ms": 50,
    # P1 分解单任务上限（含递归判据）
    "decompose_ms": 20,
    # P3 验证链单补丁上限（L1+L2+L3）
    "verify_ms": 10,
    # 幂等缓存单次 get/put 上限
    "cache_op_ms": 1,
    # solve() 串行子任务数软上限（超过建议并行化）
    "serial_subtasks_soft_limit": 16,
}


class IllegalTransitionError(RuntimeError):
    """状态机非法转换——编排器内部 bug 的信号（不是业务错误）。"""


class ProtocolOrchestrator:
    """五协议编排器——solve() 是唯一入口。"""

    def __init__(self,
                 decomposer: Optional[Decomposer] = None,
                 escalator: Optional[Escalator] = None,
                 idem: Optional[IdempotencyManager] = None,
                 executor: Optional[Executor] = None,
                 recaller: Optional[Recaller] = None,
                 max_iterations: int = 16,
                 max_subtask_retries: int = 2):
        self.decomposer = decomposer or Decomposer()
        self.escalator = escalator or Escalator(max_retries=max_subtask_retries)
        self.idem = idem or IdempotencyManager()
        self.executor = executor
        self.recaller = recaller
        self.max_iterations = max_iterations
        self.max_subtask_retries = max_subtask_retries

    # ========================================================================
    # 主入口
    # ========================================================================

    def solve(self, task: Task, signal: SignalSnapshot) -> Dict:
        """执行完整协议循环, 返回会话结果。

        返回结构: {
            status: "completed" | "escalated" | "no_executor",
            subtasks: [ {id, state, attempts, verdict?} ],
            patches: [Patch],        # 通过验证的补丁
            escalations: [EscalationDecision],
            iterations: int,
            elapsed_ms: int,
        }
        """
        t0 = time.time()
        session_key = signal.get("session_key", "")

        # P1 Decompose（幂等缓存）
        dkey = task_hash(task)
        subtasks = self.idem.decompose_cached(
            dkey, lambda: self.decomposer.decompose(task, signal))

        # 状态跟踪黑板
        states: Dict[str, TaskState] = {st["id"]: TaskState.PENDING for st in subtasks}
        attempts: Dict[str, int] = {st["id"]: 0 for st in subtasks}
        results = {
            "status": "completed",
            "subtasks": [],
            "patches": [],
            "escalations": [],
            "iterations": 0,
            "elapsed_ms": 0,
        }

        if self.executor is None:
            results["status"] = "no_executor"
            results["subtasks"] = [{"id": st["id"], "state": "pending",
                                    "attempts": 0} for st in subtasks]
            results["elapsed_ms"] = int((time.time() - t0) * 1000)
            return results

        # 就绪子任务队列（Phase 1 串行——依赖图简单按序; Phase 2 并行化）
        queue: List[SubTask] = list(subtasks)
        it = 0
        while queue and it < self.max_iterations:
            it += 1
            st = queue.pop(0)
            sid = st["id"]
            task_type = st.get("expected_output_type", "")

            # 熔断检查（P5 的 breaker 在执行前预检）
            if task_type and not self.escalator.breaker.can_execute(task_type):
                results["status"] = "escalated"
                results["escalations"].append(self._mk_escalation(
                    sid, "route_cloud", "circuit_breaker_open_precheck", signal, attempts[sid]))
                states[sid] = self._transition(states[sid], TaskState.FAILED_ROUTE)
                continue

            # P2 Execute
            states[sid] = self._transition(states[sid], TaskState.EXECUTING)
            attempts[sid] += 1
            patch = self.executor(st)
            patch.setdefault("id", f"{sid}:p{attempts[sid]}")
            patch.setdefault("task_id", sid)
            patch.setdefault("known_anchors", st.get("known_anchors", []))

            # P3 Verify（幂等缓存——同补丁不重复验证）
            states[sid] = self._transition(states[sid], TaskState.VERIFYING)
            vkey = "v:" + task_hash({"description": str(patch.get("diff", "")),
                                     "f": [str(patch.get("citations", []))]})
            verdict = self.idem.verify_cached(
                vkey, lambda: build_verification_chain(
                    reconcile_result=st.get("reconcile_result")).handle(patch))

            if verdict.get("passed"):
                states[sid] = self._transition(states[sid], TaskState.PASSED)
                self.escalator.record_success(task_type)
                results["patches"].append(patch)
                continue

            # P5 Escalate
            decision = self.escalator.escalate(
                patch, verdict, signal, attempts[sid], task_type)
            results["escalations"].append(decision)
            action = decision.get("action")

            if action == "retry" and attempts[sid] < self.max_subtask_retries:
                states[sid] = self._transition(states[sid], TaskState.FAILED_RETRYABLE)
                queue.insert(0, st)  # 优先重试; 循环入口做 FAILED_RETRYABLE→EXECUTING
            elif action == "reload" and self.recaller is not None:
                # P4 Recall——召回历史信息注入子任务上下文后重执行
                recovered = self.recaller(sid, session_key) if session_key else ""
                st = SubTask({**st, "recall_context": recovered})
                states[sid] = self._transition(states[sid], TaskState.FAILED_RETRYABLE)
                queue.insert(0, st)
            elif action == "split":
                states[sid] = self._transition(states[sid], TaskState.FAILED_SPLITTABLE)
                subs = self.decomposer.decompose(st, signal)
                if len(subs) > 1:
                    for s in reversed(subs):
                        states[s["id"]] = TaskState.PENDING
                        attempts[s["id"]] = 0
                        queue.insert(0, s)
                else:
                    # 拆不动 → 升级
                    states[sid] = self._transition(states[sid], TaskState.FAILED_ROUTE)
                    states[sid] = self._transition(states[sid], TaskState.ESCALATED)
                    results["status"] = "escalated"
            else:  # human / route_cloud / 重试耗尽 / reload 无召回器
                if action == "retry":
                    # 编排器预算耗尽而 escalator 建议 retry → 升格为 human 终态
                    results["escalations"].append(self._mk_escalation(
                        sid, "human", "subtask_retry_budget_exhausted",
                        signal, attempts[sid]))
                if states[sid] == TaskState.VERIFYING:
                    states[sid] = self._transition(states[sid], TaskState.FAILED_ROUTE)
                states[sid] = self._transition(states[sid], TaskState.ESCALATED)
                results["status"] = "escalated"

        if queue:  # 迭代上限耗尽仍有未完成任务
            results["status"] = "escalated"
            for st in queue:
                results["escalations"].append(self._mk_escalation(
                    st["id"], "human", "iteration_budget_exhausted",
                    signal, attempts.get(st["id"], 0)))

        results["subtasks"] = [
            {"id": k, "state": v.value, "attempts": attempts.get(k, 0)}
            for k, v in states.items()]
        results["iterations"] = it
        results["elapsed_ms"] = int((time.time() - t0) * 1000)
        return results

    # ========================================================================
    # 内部
    # ========================================================================

    @staticmethod
    def _transition(from_state: TaskState, to_state: TaskState) -> TaskState:
        if not can_transition(from_state, to_state):
            raise IllegalTransitionError(
                f"{from_state.value} -> {to_state.value} is not a legal transition")
        return to_state

    @staticmethod
    def _mk_escalation(task_id: str, action: str, reason: str,
                       signal: SignalSnapshot, attempt: int) -> EscalationDecision:
        from protocol_types import build_escalation
        return build_escalation(
            task_id=task_id, action=action, reason=reason,
            attempt_count=attempt,
            signals={"reread_pressure": signal.get("reread_pressure", 0),
                     "cognitive_load": signal.get("cognitive_load", 0)})


__all__ = ["ProtocolOrchestrator", "IllegalTransitionError",
           "Executor", "Recaller", "PROTOCOL_BUDGETS"]
