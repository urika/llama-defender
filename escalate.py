#!/usr/bin/env python3
"""escalate.py — P5 Escalate Protocol（stdlib only）。

验证失败后的策略切换决策——基于决策表 + 幂等闸 + 熔断器。

设计依据: 三层架构规范 §2 P5 / PRD v4.0 R11.6 / Spec-B
依赖: protocol_types.py (EscalationDecision, SignalSnapshot) + signal_types.py
"""
import time
from collections import defaultdict
from enum import Enum
from typing import Dict, Optional

from protocol_types import build_escalation, EscalationDecision, Patch, Verdict
from signal_types import SignalSnapshot


# ============================================================================
# 任务状态机
# ============================================================================

class TaskState(Enum):
    """子任务的生命周期状态——有限状态机。"""
    PENDING = "pending"               # 等待执行
    EXECUTING = "executing"           # 模型生成中
    VERIFYING = "verifying"           # 验证中
    PASSED = "passed"                 # 验证通过,可 commit
    FAILED_RETRYABLE = "failed_retryable"   # 可重试
    FAILED_SPLITTABLE = "failed_splittable" # 需分解
    FAILED_ROUTE = "failed_route"           # 需路由
    ESCALATED = "escalated"           # 已升级(终态)
    COMMITTED = "committed"           # 已提交(终态)


# 合法状态转换表——不在表中的转换视为非法
TRANSITIONS = {
    TaskState.PENDING: {TaskState.EXECUTING},
    TaskState.EXECUTING: {TaskState.VERIFYING, TaskState.FAILED_RETRYABLE},
    TaskState.VERIFYING: {TaskState.PASSED, TaskState.FAILED_RETRYABLE,
                          TaskState.FAILED_SPLITTABLE, TaskState.FAILED_ROUTE},
    TaskState.FAILED_RETRYABLE: {TaskState.EXECUTING, TaskState.ESCALATED},
    TaskState.FAILED_SPLITTABLE: {TaskState.PENDING},  # 重新入队
    TaskState.FAILED_ROUTE: {TaskState.ESCALATED},
    # 终态不可转出
    TaskState.PASSED: set(),
    TaskState.ESCALATED: set(),
    TaskState.COMMITTED: set(),
}


def can_transition(from_state: TaskState, to_state: TaskState) -> bool:
    """检查状态转换是否合法。"""
    return to_state in TRANSITIONS.get(from_state, set())


# ============================================================================
# 熔断器
# ============================================================================

class TaskCircuitBreaker:
    """同类任务熔断——连续失败 N 次后打开, 冷却期内直接升级。

    解决的问题: 同类任务反复失败时, 避免无脑重试浪费资源。
    设计依据: 设计模式 §3.5 (Circuit Breaker) / exp-2 v1 死亡螺旋的泛化防御。
    """

    def __init__(self, threshold: int = 3, cooldown_s: int = 300):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._failure_counts: Dict[str, int] = defaultdict(int)
        self._open_until: Dict[str, float] = {}

    def can_execute(self, task_type: str) -> bool:
        """检查该类任务当前是否可执行（熔断器是否打开）。"""
        until = self._open_until.get(task_type)
        if until is None:
            return True
        if time.time() >= until:
            # 冷却已过, 关闭熔断
            del self._open_until[task_type]
            self._failure_counts[task_type] = 0
            return True
        return False

    def record_failure(self, task_type: str) -> None:
        """记录一次失败——达到阈值时打开熔断。"""
        self._failure_counts[task_type] += 1
        if self._failure_counts[task_type] >= self.threshold:
            self._open_until[task_type] = time.time() + self.cooldown_s
            self._failure_counts[task_type] = 0

    def record_success(self, task_type: str) -> None:
        """记录一次成功——重置失败计数。"""
        self._failure_counts[task_type] = 0

    def status(self, task_type: str) -> Dict:
        """查询某类任务的熔断状态。"""
        until = self._open_until.get(task_type)
        is_open = until is not None and time.time() < until
        return {
            "task_type": task_type,
            "open": is_open,
            "failure_count": self._failure_counts[task_type],
            "remaining_cooldown_s": max(0, int(until - time.time())) if is_open else 0,
        }


