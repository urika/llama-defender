#!/usr/bin/env python3
"""jev_quota.py — Command Code 额度探测（只读元数据，零内容外发）。

为什么需要它：jev（typesafe/jev）与运营 CLI 会话**共享同一账户的两个滚动窗口**
（5 小时／周，按 credit 计）——窗口被会话吃满时，jev 调用会被 `HTTP 429` 挡住。
2026-09-27 的 ex04 受阻事件即此（定案＝5 小时窗超额，非并发机制）。
官方 provider 端点**没有额度接口**、响应头也无额度字段；本工具用的是 CLI 自己的
内部端点 `/alpha/billing/credits`（自 CLI bundle 提取，**未文档化** ⇒ 必须容错降级）。
分析与实测台账见 `docs/06-reference-metrics/jev-quota.md`。

用法：
  python3 tools/jev_quota.py            # 人读
  python3 tools/jev_quota.py --json     # 机器读（批前置检查用）
  python3 tools/jev_quota.py --plan     # 附计划/计费周期

退出码：0 充足 ｜ 3 任一窗 ≥80%（接近） ｜ 4 任一窗超出（exceeded） ｜ 5 不可探测
（5 与 4 严格区分——**探测失败不得被误读成额度耗尽**；与 jq/健康检查同风格）

密钥：`CMD_API_KEY` 环境变量优先，否则读 `~/.zshrc` 的 `export CMD_API_KEY=` 行
（同 mcp-launchers/jev.zsh 单一源模式）。**绝不回显密钥**，不写入 argv。
计费：元数据 GET 实测**不计次不计费**（2026-09-27 实测：连读前后 totalCount 与窗口
used 逐位不变），故可放心前置调用；但仍不应作为心跳滥用。
"""
import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = "https://api.commandcode.ai"
CREDITS_PATH = "/alpha/billing/credits"
SUBSCRIPTIONS_PATH = "/alpha/billing/subscriptions"
NEAR_PCT = 80.0
TIMEOUT_S = 30
# WAF 会拦 urllib 默认 UA（同 jev_mcp_server 的实测结论）
USER_AGENT = "jev-quota/0.1"

EXIT_OK, EXIT_NEAR, EXIT_EXCEEDED, EXIT_UNDETECTABLE = 0, 3, 4, 5


