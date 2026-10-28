#!/usr/bin/env bash
# ============================================================
# post_reboot_check.sh — 系统重启后的一次性验收脚本
#
# 检查项（逐项打勾，任一 FAIL 即红）：
#   1) swap 是否清零（重启的收益就在这里）
#   2) 代理 :4000 / 引擎 :8081 是否起来（launchd 自启项 com.llama-defender.stack）
#   3) 辅助引擎 embedgemma2 :8083
#   4) fnf 四旗标在 conf 中的状态（应全 true —— 激活戳记 2026-10-27 00:56）
#   5) br4 录制面（PROXY_REQUEST_ARCHIVE_ENABLED）
#   6) 监控守护是否在跑
#   7) capability_audit（对端仪器，须 exit 0）
#   8) 引擎 metrics 关键指标（pressure / denied / decode）
#
# 用法：bash tools/post_reboot_check.sh
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
FAIL=0

ok()   { printf '  ✅ %s\n' "$*"; }
bad()  { printf '  ❌ %s\n' "$*"; FAIL=1; }
info() { printf '  ·  %s\n' "$*"; }

echo "=== 重启后验收 $(date '+%Y-%m-%d %H:%M:%S') ==="

echo "[1] swap"
SWAP="$(sysctl -n vm.swapusage 2>/dev/null || echo 'unavailable')"
info "$SWAP"
USED="$(echo "$SWAP" | grep -oE 'used = [0-9.]+[MG]' | grep -oE '[0-9.]+' || echo 99999)"
if [[ "$USED" == "99999" ]]; then
    info "无法解析 swap（跳过）"
elif (( $(echo "$USED < 100" | bc -l 2>/dev/null || echo 0) )); then
    ok "swap 已清零（${USED}M）"
else
    bad "swap 仍 ${USED}M —— 重启未清 swap 或系统又长回去了"
fi

echo "[2] 代理 :4000 / 引擎 :8081"
if curl -s --max-time 5 http://127.0.0.1:4000/api/status >/dev/null 2>&1; then
    ok "代理 :4000 存活（$(curl -s --max-time 5 http://127.0.0.1:4000/api/status | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("proxy",{}).get("pid"),d.get("state"))' 2>/dev/null)）"
else
    bad "代理 :4000 无响应 —— 查 logs/startup-stack.log 与 logs/anthropic_proxy.log"
fi
if curl -s --max-time 5 http://127.0.0.1:8081/metrics >/dev/null 2>&1; then
    ok "引擎 :8081 存活"
else
    bad "引擎 :8081 无响应 —— 查 logs/llama-server.log（Metal 分配失败看 'not enough free memory'）"
fi

echo "[3] 辅助引擎 :8083 (embedgemma2)"
if curl -s --max-time 5 http://127.0.0.1:8083/v1/models >/dev/null 2>&1; then
    ok "embedgemma2 :8083 存活"
else
    bad "embedgemma2 :8083 无响应 —— ./manage.sh start-aux embedgemma2"
fi

echo "[4] fnf 四旗标（激活 conf）"
CONF_FILE="configs/$(readlink configs/active.conf 2>/dev/null || echo active.conf)"
info "激活配置：$CONF_FILE"
for k in PROXY_FNF_GUIDE_ENABLED PROXY_FNF_CANDIDATES_ENABLED PROXY_LOOP_ESCALATION_MEMORY_ENABLED PROXY_LOOP_BASH_PATH_KEYS_ENABLED; do
    v="$(grep -E "^export ${k}=" "$CONF_FILE" 2>/dev/null | tail -1 | sed 's/^export //; s/"//g')"
    if [[ "$v" == *true* ]]; then ok "$k=$v"; else bad "$k=${v:-未设置}（应为 true —— 激活戳记 00:56）"; fi
done

echo "[5] br4 录制面"
v="$(grep -E "^export PROXY_REQUEST_ARCHIVE_ENABLED=" "$CONF_FILE" 2>/dev/null | tail -1 | sed 's/^export //; s/"//g')"
if [[ "$v" == *true* ]]; then ok "PROXY_REQUEST_ARCHIVE_ENABLED=$v"; else bad "PROXY_REQUEST_ARCHIVE_ENABLED=${v:-未设置}"; fi
N="$(find logs/diag/archive-req -newermt '-10 minutes' -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')"
info "近 10 分钟归档文件数：$N"

echo "[6] 监控守护"
if [[ -f logs/service-monitor.pid ]] && kill -0 "$(cat logs/service-monitor.pid 2>/dev/null)" 2>/dev/null; then
    ok "监控守护在跑（PID $(cat logs/service-monitor.pid)）"
else
    bad "监控守护未运行 —— nohup ./tools/monitor_service.sh daemon 300 &"
fi

echo "[6b] 守栈作业（launchd com.llama-defender.stack）"
# 关键：launchd 作业主进程（wrapper）必须**常驻**——它退出时 launchd 回收整个
# 作业进程组，会连坐杀掉引擎/代理/辅助引擎（DEF-324）。
if pgrep -f "tools/startup_stack.sh" >/dev/null 2>&1; then
    ok "守栈 wrapper 在跑（PID $(pgrep -f 'tools/startup_stack.sh' | head -1)）"
else
    bad "守栈 wrapper 不在 —— launchd 作业会连坐杀掉栈（DEF-324）；重新装载：launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.llama-defender.stack.plist"
fi

echo "[7] capability_audit（对端仪器）"
AUDIT="../swe-eval/scripts/capability_audit.py"
if [[ -f "$AUDIT" ]]; then
    out="$(python3 "$AUDIT" 2>&1)"; rc=$?
    echo "$out" | sed 's/^/    /' | tail -12
    if [[ $rc -eq 0 ]]; then ok "capability_audit exit 0"; else bad "capability_audit exit $rc —— must-have ABSENT 时不得开批"; fi
else
    info "未找到 $AUDIT（对端仓），跳过"
fi

echo "[8] 引擎关键指标"
if curl -s --max-time 5 http://127.0.0.1:8081/metrics >/dev/null 2>&1; then
    curl -s --max-time 5 http://127.0.0.1:8081/metrics | grep -E "splash_memory_pressure|splash_decode_tokens_per_second|splash_requests_(failed|completed)" | sed 's/^/    /'
fi

echo
if [[ $FAIL -eq 0 ]]; then
    echo "✅ 全部通过"
else
    echo "❌ 存在 FAIL 项（见上）"
fi
exit $FAIL
