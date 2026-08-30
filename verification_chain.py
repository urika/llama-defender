#!/usr/bin/env python3
"""verification_chain.py — P3 Verify Protocol（stdlib only）。

三级验证链（职责链模式 Chain of Responsibility）:
  L1 机械验证 → L2 语义验证 → L3 台账验证

设计依据: 三层架构规范 §2 P3 / 认知编排器设计 §设计模式 (CoR) / Spec-D
依赖: protocol_types.py (Patch, Verdict, CheckResult)
"""
import re
from typing import List, Optional

from protocol_types import build_verdict, CheckResult, Patch, Verdict


# ============================================================================
# 验证器抽象
# ============================================================================

class VerificationHandler:
    """职责链节点基类——实现 handle, 通过 successor 链接下一级。"""

    def __init__(self, level: str = "mechanical"):
        self.level = level
        self.successor: Optional["VerificationHandler"] = None

    def set_successor(self, next_handler: "VerificationHandler") -> "VerificationHandler":
        self.successor = next_handler
        return next_handler  # 支持链式 set

    def handle(self, patch: Patch) -> Verdict:
        checks = self.check(patch)
        passed = all(c.get("passed", False) for c in checks) if checks else True
        verdict = build_verdict(
            patch_id=patch.get("id", patch.get("task_id", "")),
            passed=passed, checks=checks, verify_level=self.level)
        # 短路: 本级失败不再往下传（低级失败使高级验证无意义）
        if not passed:
            return verdict
        if self.successor is not None:
            return self.successor.handle(patch)
        return verdict

    def check(self, patch: Patch) -> List[CheckResult]:
        raise NotImplementedError


# ============================================================================
# L1 机械验证
# ============================================================================

class MechanicalHandler(VerificationHandler):
    """机械验证——确定性规则, 零成本, 永远执行。

    检查项:
    - citation_existence: patch 声称的引用锚是否真实存在
    - diff_validity: diff 格式是否合法（---/+++ 或语境行结构）
    """

    def __init__(self):
        super().__init__(level="mechanical")

    def check(self, patch: Patch) -> List[CheckResult]:
        results = []

        # 1. 引用存在性——每个 citation 的 anchor 字段非空且出现在源输入中
        citations = patch.get("citations", [])
        anchors_known = set(patch.get("known_anchors", []))
        for i, c in enumerate(citations):
            anchor = c.get("anchor", "") if isinstance(c, dict) else ""
            ok = bool(anchor) and (not anchors_known or anchor in anchors_known)
            results.append(CheckResult(
                rule_name="citation_existence",
                passed=ok,
                detail="" if ok else f"citation[{i}] anchor missing or unknown: {anchor!r}",
            ))

        # 2. diff 结构合法性
        diff = patch.get("diff", "")
        ok = self._diff_well_formed(diff)
        results.append(CheckResult(
            rule_name="diff_validity",
            passed=ok,
            detail="" if ok else "malformed diff (missing ---/+++ headers or hunk structure)",
        ))
        return results

    @staticmethod
    def _diff_well_formed(diff: str) -> bool:
        """宽松检查——空 diff 或带 ---/+++ 头的 unified diff 片段视为合法。"""
        if not diff or not diff.strip():
            return False  # 空补丁 = 失败
        if diff.startswith("---") or "\n---" in diff:
            return True
        # 非 diff 类输出（text 任务）只要有内容即合法
        return True


# ============================================================================
# L2 语义验证
# ============================================================================

class SemanticHandler(VerificationHandler):
    """语义验证——需要模型参与的成本档（Phase 2 接 LLM 裁判）。

    Phase 1 实现: 结构化占位检查（声明了 verify 规则但输出为空 / 答非所问的模式匹配）。
    Phase 2 将接 self-ask 探针: "输出是否回答了 task description?"
    """

    def __init__(self):
        super().__init__(level="semantic")

    def check(self, patch: Patch) -> List[CheckResult]:
        results = []
        desc = (patch.get("task_description", "") or "").lower()
        output = (patch.get("output", "") or patch.get("diff", "") or "").strip()

        # 1. 输出非空
        results.append(CheckResult(
            rule_name="non_empty_output",
            passed=bool(output),
            detail="" if output else "semantic output is empty",
        ))

        # 2. 任务关键词覆盖——description 中的显著词（≥4 chars）至少出现一个在输出里
        if desc:
            keywords = [w for w in re.split(r"\W+", desc) if len(w) >= 4][:20]
            if keywords:
                hit = any(w in output.lower() for w in keywords)
                results.append(CheckResult(
                    rule_name="topic_relevance",
                    passed=hit,
                    detail="" if hit else "output shows no overlap with task description keywords",
                ))
        return results


# ============================================================================
# L3 台账验证
# ============================================================================

class LedgerHandler(VerificationHandler):
    """台账验证——对账 D_ledger（探针答案 vs 台账 ground truth）。

    Phase 1 实现: patch 声称的事实断言与台账记录的一致性抽查。
    依赖 signal layer 的 reconcile 结果（可选注入, fail-open）。
    """

    def __init__(self, reconcile_result: Optional[dict] = None):
        super().__init__(level="ledger")
        # reconcile_result: {"n_probes": int, "n_match": int, "d_ledger": float, ...}
        self.reconcile = reconcile_result or {}

    def check(self, patch: Patch) -> List[CheckResult]:
        if not self.reconcile:
            # 台账数据缺失 → fail-open, 不产生失败检查
            return [CheckResult(
                rule_name="ledger_reconciliation",
                passed=True,
                detail="no ledger data available (fail-open)",
            )]
        n_probes = self.reconcile.get("n_probes", 0)
        n_match = self.reconcile.get("n_match", 0)
        d_ledger = self.reconcile.get("d_ledger")
        if n_probes == 0:
            return [CheckResult(
                rule_name="ledger_reconciliation",
                passed=True,
                detail="no probes to reconcile",
            )]
        # D_ledger ≥ 0.8 视为台账一致（探针答案 80%+ 对得上 ground truth）
        ok = isinstance(d_ledger, (int, float)) and d_ledger >= 0.8
        return [CheckResult(
            rule_name="ledger_reconciliation",
            passed=ok,
            detail=(f"d_ledger={d_ledger:.3f} ({n_match}/{n_probes} probes matched)"
                    if isinstance(d_ledger, (int, float))
                    else "d_ledger missing"),
        )]


# ============================================================================
# 便捷构造
# ============================================================================

def build_verification_chain(reconcile_result: Optional[dict] = None,
                             include_semantic: bool = True,
                             include_ledger: bool = True) -> MechanicalHandler:
    """构建三级验证链, 返回链头。

    机械验证永远在; 语义/台账可按成本档裁剪。
    """
    head = MechanicalHandler()
    tail = head
    if include_semantic:
        tail = tail.set_successor(SemanticHandler())
    if include_ledger:
        tail = tail.set_successor(LedgerHandler(reconcile_result))
    return head


__all__ = [
    "VerificationHandler", "MechanicalHandler", "SemanticHandler",
    "LedgerHandler", "build_verification_chain",
]