def load_key(env=None):
    """密钥单一源：env `CMD_API_KEY` → `~/.zshrc` 的 export 行。不打印、不落盘。"""
    env = os.environ if env is None else env
    key = env.get("CMD_API_KEY", "")
    if key:
        return key
    path = os.path.expanduser("~/.zshrc")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("export CMD_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def _get(path, key, opener=urllib.request.urlopen, timeout=TIMEOUT_S):
    req = urllib.request.Request(API_BASE + path, headers={
        "Authorization": "Bearer " + key, "User-Agent": USER_AGENT})
    with opener(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _redact(text, key):
    return text.replace(key, "[redacted]") if key else text


def fetch_usage(key, opener=urllib.request.urlopen, timeout=TIMEOUT_S,
                with_plan=False, now=None):
    """返回结构化额度快照；不可探测时抛 QuotaUndetectable（不回显密钥）。"""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        raw = _get(CREDITS_PATH, key, opener, timeout)
    except urllib.error.HTTPError as exc:
        raise QuotaUndetectable(
            "HTTP %s（端点疑似变更/凭证失效；若为 429 亦可能是本端点被限流——"
            "**不等于额度耗尽**，请对照 /provider 侧的实际拒绝原因）" % exc.code)
    except Exception as exc:  # noqa: BLE001 - 网络类异常一律归「不可探测」
        raise QuotaUndetectable("网络错误：%s" % type(exc).__name__)
    credit = raw.get("credits") or {}
    limits = raw.get("windowLimits") or {}
    windows, verdict = {}, "ok"
    for name, label in (("fiveHour", "5 小时窗"), ("weekly", "周窗")):
        w = limits.get(name) or {}
        used, cap = w.get("used"), w.get("cap")
        if used is None or not cap:
            continue
        pct = 100.0 * used / cap
        reset_at = w.get("resetAt")
        reset_dt = (datetime.datetime.fromtimestamp(reset_at / 1000.0,
                                                    datetime.timezone.utc)
                    if reset_at else None)
        windows[name] = {"label": label, "used": used, "cap": cap,
                         "pct": round(pct, 1), "exceeded": bool(w.get("exceeded")),
                         "reset_at": reset_dt.isoformat() if reset_dt else None,
                         "resets_in_s": (reset_dt - now).total_seconds() if reset_dt else None}
        if w.get("exceeded"):
            verdict = "exceeded"
        elif pct >= NEAR_PCT and verdict != "exceeded":
            verdict = "near"
    out = {"checked_at": now.isoformat(), "windows": windows,
           "verdict": verdict,
           "credits": {"remaining_monthly": credit.get("monthlyCredits"),
                       "purchased": credit.get("purchasedCredits"),
                       "free": credit.get("freeCredits"),
                       "below_threshold": credit.get("belowThreshold")}}
    if with_plan:
        try:
            sub = (_get(SUBSCRIPTIONS_PATH, key, opener, timeout) or {}).get("data") or {}
            out["plan"] = {"plan_id": sub.get("planId"), "status": sub.get("status"),
                           "period_start": sub.get("currentPeriodStart"),
                           "period_end": sub.get("currentPeriodEnd")}
        except Exception as exc:  # noqa: BLE001 - 计划信息缺失不影响额度判定
            out["plan"] = {"error": type(exc).__name__}
    return out


class QuotaUndetectable(RuntimeError):
    """额度不可探测（端点变更/凭证/网络）——调用方**不得**当作「额度耗尽」。"""


def exit_code_for(verdict):
    return {"ok": EXIT_OK, "near": EXIT_NEAR, "exceeded": EXIT_EXCEEDED}.get(
        verdict, EXIT_UNDETECTABLE)


def render(snap):
    lines = ["jev 额度（API 侧，%s）" % snap["checked_at"]]
    for name in ("fiveHour", "weekly"):
        w = snap["windows"].get(name)
        if not w:
            continue
        left = w.get("resets_in_s")
        left_s = ("还有 %.1fh" % (left / 3600.0)) if left is not None else "重置时刻未知"
        lines.append("  %-9s %10.4f / %-6.2f (%5.1f%%)  →  重置 %s（%s）"
                     % (w["label"], w["used"], w["cap"], w["pct"],
                        (w.get("reset_at") or "?")[:16].replace("T", " "), left_s))
    c = snap.get("credits") or {}
    if c.get("remaining_monthly") is not None:
        lines.append("  剩余月额：%.2f" % c["remaining_monthly"])
    if snap.get("plan", {}).get("plan_id"):
        p = snap["plan"]
        lines.append("  计划：%s（%s；周期 %s → %s）"
                     % (p["plan_id"], p["status"],
                        (p.get("period_start") or "")[:10],
                        (p.get("period_end") or "")[:10]))
    lines.append("判定：%s（退出码 %d）" % (snap["verdict"], exit_code_for(snap["verdict"])))
    if snap["verdict"] == "exceeded":
        lines.append("  ⇒ 超额：别盲退避——等窗口重置，或 `/extra` 买额度 / `/upgrade` 升档。")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Command Code 额度探测（只读元数据）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--plan", action="store_true", help="附计划/计费周期")
    args = ap.parse_args(argv)
    key = load_key()
    if not key:
        msg = "CMD_API_KEY 缺失（env 与 ~/.zshrc 均无）"
        print(json.dumps({"verdict": "undetectable", "error": msg}, ensure_ascii=False)
              if args.json else "额度探测失败：%s" % msg)
        return EXIT_UNDETECTABLE
    try:
        snap = fetch_usage(key, with_plan=args.plan)
    except QuotaUndetectable as exc:
        detail = _redact(str(exc), key)
        print(json.dumps({"verdict": "undetectable", "error": detail},
                         ensure_ascii=False) if args.json
              else "额度探测失败：%s\n（注意：不可探测 ≠ 额度耗尽）" % detail)
        return EXIT_UNDETECTABLE
    print(json.dumps(snap, ensure_ascii=False, indent=2) if args.json else render(snap))
    return exit_code_for(snap["verdict"])


if __name__ == "__main__":
    sys.exit(main())
