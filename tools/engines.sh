#!/bin/bash
# ============================================================
# engines.sh — 辅助引擎管理脚本（双引擎模式）
#
# manage.sh 只管理 active 后端(8081 35B); 本脚本管理辅助引擎
# (如 ornith-9b on 8084)。两者互不冲突。
#
# 用法:
#   tools/engines.sh list                           # 列出可用辅助引擎配置
#   tools/engines.sh start <name>                   # 启动引擎(读 configs/<name>.conf)
#   tools/engines.sh stop <name>                    # 优雅停止引擎
#   tools/engines.sh restart <name>                 # 重启引擎
#   tools/engines.sh status [name]                  # 状态(所有/单个)
#   tools/engines.sh supervise <name> [--daemon]    # 常驻守护: 引擎被 kill 自动重启
#                                                   # (应对 manage.sh restart/stop 误杀)
#   tools/engines.sh unsupervise <name>             # 停止守护
#   tools/engines.sh start-all / stop-all           # 批量
#
# 设计约束: 不动 active.conf / 不动 manage.sh 管理的主后端 / stdlib 即可运行
# ============================================================
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_DIR="$ROOT/configs"
ENGINE_DIR="$ROOT/.engines"
LOG_DIR="$ROOT/logs"
RAPID_MLX_BIN="$ROOT/.venv-rapidmlx/bin/rapid-mlx"
mkdir -p "$ENGINE_DIR"

