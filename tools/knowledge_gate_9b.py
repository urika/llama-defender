#!/usr/bin/env python3
"""9B 知识服务质量门（knowledge quality gate）。

金标集: test/fixtures/knowledge_gate_9b.json（32 条 / 5 类）
执行:   直连 aux 引擎 :8084（OpenAI chat 格式），不经过 :4000 主代理 —— 批期安全。
评分:   纯确定性（stdlib only），无 LLM 裁判，可复现。

类别与判分:
  extraction  结构化抽取 → 字段全部命中（gold ⊆ model 或 model ⊆ gold，模糊匹配抗单位/标点）
  tagging     分类打标   → 输出标签 == 金标（精确）
  summary     摘要       → 关键点组覆盖 ≥ 2/3 且不超过字数上限
  grounding   RAG 问答   → 必含答案词组全中；refuse 题须出现拒答措辞（防幻觉红线，
                           unanswerable 子集任一失手 = 整门 FAIL）
  refine      多轮精炼   → 第 2 轮约束检查（表格/字数/编号列表/内容全含），第 1 轮不计分

通过线（fixture.thresholds）:
  各类达标 + unanswerable=1.0 + overall ≥ 0.75 → exit 0，否则 exit 1。

用法:
  python3 tools/knowledge_gate_9b.py                 # 全量
  python3 tools/knowledge_gate_9b.py --case A01      # 单条
  python3 tools/knowledge_gate_9b.py --limit 3       # 冒烟
  python3 tools/knowledge_gate_9b.py --base http://127.0.0.1:8084/v1
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
DEFAULT_FIXTURE = REPO_ROOT / "test" / "fixtures" / "knowledge_gate_9b.json"
DEFAULT_BASE = "http://127.0.0.1:8084/v1"
DEFAULT_MODEL = "ornith-ai/Ornith-1.5-9B-MLX-4bit"
REPORT_DIR = REPO_ROOT / "logs" / "knowledge_gate"

REFUSAL_PATTERNS = [
    "没有提到", "未提及", "没有提及", "未提到", "不知道", "资料中没有",
    "文中没有", "未提供", "无法回答", "无相关",
    "not mention", "no information", "cannot answer", "unable to",
    "doesn't mention", "does not mention", "no such", "not provided",
    "not specified", "no record",
]

# ---------------------------------------------------------------- scoring utils

def norm(s: str) -> str:
    """归一化：小写、去空白、去标点（保留 unicode 字母/数字）——中英混排通用。"""
    s = str(s).lower().strip()
    s = re.sub(r"```[a-zA-Z]*", "", s)
    return re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)


def _hit(gold: str, text_n: str) -> bool:
    g = norm(gold)
    return bool(g) and (g in text_n or text_n in g and len(text_n) >= 2)


def extract_json(text: str):
    """从模型输出中尽力取出第一个 JSON 对象。"""
    t = re.sub(r"```[a-zA-Z]*", "", text).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def score_extraction(resp: str, case: dict):
    obj = extract_json(resp)
    if not isinstance(obj, dict):
        return False, {"error": "no_json", "raw": resp[:200]}
    fields = case["fields"]
    detail, ok_all, hits = {}, True, 0
    for k, gold in fields.items():
        golds = gold if isinstance(gold, list) else [gold]
        got = obj.get(k)
        g_n = [norm(g) for g in golds]
        text_n = norm(got) if got is not None else ""
        hit = any((gn and (gn in text_n or (text_n and text_n in gn)))
                  for gn in g_n)
        detail[k] = {"gold": gold, "got": got, "hit": hit}
        hits += int(hit)
        ok_all = ok_all and hit
    detail["_field_acc"] = round(hits / len(fields), 3)
    return ok_all, detail


def score_tagging(resp: str, case: dict):
    labels = case["labels"]
    gold = case["answer"]
    out_n = norm(resp)
    matched = [l for l in labels if norm(l) and norm(l) in out_n]
    hit = norm(gold) in [norm(m) for m in matched] if matched else (norm(gold) == out_n)
    detail = {"gold": gold, "matched_labels": matched, "raw": resp[:120]}
    return bool(hit), detail


def score_summary(resp: str, case: dict):
    r_n = norm(resp)
    groups = case["points"]
    hits, detail = 0, []
    for alts in groups:
        h = any(norm(a) in r_n for a in alts if norm(a))
        hits += int(h)
        detail.append({"alts": alts, "hit": h})
    cov = hits / len(groups)
    n_chars = len(resp.strip())
    len_ok = n_chars <= case["max_chars"]
    ok = cov >= 0.667 and len_ok
    return ok, {"coverage": round(cov, 3), "hits": f"{hits}/{len(groups)}",
                "chars": n_chars, "max_chars": case["max_chars"], "len_ok": len_ok,
                "points": detail}


def score_grounding(resp: str, case: dict):
    r_n = norm(resp)
    if case.get("refuse"):
        low = resp.lower()
        hit = any(p.lower() in low for p in REFUSAL_PATTERNS)
        return hit, {"expect": "refusal", "got_refusal": hit, "raw": resp[:200]}
    detail, ok_all = [], True
    for alts in case["must"]:
        h = any(norm(a) in r_n for a in alts if norm(a))
        detail.append({"alts": alts, "hit": h})
        ok_all = ok_all and h
    return ok_all, {"expect": "answer", "must": detail, "raw": resp[:200]}


def score_refine(resp: str, case: dict):
    chk = case["check"]
    t = chk["type"]
    if t == "markdown_table":
        rows = [l for l in resp.splitlines() if l.count("|") >= 2]
        ok = len(rows) >= chk["min_rows"] + 1  # +表头
        return ok, {"type": t, "pipe_rows": len(rows), "need": chk["min_rows"] + 1}
    if t == "max_chars":
        n = len(resp.strip())
        return n <= chk["max"], {"type": t, "chars": n, "max": chk["max"]}
    if t == "numbered_list":
        items = [l for l in resp.splitlines()
                 if re.match(r"^\s*(\d+[.、)]|[-•*])", l)]
        return len(items) >= chk["min_items"], \
            {"type": t, "items": len(items), "need": chk["min_items"]}
    if t == "contains_all":
        r_n = norm(resp)
        detail = []
        ok = True
        for it in chk["items"]:
            h = norm(it) in r_n
            detail.append({"item": it, "hit": h})
            ok = ok and h
        return ok, {"type": t, "items": detail}
    return False, {"error": f"unknown check {t}"}


SCORERS = {
    "extraction": score_extraction,
    "tagging": score_tagging,
    "summary": score_summary,
    "grounding": score_grounding,
    "refine": score_refine,
}

# ---------------------------------------------------------------- llm client

def chat(base: str, model: str, messages: list, timeout: int = 90):
    payload = json.dumps({
        "model": model, "messages": messages,
        "temperature": 0, "max_tokens": 768,
    }).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    ms = int((time.time() - t0) * 1000)
    content = body["choices"][0]["message"]["content"] or ""
    return content, ms

# ---------------------------------------------------------------- runner

def run_case(base, model, case):
    """返回 (pass, detail, calls, total_ms)。多轮: 第1轮仅作上下文，第2轮计分。"""
    scorer = SCORERS[case["cat"]]
    calls, total_ms, extra = [], 0, {}
    try:
        if case.get("multiturn"):
            r1, ms1 = chat(base, model, [{"role": "user", "content": case["turn1"]}])
            calls.append({"turn": 1, "ms": ms1, "resp": r1[:300]})
            total_ms += ms1
            msgs = [{"role": "user", "content": case["turn1"]},
                    {"role": "assistant", "content": r1},
                    {"role": "user", "content": case["prompt"]}]
        else:
            msgs = [{"role": "user", "content": case["prompt"]}]
        resp, ms2 = chat(base, model, msgs)
        calls.append({"turn": len(calls) + 1, "ms": ms2, "resp": resp[:300]})
        total_ms += ms2
        ok, detail = scorer(resp, case)
        extra["response"] = resp[:600]
        return ok, {**detail, **extra}, calls, total_ms
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, OSError) as e:
        return False, {"error": f"{type(e).__name__}: {e}"}, calls, total_ms


def main():
    ap = argparse.ArgumentParser(description="9B 知识服务质量门")
    ap.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--case", help="只跑指定 id")
    ap.add_argument("--limit", type=int, help="只跑前 N 条（冒烟）")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    fx = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    cases = fx["cases"]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"未找到 case {args.case}")
            return 2
    if args.limit:
        cases = cases[:args.limit]

    thresholds = fx["thresholds"]
    results = []
    print(f"=== 9B 知识服务质量门 | {len(cases)} cases | {args.base} ===")
    for i, c in enumerate(cases, 1):
        ok, detail, calls, ms = run_case(args.base, args.model, c)
        results.append({"id": c["id"], "cat": c["cat"], "pass": ok,
                        "ms": ms, "calls": len(calls), "detail": detail})
        mark = "✓" if ok else "✗"
        print(f"[{i:2d}/{len(cases)}] {mark} {c['id']:<26} {c['cat']:<11} {ms}ms")
        if not ok:
            brief = json.dumps(detail, ensure_ascii=False)[:220]
            print(f"          → {brief}")

    # -------- 汇总与判定
    cats = {}
    for r in results:
        cats.setdefault(r["cat"], []).append(r["pass"])
    n_all = len(results)
    n_pass = sum(r["pass"] for r in results)
    print("\n=== 类别汇总 ===")
    verdicts = {}
    for cat, passes in sorted(cats.items()):
        rate = sum(passes) / len(passes)
        thr = thresholds.get(cat, 0)
        good = rate + 1e-9 >= thr
        verdicts[cat] = good
        print(f"  {cat:<12} {sum(passes)}/{len(passes)} = {rate:.1%}  线 {thr:.1%}  "
              f"{'✓' if good else '✗'}")
    # 防幻觉红线：unanswerable 子集（grounding refuse 题）
    unans = [r for r in results
             if r["cat"] == "grounding" and r["detail"].get("expect") == "refusal"]
    unans_ok = bool(unans) and all(r["pass"] for r in unans)
    print(f"  unanswerable {sum(r['pass'] for r in unans)}/{len(unans)}  "
          f"线 {thresholds['unanswerable']:.0%}  {'✓' if unans_ok else '✗ ← 防幻觉红线'}")

    overall = n_pass / n_all
    overall_ok = overall + 1e-9 >= thresholds["overall"]
    all_cats_ok = all(verdicts.values())
    passed = all_cats_ok and unans_ok and overall_ok
    verdict = "PASS" if passed else "FAIL"
    print(f"\n=== 门判词: {verdict}  (overall {n_pass}/{n_all} = {overall:.1%}, "
          f"线 {thresholds['overall']:.1%}) ===")
    if not passed:
        reasons = [c for c, v in verdicts.items() if not v]
        if reasons:
            print(f"  未达标类别: {', '.join(reasons)}")
        if not unans_ok:
            print("  防幻觉红线失守（unanswerable 必须 100%）")
        if not overall_ok:
            print("  overall 未达标")

    if not args.no_save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORT_DIR / f"gate-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps({
            "ts": datetime.now().isoformat(), "verdict": verdict,
            "overall": overall, "cat_rates": {
                c: sum(p) / len(p) for c, p in cats.items()},
            "thresholds": thresholds, "base": args.base, "model": args.model,
            "fixture": args.fixture, "results": results,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"报告: {out}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
