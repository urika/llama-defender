#!/usr/bin/env bash
# ============================================================
# 服务健康监控：定期采集 Splash 指标，做趋势/阈值分析并给建议
#
# 用法:
#   tools/monitor_service.sh once              # 单次分析（默认输出人读摘要）
#   tools/monitor_service.sh once --json       # 单次分析（JSON）
#   tools/monitor_service.sh daemon [间隔秒]   # 守护循环（默认 300s）
#   tools/monitor_service.sh stop              # 停止守护
#
# 环境变量（均可默认）:
#   MONITOR_INTERVAL=300        采集间隔秒
#   MONITOR_AUTO_RESTART=0      1=批跑结束且已劣化时自动 restart（默认只告警）
#   MONITOR_DENIED_RATE=300     denied 每小时增量告警阈值
#   MONITOR_HEADROOM_G=8        headroom 告警阈值（GB）
#   MONITOR_TTFT_P95_S=60       ttft_p95 告警阈值（秒）
#
# 日志: logs/service-monitor.log（每周期一行摘要 + WARN/CRIT 行）
#       logs/service-monitor.latest.json（最近一次完整快照）
# 状态: logs/service-monitor.state（相邻两次差分用）
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOGFILE="logs/service-monitor.log"
LATEST="logs/service-monitor.latest.json"
STATE="logs/service-monitor.state"
PIDFILE="logs/service-monitor.pid"

INTERVAL="${MONITOR_INTERVAL:-300}"
AUTO_RESTART="${MONITOR_AUTO_RESTART:-0}"
DENIED_RATE="${MONITOR_DENIED_RATE:-300}"
HEADROOM_G="${MONITOR_HEADROOM_G:-8}"
TTFT_P95_S="${MONITOR_TTFT_P95_S:-60}"

now() { date '+%Y-%m-%d %H:%M:%S'; }

# ------------------------------------------------------------
# 采集：一次 curl 拿 /metrics + /status，python 解析为扁平 JSON
# ------------------------------------------------------------
collect() {
    curl -s --max-time 15 "http://127.0.0.1:8081/metrics" 2>/dev/null
}

analyze() {
    local metrics="$1"
    if [[ -z "$metrics" ]]; then
        echo '{"ts":"'"$(now)"'","level":"CRIT","service":"down","reason":"无法访问 :8081/metrics"}'
        return 1
    fi
    BATCH="no"
    pgrep -f "swe_test exp _exec|run_agent.py" >/dev/null 2>&1 && BATCH="yes"

    METRICS="$metrics" BATCH="$BATCH" python3 - <<'PY'
import json, os, sys

m = {}
for line in os.environ["METRICS"].splitlines():
    if line.startswith("splash_"):
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0][len("splash_"):]
            if "{" in key:  # 带标签的（memory_pressure）单独处理
                base, label = key.split("{", 1)
                if base == "memory_pressure":
                    # 必须转 float：字符串 "0" 是真值，会让 normal 态被误判成 critical
                    try: m["pressure_" + label.split('="')[1].split('"')[0]] = float(parts[1])
                    except ValueError: pass
                continue
            try: m[key] = float(parts[1])
            except ValueError: pass

BATCH = os.environ["BATCH"] == "yes"
g = lambda k, d=None: m.get(k, d)
out = {
    "ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
    "service": "up",
    "batch": BATCH,
    "memory_current_gb": round(g("memory_current_bytes", 0)/2**30, 2),
    "memory_headroom_gb": round(g("memory_headroom_bytes", 0)/2**30, 2),
    "memory_peak_gb": round(g("memory_peak_bytes", 0)/2**30, 2),
    "memory_denied": int(g("memory_denied_reservations_total", 0)),
    "kv_gb": round(g("kv_allocated_bytes", 0)/2**30, 2),
    "state_entries": int(g("state_entries", 0)),
    "state_gb": round(g("state_bytes", 0)/2**30, 2),
    "ttft_p50_ms": round(g("ttft_p50_milliseconds", 0), 1),
    "ttft_p95_ms": round(g("ttft_p95_milliseconds", 0), 1),
    "itl_p50_ms": round(g("itl_p50_milliseconds", 0), 2),
    "decode_tps": round(g("decode_tokens_per_second", 0), 1),
    "prefill_tps": round(g("prefill_tokens_per_second", 0), 1),
    "draft_acceptance": round(g("draft_acceptance_ratio", 0), 3),
    "req_submitted": int(g("requests_submitted_total", 0)),
    "req_completed": int(g("requests_completed_total", 0)),
    "req_failed": int(g("requests_failed_total", 0)),
    "req_cancelled": int(g("requests_cancelled_total", 0)),
    "pressure": "critical" if g("pressure_critical") else ("warning" if g("pressure_warning") else "normal"),
    "active_decoding": int(g("scheduler_decoding", 0)),
}
print(json.dumps(out))
PY
}

