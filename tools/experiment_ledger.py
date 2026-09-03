#!/usr/bin/env python3
"""experiment_ledger.py — 受控实验台账（append-only，手工进出场协议的纪律工具）。

设计依据: docs/03-experiments-testing/amnesia-experiment-protocol-20260830.md
台账是实验可考性的唯一登记处: 窗口起止(队列识别的时间层)、参数覆盖、
批次、结论指针——与每轮记录自带的 config 指纹(权威层)互为校验。
不做自动调度: 进出场靠 reload 手工协议,台账只负责"发生过什么"可考。

用法:
  python3 tools/experiment_ledger.py begin --id exp-1-amnesia \
      --hypothesis "ILE→熵恶化→结局下降" \
      --treatment keep_messages=12,hbe_min_chars=8000,hbe_sample_every=2 \
      --batch "syn-text-30 --force"
  python3 tools/experiment_ledger.py note  --id exp-1-amnesia --text "pilot 2 任务通过"
  python3 tools/experiment_ledger.py end   --id exp-1-amnesia \
      --conclusion "docs/04-analysis-diagnostics/amnesia-experiment-report-20260830.md E2 达标"
  python3 tools/experiment_ledger.py list  [--id exp-1-amnesia]

状态机: begin(同 id 已 running → 拒绝) → note* → end。
文件: logs/diag/experiments.jsonl(可用 EXPLEDGER_PATH 覆盖,测试用)。
"""
import argparse
import json
import os
import sys
from datetime import datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(_REPO, "logs", "diag", "experiments.jsonl")


def _rows(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except (FileNotFoundError, OSError):
        return []


def _append(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _running(rows, exp_id):
    """同 id 是否存在 begin 之后无 end 的窗口。"""
    open_ids = set()
    for r in rows:
        if r.get("exp_id") != exp_id:
            continue
        if r.get("event") == "begin":
            open_ids.add(exp_id)
        elif r.get("event") == "end":
            open_ids.discard(exp_id)
    return exp_id in open_ids


def cmd_begin(args, path):
    rows = _rows(path)
    if _running(rows, args.id):
        print("拒绝: %s 已有进行中的窗口(先 end 或换 id)" % args.id, file=sys.stderr)
        return 1
    treatment = {}
    for kv in (args.treatment or "").split(","):
        if "=" in kv:
            k, _, v = kv.partition("=")
            treatment[k.strip()] = v.strip()
    _append(path, {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": "begin", "exp_id": args.id, "status": "running",
        "hypothesis": args.hypothesis,
        "treatment": treatment,
        "batch": args.batch,
        "protocol": args.protocol,
    })
    print("OK begin %s | treatment=%s" % (args.id, treatment))
    print("  提醒: 确认三处参数已 reload 生效(RELOAD OK 日志)后才启动批跑")
    return 0


def cmd_note(args, path):
    _append(path, {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": "note", "exp_id": args.id, "text": args.text,
    })
    print("OK note %s" % args.id)
    return 0


def cmd_end(args, path):
    rows = _rows(path)
    if not _running(rows, args.id):
        print("拒绝: %s 无进行中的窗口" % args.id, file=sys.stderr)
        return 1
    _append(path, {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": "end", "exp_id": args.id, "status": "done",
        "conclusion": args.conclusion,
    })
    print("OK end %s" % args.id)
    print("  提醒: 确认三处参数已恢复并 reload,再开放常规流量")
    return 0


def cmd_list(args, path):
    rows = _rows(path)
    if args.id:
        rows = [r for r in rows if r.get("exp_id") == args.id]
    if not rows:
        print("(台账为空)")
        return 0
    for r in rows:
        head = "%s %-6s %s" % (r.get("ts", "?")[11:19], r.get("event"), r.get("exp_id"))
        if r.get("event") == "begin":
            print("%s | %s | treatment=%s | batch=%s" % (
                head, r.get("hypothesis"), r.get("treatment"), r.get("batch")))
        elif r.get("event") == "end":
            print("%s | 结论: %s" % (head, r.get("conclusion")))
        else:
            print("%s | %s" % (head, r.get("text")))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--path", default=os.environ.get("EXPLEDGER_PATH", DEFAULT_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("begin", parents=[common], help="开窗(登记假设/参数覆盖/批次)")
    b.add_argument("--id", required=True)
    b.add_argument("--hypothesis", required=True)
    b.add_argument("--treatment", default="", help="逗号分隔 k=v,如 keep_messages=12")
    b.add_argument("--batch", default="", help="批跑命令描述")
    b.add_argument("--protocol", default="", help="协议文档路径")

    n = sub.add_parser("note", parents=[common], help="追加备注(试点结果/中止原因等)")
    n.add_argument("--id", required=True)
    n.add_argument("--text", required=True)

    e = sub.add_parser("end", parents=[common], help="关窗(登记结论指针;提醒恢复参数)")
    e.add_argument("--id", required=True)
    e.add_argument("--conclusion", required=True)

    sub.add_parser("list", parents=[common], help="列出(可 --id 过滤)").add_argument("--id")

    args = p.parse_args(argv)
    return {"begin": cmd_begin, "note": cmd_note, "end": cmd_end,
            "list": cmd_list}[args.cmd](args, args.path)


if __name__ == "__main__":
    sys.exit(main())
