#!/usr/bin/env python3
"""post_governance.py — 后治理基础版 + 数据质量监控（stdlib only）。

入场层校准（EntryLayerCalibrator）+ 模式编译（PatternCompiler）+
数据质量 SLA 监控（DataQualityMonitor）。

分层语义（对齐认知策略设计 L0-L3）:
  0=rules 规则层, 1=prompts 提示层, 2=memory 记忆层, 3=control 控制层。
入场层校准: 同类任务历史显示总在层 N 成功 → 新任务推荐直接从层 N 入场。
模式编译: 层 N 的成功模式重复 ≥3 次 → 编译为 Skill 安装到层 N-1（能力下沉）。

设计依据: 三层架构规范 §8 / PRD v4.0 R12 / Spec-E
依赖: protocol_types.py (Task) + contract_registry.py (SLA 元数据)
"""
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

from protocol_types import Task

LAYER_NAMES = {0: "rules", 1: "prompts", 2: "memory", 3: "control"}
MAX_LAYER = 3


class Attribution(dict):
    """单次 solve 的层级归因——后治理的基本分析单元。"""


class Skill(dict):
    """编译后的成功模式——安装到更低层的可复用能力。"""


# ============================================================================
# 入场层校准器
# ============================================================================

class EntryLayerCalibrator:
    """根据历史数据调整某类任务的推荐入场层。

    策略: 近期成功记录中取最小成功退出层（最便宜的充分层）。
    样本不足（< min_samples 次成功）时保守返回 0。
    """

    def __init__(self, min_samples: int = 3, window: int = 50):
        self.min_samples = min_samples
        self.window = window
        self._history: Dict[str, Deque[dict]] = defaultdict(
            lambda: deque(maxlen=window))

    def update(self, task_type: str, attribution: Attribution) -> None:
        self._history[task_type].append({
            "entry_layer": attribution.get("entry_layer", 0),
            "exit_layer": attribution.get("exit_layer", 0),
            "success": bool(attribution.get("success", False)),
        })

    def recommend(self, task_type: str) -> int:
        """推荐入场层 0-3。"""
        successes = [r["exit_layer"] for r in self._history.get(task_type, ())
                     if r["success"]]
        if len(successes) < self.min_samples:
            return 0
        return max(0, min(MAX_LAYER, min(successes)))

    def stats(self, task_type: str) -> Dict:
        records = list(self._history.get(task_type, ()))
        successes = [r for r in records if r["success"]]
        return {
            "task_type": task_type,
            "samples": len(records),
            "successes": len(successes),
            "recommended_entry_layer": self.recommend(task_type),
        }


# ============================================================================
# 模式编译器
# ============================================================================

class PatternCompiler:
    """将层 N 的成功模式编译到层 N-1。

    observe() 累计同签名模式出现次数; compile() 在达到阈值时产出 Skill。
    """

    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self._counts: Dict[str, int] = defaultdict(int)
        self._last: Dict[str, dict] = {}
        self.skills: List[Skill] = []

    def observe(self, pattern: Dict) -> int:
        """记录一次模式出现, 返回累计次数。"""
        key = self._pattern_key(pattern)
        self._counts[key] += 1
        self._last[key] = pattern
        return self._counts[key]

    def compile(self, pattern: Dict) -> Optional[Skill]:
        """达到阈值时编译 Skill; 未达到返回 None。"""
        key = self._pattern_key(pattern)
        if self._counts.get(key, 0) < self.threshold:
            return None
        # 已编译过同 key → 不重复（幂等）
        if any(s.get("pattern_key") == key for s in self.skills):
            return None
        exit_layer = pattern.get("exit_layer", 0)
        task_type = pattern.get("task_type", "")
        skill = Skill(
            name=f"skill:{task_type}:{LAYER_NAMES.get(exit_layer, exit_layer)}",
            pattern_key=key,
            trigger={"task_type": task_type,
                     "trajectory_signature": pattern.get("signature", "")},
            action={"entry_layer": exit_layer,
                    "strategy_hint": pattern.get("signature", "")},
            source_layer=exit_layer,
            target_layer=max(0, exit_layer - 1),
            compiled_from_count=self._counts[key],
            compiled_at=int(time.time()),
        )
        self.skills.append(skill)
        return skill

    def count(self, pattern: Dict) -> int:
        return self._counts.get(self._pattern_key(pattern), 0)

    @staticmethod
    def _pattern_key(pattern: Dict) -> str:
        return f"{pattern.get('task_type', '')}|{pattern.get('signature', '')}"


# ============================================================================
# 后治理主入口
# ============================================================================