# ------------------------------------------------------------
# 分析：与上次快照差分 → 等级 + 建议
# ------------------------------------------------------------
sev_max() {
    # 等级取严重度更高者：OK < INFO < WARN < CRIT
    local a="$1" b="$2"
    local rank; rank=$(printf "%s\n%s" "${a:-OK}" "${b:-OK}" | awk '
        $1=="OK"{v=0} $1=="INFO"{v=1} $1=="WARN"{v=2} $1=="CRIT"{v=3} {if(v>m)m=v} END{print m}')
    case "$rank" in 0) echo OK;; 1) echo INFO;; 2) echo WARN;; 3) echo CRIT;; esac
}

report() {
    local snap="$1" prev_file="$2"
    MONITOR_TOOLS_DIR="$ROOT/tools" python3 - "$snap" "$prev_file" <<'PY'
import json, os, sys, time

sys.path.insert(0, os.environ.get("MONITOR_TOOLS_DIR", "."))
import monitor_window as mw

snap = json.loads(sys.argv[1])
state_f = sys.argv[2]
now = time.time()

prev = None
if os.path.exists(state_f):
    try:
        prev = json.load(open(state_f))
    except Exception:
        prev = None

level, alerts, advice = "OK", [], []

# 1) 服务/压力
if snap.get("pressure") == "critical":
    level = "CRIT"; alerts.append("内存压力 critical")
elif snap.get("pressure") == "warning":
    level = mw.sev_max(level, "WARN"); alerts.append("内存压力 warning")

# 2) 滚动窗口速率外推（消「相邻 5 分钟窗」外推失真）
#    实测两次误报（10-27 前：0.3h/0.4h 耗尽均未发生）——相邻窗斜率非单调
#    （Splash 周期性回收使 headroom 锯齿），单窗差分噪声大。改 30 分钟滚动
#    窗口最小二乘；样本跨度不足 MIN_SPAN_MIN 时**不外推**（只报样本不足）。
WINDOW_MIN = float(os.environ.get("MONITOR_ETA_WINDOW_MIN", 30))
MIN_SPAN_MIN = float(os.environ.get("MONITOR_ETA_MIN_SPAN_MIN", 15))
denied = snap["memory_denied"]
hr = snap["memory_headroom_gb"]

hist = mw.update_history(
    (prev or {}).get("hist") if prev else None,
    now, hr, denied, window_min=WINDOW_MIN)

# 2a) 内存准入拒绝速率（滚动窗口斜率 → 每小时增量；跨度不足不外推）
_den = mw.denied_rate(hist, min_span_min=MIN_SPAN_MIN)
den_slope = _den["rate_per_h"]
den_span = _den["span_min"]
if den_slope is not None:
    snap["denied_rate_per_h"] = round(den_slope, 1)
    snap["denied_window_min"] = round(den_span, 1)
    if den_slope > float(os.environ.get("MONITOR_DENIED_RATE", 300)):
        alerts.append(f"denied 增速 {den_slope:.0f}/h（{den_span:.0f}min 滚动窗口，"
                      f"阈值 {os.environ.get('MONITOR_DENIED_RATE', 300)}）")
        lvl = "CRIT" if den_slope > 1000 else "WARN"
        level = "CRIT" if lvl == "CRIT" else mw.sev_max(level, "WARN")

# 3) headroom
if hr < float(os.environ.get("MONITOR_HEADROOM_G", 8)):
    alerts.append(f"headroom 仅 {hr}GB（阈值 {os.environ.get('MONITOR_HEADROOM_G', 8)}GB）")
    if hr < 4: level = "CRIT"
    else: level = mw.sev_max(level, "WARN")

# 4) headroom 耗尽预测（滚动窗口斜率；跨度不足 → 不外推）
_eta = mw.headroom_eta(hist, hr, min_span_min=MIN_SPAN_MIN)
snap["headroom_eta_window_min"] = _eta["span_min"]
if _eta["sufficient"] and _eta["eta_h"] is not None:
    snap["headroom_drain_per_h"] = _eta["drain_per_h"]
    snap["headroom_eta_h"] = _eta["eta_h"]
    eta_h = _eta["eta_h"]
    if eta_h < 6:
        alerts.append(f"按 {_eta['span_min']:.0f}min 滚动速率 headroom 约 {eta_h:.1f}h 耗尽")
        if eta_h < 2: level = "CRIT"
        else: level = mw.sev_max(level, "WARN")
else:
    # 样本不足：不外推（宁可不报，不误报——两次误报的教训）
    snap["headroom_eta_h"] = None

# 5) 失败请求
if snap["req_failed"] > 0:
    level = "CRIT"
    alerts.append(f"失败请求 {snap['req_failed']}")

# 6) TTFT p95
p95 = snap["ttft_p95_ms"] / 1000.0
if p95 > float(os.environ.get("MONITOR_TTFT_P95_S", 60)):
    alerts.append(f"ttft_p95 {p95:.1f}s（阈值 {os.environ.get('MONITOR_TTFT_P95_S', 60)}s）")
    level = mw.sev_max(level, "WARN")

# 7) 热状态（从 /status 附带，若采集了）
th = snap.get("thermal", "")
if th == "heavy":
    level = "CRIT"; alerts.append("热状态 heavy（GPU 降频风险）")
elif th == "fair":
    if level == "OK": level = "INFO"
    alerts.append("热状态 fair")

# 8) 建议
recs = []
degraded = snap["memory_denied"] > 500 or hr < float(os.environ.get("MONITOR_HEADROOM_G", 8)) or p95 > 30
if snap["batch"]:
    if degraded:
        recs.append("批跑进行中且已劣化：批跑结束后立即 restart（清 KV/state，denied 归零）")
    else:
        recs.append("批跑进行中：保持观察，批跑结束后重启一次")
else:
    if degraded:
        recs.append("服务空闲但已劣化：建议立即 ./manage.sh restart")
    else:
        recs.append("服务健康，无需动作")

snap["level"] = level
snap["alerts"] = alerts
snap["advice"] = "; ".join(recs) if recs else "服务健康"

# 写状态文件（供下轮差分）
snap["ts_epoch"] = now
tmp = state_f + ".tmp"
_st = {k: snap.get(k) for k in
       ("ts_epoch", "memory_denied", "memory_headroom_gb", "kv_gb", "state_gb",
        "req_completed")}
# 滚动窗口历史样本：(ts, headroom_gb, denied)——外推用（相邻窗差分噪声大）
_st["hist"] = [[round(h[0], 3), round(h[1], 3), round(h[2], 3)] for h in hist]
json.dump(_st, open(tmp, "w"))
os.replace(tmp, state_f)

print(json.dumps(snap, ensure_ascii=False))
PY
}

