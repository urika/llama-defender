#!/usr/bin/env python3
"""decompose.py — P1 Decompose Protocol（stdlib only）。

双判据递归分解——将大任务拆为输入集在模型预算内的原子子任务。

设计依据: 三层架构规范 §2 P1 / PRD v4.0 R11.2 / Spec-C
依赖: protocol_types.py (Task, SubTask) + signal_types.py (SignalSnapshot)
"""
import hashlib
import json
from typing import List, Optional

from protocol_types import build_subtask, Task, SubTask, VerificationRule
from signal_types import SignalSnapshot


# ============================================================================
# 分解判据（策略模式，可插拔）
# ============================================================================

class DecompositionCriterion:
    """分解判据基类——子类实现 should_split 和 split。"""

    def should_split(self, task: Task, signal: SignalSnapshot) -> bool:
        raise NotImplementedError

    def split(self, task: Task) -> List[Task]:
        raise NotImplementedError


class CapacityCriterion(DecompositionCriterion):
    """容量判据——估算输入集 token 超预算时按文件拆分。"""

    def __init__(self, budget_tokens: int = 30000, chars_per_token: int = 4):
        self.budget = budget_tokens
        self.ratio = chars_per_token

    def should_split(self, task: Task, signal: SignalSnapshot) -> bool:
        est = sum(len(str(f)) for f in task.get("input_files", [])) // self.ratio
        # 加上描述本身的估算
        est += len(task.get("description", "")) // self.ratio
        return est > self.budget

    def split(self, task: Task) -> List[Task]:
        """按 input_files 拆分——每个文件一个子任务。"""
        return [
            {**task, "id": f"{task.get('id', 'task')}:f{i}",
             "input_files": [f], "depth": task.get("depth", 0) + 1,
             "parent_task_id": task.get("id")}
            for i, f in enumerate(task.get("input_files", []))
        ]


class EntropyCriterion(DecompositionCriterion):
    """熵判据——H_BE 超阈值时精炼纠缠论断（标记需分解）。"""

    def __init__(self, theta_h: float = 0.9):
        self.theta_h = theta_h

    def should_split(self, task: Task, signal: SignalSnapshot) -> bool:
        h = signal.get("h_be")
        return isinstance(h, (int, float)) and h > self.theta_h

    def split(self, task: Task) -> List[Task]:
        """熵判据不直接拆文件——标记描述需精炼，由编排器处理。"""
        return [task]  # 不拆，让编排器升级处理


# ============================================================================
# 分解器
# ============================================================================

class Decomposer:
    """P1 分解器——多判据递归。"""

    def __init__(self, criteria: Optional[List[DecompositionCriterion]] = None):
        self.criteria = criteria or [
            CapacityCriterion(),
            EntropyCriterion(),
        ]

    def decompose(self, task: Task, signal: SignalSnapshot) -> List[SubTask]:
        """递归分解直到所有子任务满足所有判据。

        返回按依赖顺序排列的 SubTask 列表（当前按文件序）。
        如果熵不随分解下降 → 标记 route_cloud（由编排器处理）。
        """
        result = self._decompose(task, signal, depth=0)
        return [self._to_subtask(t) for t in result]

    def _decompose(self, task: Task, signal: SignalSnapshot,
                   depth: int) -> List[Task]:
        if depth > 5:  # 防无限递归
            return [task]

        for criterion in self.criteria:
            if criterion.should_split(task, signal):
                subs = criterion.split(task)
                if len(subs) <= 1:
                    continue  # 无法再拆（如熵判据不拆文件）
                return [
                    sub_result
                    for sub in subs
                    for sub_result in self._decompose(sub, signal, depth + 1)
                ]
        return [task]  # 所有判据通过 → 原子任务

    def _to_subtask(self, task: Task) -> SubTask:
        """将原子 Task 转为 SubTask（添加验证规则和预算）。"""
        rules = self._default_rules(task)
        deps = self._infer_dependencies(task)
        budget = self._estimate_budget(task)
        return build_subtask(task, rules, deps, budget)

    def _default_rules(self, task: Task) -> List[VerificationRule]:
        """根据任务类型生成默认验证规则。"""
        output_type = task.get("expected_output_type", "patch")
        rules = []
        if output_type == "patch":
            rules.append(VerificationRule(
                name="citation_existence", check_type="string_match", parameters={}))
            rules.append(VerificationRule(
                name="diff_validity", check_type="diff_apply", parameters={}))
        elif output_type == "text":
            rules.append(VerificationRule(
                name="format_compliance", check_type="format", parameters={}))
        return rules

    def _infer_dependencies(self, task: Task) -> List[str]:
        """推断依赖——当前按文件序（简化版）。"""
        return []

    def _estimate_budget(self, task: Task) -> int:
        """估算子任务的 token 预算。"""
        total = sum(len(str(f)) for f in task.get("input_files", []))
        return max(1000, total // 4 + 100)


def task_hash(task: Task) -> str:
    """Task 的内容 hash——幂等缓存键。"""
    raw = json.dumps({
        "d": task.get("description", ""),
        "f": sorted(task.get("input_files", [])),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


__all__ = [
    "DecompositionCriterion", "CapacityCriterion", "EntropyCriterion",
    "Decomposer", "task_hash",
]
