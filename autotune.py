"""autotune.py — BVR 自调优闭环 Phase 1。

设计文档: docs/02-architecture-design/bvr-autotune-loop-design-20260916.md

职责（T2/T4/T5/T6，纯旁路，fail-open，不进入 proxy 运行路径）:
  - AutoTuneProposal 契约 + 三性标签（依他=observed / 遍计=unverified_proposal /
    圆成=verified）;
  - 提案提取器规则引擎: 失败事件 → 合法改造面内的参数改写提案;
  - 分级签名 + 限速 + 失败面绑定闸门（binding checks）;
  - 审计落盘 logs/diag/autotune/journal.jsonl（MB 上限轮转）;
  - apply/rollback 记录（低风险自动、中/高风险需人工签名）。

约束:
  - 标准库 only;
  - 不生成代码补丁; 只产参数改写提案（合法改造面 = CONFIG_REGISTRY reloadable
    参数, 以及显式登记的特例目标）;
  - 禁区: 密钥/模型权重/治理规则/主体边界项永不出现。

三期标签语义（对齐维特根斯坦-唯识讨论）:
  依他 (observed):           已观测的失败事实
  遍计 (unverified_proposal): 未证提案（无兑现记录）
  圆成 (verified):           已通过影子兑现闸门的提案
"""
import json
import os
import sys
import time
from typing import Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
AUTOTUNE_DIR = os.path.join(_REPO_ROOT, "logs", "diag", "autotune")
JOURNAL_FILENAME = "journal.jsonl"
APPLIED_DIRNAME = "applied"
JOURNAL_MAX_MB = 10
DEFAULT_DAILY_LIMIT = 5
DEFAULT_SESSION_LIMIT = 1
EVIDENCE_MAX = 8

THREE_NATURES = ("observed", "unverified_proposal", "verified")

# 禁区参数（永不作为目标参数）——密钥 / 主体边界 / 治理规则
# 前缀: 治理/边界类; 后缀: 密钥类。精确匹配避免子串误伤
# （如 PROXY_OOM_SAFE_TOKENS 不是密钥, 不含 _TOKEN 后缀, 合法）。
FORBIDDEN_PARAM_PREFIXES = ("BOUNDARY", "GOVERNANCE")
FORBIDDEN_PARAM_SUFFIXES = (
    "_API_KEY", "_SECRET", "_PASSWORD", "_CREDENTIAL", "_TOKEN", "_PIN",
)
FORBIDDEN_PARAM_NAMES = (
    "LLAMA_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY", "PROXY_ADMIN_TOKEN",
)

# 扩能力类（升预算/放开限制）提案降权标记
CAPABILITY_RAISING_PARAMS = (
    "PROXY_MAX_CONCURRENT", "PROXY_CTX_CHARS_LIMIT", "PROXY_MAX_REQUEST_BYTES",
    "PROXY_CLOUD_MAX_REQUEST_BYTES", "PROXY_ROUTE_CLOUD_CONCURRENT",
)

# 中风险参数（reloadable 但影响行为面广, 需人一键审批, 不自动）
MEDIUM_PARAMS = (
    "PROXY_COMPRESS_TARGET_RATIO", "PROXY_ROUTE_THRESHOLD_CHARS",
    "PROXY_ROUTE_STICKY", "PROXY_TOOL_FILTER_MAX", "PROXY_CTX_KEEP_MESSAGES",
    "PROXY_CTX_EPOCH_TRIGGER_TOKENS",
)

RISK_BY_SCOPE = {"reloadable": "low", "backend": "medium", "startup": "high"}

# 特例目标（非数值 reloadable 参数, 需要人工评审的"重划界"提案）
REVIEW_TARGETS = ("route_policy_review", "operator_library_review", "criterion_review")

REASON_CODES = (
    "no_evidence",          # 无失败面绑定
    "forbidden_param",      # 命中禁区
    "outside_legal_surface",  # 不在合法改造面（非 reloadable 且非特例目标）
    "no_prediction",        # 缺预言
    "over_daily_limit",     # 超每日晋级上限
    "over_session_limit",   # 超会话晋级上限
    "capability_raise",     # 扩能力类提案被降权
    "malformed",            # 结构不合法
)


# ============================================================
# 基础工具
# ============================================================
def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


_ID_SEQ = {"n": 0}