# ------------------------------------------------------------
# 输出
# ------------------------------------------------------------
emit() {
    local snap="$1" mode="${2:-text}"
    if [[ "$mode" == "json" || "$mode" == "--json" ]]; then
        echo "$snap"
        return
    fi
    python3 - "$snap" <<'PY'
import json, sys
s = json.loads(sys.argv[1])
lvl = s.get("level", "?")
print(f"[{s['ts']}] {lvl} | batch={s.get('batch')} | "
      f"mem {s.get('memory_current_gb')}G head {s.get('memory_headroom_gb')}G "
      f"denied {s.get('memory_denied')}(+{s.get('denied_rate_per_h','—')}/h) "
      f"kv {s.get('kv_gb')}G state {s.get('state_gb')}G/{s.get('state_entries')} | "
      f"ttft {s.get('ttft_p50_ms')}ms/{s.get('ttft_p95_ms')}ms itl {s.get('itl_p50_ms')}ms "
      f"dec {s.get('decode_tps')} pre {s.get('prefill_tps')} draft {s.get('draft_acceptance')} | "
      f"th {s.get('thermal','—')} | "
      f"eta {s.get('headroom_eta_h') if s.get('headroom_eta_h') is not None else '—'}"
      f"({s.get('headroom_eta_window_min','—')}min 窗) | {s.get('advice','')}")
for a in s.get("alerts", []):
    print(f"    ⚠ {a}")
PY
}

append_log() {
    local snap="$1"
    local lvl; lvl=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('level','?'))" "$snap")
    emit "$snap" >> "$LOGFILE"
    if [[ "$lvl" == "WARN" || "$lvl" == "CRIT" ]]; then
        echo "$snap" | python3 -c "
