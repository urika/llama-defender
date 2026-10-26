#!/usr/bin/env bash
# ============================================================
# startup_stack.sh — 开机自启包装（launchd RunAtLoad 调用）
#
# 背景（2026-10-27）：本机无 llama-defender 的 launchd 自启项 → 系统重启后
# 代理（:4000）与 Splash 引擎（:8081）不会自动恢复，需人工 `./manage.sh start`。
# 本包装把「起栈＋起监控守护」固化，供 ~/Library/LaunchAgents/
# com.llama-defender.stack.plist 在登录时调用。
#
# 行为：
#   1) 等待系统登录后内存回落（sleep 20s）——刚重启时 Metal 分配易失败
#   2) ./manage.sh start（起引擎＋代理＋CONFIG_AUTOSTART_AUX 辅助引擎）
#   3) 起服务健康监控守护（tools/monitor_service.sh daemon 300）
#   4) 写日志 logs/startup-stack.log
#
# 幂等：manage.sh start 本身幂等（引擎已在跑则跳过）；监控守护自带 PID 文件互斥。
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG="logs/startup-stack.log"

# launchd 登录环境的 PATH 不含 Homebrew（实测 reboot 后 manage.sh start 报
# 「未找到 splash 命令」）——显式补齐，否则引擎起不来。
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export HOME="${HOME:-/Users/jinsongwang}"

mkdir -p logs
log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

log "=== 开机自启开始 ==="
sleep 20

# 1) 起栈
./manage.sh start >> "$LOG" 2>&1
rc=$?
log "manage.sh start rc=$rc"

# 2) 监控守护交给独立 launchd 作业（com.llama-defender.monitor，KeepAlive=true）
#    ——不可在此处 nohup 起：launchd 在本作业退出时会连坐杀掉作业组内的子进程
#    （2026-10-27 reboot 实测：守护随 wrapper 退出被 SIGTERM，PID 2726 即死）。
if [[ -f logs/service-monitor.pid ]] && kill -0 "$(cat logs/service-monitor.pid 2>/dev/null)" 2>/dev/null; then
    log "监控守护已在运行 (PID $(cat logs/service-monitor.pid))"
else
    log "监控守护由 launchd 作业 com.llama-defender.monitor 托管（此处不重复拉起）"
fi

# 3) 状态快照
{
    echo "--- 状态 ---"
    ./manage.sh status 2>&1 | head -30
    echo "--- swap ---"
    sysctl -n vm.swapusage 2>/dev/null || true
} >> "$LOG" 2>&1

log "=== 开机自启结束 ==="
