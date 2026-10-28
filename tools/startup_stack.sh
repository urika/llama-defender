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
#    ——不可在此处 nohup 起：launchd 在主作业退出时会连坐杀掉作业组内的子进程
#    （2026-10-27 reboot 实测：守护随 wrapper 退出被 SIGTERM，PID 2726 即死）。
if [[ -f logs/service-monitor.pid ]] && kill -0 "$(cat logs/service-monitor.pid 2>/dev/null)" 2>/dev/null; then
    log "监控守护已在运行 (PID $(cat logs/service-monitor.pid))"
else
    log "监控守护由 launchd 作业 com.llama-defender.monitor 托管（此处不重复拉起）"
fi

# 3) 起栈后状态快照
{
    echo "--- 状态 ---"
    ./manage.sh status 2>&1 | head -30
    echo "--- swap ---"
    sysctl -n vm.swapusage 2>/dev/null || true
} >> "$LOG" 2>&1

# 4) 常驻前台托住栈（DEF-324，2026-10-28）
#    launchd 在作业主进程退出时会回收**整个作业进程组**；而引擎/代理/辅助引擎
#    都是本 wrapper 的子进程 → wrapper 一退出它们就被连坐杀掉。
#    实测：10-28 22:56 自启成功（引擎 1645/代理 1743/eg2 1772），wrapper 22:57:18
#    退出 → 23:17 前栈已全灭（:4000/:8081/:8083 全无监听）。
#    故 wrapper 必须常驻：栈在则守；栈亡则区分「人工停机」与「异常退出」——
#    人工停机（manage.sh stop 有 service_stop 生命周期事件）→ exit 0（launchd 不再拉起）；
#    异常退出 → exit 1（配合 plist 的 KeepAlive={"SuccessfulExit":false} 由 launchd 重启）。
stop_count_at_start="$(grep -ac '"event": *"service_stop"' logs/lifecycle_events.jsonl 2>/dev/null || echo 0)"

_stack_proxy_alive() {
    local p
    p="$(cat anthropic_proxy.pid 2>/dev/null || true)"
    [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null
}

_stack_engine_alive() {
    lsof -Pi :8081 -sTCP:LISTEN -t >/dev/null 2>&1
}

_stop_count_now() {
    grep -ac '"event": *"service_stop"' logs/lifecycle_events.jsonl 2>/dev/null || echo 0
}

while true; do
    sleep 30
    if _stack_proxy_alive && _stack_engine_alive; then
        continue
    fi

    new_stop=0
    [[ "$(_stop_count_now)" -gt "$stop_count_at_start" ]] && new_stop=1

    if ! _stack_proxy_alive; then
        # 代理不在＝整栈停机：人工 stop → exit 0（不拉起）；异常 → exit 1（launchd 重启）
        if [[ "$new_stop" == "1" ]]; then
            log "整栈已停止（代理不在 + 人工 service_stop）→ 正常退出，不自动拉起"
            exit 0
        fi
        log "整栈异常退出（代理不在且无 service_stop）→ 非零退出，交 launchd 重启本作业"
        exit 1
    fi

    # 走到这里＝代理在、引擎不在。
    # 注意：`./manage.sh stop-backend`（如为释放内存/换模维护）只停引擎、代理照旧，
    # 生命周期事件 detail 带 component=backend——**这不是整栈停机**，
    # 不能据此退出（2026-10-29 01:21 实测误判：wrapper 退出后无人守栈）。
    if [[ "$new_stop" == "1" ]]; then
        log "引擎被人工停止（service_stop）→ 保持观察：不重启引擎、不退出守栈"
        stop_count_at_start="$(_stop_count_now)"   # 消费事件，避免重复判定
        continue
    fi
    log "引擎异常退出（代理在、无 service_stop 事件）→ 非零退出，交 launchd 重启本作业"
    exit 1
done

log "=== 开机自启结束 ==="
