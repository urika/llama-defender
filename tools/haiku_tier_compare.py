#!/usr/bin/env python3
"""haiku 档分流实测：local-9b(:8084) vs glm-5.3-flash(zhipu-cn 直连) 双臂对比。

动机: models.json 里 claude-haiku-4-5 路由为 prefer_local（local_model=local-default，
即 35B 主引擎）——本实验回答「haiku 档本地兜底能否下沉到 9B 辅助引擎」。

金标: test/fixtures/haiku_tier_cases.json
  - TC1-9: promptfoo 核心用例断言直译（布尔口径）
  - tagging/extraction/summary: 复用 knowledge_gate_9b.json + gate 模块 scorer
安全: 只打 :8084 辅助引擎与云端 API，不碰 :4000/:8081 —— 批期安全。
评分: 确定性（复用 knowledge_gate_9b 的 norm/scorer），无 LLM 裁判。

用法:
  python3 tools/haiku_tier_compare.py                # 全量 31 条双臂
  python3 tools/haiku_tier_compare.py --limit 2      # 冒烟
  python3 tools/haiku_tier_compare.py --arm 9b       # 只跑单臂（续跑用）
  python3 tools/haiku_tier_compare.py --arm flash
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
import knowledge_gate_9b as gate  # noqa: E402  复用 norm/score_* 确定性评分

FIXTURE = REPO_ROOT / "test" / "fixtures" / "haiku_tier_cases.json"
GATE_FIXTURE = REPO_ROOT / "test" / "fixtures" / "knowledge_gate_9b.json"
SECRET = REPO_ROOT / "configs" / "secret.local.conf"
REPORT_DIR = REPO_ROOT / "logs" / "haiku_tier"

B9B = "http://127.0.0.1:8084/v1"
MODEL_9B = "ornith-ai/Ornith-1.5-9B-MLX-4bit"
FLASH_BASE = "https://open.bigmodel.cn/api/anthropic"
FLASH_MODEL = "glm-5.3-flash"
TEMP = 0          # 确定性口径（promptfoo 原配 0.6，本实验统一 0）
# 4096: glm-flash 走 anthropic 协议时 thinking 块计入 max_tokens——1024 会在长题上
# 被思考耗尽致 text 空响应（2026-10-16 首跑 artifact，5 个 flash 失手全为此因）
MAX_TOKENS = 4096


def load_secret_key(name: str) -> str:
    for line in SECRET.read_text(encoding="utf-8").splitlines():
        m = re.match(rf'(?:export )?{name}="(.*)"\s*$', line.strip())
        if m:
            return m.group(1)
    return ""


# ---------------------------------------------------------------- clients

def call_9b(prompt: str, timeout: int = 120):
    payload = json.dumps({
        "model": MODEL_9B, "temperature": TEMP, "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        B9B.rstrip("/") + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    ms = int((time.time() - t0) * 1000)
    return body["choices"][0]["message"].get("content") or "", ms


def call_flash(prompt: str, key: str, timeout: int = 120):
    payload = json.dumps({
        "model": FLASH_MODEL, "max_tokens": MAX_TOKENS, "temperature": TEMP,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        FLASH_BASE.rstrip("/") + "/v1/messages", data=payload,
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    ms = int((time.time() - t0) * 1000)
    text = "".join(b.get("text", "") for b in body.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "text")
    return text, ms


# ---------------------------------------------------------------- scoring

def _assert_hit(a: dict, resp: str) -> bool:
    hay = resp if a.get("match") == "raw" else gate.norm(resp)
    def hit(v: str) -> bool:
        needle = v if a.get("match") == "raw" else gate.norm(v)
        return bool(needle) and needle in hay
    if a["type"] == "contains":
        return hit(a["value"])
    if a["type"] == "contains_any":
        return any(hit(v) for v in a["values"])
    if a["type"] == "not_contains":
        return not hit(a["value"])
    return False


def score_tc(resp: str, case: dict, g: dict):
    if not resp.strip():
        return False, {"error": "empty"}
    low = resp.lower()
    detail, ok = [], True
    for a in case.get("asserts", []):
        h = _assert_hit(a, resp)
        detail.append({"assert": a["type"], "target": a.get("value") or a.get("values"),
                       "match": a.get("match", "norm"), "hit": h})
        ok = ok and h
    min_len = case.get("min_len", g["min_len_default"])
    n = len(resp.strip())
    len_ok = n >= min_len and n <= g["max_len"]
    for bad in g.get("not_contains", []):
        if bad.lower() in low:
            ok = False
            detail.append({"assert": "not_contains", "target": bad, "hit": False})
    return ok and len_ok, {"asserts": detail, "chars": n,
                           "len_ok": len_ok, "min_len": min_len}


GATE_SCORERS = {"tagging": gate.score_tagging, "extraction": gate.score_extraction,
                "summary": gate.score_summary}


def build_cases(fx: dict, gate_fx: dict):
    cases = list(fx["tc_cases"])
    wanted = set(fx["gate_subset"])
    for c in gate_fx["cases"]:
        if c["cat"] in wanted:
            cases.append(c)
    return cases


def score(case: dict, resp: str, g: dict):
    if case["cat"] == "tc":
        return score_tc(resp, case, g)
    return GATE_SCORERS[case["cat"]](resp, case)


# ---------------------------------------------------------------- runner

def run_arm(arm: str, cases, key: str):
    caller = call_9b if arm == "9b" else (lambda p: call_flash(p, key))
    results = []
    for i, c in enumerate(cases, 1):
        try:
            resp, ms = caller(c["prompt"])
            ok, detail = score(c, resp, GLOBAL)
            err = None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            resp, ms, ok, detail, err = "", 0, False, {}, f"{type(e).__name__}: {e}"
        results.append({"id": c["id"], "cat": c["cat"], "pass": bool(ok),
                        "ms": ms, "chars": len(resp), "error": err,
                        "detail": detail, "resp": resp[:240]})
        mark = "✓" if ok else "✗"
        print(f"  [{i:2d}/{len(cases)}] {mark} {c['id']:<24} {c['cat']:<11} "
              f"{ms}ms  {'' if not err else err[:80]}")
    return results


def stats(results):
    ms_list = sorted(r["ms"] for r in results if r["ms"] > 0)
    if not ms_list:
        return {"n": 0}
    def pct(p):
        k = max(0, min(len(ms_list) - 1, int(round(p * (len(ms_list) - 1)))))
        return ms_list[k]
    return {"n": len(ms_list), "pass_rate": round(sum(r["pass"] for r in results) / len(results), 3),
            "mean_ms": sum(ms_list) // len(ms_list), "p50_ms": pct(0.5), "p95_ms": pct(0.95)}


GLOBAL = {}


def main():
    global GLOBAL
    ap = argparse.ArgumentParser(description="haiku 档分流实测（9B vs glm-flash）")
    ap.add_argument("--fixture", default=str(FIXTURE))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--arm", choices=["9b", "flash"], help="只跑单臂")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    fx = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    gate_fx = json.loads(GATE_FIXTURE.read_text(encoding="utf-8"))
    GLOBAL = fx["global_asserts"]
    cases = build_cases(fx, gate_fx)
    if args.limit:
        cases = cases[:args.limit]

    key = load_secret_key("BIGMODEL_API_TOKEN")
    if not key:
        print("!! secret.local.conf 缺 BIGMODEL_API_TOKEN，flash 臂不可用")
        return 2

    print(f"=== haiku 档分流实测 | {len(cases)} cases × 2 臂 | 9b=:8084 flash={FLASH_MODEL} ===")
    out = {"ts": datetime.now().isoformat(), "fixture": args.fixture,
           "arms": {}, "cases": {c["id"]: {"cat": c["cat"]} for c in cases}}

    arms = [args.arm] if args.arm else ["9b", "flash"]
    all_results = {}
    for arm in arms:
        print(f"\n--- 臂: {arm} ---")
        rs = run_arm(arm, cases, key)
        all_results[arm] = rs
        out["arms"][arm] = stats(rs)
        for r in rs:
            out["cases"].setdefault(r["id"], {"cat": r["cat"]})[arm] = r

    # ---- 汇总
    print("\n=== 汇总 ===")
    subsets = {}
    for arm, rs in all_results.items():
        print(f"  [{arm}] 总体 {sum(r['pass'] for r in rs)}/{len(rs)}" +
              (f"  mean {out['arms'][arm].get('mean_ms')}ms  "
               f"p50 {out['arms'][arm].get('p50_ms')}ms  "
               f"p95 {out['arms'][arm].get('p95_ms')}ms"
               if out["arms"][arm].get("n") else "  (全失败)"))
        for cat in ("tc", "tagging", "extraction", "summary"):
            sub = [r for r in rs if r["cat"] == cat]
            if sub:
                subsets.setdefault(cat, {})[arm] = f"{sum(r['pass'] for r in sub)}/{len(sub)}"
    for cat, by_arm in sorted(subsets.items()):
        print(f"  {cat:<11} 9b={by_arm.get('9b', '-')}  flash={by_arm.get('flash', '-')}")

    if not args.no_save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        f = REPORT_DIR / f"compare-{datetime.now():%Y%m%d-%H%M%S}.json"
        f.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"报告: {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
