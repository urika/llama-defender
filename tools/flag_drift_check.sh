#!/usr/bin/env bash
# ============================================================
# flag_drift_check.sh — conf 声明的旗标 vs 运行代理实际旗标
#
# 用途：抓「conf 改了但进程没生效」这类事故（2026-10-22 br4 录制失明、
#       10-29 ctx_recall 死环 的共同根因）。capability_audit 只看 conf，
#       覆盖不到运行面；本脚本直接比对 `ps -wwE -p <代理 pid>`。
#
# 用法:
#   tools/flag_drift_check.sh            # 人读表格；有漂移 exit 1
#   tools/flag_drift_check.sh --json     # JSON（供监控/编排消费）
#   tools/flag_drift_check.sh --quiet    # 仅在有漂移时输出
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="text"
case "${1:-}" in
  --json)  MODE="json";;
  --quiet) MODE="quiet";;
  -h|--help) sed -n '2,16p' "$0"; exit 0;;
esac

PID="$(pgrep -f anthropic_proxy.py | head -1 || true)"
if [[ -z "$PID" ]]; then
  [[ "$MODE" == "json" ]] && echo '{"ok":false,"reason":"proxy_not_running","drift":[]}' \
                          || echo "[flag-drift] 代理未运行" >&2
  exit 2
fi

CONF="configs/$(readlink configs/active.conf 2>/dev/null || echo active.conf)"

PID="$PID" CONF="$CONF" MODE="$MODE" ROOT="$ROOT" python3 - <<'PY'
import json, os, subprocess, sys

sys.path.insert(0, os.path.join(os.environ["ROOT"], "tools"))
sys.path.insert(0, os.environ["ROOT"])
import flag_drift

pid = os.environ["PID"]
conf_path = os.environ["CONF"]
mode = os.environ["MODE"]

ps_out = subprocess.run(["ps", "-wwE", "-p", pid],
                        capture_output=True, text=True).stdout
proc = flag_drift.parse_process_env(ps_out)

import proxy_state as _ps
conf = _ps._parse_conf_env(conf_path)

reloadable = {s[0] for s in _ps._RELOAD_SPEC}
drift = flag_drift.diff(conf, proc, reloadable)

if mode == "json":
    print(json.dumps({"ok": not drift, "proxy_pid": int(pid),
                      "conf": conf_path, "drift": drift,
                      "summary": flag_drift.summarize(drift)}, ensure_ascii=False))
    sys.exit(0 if not drift else 1)

if not drift:
    if mode != "quiet":
        print(f"[flag-drift] ✅ 无漂移（代理 pid={pid}，conf={conf_path}，"
              f"比对了 {len(conf)} 个显式声明键）")
    sys.exit(0)

if mode != "quiet":
    print(f"[flag-drift] ⚠ conf 与运行进程漂移（代理 pid={pid}，conf={conf_path}）")
    print(f"  {'旗标':<46} {'conf':<12} {'进程':<20} 修复")
    for d in drift:
        print(f"  {d['key']:<46} {d['conf']:<12} {d['proc']:<20} "
              f"{'./manage.sh reload' if d['fix']=='reload' else '需重启代理'}")
else:
    print(flag_drift.summarize(drift))
sys.exit(1)
PY