# 颜色
info()  { printf '\033[0;32m[INFO]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
error() { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*"; }

# 引擎 PID 文件/日志
_pidfile()  { echo "$ENGINE_DIR/$1.pid"; }
_spfile()   { echo "$ENGINE_DIR/$1.supervisor.pid"; }
_logfile()  { echo "$LOG_DIR/$1.log"; }

_usage() {
    echo "用法: tools/engines.sh {list|start|stop|restart|status|supervise|unsupervise|start-all|stop-all} [name]"
}

# 列出可用辅助引擎配置(排除 active 与已归档)
_cmd_list() {
    local active active_name
    active="$(readlink "$CONF_DIR/active.conf" 2>/dev/null | sed 's/\.conf$//')"
    echo "可用辅助引擎配置 (active=$active, 由 manage.sh 管理):"
    for f in "$CONF_DIR"/*.conf; do
        local name
        name="$(basename "$f" .conf)"
        [[ "$name" == "$active" || "$name" == "active" || "$name" == "secret" ]] && continue
        [[ -d "$CONF_DIR/archived" && "$f" == "$CONF_DIR/archived/"* ]] && continue
        local desc
        desc="$(grep -m1 '^CONFIG_DESC=' "$f" 2>/dev/null | sed 's/^CONFIG_DESC=//; s/^"//; s/"$//')"
        echo "  $name  $desc"
    done
}

# 从配置读取变量(source 到子 shell 提取)
_get_conf() {
    local name="$1" key="$2"
    # 提取 CONF 文件中的 KEY 值(处理 export/引号)
    ( source "$CONF_DIR/$name.conf" 2>/dev/null; printf '%s' "${!key:-}" )
}

# 检查引擎是否存活
_is_alive() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# 启动一个引擎
_cmd_start() {
    local name="$1"
    local conf="$CONF_DIR/$name.conf"
    if [[ ! -f "$conf" ]]; then
        error "配置不存在: $conf"; return 1
    fi
    local pf _pidfile="$(_pidfile "$name")"
    pf="$(cat "$_pidfile" 2>/dev/null)"
    if _is_alive "$pf"; then
        warn "引擎 $name 已在运行 (PID: $pf)"; return 0
    fi
    local model port extra toolparser reasoning
    model="$(_get_conf "$name" LLAMA_MODEL)"
    port="$(_get_conf "$name" LLAMA_PORT)"
    extra="$(_get_conf "$name" RAPID_MLX_EXTRA_ARGS)"
    toolparser="$(_get_conf "$name" RAPID_MLX_TOOL_PARSER)"
    reasoning="$(_get_conf "$name" RAPID_MLX_REASONING_PARSER)"
    if [[ -z "$model" || -z "$port" ]]; then
        error "$name.conf 缺少 LLAMA_MODEL/LLAMA_PORT"; return 1
    fi
    # 端口冲突检查
    if lsof -Pi :"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
        error "端口 $port 已被占用"; return 1
    fi
    local args=()
    [[ -n "$extra" ]] && args+=( $extra )
    [[ -n "$toolparser" ]] && args+=( --tool-call-parser "$toolparser" )
    [[ -n "$reasoning" ]] && args+=( --reasoning-parser "$reasoning" )
    export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
    nohup "$RAPID_MLX_BIN" serve "$model" \
        --host 127.0.0.1 --port "$port" --enable-prefix-cache \
        "${args[@]}" > "$(_logfile "$name")" 2>&1 &
    local pid=$!
    echo "$pid" > "$_pidfile"
    info "启动引擎 $name (PID: $pid, :$port, model=$model)"
    # 等待就绪
    for _ in $(seq 1 40); do
        if curl -s --max-time 2 "http://127.0.0.1:$port/v1/models" >/dev/null 2>&1; then
            info "✅ $name 就绪 (:${port})"; return 0
        fi
        sleep 2
    done
    warn "$name 启动超时(80s), 日志: $(_logfile "$name")"
    return 1
}

# 优雅停止一个引擎
_cmd_stop() {
    local name="$1"
    local pf _pidfile="$(_pidfile "$name")"
    pf="$(cat "$_pidfile" 2>/dev/null)"
    if _is_alive "$pf"; then
        # 优雅 SIGTERM(rapid-mlx 会保存 prefix cache), 超时后 SIGKILL
        kill "$pf" 2>/dev/null
        for _ in $(seq 1 20); do
            _is_alive "$pf" || break
            sleep 1
        done
        if _is_alive "$pf"; then
            warn "$name 未优雅退出, 强制 kill -9 (PID: $pf)"
            kill -9 "$pf" 2>/dev/null
        fi
        info "已停止引擎 $name"
    else
        info "引擎 $name 未在运行"
    fi
    rm -f "$_pidfile"
    # 连带停守护(除非守护仍被 supervisor 管理——stop 只停引擎, 守护会重生)
}

# 状态
_cmd_status() {
    local name="${1:-}"
    if [[ -n "$name" ]]; then
        _status_one "$name"
    else
        for f in "$CONF_DIR"/*.conf; do
            local n
            n="$(basename "$f" .conf)"
            _status_one "$n"
        done
    fi
}
_status_one() {
    local name="$1" pf sp port
    pf="$(cat "$(_pidfile "$name")" 2>/dev/null)"
    sp="$(cat "$(_spfile "$name")" 2>/dev/null)"
    port="$(_get_conf "$name" LLAMA_PORT)"
    if _is_alive "$pf"; then
        printf '  %-14s ✅ 运行中 PID=%s :%s%s\n' "$name" "$pf" "$port" "$(_is_alive "$sp" && echo " (守护)" || echo "")"
    else
        printf '  %-14s ⚪ 停止\n' "$name"
    fi
}

# 常驻守护: 引擎死亡自动重启(应对 manage.sh restart/stop 误杀)
_cmd_supervise() {
    local name="$1" mode="${2:-}"
    local spfile="$(_spfile "$name")"
    if [[ "$mode" == "--daemon" ]]; then
        if _is_alive "$(cat "$spfile" 2>/dev/null)"; then
            info "守护已运行 (PID: $(cat "$spfile"))"; return 0
        fi
        nohup "$0" _loop "$name" > "$LOG_DIR/$name.supervisor.log" 2>&1 &
        echo $! > "$spfile"
        info "守护已启动 (PID: $!, 监督引擎 $name)"
        return 0
    fi
    return 0
}
_cmd_supervise_loop() {
    local name="$1"
    echo $$ > "$(_spfile "$name")"
    info "守护开始监督引擎 $name (PID: $$) — 引擎被 kill 将自动重启"
    while true; do
        _cmd_start "$name" >/dev/null 2>&1
        sleep 10
    done
}
_cmd_unsupervise() {
    local name="$1" sp
    sp="$(cat "$(_spfile "$name")" 2>/dev/null)"
    if _is_alive "$sp"; then
        kill "$sp" 2>/dev/null
        info "已停止守护 (PID: $sp)"
    fi
    rm -f "$(_spfile "$name")"
}

_cmd_start_all() {
    for f in "$CONF_DIR"/*.conf; do
        local n; n="$(basename "$f" .conf)"
        [[ "$n" == "$(readlink "$CONF_DIR/active.conf" 2>/dev/null | sed 's/\.conf$//')" ]] && continue
        _cmd_start "$n" || true
    done
}
_cmd_stop_all() {
    for f in "$CONF_DIR"/*.conf; do
        local n; n="$(basename "$f" .conf)"
        [[ "$n" == "$(readlink "$CONF_DIR/active.conf" 2>/dev/null | sed 's/\.conf$//')" ]] && continue
        _cmd_stop "$n"
    done
}

# 入口
cmd="${1:-}"; name="${2:-}"
case "$cmd" in
    list)        _cmd_list ;;
    start)       _cmd_start "$name" ;;
    stop)        _cmd_stop "$name" ;;
    restart)     _cmd_stop "$name"; _cmd_start "$name" ;;
    status)      _cmd_status "$name" ;;
    supervise)   _cmd_supervise "$name" "${3:-}" ;;
    _loop)       _cmd_supervise_loop "$name" ;;
    unsupervise) _cmd_unsupervise "$name" ;;
    start-all)   _cmd_start_all ;;
    stop-all)    _cmd_stop_all ;;
    *)           _usage; exit 1 ;;
esac