class PostGovernance:
    """从每次 solve 的层级轨迹中提取改进信号。"""

    def __init__(self,
                 calibrator: Optional[EntryLayerCalibrator] = None,
                 compiler: Optional[PatternCompiler] = None):
        self.calibrator = calibrator or EntryLayerCalibrator()
        self.compiler = compiler or PatternCompiler()
        self._lock = threading.Lock()

    def process(self, task: Task, layer_trajectory: List[Dict],
                outcome) -> Attribution:
        """处理一次 solve。

        layer_trajectory: [{"layer": 0, "outcome": "fail"}, ..., {"layer": 2, "outcome": "success"}]
        outcome: bool 或编排器结果 dict（"completed" 视为成功）。
        返回归因; 若触发模式编译, 同时产出 Skill。
        """
        attribution = self._attribute(task, layer_trajectory, outcome)
        with self._lock:
            self.calibrator.update(attribution.get("task_type", ""),
                                   attribution)
            pattern = {
                "task_type": attribution.get("task_type", ""),
                "signature": attribution.get("pattern", ""),
                "entry_layer": attribution.get("entry_layer", 0),
                "exit_layer": attribution.get("exit_layer", 0),
                "success": attribution.get("success", False),
            }
            count = self.compiler.observe(pattern)
            attribution["pattern_count"] = count
            attribution["repeated_pattern"] = count >= self.compiler.threshold
            if attribution["repeated_pattern"]:
                skill = self.compiler.compile(pattern)
                if skill is not None:
                    attribution["compiled_skill"] = skill["name"]
        return attribution

    def recommend_entry_layer(self, task_type: str) -> int:
        with self._lock:
            return self.calibrator.recommend(task_type)

    # ------------------------------------------------------------------

    @staticmethod
    def _attribute(task: Task, layer_trajectory: List[Dict],
                   outcome) -> Attribution:
        if isinstance(outcome, dict):
            success = outcome.get("status") == "completed"
        else:
            success = bool(outcome)
        layers = [step.get("layer", 0) for step in layer_trajectory] or [0]
        signature = ">".join(
            f"{s.get('layer', 0)}{'S' if s.get('outcome') == 'success' else 'F'}"
            for s in layer_trajectory) or "0?"
        return Attribution(
            task_type=task.get("expected_output_type", ""),
            entry_layer=layers[0],
            exit_layer=layers[-1],
            success=success,
            pattern=signature,
        )


# ============================================================================
# 数据质量监控（Spec-E 运维部分；端点接线待 admin_server.py 释放后接入）
# ============================================================================

def data_quality_report(live_stats: Dict) -> Dict:
    """各数据产品的 SLA 满足情况。

    live_stats 形如:
      {"SignalSnapshot": {"last_emit_age_s": 12, "ifc_enabled": True},
       "ManifestLine": {"coverage": 1.0}}

    返回 {product: {"sla": {...}, "satisfied": bool, "detail": str}}。
    未知产品/未知 SLA 键 fail-open（satisfied=True）。
    """
    from contract_registry import CONTRACT_REGISTRY

    report: Dict[str, Dict] = {}
    for name, meta in CONTRACT_REGISTRY.items():
        sla = meta.get("sla")
        if not sla:
            continue
        stats = live_stats.get(name, {})
        checks = []
        if "freshness" in sla:
            age = stats.get("last_emit_age_s")
            ok = isinstance(age, (int, float)) and age <= 60
            checks.append(("freshness", ok,
                           f"last_emit_age_s={age}"))
        if "completeness" in sla:
            # "ifc_enabled": 信号层开关打开即视为完备来源可用
            enabled = bool(stats.get("ifc_enabled"))
            checks.append(("completeness", enabled,
                           f"ifc_enabled={enabled}"))
        if "coverage" in sla:
            cov = stats.get("coverage")
            ok = isinstance(cov, (int, float)) and cov >= 1.0
            checks.append(("coverage", ok, f"coverage={cov}"))
        unknown = set(sla) - {"freshness", "completeness", "coverage"}
        failed = [(k, d) for k, ok, d in checks if not ok]
        report[name] = {
            "sla": sla,
            "satisfied": len(failed) == 0 and not unknown,
            "failed_checks": [k for k, _ in failed],
            "unknown_sla_keys": sorted(unknown),
            "detail": "; ".join(f"{k}:{d}" for k, _, d in checks) or "no checks",
        }
    report["_summary"] = {
        "products": len(report),
        "satisfied": sum(1 for v in report.values() if v.get("satisfied")),
        "generated_at": int(time.time()),
    }
    return report


__all__ = [
    "LAYER_NAMES", "Attribution", "Skill",
    "EntryLayerCalibrator", "PatternCompiler", "PostGovernance",
    "data_quality_report",
]
