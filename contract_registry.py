#!/usr/bin/env python3
"""contract_registry.py — 契约注册表（stdlib only，叶子元数据模块）。

所有数据契约的元数据管理 + 运行时验证。
fail-open: 验证失败返回错误列表，不抛异常。
"""
from typing import Any, Dict, List

CONTRACT_REGISTRY: Dict[str, Dict[str, Any]] = {

    # ── Signal 域 ──
    "SignalSnapshot": {
        "version": 1,
        "module": "signal_types",
        "factory": "build_signal_snapshot",
        "producer": ["ifc_metrics.build_ifc_section"],
        "consumers": ["P1_decompose", "P5_escalate", "post_governance", "trace_query"],
        "join_keys": ["session_key", "turn"],
        "sla": {"freshness": "realtime", "completeness": "ifc_enabled"},
    },
    "ViewSummary": {
        "version": 1,
        "module": "signal_types",
        "producer": ["ifc_metrics.view_summary"],
        "consumers": ["ifc_metrics.diff_views"],
        "join_keys": [],
    },
    "DiffResult": {
        "version": 1,
        "module": "signal_types",
        "producer": ["ifc_metrics.diff_views"],
        "consumers": ["ifc_metrics.infer_ile_kinds"],
        "join_keys": [],
    },
    "ReconcileResult": {
        "version": 1,
        "module": "signal_types",
        "producer": ["ifc_metrics.reconcile"],
        "consumers": ["trace_query"],
        "join_keys": ["session_key"],
    },

    # ── Protocol 域 ──
    "Task": {
        "version": 1,
        "module": "protocol_types",
        "factory": "build_task",
        "producer": ["application_layer"],
        "consumers": ["P1_decompose", "P2_execute"],
        "join_keys": ["id"],
    },
    "SubTask": {
        "version": 1,
        "module": "protocol_types",
        "factory": "build_subtask",
        "producer": ["P1_decompose"],
        "consumers": ["P2_execute", "P3_verify"],
        "join_keys": ["id", "parent_task_id"],
    },
    "Patch": {
        "version": 1,
        "module": "protocol_types",
        "factory": "build_patch",
        "producer": ["P2_execute"],
        "consumers": ["P3_verify", "git_commit", "post_governance"],
        "join_keys": ["task_id"],
    },
    "Citation": {
        "version": 1,
        "module": "protocol_types",
        "producer": ["P2_execute"],
        "consumers": ["P3_verify"],
        "join_keys": ["file"],
    },
    "Verdict": {
        "version": 1,
        "module": "protocol_types",
        "factory": "build_verdict",
        "producer": ["P3_verify"],
        "consumers": ["P5_escalate", "post_governance"],
        "join_keys": ["patch_id"],
    },
    "EscalationDecision": {
        "version": 1,
        "module": "protocol_types",
        "factory": "build_escalation",
        "producer": ["P5_escalate"],
        "consumers": ["post_governance", "circuit_breaker"],
        "join_keys": ["task_id"],
    },

    # ── 跨域 ──
    "SessionState": {
        "version": 1,
        "module": "protocol_types",
        "producer": ["protocol_orchestrator._init_state"],
        "consumers": ["所有协议"],
        "join_keys": ["session_key"],
    },
    "ManifestLine": {
        "version": 1,
        "module": "memory_stores",
        "producer": ["truncation_fifo_hook", "context_engine_collapse_hook"],
        "consumers": ["P4_recall", "E1_audit", "trace_query"],
        "join_keys": ["session_key", "anchor"],
        "sla": {"coverage": "100%"},
    },
}


def validate_contract(data: dict, contract_name: str,
                      strict: bool = False) -> List[str]:
    """验证数据是否符合契约——返回错误列表（空=通过）。fail-open。"""
    meta = CONTRACT_REGISTRY.get(contract_name)
    if not meta:
        return [f"未注册的契约: {contract_name}"]

    errors = []

    # 检查 contract_version
    if "contract_version" not in data and meta.get("factory"):
        errors.append(f"缺少 contract_version 字段")

    # 检查 join_keys 存在性（非空值即可）
    for key in meta.get("join_keys", []):
        if key not in data:
            errors.append(f"缺少 join key: {key}")

    return errors


def get_consumers(contract_name: str) -> List[str]:
    """查询某契约的所有消费者——变更影响分析。"""
    return CONTRACT_REGISTRY.get(contract_name, {}).get("consumers", [])


def get_producers(contract_name: str) -> List[str]:
    """查询某契约的所有生产者。"""
    return CONTRACT_REGISTRY.get(contract_name, {}).get("producers", [])


def get_producer_contracts(protocol_name: str) -> Dict[str, List[str]]:
    """查询某协议涉及的契约（生产+消费）——依赖分析。"""
    produces, consumes = [], []
    for name, meta in CONTRACT_REGISTRY.items():
        if protocol_name in meta.get("producer", []):
            produces.append(name)
        if protocol_name in meta.get("consumers", []):
            consumes.append(name)
    return {"produces": produces, "consumes": consumes}


def list_contracts() -> List[str]:
    """列出所有已注册契约。"""
    return sorted(CONTRACT_REGISTRY.keys())


__all__ = [
    "CONTRACT_REGISTRY", "validate_contract",
    "get_consumers", "get_producers", "get_producer_contracts",
    "list_contracts",
]
