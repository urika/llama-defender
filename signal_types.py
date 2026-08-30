#!/usr/bin/env python3
"""signal_types.py — Signal Layer 数据契约（stdlib only，叶子模块）。

定义 Signal Layer 产出、Protocol Layer 消费的 4 个核心类型。
所有字段 Optional（total=False）→ Signal 层故障时 Protocol 层以默认值运行（fail-open）。

契约版本: 1（只加 Optional 字段不+1；删/改字段才+1）
"""
from typing import Any, Dict, List, Optional

# 遵循 protocol_types.CONTRACT_VERSION 的版本纪律
CONTRACT_VERSION = 1


class ViewSummary(dict):
    """发送视图摘要——差分计算的基线。

    由 ifc_metrics.view_summary() 生产。
    描述某一轮发送给模型的消息的结构（锚点、字符量、类型分布）。
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class DiffResult(dict):
    """相邻两轮视图差分——ILE 判定的输入。

    由 ifc_metrics.diff_views() 生产。
    描述两轮之间信息单元的变化（丢弃/收缩/新增/重置）。
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class ReconcileResult(dict):
    """台账对账结果——D_ledger（精度轴）。

    由 ifc_metrics.reconcile() 生产。
    描述探针答案与台账 ground truth 的偏差。
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class SignalSnapshot(dict):
    """某时刻的完整信号快照——P1 分解判据 + P5 升级触发的输入。

    由 Signal Layer(ifc_metrics.build_ifc_section 等) 聚合生产。
    Protocol Layer 的 Decomposer 和 Escalator 消费此类型做决策。

    设计约束:
    - 所有字段 Optional → Signal 层故障时 Protocol 以默认值运行
    - 不可变（值对象，创建后不应修改）
    - JSON 可序列化
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


# ===== 工厂函数（便捷构造）=====

def build_signal_snapshot(
    h_be: Optional[float] = None,
    h_be_trend: Optional[float] = None,
    d_ledger: Optional[float] = None,
    retention: Optional[float] = None,
    rationale_ratio: Optional[float] = None,
    ile: bool = False,
    ile_kinds: Optional[List[str]] = None,
    view_reset: bool = False,
    reread_pressure: int = 0,
    action_diversity: Optional[float] = None,
    cognitive_load: float = 0.0,
    config_fingerprint: str = "",
    session_key: str = "",
    turn: int = 0,
) -> SignalSnapshot:
    """构建 SignalSnapshot——所有参数有默认值（fail-open）。"""
    return SignalSnapshot(
        contract_version=CONTRACT_VERSION,
        h_be=h_be,
        h_be_trend=h_be_trend,
        d_ledger=d_ledger,
        retention=retention,
        rationale_ratio=rationale_ratio,
        ile=ile,
        ile_kinds=ile_kinds or [],
        view_reset=view_reset,
        reread_pressure=reread_pressure,
        action_diversity=action_diversity,
        cognitive_load=cognitive_load,
        config_fingerprint=config_fingerprint,
        session_key=session_key,
        turn=turn,
    )


__all__ = [
    "CONTRACT_VERSION",
    "ViewSummary", "DiffResult", "ReconcileResult", "SignalSnapshot",
    "build_signal_snapshot",
]