# ============================================================================
# P5 Escalate 决策器
# ============================================================================

class Escalator:
    """P5 升级决策器——决策表驱动, 幂等闸, 熔断检查。

    决策优先级(高→低):
    1. 熔断器打开 → route_cloud (不重试)
    2. 尝试次数 ≥ max_retries → human (终态, 不再重试)
    3. reread_pressure ≥ 2 → reload (召回+重执行)
    4. cognitive_load > hard_limit → split (分解)
    5. H_BE 趋势持续上升 → route_cloud
    6. 默认 → retry (同层重试)
    """

    def __init__(self,
                 max_retries: int = 2,
                 breaker: Optional[TaskCircuitBreaker] = None,
                 hard_load_limit: float = 0.85,
                 hbe_trend_threshold: float = 0.02):
        self.max_retries = max_retries
        self.breaker = breaker or TaskCircuitBreaker()
        self.hard_load_limit = hard_load_limit
        self.hbe_trend_threshold = hbe_trend_threshold

    def escalate(self,
                 patch: Patch,
                 verdict: Verdict,
                 signal: SignalSnapshot,
                 attempt: int,
                 task_type: str = "") -> EscalationDecision:
        """根据验证结果 + 信号 + 尝试次数决定升级动作。"""
        # 1. 熔断检查(最高优先级)
        if task_type and not self.breaker.can_execute(task_type):
            self.breaker.record_failure(task_type)  # 保持打开
            return self._decision(
                task_type or patch.get("task_id", ""),
                "route_cloud", "circuit_breaker_open",
                signal, attempt)

        # 2. 幂等闸(终态)
        if attempt >= self.max_retries:
            if task_type:
                self.breaker.record_failure(task_type)
            return self._decision(
                patch.get("task_id", ""),
                "human", "max_retries_exceeded",
                signal, attempt)

        # 3. 重读压力 → 召回+重执行
        if signal.get("reread_pressure", 0) >= 2:
            return self._decision(
                patch.get("task_id", ""),
                "reload", f"reread_pressure={signal['reread_pressure']}",
                signal, attempt)

        # 4. 认知负荷超限 → 分解
        if signal.get("cognitive_load", 0) > self.hard_load_limit:
            return self._decision(
                patch.get("task_id", ""),
                "split", f"cognitive_load={signal.get('cognitive_load', 0):.2f} > {self.hard_load_limit}",
                signal, attempt)

        # 5. H_BE 趋势上升 → 云端
        hbe_trend = signal.get("h_be_trend") or signal.get("h_be", 0)
        if isinstance(hbe_trend, (int, float)) and hbe_trend > self.hbe_trend_threshold:
            return self._decision(
                patch.get("task_id", ""),
                "route_cloud", f"hbe_trend={hbe_trend:.3f} > {self.hbe_trend_threshold}",
                signal, attempt)

        # 6. 默认: 同层重试
        return self._decision(
            patch.get("task_id", ""),
            "retry", self._first_error(verdict),
            signal, attempt)

    def record_success(self, task_type: str) -> None:
        """任务成功时通知熔断器。"""
        if task_type:
            self.breaker.record_success(task_type)

    def _decision(self, task_id, action, reason, signal, attempt):
        return build_escalation(
            task_id=task_id, action=action, reason=reason,
            attempt_count=attempt,
            signals={
                "reread_pressure": signal.get("reread_pressure", 0),
                "cognitive_load": signal.get("cognitive_load", 0),
                "h_be_trend": signal.get("h_be_trend"),
                "h_be": signal.get("h_be"),
            })

    @staticmethod
    def _first_error(verdict: Verdict) -> str:
        checks = verdict.get("checks", [])
        for c in checks:
            if isinstance(c, dict) and not c.get("passed", True):
                return c.get("detail", c.get("rule_name", "verification_failed"))
        return "verification_failed"


__all__ = [
    "TaskState", "TRANSITIONS", "can_transition",
    "TaskCircuitBreaker", "Escalator",
]