def make_proposal_id(ts: Optional[str] = None) -> str:
    base = (ts or utc_ts()).replace("-", "").replace(":", "").replace("T", "")
    _ID_SEQ["n"] += 1
    return "at_%s_%02d" % (base, _ID_SEQ["n"])


def _is_forbidden_param(param: str) -> bool:
    up = (param or "").upper()
    if up in FORBIDDEN_PARAM_NAMES:
        return True
    if any(up.startswith(p) for p in FORBIDDEN_PARAM_PREFIXES):
        return True
    return any(up.endswith(s) for s in FORBIDDEN_PARAM_SUFFIXES)


def is_capability_raising(param: str) -> bool:
    return param in CAPABILITY_RAISING_PARAMS


# 三性标签
def label_nature(proposal_status: str) -> str:
    mapping = {
        "applied": "verified",
        "pending": "unverified_proposal",
        "rejected": "unverified_proposal",
        "rolled_back": "unverified_proposal",
    }
    return mapping.get(proposal_status, "observed")


# ============================================================
# 合法改造面校验
# ============================================================
def reloadable_param_set() -> Optional[set]:
    """读取 CONFIG_REGISTRY 的 reloadable 参数集合; 失败返回 None（fail-open）。"""
    try:
        from proxy_config import CONFIG_REGISTRY  # deferred, 避免循环依赖
    except Exception:
        return None
    out = set()
    for name, meta in CONFIG_REGISTRY.items():
        if isinstance(meta, dict) and meta.get("scope") == "reloadable":
            out.add(name)
    return out


def validate_target_param(param: str,
                         reloadable_set: Optional[set] = None) -> tuple:
    """校验目标参数是否在合法改造面内。

    返回 (ok: bool, reason: Optional[str])。
    禁区 → forbidden_param; 非 reloadable 且非特例 → outside_legal_surface。
    """
    if not isinstance(param, str) or not param:
        return False, "malformed"
    if _is_forbidden_param(param):
        return False, "forbidden_param"
    if param in REVIEW_TARGETS:
        return True, None
    if reloadable_set is None:
        reloadable_set = reloadable_param_set()
    if reloadable_set is not None and param not in reloadable_set:
        return False, "outside_legal_surface"
    return True, None


def classify_risk(param: str) -> str:
    """按参数类型分级: 特例目标=high; 扩能力/中风险集=medium; 其余 reloadable=low。"""
    if param in REVIEW_TARGETS:
        return "high"
    if is_capability_raising(param) or param in MEDIUM_PARAMS:
        return "medium"
    return "low"


# ============================================================
# 提案结构
# ============================================================
def build_proposal(target_param: str, from_val: Optional[str],
                   to_val: Optional[str], evidence_refs: List[str],
                   metric: str, delta: str,
                   source: str = "rule", ts: Optional[str] = None) -> Dict:
    """构造 AutoTuneProposal（含三性标签与风险分级）。"""
    ts = ts or utc_ts()
    return {
        "proposal_id": make_proposal_id(ts),
        "ts": ts,
        "source": source,
        "target_param": target_param,
        "from": from_val,
        "to": to_val,
        "evidence_ref": list(evidence_refs)[:EVIDENCE_MAX],
        "prediction": {"metric": metric, "delta": delta},
        "risk_class": classify_risk(target_param),
        "status": "pending",
        "three_nature": "unverified_proposal",
        "signature": None,
        "rollback_cmd": None,
    }


# ============================================================
# 提取器规则引擎（T2）
# ============================================================
# 失败事件类型 → 合法改造面内的候选改写 (param, adjust, risk_hint)
# adjust 为 callable(from_val_str) -> to_val_str; None 表示需人工（重划界）。
# 只认"修失败", 不认"长能力"——所有规则都是收紧或保上下文, 不放宽并发/预算。
RULES = {
    "loop": {
        "param": "PROXY_LOOP_THRESHOLD",
        "adjust": lambda v: str(int(v) + 1),
        "metric": "loop_l1_rate",
        "delta": "-20%",
    },
    "amnesia": {
        "param": "PROXY_CTX_KEEP_MESSAGES",
        "adjust": lambda v: str(int(v) + 10),
        "metric": "amnesia_turn_gap",
        "delta": "-30%",
    },
    "prefix_break": {
        "param": "PROXY_CTX_KEEP_MESSAGES",
        "adjust": lambda v: str(int(v) + 10),
        "metric": "prefix_hit_ratio",
        "delta": "+10%",
    },
    "oom_evict": {
        "param": "PROXY_OOM_SAFE_TOKENS",
        "adjust": lambda v: str(max(0, int(v) - 5000)),
        "metric": "metal_evict_rows",
        "delta": "-50%",
    },
    "dup_reread": {
        "param": "PROXY_CTX_KEEP_MESSAGES",
        "adjust": lambda v: str(int(v) + 5),
        "metric": "dup_reread_rate",
        "delta": "-30%",
    },
    "off_track": {
        "param": "route_policy_review",
        "adjust": None,  # 重划界提案, 永不自动
        "metric": "stagnation_advisor",
        "delta": "review",
    },
}