import json,sys
s=json.loads(sys.stdin.read())
for a in s.get('alerts',[]): print(f\"[{s['ts']}] {s.get('level')} | {a}\")" >> "$LOGFILE"
    fi
    echo "$snap" > "$LATEST"
}

cmd_once() {
    local mode="text"; [[ "${1:-}" == "--json" ]] && mode="json"
    mkdir -p logs
    local m snap th
    m=$(collect) || true
    if ! snap=$(analyze "$m" 2>/dev/null); then
        local down="{\"ts\":\"$(now)\",\"level\":\"CRIT\",\"service\":\"down\",\"reason\":\"无法访问 :8081/metrics\",\"advice\":\"服务不可达：./manage.sh status 查看；必要时 restart\"}"
        emit "$down" "$mode" | tee -a "$LOGFILE"
        return 1
    fi
    # 附加热状态
    th=$(curl -s --max-time 10 http://127.0.0.1:8081/status 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('thermal_state','—'))" 2>/dev/null || echo "—")
    snap=$(echo "$snap" | python3 -c "import json,sys;s=json.loads(sys.stdin.read());s['thermal']=sys.argv[1];print(json.dumps(s,ensure_ascii=False))" "$th")
    # 差分分析（等级/告警/建议/headroom 预测）
    snap=$(report "$snap" "$STATE")
    emit "$snap" "$mode"
    append_log "$snap"
    # 守护模式下：批跑结束且已劣化 → 可选自动重启
    if [[ "${MONITOR_AUTO_RESTART:-0}" == "1" ]] && [[ "${2:-}" == "daemon" ]]; then
        local batch degraded
        batch=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('batch'))" "$snap")
        degraded=$(python3 -c "
import json,sys
s=json.loads(sys.argv[1])
print('yes' if (s['memory_denied']>500 or s['memory_headroom_gb']<float(os.environ.get('MONITOR_HEADROOM_G',8)) or s['ttft_p95_ms']>30000) else 'no')" "$snap")
        if [[ "$batch" == "False" && "$degraded" == "yes" ]]; then
            warn "批跑已结束且服务劣化，执行自动重启（MONITOR_AUTO_RESTART=1）"
            ./manage.sh restart >> "$LOGFILE" 2>&1 && info "自动重启完成"
        fi
    fi
}

cmd_daemon() {
    INTERVAL="${1:-$INTERVAL}"
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "监控守护已在运行 (PID: $(cat "$PIDFILE"))"; return 1
    fi
    mkdir -p logs
    echo "$$" > "$PIDFILE"
    info "监控守护启动 (PID $$, 间隔 ${INTERVAL}s, 日志 $LOGFILE, 自动重启=$AUTO_RESTART)"
    while true; do
        cmd_once "" "daemon" || true
        sleep "$INTERVAL"
    done
}

cmd_stop() {
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
        info "监控守护已停止"
    else
        warn "监控守护未在运行"; rm -f "$PIDFILE"
    fi
}

cmd_restart_degraded() {
    # 批跑结束后自动重启（需 MONITOR_AUTO_RESTART=1）
    local m snap
    m=$(collect) || { warn "无法采集指标"; return 1; }
    snap=$(analyze "$m" 2>/dev/null) || return 1
    local batch degraded
    batch=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('batch'))" "$snap")
    degraded=$(python3 -c "
import json,sys
s=json.loads(sys.argv[1])
print('yes' if (s['memory_denied']>500 or s['memory_headroom_gb']<float(os.environ.get('MONITOR_HEADROOM_G',8))) else 'no')" "$snap" 2>/dev/null || echo no)
    if [[ "$batch" == "False" && "$degraded" == "yes" ]]; then
        warn "批跑已结束且服务劣化，执行自动重启（MONITOR_AUTO_RESTART=1）"
        ./manage.sh restart >> "$LOGFILE" 2>&1
        info "自动重启完成"
    fi
}

info()  { printf "\033[0;32m[INFO]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }

case "${1:-once}" in
    once)   shift || true; cmd_once "$@" ;;
    daemon) shift || true; cmd_daemon "$@" ;;
    stop)   cmd_stop ;;
    status) if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
                info "监控守护运行中 (PID: $(cat "$PIDFILE"), 间隔 ${INTERVAL}s)"
                [[ -f "$LATEST" ]] && emit "$(cat "$LATEST")"
            else
                warn "监控守护未运行（启动: tools/monitor_service.sh daemon）"
            fi ;;
    *) echo "用法: tools/monitor_service.sh once [--json] | daemon [间隔秒] | stop | status"; exit 1 ;;
esac
