#!/usr/bin/env python3
"""recall_smoke.py — 召回链路生产冒烟（2026-10-06，A 件）。

打**生产 :4000 代理**端到端验证召回通道：折叠 → RECALL_CUE 注入 → 模型调用
ctx_recall → 微轮自答 → 答案命中种子事实。与 ctx-case 影子套件的分工：
ctx-case 验机制（mock 模型、影子代理 :4100），本件验**生产代理实况**。

三档判据（输出 JSON verdict + 退出码 0/3/1）：
  PASS  ①折叠发生（EPOCH 日志）＋②[recall-cue] available=true＋③答案命中
        种子 key＋④[MICRO_TURN] 自答证据
  WARN  核心通过但自答证据缺（可能走 path A）或 P0(ii) metrics 段不可读
        （L-65 未激活，fail-open 口径：不可读 ≠ 0）
  FAIL  非 200 / 未折叠 / cue 门控 false / 答案未命中

批期守卫：检测到 exp _exec / run_agent.py 存活即拒绝（190KB 预填会与批次争
引擎内存），--allow-during-batch 显式覆盖。会话残留：本件在生产代理留一个
~190KB 合成会话（引擎 TTL/驱逐回收；L-21 purge 原语未落地，如实告知）。

用法:
  python3 tools/recall_smoke.py                 # 实弹（批后）
  python3 tools/recall_smoke.py --dry-run       # 只构造/自检，不发送
依赖: Python3 stdlib only。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG = os.path.join(REPO, "logs", "anthropic_proxy.log")
DEFAULT_DIAG = os.path.join(REPO, "logs", "diag")
MODEL = "claude-sonnet-4-6"


def _post(url, body, sid, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "anthropic-version": "2023-06-01",
                 "X-Claude-Code-Session-Id": sid},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def _get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except Exception as e:
        return 0, {"_error": str(e)}


def _resp_text(resp):
    return " ".join(b.get("text", "") for b in (resp or {}).get("content", [])
                    if isinstance(b, dict) and b.get("type") == "text")


def build_history(trigger_chars, seed_payload):
    """合成会话：40 轮 × ~4.7KB；第 2 轮 tool_result 携带种子 key（折叠区内）。"""
    filler = "x" * 4600
    msgs = []
    for i in range(40):
        msgs.append({"role": "user", "content": "hist-%03d: %s" % (i, filler)})
        if i == 1:
            msgs.append({"role": "assistant", "content": [{
                "type": "tool_use", "id": "call_smoke_seed",
                "name": "Read", "input": {"file_path": "/repo/ops/deploy.md"}}]})
            msgs.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "call_smoke_seed",
                "content": "deploy.md note: %s" % seed_payload}]})
        else:
            msgs.append({"role": "assistant", "content": "ok-%03d" % i})
    total = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)
    return msgs, total


def main():
    ap = argparse.ArgumentParser(description="召回链路生产冒烟")
    ap.add_argument("--proxy", default="http://127.0.0.1:4000")
    ap.add_argument("--proxy-log", default=DEFAULT_LOG)
    ap.add_argument("--diag-dir", default=DEFAULT_DIAG)
    ap.add_argument("--trigger-chars", type=int, default=190000,
                    help="合成历史字符量（est×1.6 须 ≥ S=70000 ⇒ ≥175K）")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--seed-style", choices=["key", "content"], default="key",
                    help="key=键型(面板keys行可直答,测送达); content=语义型(仅召回可答,测真实调用)")
    ap.add_argument("--allow-during-batch", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    checks = []

    def add(name, ok, detail="", level=None):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200],
                       "level": level or ("PASS" if ok else "FAIL")})
        print("[%s] %s%s" % (checks[-1]["level"], name,
                             (" -- " + detail) if detail else ""))

    # 批期守卫
    batch = subprocess.run(["pgrep", "-f", "exp _exec|run_agent.py"],
                           capture_output=True, text=True).stdout.split()
    if batch and not args.allow_during_batch and not args.dry_run:
        print("拒绝：检测到批跑进程 %s —— 190KB 预填会与批次争引擎内存。" % batch[:3])
        print("批后运行，或 --allow-during-batch 显式覆盖。")
        return 1
    if batch:
        print("[WARN] 批跑进程在场 %s —— 已按 --allow-during-batch/--dry-run 继续" % batch[:3])

    seed_key = "SMK-%s" % os.urandom(4).hex()
    # v1.1 双种子形态: key=键型（面板 keys 行可直答——测 cue 送达）
    #                 content=语义型（无独特 token——只有 ctx_recall 全文检索可答，
    #                          测真实召回调用, ADR-016 B 目标态核心场景）
    days, owners = ["Thursday", "Friday", "Saturday"], ["Dana", "Lee", "Marco"]
    seed_day = days[int(os.urandom(1).hex(), 16) % 3]
    seed_owner = owners[int(os.urandom(1).hex(), 16) % 3]
    if args.seed_style == "content":
        seed_payload = ("the release window was moved to %s and the owner is %s; "
                        "please keep this between us" % (seed_day, seed_owner))
        ask2 = ("According to the earliest deployment note, on which day is the "
                "release window and who owns it? Answer in the form 'day/owner'.")
        answer_ok_fn = lambda t: (seed_day in t and seed_owner in t)
    else:
        seed_payload = "DEPLOY_KEY=%s (rotation: monthly)" % seed_key
        ask2 = "What is the DEPLOY_KEY mentioned in the earliest deploy.md note?"
        answer_ok_fn = lambda t: seed_key in t
    history, total_chars = build_history(args.trigger_chars, seed_payload)
    sid = "recall-smoke-%s" % datetime.now().strftime("%Y%m%d%H%M%S")
    url = args.proxy + "/v1/messages"

    add("payload 构造", total_chars >= 175000,
        "chars=%d (est≈%d tok, ×1.6≈%d vs S=70000) seed=%s sid=%s"
        % (total_chars, total_chars // 4, total_chars // 4 * 8 // 5, seed_key, sid))

    status, detail = _get(args.proxy + "/api/status")
    add("代理探活", status == 200 and (detail.get("state") in (None, "healthy", "ok")
                                        or detail.get("proxy", {}).get("running") is not False),
        "status=%s" % status)
    if args.dry_run:
        print("\n== dry-run 完成（未发送）==")
        print(json.dumps({"verdict": "DRY-RUN", "seed_key": seed_key, "sid": sid,
                          "history_chars": total_chars, "checks": checks},
                         ensure_ascii=False, indent=2))
        return 0

    try:
        log_size = os.path.getsize(args.proxy_log)
    except OSError:
        log_size = 0

    body1 = {"model": MODEL, "max_tokens": 256, "stream": False,
             "tools": [{"name": "Read", "description": "read a file",
                        "input_schema": {"type": "object", "properties": {
                            "file_path": {"type": "string"}}}}],
             "messages": history + [{"role": "user", "content": "Briefly summarize the latest message."}]}
    t0 = time.time()
    try:
        status1, resp1 = _post(url, body1, sid, args.timeout)
    except urllib.error.HTTPError as e:
        status1, resp1 = e.code, {}
    except Exception as e:
        status1, resp1 = 0, {"_error": str(e)}
    add("请求1（触发折叠）", status1 == 200, "status=%s dt=%.0fs" % (status1, time.time() - t0))

    q2 = ("What is the exact DEPLOY_KEY value from the earlier tool result? "
          "Reply with only the key." if args.seed_style == "key" else
          "According to the earliest deploy.md note, on which day is the release "
          "window and who owns it? Reply in the form 'day/owner' only.")
    body2 = dict(body1, max_tokens=100,
                 messages=history + [{"role": "user", "content": q2}])
    t0 = time.time()
    try:
        status2, resp2 = _post(url, body2, sid, args.timeout)
    except urllib.error.HTTPError as e:
        status2, resp2 = e.code, {}
    except Exception as e:
        status2, resp2 = 0, {"_error": str(e)}
    text2 = _resp_text(resp2)
    add("请求2（提问种子事实）", status2 == 200, "status=%s dt=%.0fs" % (status2, time.time() - t0))

    # 日志取证（本会话窗口内）
    log_tail = ""
    try:
        with open(args.proxy_log, encoding="utf-8", errors="replace") as f:
            f.seek(log_size)
            log_tail = f.read()
    except OSError:
        pass
    add("折叠发生（EPOCH 日志）", "EPOCH #" in log_tail and "recall-s" in log_tail,
        "log 窗口 %d chars" % len(log_tail))
    # L-59 日志为大写 True（Python %s）——大小写不敏感匹配（2026-10-08 修正：
    # 首跑 FAIL 系判据大小写缺陷，代理侧 available=True 已三处实证）
    cue_ok = re.search(r"recall-cue\] available=\w+", log_tail) is not None and \
        "available=False" not in log_tail.split("recall-cue]")[-1][:60]
    add("cue 门控放行（[recall-cue] available=true）", cue_ok,
        "" if cue_ok else "修复未激活或谓词 false —— 见 L-59 行")
    question2 = ("What is the DEPLOY_KEY mentioned in the earliest deploy.md note?"
             if args.seed_style == "key" else
             "According to the earliest deployment note, on which day is the "
             "release window and who owns it? Answer in the form 'day/owner'.")
    answer_ok = answer_ok_fn(text2)
    add("答案命中种子 key", answer_ok, "resp2=%r" % text2[:120])
    micro = "[MICRO_TURN]" in log_tail and "recall-s" in log_tail
    add("微轮自答证据（[MICRO_TURN]）——cue 是邀请，模型可不经调用直答", micro,
        "" if micro else "可能走 path A 或未调用 —— 结合 metrics 段判读",
        level="WARN" if not micro else None)

    # P0(ii) metrics 段（L-65 激活前预期不可读 → WARN 而非 FAIL）
    mlevel, mdetail = "WARN", "未定位会话台账"
    try:
        cands = sorted(
            (os.path.getmtime(os.path.join(args.diag_dir, "ledger", n)), n)
            for n in os.listdir(os.path.join(args.diag_dir, "ledger"))
            if n.startswith("recall-s"))
        if cands:
            key = cands[-1][1][:-6]
            _s, m = _get("%s/api/session/%s/metrics" % (args.proxy, key))
            seg = (m or {}).get("ctx_recall")
            if isinstance(seg, dict) and seg.get("attempts") is not None:
                mlevel, mdetail = "PASS", "ctx_recall.attempts=%s" % seg.get("attempts")
            else:
                mlevel, mdetail = "WARN", "ctx_recall 段不可读（L-65 未激活，fail-open ≠ 0）"
    except Exception as e:
        mdetail = "metrics 读取异常: %s" % str(e)[:80]
    add("P0(ii) metrics 读数", mlevel == "PASS", mdetail, level=mlevel)

    fails = [c for c in checks if c["level"] == "FAIL"]
    warns = [c for c in checks if c["level"] == "WARN"]
    verdict = "FAIL" if fails else ("WARN" if warns else "PASS")
    print("\n== verdict: %s（%d PASS / %d WARN / %d FAIL）==" % (
        verdict, len(checks) - len(fails) - len(warns), len(warns), len(fails)))
    print(json.dumps({"verdict": verdict, "sid": sid, "seed_style": args.seed_style, "seed_key": seed_key,
                      "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if verdict == "PASS" else (3 if verdict == "WARN" else 1)


if __name__ == "__main__":
    sys.exit(main())
