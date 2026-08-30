#!/usr/bin/env python3
"""protocol_types.py — Protocol Layer 数据契约（stdlib only）。

定义五协议（P1-P5）+ 编排器的 8 个核心类型。
依赖 signal_types（SignalSnapshot）。

契约版本: 1
"""
from typing import Any, Dict, List, Optional, Tuple

from signal_types import CONTRACT_VERSION as SIGNAL_CONTRACT_VERSION

CONTRACT_VERSION = 1


class Task(dict):
    """待执行的任务——Application 层创建，P1 Decompose 消费。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class SubTask(Task):
    """分解后的原子工作单元——P1 生产，P2 Execute 消费。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class VerificationRule(dict):
    """单条验证规则——P3 Verify 消费。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class Citation(dict):
    """输出与证据之间的锚——P2 生产，P3 校验对象。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class Patch(dict):
    """带引用的变更提议——P2 生产，P3 消费。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class CheckResult(dict):
    """单条验证规则的执行结果——P3 生产，P5 消费。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class Verdict(dict):
    """补丁是否通过验收——P3 生产，P5 消费。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class EscalationDecision(dict):
    """验证失败后的策略切换决策——P5 生产，编排器消费。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class SessionState(dict):
    """跨协议共享的黑板——所有协议读写，不互相引用。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


# ===== 工厂函数 =====

def build_task(task_id: str, description: str, input_files: List[str],
               expected_output_type: str = "patch",
               constraints: Optional[Dict] = None,
               parent_task_id: Optional[str] = None, depth: int = 0) -> Task:
    return Task(
        contract_version=CONTRACT_VERSION,
        id=task_id, description=description,
        input_files=input_files,
        expected_output_type=expected_output_type,
        constraints=constraints or {},
        parent_task_id=parent_task_id, depth=depth,
    )


def build_subtask(task: Task, verification_rules: List[VerificationRule],
                  dependencies: List[str], budget_tokens: int) -> SubTask:
    return SubTask(
        **task,
        verification_rules=verification_rules,
        dependencies=dependencies,
        budget_tokens=budget_tokens,
    )


def build_patch(task_id: str, diff: str,
                citations: List[Citation],
                model_confidence: float = 0.0) -> Patch:
    return Patch(
        contract_version=CONTRACT_VERSION,
        task_id=task_id, diff=diff,
        citations=citations,
        model_confidence=model_confidence,
    )


def build_verdict(patch_id: str, passed: bool,
                  checks: List[CheckResult],
                  verify_level: str = "mechanical") -> Verdict:
    return Verdict(
        contract_version=CONTRACT_VERSION,
        patch_id=patch_id, passed=passed,
        checks=checks, verify_level=verify_level,
    )


def build_escalation(task_id: str, action: str, reason: str,
                     attempt_count: int = 0,
                     signals: Optional[Dict] = None,
                     refined_task: Optional[SubTask] = None) -> EscalationDecision:
    return EscalationDecision(
        contract_version=CONTRACT_VERSION,
        task_id=task_id, action=action, reason=reason,
        signals_at_decision=signals or {},
        attempt_count=attempt_count,
        refined_task=refined_task,
    )


__all__ = [
    "CONTRACT_VERSION",
    "Task", "SubTask", "VerificationRule", "Citation", "Patch",
    "CheckResult", "Verdict", "EscalationDecision", "SessionState",
    "build_task", "build_subtask", "build_patch", "build_verdict",
    "build_escalation",
]