def current_param_value(param: str, values: Dict[str, str],
                        default: Optional[str] = None) -> Optional[str]:
    """从给定参数值快照取当前值; 缺省回退 default。"""
    if param in values:
        return str(values[param])
    if param in REVIEW_TARGETS:
        return None
    return default


def extract_proposals_from_events(events: List[Dict],
                                  values: Optional[Dict[str, str]] = None) -> List[Dict]:
    """规则引擎: 失败事件列表 → 提案列表（每条事件至多一提案, 幂等去重）。

    events: [{"type": "loop", "ref": "fact://loop/<sid>/<seq>", ...}, ...]
    values: 参数当前值快照 {param: value}（用于 from/to 计算）
    """
    values = values or {}
    proposals: List[Dict] = []
    seen = set()
    for ev in events:
        ev_type = ev.get("type") or ""
        rule = RULES.get(ev_type)
        if rule is None:
            continue
        ref = str(ev.get("ref") or "fact://unknown")
        if ref in seen:
            continue
        seen.add(ref)
        param = rule["param"]
        cur = current_param_value(param, values)
        if rule["adjust"] is None:
            to_val = None
            # 特例（重划界）必须带 at_least 一条失败引用; 无 from
            if not cur:
                from_val = None
            else:
                from_val = cur
        else:
            if cur is None:
                continue  # 无当前值无法计算 from/to
            from_val = cur
            try:
                to_val = rule["adjust"](cur)
            except (ValueError, TypeError):
                continue
        prop = build_proposal(
            target_param=param, from_val=from_val, to_val=to_val,
            evidence_refs=[ref], metric=rule["metric"], delta=rule["delta"],
        )
        proposals.append(prop)
    return proposals


# ============================================================
# 闸门（T4 限速 + 失败面绑定 + T6 防博弈）
# ============================================================
def gate_proposal(proposal: Dict,
                  daily_applied: int = 0,
                  session_applied: int = 0,
                  daily_limit: int = DEFAULT_DAILY_LIMIT,
                  session_limit: int = DEFAULT_SESSION_LIMIT,
                  reloadable_set: Optional[set] = None) -> tuple:
    """逐条闸门: 返回 (ok, reason_code | None)。

    判定顺序: 结构 → 禁区/改造面 → 失败面绑定 → 预言 → 扩能力降权 → 限速。
    """
    # 结构
    if not isinstance(proposal, dict) or not proposal.get("target_param"):
        return False, "malformed"
    param = proposal["target_param"]

    # 禁区 / 改造面
    ok, reason = validate_target_param(param, reloadable_set)
    if not ok:
        return False, reason

    # 失败面绑定: 无证据引用直接驳回
    refs = proposal.get("evidence_ref") or []
    if not refs:
        return False, "no_evidence"

    # 预言: 特例目标(重划界)允许 delta=review; 数值改写必须带可兑现预言
    pred = proposal.get("prediction") or {}
    metric = pred.get("metric")
    delta = pred.get("delta")
    if param in REVIEW_TARGETS:
        if delta != "review":
            return False, "no_prediction"
    elif not metric or not delta or delta == "review":
        return False, "no_prediction"

    # 扩能力降权（T6: 只认修失败, 不认长能力）
    if is_capability_raising(param) and proposal.get("status") == "pending":
        return False, "capability_raise"

    # 限速
    if session_applied >= session_limit:
        return False, "over_session_limit"
    if daily_applied >= daily_limit:
        return False, "over_daily_limit"

    return True, None


# ============================================================
# 审计落盘（logs/diag/autotune/journal.jsonl, MB 上限轮转）
# ============================================================
def _journal_path(base_dir: Optional[str] = None) -> str:
    base = base_dir or AUTOTUNE_DIR
    return os.path.join(base, JOURNAL_FILENAME)


def ensure_dir(base_dir: Optional[str] = None) -> str:
    base = base_dir or AUTOTUNE_DIR
    applied = os.path.join(base, APPLIED_DIRNAME)
    os.makedirs(applied, exist_ok=True)
    return base


def _enforce_journal_cap(path: str, max_mb: int = JOURNAL_MAX_MB) -> None:
    """超上限删最老记录（同台账/清单 _enforce 策略）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    cap = max_mb * 1024 * 1024
    if size <= cap:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        keep = []
        total = 0
        for ln in reversed(lines):
            total += len(ln)
            keep.append(ln)
            if total >= cap * 0.5:  # 压到半容量, 避免频繁轮转
                break
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(reversed(keep))
    except OSError:
        pass


def journal_record(proposal: Dict, status: str, signature: Optional[str] = None,
                   reason: Optional[str] = None,
                   base_dir: Optional[str] = None) -> Dict:
    """写一条审计记录（变更时更新提案 status/三性标签/签名/原因）。"""
    rec = dict(proposal)
    rec["ts"] = utc_ts()
    rec["status"] = status
    rec["three_nature"] = label_nature(status)
    if signature is not None:
        rec["signature"] = signature
    if reason is not None:
        rec["reason"] = reason
    base = ensure_dir(base_dir)
    path = _journal_path(base)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _enforce_journal_cap(path)
    except OSError:
        return rec
    return rec


def list_journal(base_dir: Optional[str] = None) -> List[Dict]:
    base = base_dir or AUTOTUNE_DIR
    path = _journal_path(base)
    records = []
    if not os.path.exists(path):
        return records
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return records
    return records


# ============================================================
# 分级签名 + apply / rollback（T5）
# ============================================================
def apply_proposal(proposal: Dict, signature: str,
                   daily_applied: int = 0,
                   session_applied: int = 0,
                   base_dir: Optional[str] = None,
                   reloadable_set: Optional[set] = None,
                   allow_auto: bool = True) -> tuple:
    """应用一条提案。返回 (ok, detail)。

    签名规则（T5）:
      low     : signature in ("auto", "human") 且 allow_auto
      medium  : 仅 "human"
      high    : 仅 "human"（重划界提案永不自动）
    """
    ok, reason = gate_proposal(proposal, daily_applied, session_applied,
                               reloadable_set=reloadable_set)
    if not ok:
        journal_record(proposal, "rejected", signature="gate", reason=reason,
                       base_dir=base_dir)
        return False, reason

    risk = proposal.get("risk_class", "low")
    if risk == "low":
        if signature not in ("auto", "human") or not allow_auto:
            journal_record(proposal, "rejected", signature=signature,
                           reason="signature_required", base_dir=base_dir)
            return False, "signature_required"
    else:
        if signature != "human":
            journal_record(proposal, "rejected", signature=signature,
                           reason="human_signature_required", base_dir=base_dir)
            return False, "human_signature_required"

    applied = dict(proposal)
    applied["status"] = "applied"
    applied["three_nature"] = "verified"
    applied["signature"] = signature
    applied["applied_ts"] = utc_ts()
    applied["rollback_cmd"] = "manage.sh autotune rollback %s" % (
        proposal.get("proposal_id", ""))
    # 低风险把参数改写落到 overrides 文件（人类/后续 reload 钩子消费）
    # 参数行内嵌 proposal_id, 使 rollback 能精确删除该提案的行
    if risk == "low" and proposal.get("to") is not None:
        try:
            ov_path = os.path.join(ensure_dir(base_dir), "overrides.conf")
            with open(ov_path, "a", encoding="utf-8") as f:
                f.write("# %s %s %s -> %s (%s)\n" % (
                    proposal["proposal_id"], proposal["target_param"],
                    proposal.get("from"), proposal["to"], signature))
                f.write("%s=%s # %s\n" % (
                    proposal["target_param"], proposal["to"],
                    proposal["proposal_id"]))
        except OSError:
            pass
    journal_record(applied, "applied", signature=signature, base_dir=base_dir)
    return True, "applied"


def rollback_proposal(proposal_id: str,
                      base_dir: Optional[str] = None) -> tuple:
    """回滚一条已应用提案: 记录 rolled_back + 从 overrides.conf 移除该行。"""
    records = list_journal(base_dir)
    target = None
    for r in records:
        if r.get("proposal_id") == proposal_id and r.get("status") == "applied":
            target = r
            break
    if target is None:
        return False, "not_found"
    base = ensure_dir(base_dir)
    ov_path = os.path.join(base, "overrides.conf")
    if os.path.exists(ov_path):
        try:
            with open(ov_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # 按内嵌 proposal_id 精确删除该提案的注释行 + 参数行
            out = [ln for ln in lines if proposal_id not in ln]
            with open(ov_path, "w", encoding="utf-8") as f:
                f.writelines(out)
        except OSError:
            pass
    journal_record(target, "rolled_back", signature="human",
                   reason="rollback", base_dir=base_dir)
    return True, "rolled_back"


# ============================================================
# 会话/每日晋级计数（限速用）
# ============================================================
def applied_counts(base_dir: Optional[str] = None) -> Dict[str, int]:
    """返回 {"daily": n, "session": n}——当日与最近一条已应用提案的计数。"""
    records = list_journal(base_dir)
    applied = [r for r in records if r.get("status") == "applied"]
    today = utc_ts()[:10]
    daily = sum(1 for r in applied if str(r.get("ts", ""))[:10] == today)
    return {"daily": daily, "session": min(1, daily) if applied else 0}


__all__ = [
    "AUTOTUNE_DIR", "THREE_NATURES", "REASON_CODES", "RULES",
    "build_proposal", "extract_proposals_from_events", "gate_proposal",
    "apply_proposal", "rollback_proposal", "journal_record", "list_journal",
    "applied_counts", "label_nature", "validate_target_param",
    "classify_risk", "make_proposal_id", "reloadable_param_set",
]


# ============================================================
# CLI（供 manage.sh autotune 子命令调用, 纯 stdlib）
# ============================================================
def _load_events(path):
    events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return events


def _read_conf_values(path):
    """解析 Bash-sourcable 配置文件的 KEY="value" 行。"""
    vals = {}
    if not path or not os.path.exists(path):
        return vals
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            vals[key.strip()] = value
    return vals


def _print_proposal(p):
    print(json.dumps(p, ensure_ascii=False))


def cli_main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "help"
    base = os.environ.get("AUTOTUNE_DIR")

    if cmd == "generate":
        path = argv[1] if len(argv) > 1 else None
        if not path:
            print("usage: autotune.py generate <events.jsonl> [--conf <conf>]",
                  file=sys.stderr)
            return 2
        conf = None
        if "--conf" in argv:
            conf = argv[argv.index("--conf") + 1]
        events = _load_events(path)
        values = _read_conf_values(conf or os.path.join("configs", "active.conf"))
        props = extract_proposals_from_events(events, values)
        for p in props:
            journal_record(p, "pending", base_dir=base)
        for p in props:
            _print_proposal(p)
        return 0

    if cmd == "list":
        for r in list_journal(base):
            _print_proposal(r)
        return 0

    if cmd == "apply":
        pid = argv[1] if len(argv) > 1 else None
        if not pid:
            print("usage: autotune.py apply <id> [--auto|--human]",
                  file=sys.stderr)
            return 2
        sig = "human" if "--human" in argv else "auto"
        recs = list_journal(base)
        target = next((r for r in recs
                       if r.get("proposal_id") == pid
                       and r.get("status") == "pending"), None)
        if target is None:
            print("no pending proposal: %s" % pid, file=sys.stderr)
            return 3
        ok, detail = apply_proposal(target, sig, base_dir=base)
        print("%s %s" % (ok, detail))
        return 0 if ok else 1

    if cmd == "rollback":
        pid = argv[1] if len(argv) > 1 else None
        if not pid:
            print("usage: autotune.py rollback <id>", file=sys.stderr)
            return 2
        ok, detail = rollback_proposal(pid, base_dir=base)
        print("%s %s" % (ok, detail))
        return 0 if ok else 1

    if cmd == "status":
        counts = applied_counts(base)
        total = len(list_journal(base))
        print(json.dumps({"total": total, "daily_applied": counts["daily"]}))
        return 0

    print("usage: autotune.py <generate|list|apply|rollback|status> ...",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(cli_main())