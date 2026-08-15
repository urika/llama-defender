#!/usr/bin/env bash
# ============================================================
# llama.cpp / Rapid-MLX 服务管理脚本
# 支持后端: llama-server | rapid-mlx
# 命令: start | stop | status | restart | list | switch | current
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$SCRIPT_DIR/llama-server.pid"
LOGFILE="$SCRIPT_DIR/logs/llama-server.log"
PROXY_PIDFILE="$SCRIPT_DIR/anthropic_proxy.pid"
PROXY_LOGFILE="$SCRIPT_DIR/logs/anthropic_proxy.log"
WATCHDOG_PIDFILE="$SCRIPT_DIR/watchdog.pid"
WATCHDOG_LOGFILE="$SCRIPT_DIR/logs/watchdog.log"
WATCHDOG_STATE_PATH="$SCRIPT_DIR/logs/watchdog_state.json"
MANAGE_LOCKFILE="$SCRIPT_DIR/.manage.lock"

# 确保日志目录存在
mkdir -p "$SCRIPT_DIR/logs"
CONFIG_DIR="$SCRIPT_DIR/configs"
ACTIVE_CONF="$CONFIG_DIR/active.conf"

# 代理默认配置
: "${PROXY_PORT:=4000}"
: "${PROXY_HOST:=127.0.0.1}"

# ============================================================
# 加载配置文件（如果存在）
# ============================================================
_load_config() {
    if [[ -L "$ACTIVE_CONF" && -f "$ACTIVE_CONF" ]]; then
        # shellcheck source=/dev/null
        source "$ACTIVE_CONF"
    fi
    # Always load local secrets (API keys) if present — git-ignored.
    # This makes LLAMA_API_KEY available at startup so hot-switch to
    # cloud mode works without restarting manage.sh.
    if [[ -f "$CONFIG_DIR/secret.local.conf" ]]; then
        # shellcheck source=/dev/null
        # set -a 使 secret 中的变量（含分提供商 KIMI_API_KEY/ZHIPU_API_KEY 等
        # model_registry providers.key_env 引用的 key）导出给代理子进程，
        # 启动即生效，无需依赖首次 SIGHUP 补载。
        set -a
        source "$CONFIG_DIR/secret.local.conf"
        set +a
    fi
}

# 加载当前激活配置
_load_config

# ============================================================
# 仓库级文件锁 (R4): 变更类命令互斥, 防止 agent_go 与 watchdog 并发
# ============================================================
# macOS 通常没有 flock, 但自带 shlock; Linux 优先 flock 若可用。
# 锁文件含持有者 PID, 进程崩溃后下次加锁会自动清理失效锁。
_acquire_manage_lock() {
    local timeout="${1:-5}"
    local elapsed=0

    # Linux: 优先使用 flock (阻塞/超时语义更干净)
    if command -v flock >/dev/null 2>&1; then
        local fd
        exec {fd}>"$MANAGE_LOCKFILE"
        if flock -w "$timeout" "$fd" 2>/dev/null; then
            MANAGE_LOCK_FD=$fd
            return 0
        fi
        exec {fd}>&- 2>/dev/null || true
        error "无法获取 manage.sh 互斥锁 (超时 ${timeout}s); 可能有其他管理命令正在执行"
        return 1
    fi

    # macOS / fallback: 使用 shlock 轮询
    if ! command -v shlock >/dev/null 2>&1; then
        warn "系统未安装 flock/shlock, 跳过管理锁 (fail-open)"
        return 0
    fi

    while (( elapsed < timeout )); do
        if shlock -f "$MANAGE_LOCKFILE" -p $$ 2>/dev/null; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    error "无法获取 manage.sh 互斥锁 (超时 ${timeout}s); 可能有其他管理命令正在执行"
    return 1
}

_release_manage_lock() {
    if [[ -n "${MANAGE_LOCK_FD:-}" ]]; then
        flock -u "$MANAGE_LOCK_FD" 2>/dev/null || true
        exec {MANAGE_LOCK_FD}>&- 2>/dev/null || true
        unset MANAGE_LOCK_FD
    fi
    # shlock 路径: 直接删除锁文件
    rm -f "$MANAGE_LOCKFILE"
}

_with_manage_lock() {
    _acquire_manage_lock || return 1
    local rc=0
    "$@" || rc=$?
    _release_manage_lock
    return $rc
}

# ============================================================
# 默认配置
# ============================================================
: "${LLAMA_BACKEND:=llama-server}"
: "${LLAMA_MODEL:=mlx-community/Qwen3.6-35B-A3B-4bit}"
: "${LLAMA_PORT:=8081}"
: "${LLAMA_HOST:=127.0.0.1}"
: "${LLAMA_CTX:=131072}"
: "${LLAMA_BATCH:=2048}"
: "${LLAMA_UBATCH:=512}"
: "${LLAMA_N_PREDICT:=-1}"
: "${LLAMA_THREADS:=8}"
: "${LLAMA_KV_K:=q8_0}"
: "${LLAMA_KV_V:=q8_0}"
: "${LLAMA_TEMP:=0.6}"
: "${LLAMA_TOP_P:=0.95}"
: "${LLAMA_TOP_K:=20}"
: "${LLAMA_PRESENCE_PENALTY:=0.0}"
: "${LLAMA_MIN_P:=0.0}"
: "${LLAMA_THINKING:=false}"
: "${LLAMA_EXTRA_ARGS:=--jinja --flash-attn on --fit on}"

# Rapid-MLX 默认参数
: "${RAPID_MLX_TOOL_PARSER:=qwen3_coder_xml}"
: "${RAPID_MLX_REASONING_PARSER=qwen3}"
: "${RAPID_MLX_ENABLE_PREFIX_CACHE:=true}"
: "${RAPID_MLX_KV_QUANTIZATION:=false}"
: "${RAPID_MLX_KV_QUANT_BITS:=8}"

# MLX-VLM 默认参数
: "${MLX_VLM_KV_BITS:=4}"
: "${MLX_VLM_ENABLE_THINKING:=false}"
: "${MLX_VLM_DRAFT_MODEL:=}"
: "${MLX_VLM_DRAFT_KIND:=}"
: "${MLX_VLM_MAX_KV_SIZE:=}"

# 代理并发控制
: "${PROXY_MAX_CONCURRENT:=1}"

# ============================================================
# 颜色输出
# ============================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ============================================================
# 生命周期事件日志 (R7): 切换/重启/自动恢复等关键事件写入 JSONL
# ============================================================
: "${LIFECYCLE_EVENTS_PATH:="$SCRIPT_DIR/logs/lifecycle_events.jsonl"}"

_log_lifecycle_event() {
    local event="${1:-}"
    local detail="${2:-}"
    if [[ -z "$event" ]]; then
        return 0
    fi
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    mkdir -p "$SCRIPT_DIR/logs"
    # detail 中的双引号和反斜杠需要转义，使用 python3 保证合法 JSON
    python3 - <<PY >> "$LIFECYCLE_EVENTS_PATH"
import json, sys
rec = {"ts": "$ts", "event": "$event", "detail": "$detail"}
print(json.dumps(rec, ensure_ascii=False))
PY
}

# ============================================================
# 获取当前配置名称
# ============================================================
_current_config_name() {
    if [[ -L "$ACTIVE_CONF" ]]; then
        basename "$(readlink "$ACTIVE_CONF")" .conf
    else
        echo "default"
    fi
}

# ============================================================
# 获取当前运行的后端类型
# ============================================================
_current_backend() {
    if [[ -f "$PIDFILE" ]]; then
        local pid
        pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
        if kill -0 "$pid" 2>/dev/null; then
            local comm
            comm=$(ps -p "$pid" -o comm= 2>/dev/null | tr -d ' ')
            if [[ "$comm" == "llama-server" ]]; then
                echo "llama-server"
                return 0
            elif [[ "$comm" == "rapid-mlx" || "$comm" == "vllm-mlx" ]]; then
                echo "rapid-mlx"
                return 0
            elif [[ "$comm" == "mlx_vlm" ]]; then
                echo "mlx_vlm"
                return 0
            fi
        fi
    fi
    # fallback: search process
    if pgrep -f "rapid-mlx" >/dev/null 2>&1; then
        echo "rapid-mlx"
        return 0
    elif pgrep -f "mlx_vlm" >/dev/null 2>&1; then
        echo "mlx_vlm"
        return 0
    elif pgrep -x "llama-server" >/dev/null 2>&1; then
        echo "llama-server"
        return 0
    fi
    return 1
}

# ============================================================
# 获取代理 PID
# ============================================================
_get_proxy_pid() {
    if [[ -f "$PROXY_PIDFILE" ]]; then
        local pid
        pid=$(cat "$PROXY_PIDFILE" 2>/dev/null) || return 1
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
    fi
    # fallback: search by script name
    local pid
    pid=$(pgrep -f "anthropic_proxy.py" 2>/dev/null | head -1)
    if [[ -n "$pid" ]]; then
        echo "$pid" > "$PROXY_PIDFILE"
        echo "$pid"
        return 0
    fi
    return 1
}

# ============================================================
# 获取后端进程 PID
# ============================================================
_get_pid() {
    local backend
    backend=$(_current_backend 2>/dev/null) || return 1
    
    if [[ -f "$PIDFILE" ]]; then
        local pid
        pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
        if kill -0 "$pid" 2>/dev/null; then
            local comm
            comm=$(ps -p "$pid" -o comm= 2>/dev/null | tr -d ' ')
            if [[ "$comm" == "$backend" ]]; then
                echo "$pid"
                return 0
            fi
        fi
    fi
    
    # fallback: search by backend name
    local pid
    if [[ "$backend" == "rapid-mlx" || "$backend" == "vllm-mlx" ]]; then
        pid=$(pgrep -f "rapid-mlx" 2>/dev/null | head -1)
    elif [[ "$backend" == "mlx_vlm" ]]; then
        pid=$(pgrep -f "mlx_vlm" 2>/dev/null | head -1)
    else
        pid=$(pgrep -x "$backend" 2>/dev/null | head -1)
    fi
    if [[ -n "$pid" ]]; then
        echo "$pid" > "$PIDFILE"
        echo "$pid"
        return 0
    fi
    return 1
}

# ============================================================
# 检查端口占用
# ============================================================
_check_port() {
    local port="$1"
    local pids
    pids=$(lsof -Pi ":$port" -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ')
    if [[ -n "$pids" ]]; then
        # 代理端口被占用时，先检查是否是已记录的代理进程或僵尸进程
        if [[ "$port" == "$PROXY_PORT" ]]; then
            local proxy_pid
            proxy_pid=$(_get_proxy_pid 2>/dev/null) || true
            for p in $pids; do
                if [[ -n "$proxy_pid" && "$p" == "$proxy_pid" ]] && ! kill -0 "$p" 2>/dev/null; then
                    warn "端口 $port 被已死亡的代理 PID 文件占用，清理..."
                    rm -f "$PROXY_PIDFILE"
                    continue
                fi
            done
            # 重新检查是否仍有其他进程占用
            pids=$(lsof -Pi ":$port" -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ')
            [[ -z "$pids" ]] && return 0
        fi
        error "端口 $port 已被占用 (PID: $pids)"
        ps -p $pids -o pid,comm,args 2>/dev/null | tail -n +2
        return 1
    fi
    return 0
}

# ============================================================
# 获取进程正在下载的 HuggingFace 模型文件总字节数
# 兼容 llama.cpp (.downloadInProgress) 与 huggingface_hub (.incomplete)
# stdout: <total_bytes> <file_count>，没有正在下载时输出 "0 0"
# ============================================================
_get_download_progress() {
    local pid="$1"
    lsof -p "$pid" -nP 2>/dev/null \
        | awk '/huggingface.*(downloadInProgress|\.incomplete)/ {sum += $7; n++} END {printf "%d %d\n", sum+0, n+0}'
}

# ============================================================
# 格式化字节数为人类可读形式
# ============================================================
_format_bytes() {
    local b=$1
    if (( b >= 1073741824 )); then
        printf "%d.%01d GiB" $((b / 1073741824)) $(( (b * 10 / 1073741824) % 10 ))
    elif (( b >= 1048576 )); then
        printf "%d MiB" $((b / 1048576))
    elif (( b >= 1024 )); then
        printf "%d KiB" $((b / 1024))
    else
        printf "%d B" "$b"
    fi
}

# ============================================================
# 等待后端服务就绪
# 区分两个阶段：
#   1) 下载阶段：检测到 .downloadInProgress/.incomplete 文件时，
#      显示下载进度，仅当下载长时间无进展才判定为卡住
#   2) 启动阶段：下载完成后给 STARTUP_TIMEOUT 秒加载模型
# 参数:
#   $1 = pid
#   $2 = 服务名 (用于日志输出)
# 返回:
#   0 = 就绪
#   1 = 进程退出 / 下载停滞 / 启动超时
# ============================================================
_wait_for_ready() {
    local pid="$1"
    local name="$2"
    local startup_timeout="${STARTUP_TIMEOUT:-60}"
    local stall_timeout="${DOWNLOAD_STALL_TIMEOUT:-120}"
    local hard_limit="${WAIT_HARD_LIMIT:-1800}"  # 30 分钟硬上限

    local elapsed=0
    local startup_idle=0
    local download_idle=0
    local last_dl_size=0
    local was_downloading=0

    while (( elapsed < hard_limit )); do
        if ! kill -0 "$pid" 2>/dev/null; then
            error "进程已退出！查看日志: $LOGFILE"
            tail -n 20 "$LOGFILE" 2>/dev/null
            rm -f "$PIDFILE"
            return 1
        fi

        if curl -s --max-time 2 "http://$LLAMA_HOST:$LLAMA_PORT/v1/models" >/dev/null 2>&1; then
            info "✅ $name 就绪 (PID: $pid)"
            return 0
        fi

        # 检查下载进度
        local dl_info dl_size dl_count
        dl_info=$(_get_download_progress "$pid")
        dl_size=${dl_info% *}
        dl_count=${dl_info##* }

        if (( dl_count > 0 )); then
            # 下载阶段
            was_downloading=1
            startup_idle=0  # 重置启动计时器
            if (( dl_size > last_dl_size )); then
                download_idle=0
                last_dl_size=$dl_size
            else
                download_idle=$((download_idle + 1))
            fi
            if (( elapsed % 5 == 0 )); then
                echo "  ⬇  下载中: $dl_count 个文件, 已 $(_format_bytes "$dl_size")"
            fi
            if (( download_idle >= stall_timeout )); then
                warn "下载停滞超过 ${stall_timeout}s (尺寸未增长)，可能网络异常 (PID: $pid)"
                warn "最近日志:"
                tail -n 10 "$LOGFILE" 2>/dev/null
                return 1
            fi
        else
            # 启动阶段
            startup_idle=$((startup_idle + 1))
            if (( elapsed % 10 == 0 )); then
                if (( was_downloading == 1 )); then
                    echo "  📦 下载完成，加载模型中... (${startup_idle}/${startup_timeout}s)"
                else
                    echo "  等待中... (${startup_idle}/${startup_timeout}s)"
                fi
            fi
            if (( startup_idle >= startup_timeout )); then
                warn "服务启动超时 (${startup_timeout}s 未响应)，但进程仍在运行 (PID: $pid)"
                warn "可通过 './manage.sh logs' 查看后续日志，或 './manage.sh status' 确认就绪"
                return 1
            fi
        fi

        sleep 1
        elapsed=$((elapsed + 1))
    done

    warn "等待 ${hard_limit}s 仍未就绪 (PID: $pid)，请人工排查"
    return 1
}

# ============================================================
# 启动 llama-server
# ============================================================
_start_llama_server() {
    _check_port "$LLAMA_PORT" || return 1

    info "启动 llama-server..."
    info "  配置: ${CYAN}$(_current_config_name)${NC}"
    info "  模型: $LLAMA_MODEL"
    info "  地址: $LLAMA_HOST:$LLAMA_PORT"
    info "  上下文: $LLAMA_CTX"
    info "  KV 量化: K=$LLAMA_KV_K, V=$LLAMA_KV_V"
    info "  线程: $LLAMA_THREADS"
    info "  采样: temp=$LLAMA_TEMP, top-p=$LLAMA_TOP_P, top-k=$LLAMA_TOP_K"
    [[ -n "$LLAMA_THINKING" ]] && info "  Thinking: $LLAMA_THINKING"

    # 构建启动参数
    local args=()
    if [[ "$LLAMA_MODEL" == /* || "$LLAMA_MODEL" == ./* ]]; then
        args+=(-m "$LLAMA_MODEL")
    else
        args+=(-hf "$LLAMA_MODEL")
    fi
    args+=(
        --host "$LLAMA_HOST"
        --port "$LLAMA_PORT"
        -c "$LLAMA_CTX"
        -b "$LLAMA_BATCH"
        -ub "$LLAMA_UBATCH"
        -n "$LLAMA_N_PREDICT"
        -t "$LLAMA_THREADS"
        --cache-type-k "$LLAMA_KV_K"
        --cache-type-v "$LLAMA_KV_V"
        --temp "$LLAMA_TEMP"
        --top-p "$LLAMA_TOP_P"
        --top-k "$LLAMA_TOP_K"
        --presence-penalty "$LLAMA_PRESENCE_PENALTY"
        --min-p "$LLAMA_MIN_P"
    )

    if [[ "$LLAMA_THINKING" == "false" ]]; then
        args+=(--chat-template-kwargs '{"enable_thinking":false}')
    elif [[ "$LLAMA_THINKING" == "true" ]]; then
        args+=(--chat-template-kwargs '{"enable_thinking":true}')
    fi

    # Custom chat template override (e.g. fixed Qwen template)
    if [[ -n "${LLAMA_CHAT_TEMPLATE:-}" && -f "$LLAMA_CHAT_TEMPLATE" ]]; then
        local _template_content
        _template_content=$(cat "$LLAMA_CHAT_TEMPLATE")
        args+=(--chat-template "$_template_content")
    fi

    if [[ -n "$LLAMA_EXTRA_ARGS" ]]; then
        read -ra extra <<< "$LLAMA_EXTRA_ARGS"
        args+=("${extra[@]}")
    fi

    nohup llama-server "${args[@]}" >> "$LOGFILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PIDFILE"

    info "进程已启动 (PID: $new_pid)，等待就绪..."
    _wait_for_ready "$new_pid" "llama-server"
}

# ============================================================
# 启动 Rapid-MLX
# ============================================================
_start_rapid_mlx() {
    _check_port "$LLAMA_PORT" || return 1

    # DEF-006: GPU 内存安全检查 — 防止 kernel panic
    if [[ -n "${RAPID_MLX_EXTRA_ARGS:-}" ]]; then
        local gpu_mem_val
        gpu_mem_val=$(echo "$RAPID_MLX_EXTRA_ARGS" | grep -oE '\-\-gpu-memory-utilization[[:space:]]+([0-9.]+)' | grep -oE '[0-9.]+$' || true)
        if [[ -n "$gpu_mem_val" ]]; then
            if ! command -v bc &>/dev/null; then
                warn "DEF-006: bc 未安装，无法验证 --gpu-memory-utilization=$gpu_mem_val"
                warn "  brew install bc 可启用自动检查 (推荐 ≤ 0.80)"
            elif (( $(echo "$gpu_mem_val > 0.85" | bc -l 2>/dev/null || echo 0) )); then
                error "DEF-006 安全检查: --gpu-memory-utilization=$gpu_mem_val > 0.85"
                error "超过 0.85 可能触发 macOS kernel panic (Apple Silicon firmware 限制)"
                error "请在配置文件中降低 --gpu-memory-utilization 的值 (推荐 ≤ 0.80)"
                return 1
            elif (( $(echo "$gpu_mem_val > 0.80" | bc -l 2>/dev/null || echo 0) )); then
                warn "DEF-006 警告: --gpu-memory-utilization=$gpu_mem_val 接近危险阈值 (0.80-0.85)"
                warn "如果出现 kernel panic，请降低到 ≤ 0.80"
            fi
        fi
    fi

    info "启动 ${LLAMA_SERVER_BIN:-rapid-mlx}..."
    info "  配置: ${CYAN}$(_current_config_name)${NC}"
    info "  模型: $LLAMA_MODEL"
    info "  地址: $LLAMA_HOST:$LLAMA_PORT"
    info "  工具解析: $RAPID_MLX_TOOL_PARSER"
    info "  推理解析: $RAPID_MLX_REASONING_PARSER"

    # DEF-007: 自动检测 chat_template 是否需要修复
    if [[ "$LLAMA_MODEL" == *"/"* ]]; then
        local _model_slug
        _model_slug=$(echo "$LLAMA_MODEL" | tr '/' '--')
        local _cache_base="$HOME/.cache/huggingface/hub/models--${_model_slug}/snapshots"
        if [[ -d "$_cache_base" ]]; then
            for _snap_dir in "$_cache_base"/*/; do
                local _tpl="$_snap_dir/chat_template.jinja"
                if [[ -f "$_tpl" ]]; then
                    if ! grep -q "is_system_content" "$_tpl" 2>/dev/null; then
                        warn "DEF-007: 检测到未修复的 chat_template"
                        warn "  路径: ${_snap_dir%/}"
                        warn "  运行: ./manage.sh fix-template ${_snap_dir%/}"
                    fi
                fi
            done
        fi
    fi
    if [[ "$RAPID_MLX_KV_QUANTIZATION" == "true" ]]; then
        info "  KV 量化: ${GREEN}启用${NC} ($RAPID_MLX_KV_QUANT_BITS-bit)"
    else
        info "  KV 量化: 未启用 (FP16)"
    fi

    local args=(
        serve "$LLAMA_MODEL"
        --host "$LLAMA_HOST"
        --port "$LLAMA_PORT"
        --enable-auto-tool-choice
        --tool-call-parser "$RAPID_MLX_TOOL_PARSER"
    )

    # Coder 模型不需要 reasoning parser（空字符串时跳过）
    if [[ -n "${RAPID_MLX_REASONING_PARSER:-}" ]]; then
        args+=(--reasoning-parser "$RAPID_MLX_REASONING_PARSER")
    fi

    if [[ "${LLAMA_THINKING:-false}" != "true" ]]; then
        args+=(--no-thinking)
    fi

    args+=(
        --log-level INFO
    )

    if [[ "$RAPID_MLX_ENABLE_PREFIX_CACHE" == "true" ]]; then
        args+=(--enable-prefix-cache)
    else
        args+=(--disable-prefix-cache)
    fi

    if [[ "$RAPID_MLX_KV_QUANTIZATION" == "true" ]]; then
        args+=(
            --kv-cache-quantization
            --kv-cache-quantization-bits "$RAPID_MLX_KV_QUANT_BITS"
        )
    fi

    if [[ -n "${RAPID_MLX_EXTRA_ARGS:-}" ]]; then
        read -ra extra <<< "$RAPID_MLX_EXTRA_ARGS"
        args+=("${extra[@]}")
    fi

    nohup ${LLAMA_SERVER_BIN:-rapid-mlx} "${args[@]}" >> "$LOGFILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PIDFILE"

    info "进程已启动 (PID: $new_pid)，等待就绪..."
    _wait_for_ready "$new_pid" "Rapid-MLX"
}

# ============================================================
# 启动 MLX-VLM (Vision Language Model 推理服务)
# ============================================================
_start_mlx_vlm() {
    _check_port "$LLAMA_PORT" || return 1

    # 确定二进制
    local bin="${LLAMA_SERVER_BIN:-mlx_vlm}"

    info "启动 ${bin}..."
    info "  配置: ${CYAN}$(_current_config_name)${NC}"
    info "  模型: $LLAMA_MODEL"
    info "  地址: $LLAMA_HOST:$LLAMA_PORT"

    local args=(
        --model "$LLAMA_MODEL"
        --host "$LLAMA_HOST"
        --port "$LLAMA_PORT"
        --log-level INFO
    )

    # KV cache 量化
    if [[ -n "${MLX_VLM_KV_BITS:-}" ]]; then
        args+=(--kv-bits "$MLX_VLM_KV_BITS")
        info "  KV 量化: ${MLX_VLM_KV_BITS}-bit"
    fi

    # Thinking mode
    if [[ "${MLX_VLM_ENABLE_THINKING:-false}" == "true" ]]; then
        args+=(--enable-thinking)
        info "  Thinking: ${GREEN}启用${NC}"
    else
        info "  Thinking: 关闭"
    fi

    # 推测解码
    if [[ -n "${MLX_VLM_DRAFT_MODEL:-}" ]]; then
        args+=(--draft-model "$MLX_VLM_DRAFT_MODEL")
        info "  Draft model: $MLX_VLM_DRAFT_MODEL"
        if [[ -n "${MLX_VLM_DRAFT_KIND:-}" ]]; then
            args+=(--draft-kind "$MLX_VLM_DRAFT_KIND")
            info "  Draft kind: $MLX_VLM_DRAFT_KIND"
        fi
    fi

    # Max KV size
    if [[ -n "${MLX_VLM_MAX_KV_SIZE:-}" ]]; then
        args+=(--max-kv-size "$MLX_VLM_MAX_KV_SIZE")
        info "  Max KV size: $MLX_VLM_MAX_KV_SIZE"
    fi

    # 额外参数
    if [[ -n "${MLX_VLM_EXTRA_ARGS:-}" ]]; then
        read -ra extra <<< "$MLX_VLM_EXTRA_ARGS"
        args+=("${extra[@]}")
    fi

    nohup ${bin} "${args[@]}" >> "$LOGFILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PIDFILE"

    info "进程已启动 (PID: $new_pid)，等待就绪..."
    _wait_for_ready "$new_pid" "MLX-VLM"
}

# ============================================================
# 启动代理
# ============================================================
_start_proxy() {
    local proxy_pid
    if proxy_pid=$(_get_proxy_pid 2>/dev/null); then
        # 已有代理进程，额外验证端口是否真的有服务响应
        if curl -s --max-time 2 "http://$PROXY_HOST:$PROXY_PORT/v1/models" >/dev/null 2>&1; then
            warn "代理已在运行且可连接 (PID: $proxy_pid)"
            return 0
        fi
        warn "代理 PID 文件存在但端口无响应，清理旧代理..."
        _stop_proxy || true
        sleep 1
    fi

    _check_port "$PROXY_PORT" || return 1

    # 使用配置中的 LLAMA_BASE_URL 如果有的话，否则用本地
    local base_url="${LLAMA_BASE_URL:-http://$LLAMA_HOST:$LLAMA_PORT/v1}"

    local active_profile="${PROXY_COMPRESSION_PROFILE:-balanced}"
    info "启动 anthropic_proxy.py..."
    info "  地址: $PROXY_HOST:$PROXY_PORT"
    info "  后端: $base_url"
    info "  Profile: $active_profile"

    # 使用 bash -c wrapper 捕获崩溃输出到日志；wrapper 在函数末尾删除
    local proxy_wrapper="$SCRIPT_DIR/.start_proxy_$$.sh"
    cat > "$proxy_wrapper" <<EOF
#!/usr/bin/env bash
exec >> "$PROXY_LOGFILE" 2>&1
exec python3 "$SCRIPT_DIR/anthropic_proxy.py"
EOF
    chmod +x "$proxy_wrapper"

    # PROXY_* env vars below follow a 3-tier priority chain (highest first):
    #   1. Active config file (configs/<active>.conf), sourced earlier
    #   2. Manage.sh defaults (this block, after the `:-` operator)
    #   3. anthropic_proxy.py module-level defaults (os.environ.get fallback)
    # This is the Phase 2 cleanup of the original "manage.sh default=char vs
    # conf sets fifo/rounds" priority ambiguity. The alias LLAMA_CTX_STRATEGY
    # is supported as a more semantic name for the same env var.
    LLAMA_BASE_URL="$base_url" \
    LLAMA_API_KEY="${LLAMA_API_KEY:-sk-1234}" \
    MODEL_NAME="${MODEL_NAME:-$LLAMA_MODEL}" \
    PORT="$PROXY_PORT" \
    HOST="$PROXY_HOST" \
    PROXY_LOG_PATH="$PROXY_LOGFILE" \
    PROXY_MAX_CONCURRENT="${PROXY_MAX_CONCURRENT:-1}" \
    PROXY_CLEAR_ENABLED="${PROXY_CLEAR_ENABLED:-true}" \
    PROXY_CLEAR_THRESHOLD="${PROXY_CLEAR_THRESHOLD:-50000}" \
    PROXY_TOOL_KEEP="${PROXY_TOOL_KEEP:-5}" \
    PROXY_CTX_LIMIT_ENABLED="${PROXY_CTX_LIMIT_ENABLED:-true}" \
    PROXY_CTX_CHARS_LIMIT="${PROXY_CTX_CHARS_LIMIT:-350000}" \
    PROXY_CTX_TRUNCATE_STRATEGY="${PROXY_CTX_TRUNCATE_STRATEGY:-${LLAMA_CTX_STRATEGY:-char}}" \
    PROXY_CTX_KEEP_ROUNDS="${PROXY_CTX_KEEP_ROUNDS:-10}" \
    PROXY_CTX_TOKEN_BUDGET="${PROXY_CTX_TOKEN_BUDGET:-30000}" \
    PROXY_CTX_TOKEN_RATIO="${PROXY_CTX_TOKEN_RATIO:-0.2}" \
    PROXY_CTX_KEEP_HEAD="${PROXY_CTX_KEEP_HEAD:-2}" \
    PROXY_CTX_KEEP_TAIL="${PROXY_CTX_KEEP_TAIL:-6}" \
    PROXY_SAVE_REQUESTS="${PROXY_SAVE_REQUESTS:-}" \
    PROXY_SAVE_REQUESTS_DIR="${PROXY_SAVE_REQUESTS_DIR:-/tmp/anthropic_requests}" \
    PROXY_SAVE_REQUESTS_MAX="${PROXY_SAVE_REQUESTS_MAX:-10}" \
    PROXY_COMPRESSION_PROFILE="${PROXY_COMPRESSION_PROFILE:-balanced}" \
    PROXY_BM25_ENABLED="${PROXY_BM25_ENABLED:-true}" \
    PROXY_BM25_K1="${PROXY_BM25_K1:-1.5}" \
    PROXY_BM25_B="${PROXY_BM25_B:-0.75}" \
    PROXY_BM25_KEEP_THRESHOLD="${PROXY_BM25_KEEP_THRESHOLD:-3.5}" \
    PROXY_BM25_DROP_THRESHOLD="${PROXY_BM25_DROP_THRESHOLD:-0.5}" \
    PROXY_BM25_MIN_PREFIX="${PROXY_BM25_MIN_PREFIX:-4}" \
    PROXY_BM25_IDF_LRU_MAX="${PROXY_BM25_IDF_LRU_MAX:-10000}" \
    PROXY_CONTENT_TOOLS_FALLBACK="${PROXY_CONTENT_TOOLS_FALLBACK:-true}" \
    PROXY_MAX_TOKENS_OVERRIDE="${PROXY_MAX_TOKENS_OVERRIDE:-0}" \
    PROXY_OUTPUT_TOKEN_LIMIT_RATIO="${PROXY_OUTPUT_TOKEN_LIMIT_RATIO:-1.5}" \
    PROXY_BACKEND_TIMEOUT="${PROXY_BACKEND_TIMEOUT:-600}" \
    PROXY_MAX_REQUEST_BYTES="${PROXY_MAX_REQUEST_BYTES:-512000}" \
    PROXY_CLOUD_MAX_REQUEST_BYTES="${PROXY_CLOUD_MAX_REQUEST_BYTES:-2097152}" \
    PROXY_OOM_SAFE_CHARS="${PROXY_OOM_SAFE_CHARS:-${PROXY_PRE_TRUNCATE_CHARS:-200000}}" \
    PROXY_SESSION_CONTINUATION_ENABLED="${PROXY_SESSION_CONTINUATION_ENABLED:-true}" \
    PROXY_SESSION_CONTINUATION_MIN_REQUESTS="${PROXY_SESSION_CONTINUATION_MIN_REQUESTS:-2}" \
    PROXY_CHARS_EXPANSION="${PROXY_CHARS_EXPANSION:-90000}" \
    PROXY_ROUTE_ENABLED="${PROXY_ROUTE_ENABLED:-false}" \
    PROXY_ROUTE_THRESHOLD_CHARS="${PROXY_ROUTE_THRESHOLD_CHARS:-90000}" \
    PROXY_ROUTE_MEMORY_PCT="${PROXY_ROUTE_MEMORY_PCT:-85}" \
    PROXY_ROUTE_CLOUD_CONCURRENT="${PROXY_ROUTE_CLOUD_CONCURRENT:-2}" \
    PROXY_ROUTE_FALLBACK_ENABLED="${PROXY_ROUTE_FALLBACK_ENABLED:-true}" \
    PROXY_ROUTE_MAX_CLOUD_FAILS="${PROXY_ROUTE_MAX_CLOUD_FAILS:-3}" \
    PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS="${PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS:-300}" \
    PROXY_ROUTE_STICKY="${PROXY_ROUTE_STICKY:-true}" \
    PROXY_ROUTE_STICKY_RETURN_ROUNDS="${PROXY_ROUTE_STICKY_RETURN_ROUNDS:-5}" \
    PROXY_ROUTE_STICKY_RETURN_RATIO="${PROXY_ROUTE_STICKY_RETURN_RATIO:-0.7}" \
    PROXY_CLOUD_BASE_URL="${PROXY_CLOUD_BASE_URL:-https://api.deepseek.com/v1}" \
    PROXY_CLOUD_MODEL="${PROXY_CLOUD_MODEL:-deepseek-v4-pro}" \
    PROXY_CLOUD_PRICE_INPUT="${PROXY_CLOUD_PRICE_INPUT:-0.5}" \
    PROXY_CLOUD_PRICE_OUTPUT="${PROXY_CLOUD_PRICE_OUTPUT:-1.5}" \
    PROXY_DYNAMIC_MAX_TOKENS_ENABLED="${PROXY_DYNAMIC_MAX_TOKENS_ENABLED:-true}" \
    PROXY_DYNAMIC_MAX_TOKENS_INIT="${PROXY_DYNAMIC_MAX_TOKENS_INIT:-4096}" \
    PROXY_DYNAMIC_MAX_TOKENS_GROWTH="${PROXY_DYNAMIC_MAX_TOKENS_GROWTH:-4096}" \
    PROXY_DYNAMIC_MAX_TOKENS_SATURATION="${PROXY_DYNAMIC_MAX_TOKENS_SATURATION:-2048}" \
    PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO="${PROXY_DYNAMIC_MAX_TOKENS_RAPID_MLX_RATIO:-0.8}" \
    nohup "$proxy_wrapper" </dev/null >/dev/null 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PROXY_PIDFILE"

    info "代理进程已启动 (PID: $new_pid)，等待就绪..."

    local i
    for i in {1..30}; do
        if ! kill -0 "$new_pid" 2>/dev/null; then
            error "代理进程已退出！查看日志: $PROXY_LOGFILE"
            tail -n 30 "$PROXY_LOGFILE" 2>/dev/null
            rm -f "$PROXY_PIDFILE" "$proxy_wrapper"
            return 1
        fi

        if curl -s --max-time 2 "http://$PROXY_HOST:$PROXY_PORT/v1/models" >/dev/null 2>&1; then
            info "✅ anthropic_proxy.py 就绪 (PID: $new_pid)"
            rm -f "$proxy_wrapper"
            return 0
        fi

        sleep 1
        if (( i % 10 == 0 )); then
            echo "  等待代理就绪... ($i/30)"
        fi
    done

    warn "代理启动超时，但进程仍在运行 (PID: $new_pid)"
    warn "查看日志: $PROXY_LOGFILE"
    rm -f "$proxy_wrapper"
    return 1
}

# ============================================================
# 停止代理
# ============================================================
_stop_proxy() {
    local proxy_pid
    if ! proxy_pid=$(_get_proxy_pid 2>/dev/null); then
        return 0
    fi

    info "停止 anthropic_proxy.py (PID: $proxy_pid)..."
    kill "$proxy_pid" 2>/dev/null || true

    local i
    for i in {1..10}; do
        if ! kill -0 "$proxy_pid" 2>/dev/null; then
            rm -f "$PROXY_PIDFILE"
            return 0
        fi
        sleep 1
    done

    kill -9 "$proxy_pid" 2>/dev/null || true
    sleep 1
    rm -f "$PROXY_PIDFILE"
}

# ============================================================
# 启动服务（主入口）
# ============================================================
cmd_start() {
    local pid
    if pid=$(_get_pid 2>/dev/null); then
        # 后端已在运行，检查并启动代理
        info "后端已在运行 (PID: $pid, backend: $(_current_backend))"
        _start_proxy
        return $?
    fi

    case "$LLAMA_BACKEND" in
        rapid-mlx|vllm-mlx)
            _start_rapid_mlx || return 1
            ;;
        mlx_vlm|mlx-vlm)
            _start_mlx_vlm || return 1
            ;;
        cloud|deepseek-cloud|openai-cloud)
            # 云模式：直接启动代理，不启动本地后端
            _start_proxy
            return $?
            ;;
        llama-server|*)
            _start_llama_server || return 1
            ;;
    esac

    # 后端就绪后启动代理
    if _get_pid >/dev/null 2>&1; then
        _start_proxy
        _log_lifecycle_event "service_start" "profile=$(_current_config_name) backend=$LLAMA_BACKEND"
    else
        error "后端进程未运行，跳过代理启动"
        return 1
    fi
}

# ============================================================
# 启动云端代理（DeepSeek / OpenAI 等）
# ============================================================
cmd_start_cloud() {
    # 检查 API Key
    if [[ -z "${LLAMA_API_KEY:-}" ]]; then
        error "未设置 LLAMA_API_KEY，无法启动云端模式"
        error "请先设置环境变量: export LLAMA_API_KEY=\"sk-你的Key\""
        info ""
        info "DeepSeek 注册地址: https://platform.deepseek.com/"
        return 1
    fi

    # 检查是否已设置云 API URL
    if [[ -z "${LLAMA_BASE_URL:-}" ]]; then
        warn "未设置 LLAMA_BASE_URL，使用默认 DeepSeek: https://api.deepseek.com/v1"
        LLAMA_BASE_URL="https://api.deepseek.com/v1"
    fi

    # 检查 URL 是否为云 API
    if [[ ! "$LLAMA_BASE_URL" =~ (deepseek|openai|api\.) ]]; then
        warn "LLAMA_BASE_URL 看起来不像云 API: $LLAMA_BASE_URL"
        warn "确认后继续启动 (3秒)..."
        sleep 3
    fi

    info "启动云端代理模式..."
    info "  后端 URL:   $LLAMA_BASE_URL"
    info "  模型:       ${MODEL_NAME:-deepseek-chat}"
    info "  API Key:    ${LLAMA_API_KEY:0:8}****"
    info "  并发:       ${PROXY_MAX_CONCURRENT:-4}"

    # 停止可能运行的本地后端（避免端口冲突）
    if _get_pid >/dev/null 2>&1; then
        warn "检测到本地后端在运行，先停止..."
        cmd_stop || true
        sleep 1
    fi

    # 启动代理（不启动本地后端）
    if _start_proxy; then
        _log_lifecycle_event "service_start" "profile=$(_current_config_name) backend=cloud base_url=$LLAMA_BASE_URL"
        # 云端 API 健康检查
        info "验证云端 API 可达性..."
        local health_url="${LLAMA_BASE_URL%/}/models"
        local health_rc
        health_rc=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
            -H "Authorization: Bearer $LLAMA_API_KEY" \
            "$health_url" 2>/dev/null || echo "000")
        if [[ "$health_rc" == "200" ]]; then
            info "✅ 云端 API 可达 ($health_url)"
        elif [[ "$health_rc" == "000" ]]; then
            warn "⚠️  云端 API 不可达 ($health_url): 连接失败/超时"
            warn "   代理已启动，但请求可能失败"
        else
            warn "⚠️  云端 API 返回 HTTP $health_rc (预期 200)"
            warn "   请检查 API Key 和 URL 是否正确"
        fi

        info ""
        info "✅ 云端代理已启动"
        info ""
        info "Claude Code 配置命令:"
        info "  export ANTHROPIC_BASE_URL=http://$PROXY_HOST:$PROXY_PORT"
        info "  export ANTHROPIC_AUTH_TOKEN=sk-any"
        info "  cd /your/project && claude"
        info ""
        info "状态页面: http://$PROXY_HOST:$PROXY_PORT/status"
    else
        error "云端代理启动失败"
        return 1
    fi
}

# ============================================================
# 停止服务
# ============================================================
cmd_stop() {
    # 停止 watchdog（如有）
    cmd_stop_watchdog

    # 先停止代理
    _stop_proxy

    # 停止当前运行的任何后端
    local backend pid
    backend=$(_current_backend 2>/dev/null) || true

    if [[ -n "$backend" ]]; then
        pid=$(_get_pid 2>/dev/null) || true
        if [[ -n "$pid" ]]; then
            info "停止 $backend (PID: $pid)..."
            kill "$pid" 2>/dev/null || true

            local i
            for i in {1..15}; do
                if ! kill -0 "$pid" 2>/dev/null; then
                    info "✅ 后端已停止"
                    rm -f "$PIDFILE"
                    return 0
                fi
                sleep 1
            done

            warn "优雅停止超时，强制终止..."
            kill -9 "$pid" 2>/dev/null || true
            sleep 1

            if ! kill -0 "$pid" 2>/dev/null; then
                info "✅ 后端已强制停止"
                rm -f "$PIDFILE"
                _log_lifecycle_event "service_stop" "profile=$(_current_config_name) backend=${backend:-unknown}"
                return 0
            fi

            error "无法停止进程 (PID: $pid)"
            return 1
        fi
    fi

    warn "后端服务未在运行"
    rm -f "$PIDFILE"
    _log_lifecycle_event "service_stop" "profile=$(_current_config_name)"
    return 0
}

# ============================================================
# 查询状态
# ============================================================
cmd_status() {
    local pid backend is_cloud=false

    # 检测是否为云模式
    if [[ -n "${LLAMA_BASE_URL:-}" ]] && [[ "$LLAMA_BASE_URL" =~ (deepseek|openai|api\.) ]]; then
        is_cloud=true
    fi

    if [[ "$is_cloud" == "true" ]]; then
        echo "状态: ${GREEN}云端模式${NC}"
        echo "  后端类型: ${CYAN}云端 API${NC}"
        echo "  API 端点: $LLAMA_BASE_URL"
        echo "  模型:     ${MODEL_NAME:-deepseek-chat}"
        if [[ -n "${LLAMA_API_KEY:-}" ]]; then
            echo "  API Key:  ${LLAMA_API_KEY:0:8}****"
        fi
        echo ""
    else
        # 本地模式：检查后端进程
        if ! pid=$(_get_pid 2>/dev/null); then
            echo "状态: ${RED}未运行${NC}"
            echo "  PID 文件: $PIDFILE"
            echo "  日志文件: $LOGFILE"
            echo "  当前配置: ${CYAN}$(_current_config_name)${NC}"
            return 1
        fi

        backend=$(_current_backend 2>/dev/null || echo "unknown")

        echo "状态: ${GREEN}运行中${NC}"
        echo "  后端:     ${CYAN}$backend${NC}"
        echo "  配置:     ${CYAN}$(_current_config_name)${NC}"
        echo "  PID:      $pid"

        local proc_info
        proc_info=$(ps -p "$pid" -o rss=,etime=,pcpu= 2>/dev/null | awk '{printf "  内存: %.1f GB\n  运行时间: %s\n  CPU: %s%%", $1/1024/1024, $2, $3}')
        echo "$proc_info"

        local api_status
        if curl -s --max-time 3 "http://$LLAMA_HOST:$LLAMA_PORT/v1/models" >/dev/null 2>&1; then
            api_status="${GREEN}正常${NC}"
        else
            api_status="${RED}无响应${NC}"
        fi
        echo "  API ($LLAMA_HOST:$LLAMA_PORT): $api_status"

        local model_info
        model_info=$(curl -s --max-time 3 "http://$LLAMA_HOST:$LLAMA_PORT/v1/models" 2>/dev/null | \
            python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('models',[{}])[0].get('model','?'))" 2>/dev/null || echo "?")
        echo "  模型:     $model_info"

        if [[ -f "$LOGFILE" ]]; then
            local last_log
            last_log=$(tail -n 1 "$LOGFILE" 2>/dev/null | cut -c1-80)
            echo "  最新日志: $last_log"
        fi
        echo ""
    fi

    # 代理状态（通用）
    local proxy_pid proxy_status
    if proxy_pid=$(_get_proxy_pid 2>/dev/null); then
        if curl -s --max-time 3 "http://$PROXY_HOST:$PROXY_PORT/v1/models" >/dev/null 2>&1; then
            proxy_status="${GREEN}运行中${NC}"
        else
            proxy_status="${YELLOW}无响应${NC}"
        fi
        echo "代理 (anthropic_proxy.py):"
        echo "  状态:     $proxy_status"
        echo "  PID:      $proxy_pid"
        echo "  地址:     http://$PROXY_HOST:$PROXY_PORT"
    else
        echo "代理 (anthropic_proxy.py): ${RED}未运行${NC}"
    fi
}

# ============================================================
# 重启服务
# ============================================================
cmd_restart() {
    _log_lifecycle_event "service_restart" "profile=$(_current_config_name) reason=manual"
    cmd_stop || true
    # 等待端口完全释放，避免 SO_REUSEADDR 等待期间的竞争
    local waited=0
    while lsof -Pi ":$LLAMA_PORT" -sTCP:LISTEN -t >/dev/null 2>&1 || lsof -Pi ":$PROXY_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; do
        if (( waited >= 5 )); then
            warn "端口仍被占用，继续强制启动..."
            break
        fi
        echo "  等待端口释放... ($waited/5s)"
        sleep 1
        waited=$((waited + 1))
    done
    cmd_start
}

# ============================================================
# 查看日志
# ============================================================
cmd_logs() {
    if [[ -f "$LOGFILE" ]]; then
        tail -n "${1:-50}" "$LOGFILE"
    else
        warn "日志文件不存在: $LOGFILE"
    fi
}

# ============================================================
# 查看代理日志
# ============================================================
cmd_proxy_logs() {
    if [[ -f "$PROXY_LOGFILE" ]]; then
        tail -n "${1:-50}" "$PROXY_LOGFILE"
    else
        warn "日志文件不存在: $PROXY_LOGFILE"
    fi
}

# ============================================================
# 列出所有配置
# ============================================================
cmd_list() {
    echo "可用配置:"
    echo ""

    local active
    active=$(_current_config_name)

    for conf in "$CONFIG_DIR"/*.conf; do
        [[ -f "$conf" ]] || continue
        [[ "$(basename "$conf")" == "active.conf" ]] && continue

        local name desc memory backend marker
        name=$(basename "$conf" .conf)
        desc=$(grep "^CONFIG_DESC=" "$conf" 2>/dev/null | cut -d'"' -f2 || echo "-")
        memory=$(grep "^CONFIG_MEMORY=" "$conf" 2>/dev/null | cut -d'"' -f2 || echo "-")
        backend=$(grep "^LLAMA_BACKEND=" "$conf" 2>/dev/null | cut -d'"' -f2 || echo "llama-server")

        if [[ "$name" == "$active" ]]; then
            marker="${GREEN}● 当前激活${NC}"
        else
            marker="  "
        fi

        echo -e "  ${CYAN}$name${NC} $marker"
        echo "    后端: $backend"
        echo "    用途: $desc"
        echo "    内存: $memory"
        echo ""
    done
}

# ============================================================
# 模型目录（configs/models.json）查看与校验
# ============================================================
cmd_models() {
    python3 - <<'PYEOF'
import json, os, re, sys

sys.path.insert(0, os.getcwd())
import model_registry

# Key readiness: os.environ (manage.sh exports) + secret/active conf parsing.
_CONF_KEYS = {}
for _path in ("configs/secret.local.conf", "configs/active.conf"):
    try:
        for _line in open(_path, encoding="utf-8"):
            m = re.match(r'^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)="([^"]*)"', _line)
            if m:
                _CONF_KEYS.setdefault(m.group(1), m.group(2))
    except OSError:
        pass

def key_set(key_env):
    if not key_env:
        return False
    return bool(os.environ.get(key_env) or _CONF_KEYS.get(key_env))

from_file = model_registry.load()
err = model_registry.last_error()
hash_ = model_registry.catalog_hash()

print("模型目录 (Model Catalog)")
print("=" * 60)
if err:
    print(f"  ⚠ 目录错误: {err}")
    print(f"  来源: {'synthesized(兼容合成)' if not from_file else 'file'}")
else:
    print(f"  来源: {'file (configs/models.json)' if from_file else 'synthesized (无目录文件, 兼容合成)'}")
print(f"  hash: {hash_}")
print()

print("提供商 (providers)")
for name in model_registry.list_providers():
    p = model_registry.get_provider(name) or {}
    url = p.get("base_url") or ("$" + p.get("base_url_env", "?"))
    cc = p.get("concurrent") or ("$" + p.get("concurrent_env", "?"))
    ks = key_set(p.get("key_env", ""))
    proto = (" [" + p["protocol"] + "]") if p.get("protocol", "openai") != "openai" else ""
    print(f"  {name:10} key={'✓' if ks else '✗'}({p.get('key_env','')}) concurrent={cc}{proto}")
    print(f"             {url}")
    if p.get("anthropic_base_url"):
        print(f"             anthropic: {p['anthropic_base_url']}")
print()

print("模型 (models)")
for name in model_registry.list_models():
    m = model_registry.get_model(name) or {}
    caps = m.get("capabilities") or {}
    price = m.get("price")
    price_s = f"¥{price['input']}/{price['output']}" if price else "-"
    print(f"  {name:28} {m.get('provider',''):9} {m.get('tier',''):9} "
          f"ctx={caps.get('context_tokens') or '-':>8} think={caps.get('thinking') or '-':10} {price_s}")
print()

print("路由 (routes: 别名 → 云端模型)")
for alias, r in model_registry.build_route_preferences().items():
    chain = " → ".join([r["cloud_model"]] + r.get("fallback_models", []))
    print(f"  {alias:22} {r['route_bias']:13} {r['behavior']:15} {chain}")
print()

d = model_registry._catalog().get("defaults", {})
print(f"defaults: cloud_model={d.get('cloud_model')} daily_budget={d.get('daily_budget')}")
PYEOF
}

cmd_models_validate() {
    python3 - <<'PYEOF'
import os, sys
sys.path.insert(0, os.getcwd())
import model_registry

model_registry._reset()
from_file = model_registry.load()
err = model_registry.last_error()
if err:
    print(f"✗ 目录校验失败: {err}")
    if not from_file:
        print("  (当前以兼容合成目录运行；请修复 configs/models.json 后重试)")
    sys.exit(1)
if not from_file:
    print("⚠ configs/models.json 不存在 — 将使用兼容合成目录（行为等价，但无法配置多云模型）")
else:
    print(f"✓ configs/models.json 校验通过 (hash={model_registry.catalog_hash()})")
sys.exit(0)
PYEOF
}

# ============================================================
# 切换配置
# ============================================================
cmd_switch() {
    local target="${1:-}"

    if [[ -z "$target" ]]; then
        error "请指定配置名称"
        echo ""
        cmd_list
        echo ""
        echo "用法: ./manage.sh switch <配置名>"
        return 1
    fi

    local target_file="$CONFIG_DIR/$target.conf"
    if [[ ! -f "$target_file" ]]; then
        error "配置不存在: $target"
        echo ""
        cmd_list
        return 1
    fi

    local pid
    if pid=$(_get_pid 2>/dev/null); then
        warn "服务正在运行 (PID: $pid, backend: $(_current_backend))"
        if [[ -t 0 ]]; then
            # Interactive: ask whether to stop
            warn "切换配置后需重启或 reload 生效"
            read -p "是否先停止服务再切换? [Y/n] " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                cmd_stop || true
            fi
        else
            # Non-interactive: just switch the symlink, don't stop
            info "非交互模式: 仅切换配置软链接，不停止服务"
            info "切换后请执行 ./manage.sh reload (热重载) 或 restart (重启)"
        fi
    fi

    local old_profile
    old_profile=$(_current_config_name)
    ln -sf "$target.conf" "$ACTIVE_CONF"
    info "配置已切换为: ${CYAN}$target${NC}"
    _log_lifecycle_event "profile_switch" "from=$old_profile to=$target"

    _load_config
    echo ""
    echo "配置详情:"
    echo "  后端: $LLAMA_BACKEND"
    echo "  模型: $LLAMA_MODEL"
    if [[ "$LLAMA_BACKEND" == "rapid-mlx" ]]; then
        echo "  KV量化: $RAPID_MLX_KV_QUANTIZATION ($RAPID_MLX_KV_QUANT_BITS-bit)"
    else
        echo "  上下文: $LLAMA_CTX"
        echo "  KV: K=$LLAMA_KV_K, V=$LLAMA_KV_V"
    fi
    echo "  采样: temp=$LLAMA_TEMP, top-p=$LLAMA_TOP_P"
    echo ""
    echo "运行 ./manage.sh start 启动服务"
}

# ============================================================
# 热重载代理配置（SIGHUP，不重启 proxy 进程）
# ============================================================
cmd_reload() {
    local proxy_pid
    if ! proxy_pid=$(_get_proxy_pid 2>/dev/null); then
        error "代理未运行，无法热重载"
        error "请先 ./manage.sh start 启动服务"
        return 1
    fi

    # 重新加载配置文件（active.conf 可能已被 switch 更新）
    _load_config

    info "热重载代理配置 (PID: $proxy_pid)..."
    info "  配置: $(_current_config_name)"
    info "  后端: ${LLAMA_BACKEND:-llama-server}"

    # 发送 SIGHUP 信号，触发 proxy 的 _reload_config()
    if ! kill -HUP "$proxy_pid" 2>/dev/null; then
        error "SIGHUP 发送失败 (PID: $proxy_pid)"
        return 1
    fi

    _log_lifecycle_event "config_reload" "profile=$(_current_config_name) backend=${LLAMA_BACKEND:-llama-server} pid=$proxy_pid"

    info "✅ SIGHUP 已发送，代理将在下个请求间隙重载配置"
    info "  无需重启进程，正在处理的请求不受影响"

    # 等待代理重载完成（最多 30 秒），优先使用 /api/status，旧版代理回退 /v1/models
    local i ready_url="http://$PROXY_HOST:$PROXY_PORT/api/status"
    for i in {1..30}; do
        if ! kill -0 "$proxy_pid" 2>/dev/null; then
            error "代理在重载过程中退出"
            return 1
        fi
        if curl -s --max-time 2 "$ready_url" >/dev/null 2>&1; then
            info "✅ 代理热重载完成"
            break
        fi
        if (( i == 5 )) && ! curl -s --max-time 2 "http://$PROXY_HOST:$PROXY_PORT/v1/models" >/dev/null 2>&1; then
            # 旧版代理可能没有 /api/status，fallback 到 /v1/models
            ready_url="http://$PROXY_HOST:$PROXY_PORT/v1/models"
        fi
        if (( i == 30 )); then
            warn "代理重载确认超时，但进程仍在运行 (PID: $proxy_pid)"
            warn "  请通过 ./manage.sh status 或 curl $ready_url 手动确认"
            return 0
        fi
        sleep 1
    done

    info ""
    info "重载内容:"
    info "  - 后端路由 (LLAMA_BASE_URL, BACKEND_TYPE, MODEL_NAME)"
    info "  - 并发控制 (PROXY_MAX_CONCURRENT + Semaphore 重建)"
    info "  - 上下文管理 (clearing/truncation/lifecycle 阈值)"
    info "  - 工具过滤/循环检测/blocker 等"
    info ""
    info "注意: 本地模型的启停需独立操作:"
    info "  ./manage.sh start-backend   启动本地模型"
    info "  ./manage.sh stop-backend    停止本地模型(释放 GPU 内存)"
    info "  云端模式无需本地模型"
}

# ============================================================
# 启动本地后端（独立于代理，用于热切换场景）
# ============================================================
cmd_start_backend() {
    local pid
    if pid=$(_get_pid 2>/dev/null); then
        warn "本地后端已在运行 (PID: $pid)"
        return 0
    fi

    _load_config
    info "启动本地后端 (独立模式)..."
    info "  配置: $(_current_config_name)"

    case "$LLAMA_BACKEND" in
        rapid-mlx|vllm-mlx)
            _start_rapid_mlx || return 1
            ;;
        mlx_vlm|mlx-vlm)
            _start_mlx_vlm || return 1
            ;;
        cloud|deepseek-cloud|openai-cloud)
            error "当前配置为云端模式 ($LLAMA_BACKEND)，无本地后端可启动"
            error "请先 ./manage.sh switch <local-config> 切换到本地配置"
            return 1
            ;;
        llama-server|*)
            _start_llama_server || return 1
            ;;
    esac

    info "✅ 本地后端已启动"
    _log_lifecycle_event "service_start" "profile=$(_current_config_name) backend=$LLAMA_BACKEND component=backend"
    info "  如需代理也使用此后端，请: ./manage.sh reload"
}

# ============================================================
# 停止本地后端（独立于代理，释放 GPU 内存）
# ============================================================
cmd_stop_backend() {
    # 停止 watchdog（如有）
    cmd_stop_watchdog

    local backend pid
    backend=$(_current_backend 2>/dev/null) || true

    if [[ -z "$backend" ]]; then
        warn "没有本地后端在运行"
        return 0
    fi

    pid=$(_get_pid 2>/dev/null) || true
    if [[ -z "$pid" ]]; then
        warn "本地后端未在运行"
        rm -f "$PIDFILE"
        return 0
    fi

    info "停止本地后端 $backend (PID: $pid)..."

    kill "$pid" 2>/dev/null || true
    local i
    for i in {1..15}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            info "✅ 本地后端已停止 (GPU 内存已释放)"
            rm -f "$PIDFILE"
            _log_lifecycle_event "service_stop" "profile=$(_current_config_name) backend=$backend component=backend"
            return 0
        fi
        sleep 1
    done

    warn "优雅停止超时，强制终止..."
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        info "✅ 本地后端已强制停止"
        rm -f "$PIDFILE"
        _log_lifecycle_event "service_stop" "profile=$(_current_config_name) backend=$backend component=backend"
        return 0
    fi

    error "无法停止本地后端 (PID: $pid)"
    return 1
}

# ============================================================
# 显示当前配置
# ============================================================
cmd_current() {
    local name desc
    name=$(_current_config_name)
    desc=$(grep "^CONFIG_DESC=" "$ACTIVE_CONF" 2>/dev/null | cut -d'"' -f2 || echo "-")

    echo -e "当前配置: ${CYAN}$name${NC}"
    echo "  描述: $desc"
    echo "  后端: $LLAMA_BACKEND"
    echo ""
    echo "环境变量:"
    echo "  LLAMA_BACKEND=$LLAMA_BACKEND"
    echo "  LLAMA_MODEL=$LLAMA_MODEL"
    echo "  LLAMA_PORT=$LLAMA_PORT"
    if [[ "$LLAMA_BACKEND" == "rapid-mlx" ]]; then
        echo "  RAPID_MLX_KV_QUANTIZATION=$RAPID_MLX_KV_QUANTIZATION"
        echo "  RAPID_MLX_KV_QUANT_BITS=$RAPID_MLX_KV_QUANT_BITS"
        echo "  RAPID_MLX_TOOL_PARSER=$RAPID_MLX_TOOL_PARSER"
        echo "  RAPID_MLX_ENABLE_PREFIX_CACHE=$RAPID_MLX_ENABLE_PREFIX_CACHE"
    else
        echo "  LLAMA_CTX=$LLAMA_CTX"
        echo "  LLAMA_KV_K=$LLAMA_KV_K, LLAMA_KV_V=$LLAMA_KV_V"
    fi
    echo "  LLAMA_TEMP=$LLAMA_TEMP, LLAMA_TOP_P=$LLAMA_TOP_P"
    echo "  LLAMA_THINKING=$LLAMA_THINKING"
    echo "  LLAMA_EXTRA_ARGS=$LLAMA_EXTRA_ARGS"
}

# ============================================================
# 检测模型家族（Coder vs 通用 Qwen）
# ============================================================
_detect_model_family() {
    local model_dir="$1"

    # 1. 检查目录名（不区分大小写）
    if echo "$model_dir" | grep -qi 'coder'; then
        echo "coder"
        return
    fi

    # 2. 检查 tokenizer_config.json 中的模型名
    local tc="$model_dir/tokenizer_config.json"
    if [[ -f "$tc" ]]; then
        if grep -qi 'coder' "$tc" 2>/dev/null; then
            echo "coder"
            return
        fi
    fi

    # 3. 默认通用 Qwen
    echo "qwen"
}

# ============================================================
# 修复 Chat Template (DEF-007)
# 用法:
#   ./manage.sh fix-template <model_dir>           自动检测模型类型
#   ./manage.sh fix-template <model_dir> --coder    强制 Coder 完整模板
#   ./manage.sh fix-template <model_dir> --minimal  强制通用最小模板
#   ./manage.sh fix-template                        列出已缓存的模型
# ============================================================
cmd_fix_template() {
    local model_dir="${1:-}"
    local type_flag="${2:-}"

    if [[ -z "$model_dir" ]]; then
        error "用法: ./manage.sh fix-template <model_dir> [--coder|--minimal]"
        error "  model_dir: HuggingFace 模型目录 (如 ~/.cache/huggingface/hub/models--*/snapshots/*)"
        info ""
        info "搜索已缓存的 Qwen 模型:"
        find ~/.cache/huggingface/hub -name "chat_template.jinja" -type f 2>/dev/null | while read -r f; do
            local dir family
            dir=$(dirname "$f")
            family=$(_detect_model_family "$dir")
            if [[ "$family" == "coder" ]]; then
                echo "  ${CYAN}[Coder]${NC} $dir"
            else
                echo "  [Qwen] $dir"
            fi
        done
        return 1
    fi

    # 确定模型家族
    local family
    case "$type_flag" in
        --coder)   family="coder" ;;
        --minimal) family="qwen" ;;
        "")
            family=$(_detect_model_family "$model_dir")
            ;;
        *)
            error "未知选项: $type_flag"
            error "支持: --coder (Coder完整模板), --minimal (通用最小模板)"
            return 1
            ;;
    esac

    # 选择模板文件
    local template_src template_name
    case "$family" in
        coder)
            # Qwen3.6 需要 thinking 开关逻辑，使用专用模板
            if echo "$model_dir" | grep -qi '3\.6\|3_6\|36'; then
                template_src="$SCRIPT_DIR/assets/chat-templates/qwen3.6-fixed.jinja"
                template_name="qwen3.6-fixed.jinja (3.6 专用: thinking开关 + tools + vision)"
            else
                template_src="$SCRIPT_DIR/assets/chat-templates/qwen3-coder-fixed.jinja"
                template_name="qwen3-coder-fixed.jinja (Coder 完整版: tools + vision + 错误检测)"
            fi
            ;;
        qwen|*)
            template_src="$SCRIPT_DIR/assets/chat-templates/qwen3-fixed.jinja"
            template_name="qwen3-fixed.jinja (通用最小版: system/developer 修复)"
            ;;
    esac

    if [[ ! -f "$template_src" ]]; then
        error "修复模板不存在: $template_src"
        return 1
    fi

    local target="$model_dir/chat_template.jinja"
    if [[ ! -e "$target" ]]; then
        warn "目标文件不存在: $target"
        info "将创建新文件"
    else
        # 创建备份
        local backup="${target}.bak.$(date +%Y%m%d_%H%M%S)"
        cp "$target" "$backup"
        info "已备份: $backup"
    fi

    # 如果是软链接，先删除
    if [[ -L "$target" ]]; then
        info "删除旧软链接: $target -> $(readlink "$target")"
        rm -f "$target"
    fi

    cp "$template_src" "$target"
    info "已修复: $target"
    info "模板类型: ${CYAN}${family}${NC}"
    info "模板文件: $template_name"
    info "请重启服务以生效: ./manage.sh restart"
}

# ============================================================
# Watchdog 状态持久化 (R5)
# ============================================================
_write_watchdog_state() {
    local enabled="${1:-true}"
    local last_restart_at="${2:-}"
    local restart_count_1h="${3:-0}"
    local last_failure_reason="${4:-}"
    local pid="${5:-$$}"
    mkdir -p "$SCRIPT_DIR/logs"
    cat > "$WATCHDOG_STATE_PATH" <<EOF
{
  "enabled": $enabled,
  "pid": $pid,
  "last_restart_at": "$last_restart_at",
  "restart_count_1h": $restart_count_1h,
  "last_failure_reason": "$last_failure_reason"
}
EOF
}

# ============================================================
# Watchdog: 监控后端健康，性能衰减时自动重启
# ============================================================
cmd_watchdog() {
    local interval="${WATCHDOG_INTERVAL:-60}"
    local threshold="${WATCHDOG_TOK_THRESHOLD:-15}"
    local consecutive_fail=0
    local max_fail="${WATCHDOG_MAX_FAIL:-3}"
    local restart_count=0
    local restart_window=$(date +%s)

    # 支持 daemon 模式: ./manage.sh watchdog --daemon
    if [[ "$1" == "--daemon" ]]; then
        # Fork into background with proper daemonization
        (
            trap '' HUP
            exec </dev/null
            exec >> "$WATCHDOG_LOGFILE" 2>&1
            trap 'rm -f "$WATCHDOG_PIDFILE"; _write_watchdog_state false "" 0 "" ""' EXIT

            info "Watchdog 后台运行 (PID: $$)"
            info "Watchdog 启动 (间隔=${interval}s, 阈值=${threshold} tok/s, 连续失败=${max_fail})"
            info "  后端: ${LLAMA_BACKEND:-rapid-mlx}:${LLAMA_PORT:-8081}"

            _watchdog_loop "$interval" "$threshold" "$max_fail"
        ) &
        disown
        local daemon_pid=$!
        echo "$daemon_pid" > "$WATCHDOG_PIDFILE"
        _write_watchdog_state true "" 0 "" "$daemon_pid"
        info "Watchdog 已后台启动 (PID: $daemon_pid)"
        sleep 0.3
        exit 0
    fi

    _write_watchdog_state true "" 0 "" $$

    info "Watchdog 启动 (间隔=${interval}s, 阈值=${threshold} tok/s, 连续失败=${max_fail})"
    info "  后端: ${LLAMA_BACKEND:-rapid-mlx}:${LLAMA_PORT:-8081}"
    _watchdog_loop "$interval" "$threshold" "$max_fail"
}

_watchdog_loop() {
    local interval="$1"
    local threshold="$2"
    local max_fail="$3"
    local consecutive_fail=0
    local restart_count=0
    local restart_window
    restart_window=$(date +%s)

    while true; do
        sleep "$interval"

        local now
        now=$(date +%s)
        if (( now - restart_window > 3600 )); then
            restart_count=0
            restart_window=$now
        fi

        local pid
        if ! pid=$(_get_pid 2>/dev/null); then
            if (( restart_count >= 6 )); then
                error "每小时重启超过 6 次，停止 watchdog"
                break
            fi
            warn "后端未运行,尝试重启..."
            _watchdog_do_restart "$restart_count" "backend_down"
            restart_count=$((restart_count + 1))
            consecutive_fail=0
            continue
        fi

        local health
        health=$(curl -s --max-time 5 "http://${LLAMA_HOST:-127.0.0.1}:${LLAMA_PORT:-8081}/v1/models" 2>/dev/null)
        if [[ -z "$health" ]]; then
            consecutive_fail=$((consecutive_fail + 1))
            warn "后端健康检查失败 ($consecutive_fail/$max_fail)"
            if (( consecutive_fail >= max_fail )); then
                if (( restart_count >= 6 )); then
                    error "每小时重启超过 6 次，停止 watchdog"
                    break
                fi
                error "后端连续 $max_fail 次无响应,自动重启"
                _watchdog_do_restart "$restart_count" "backend_unresponsive"
                restart_count=$((restart_count + 1))
                consecutive_fail=0
            fi
            continue
        fi
        consecutive_fail=0

        local metrics_line
        metrics_line=$(grep -a "prompt_n\|predicted_n\|tok/s" "$SCRIPT_DIR/logs/llama-server.log" 2>/dev/null | tail -1)
        if [[ -n "$metrics_line" ]]; then
            local tok_s
            tok_s=$(echo "$metrics_line" | grep -oE '[0-9]+\.[0-9]+ tok/s' | grep -oE '[0-9]+\.[0-9]+' | tail -1)
            if [[ -n "$tok_s" ]]; then
                if (( $(echo "$tok_s < $threshold" | bc -l 2>/dev/null || echo 0) )); then
                    if (( restart_count >= 6 )); then
                        error "每小时重启超过 6 次，停止 watchdog"
                        break
                    fi
                    warn "性能衰减: ${tok_s} tok/s < ${threshold} tok/s,重启后端..."
                    _watchdog_do_restart "$restart_count" "performance_degradation"
                    restart_count=$((restart_count + 1))
                fi
            fi
        fi
    done
}

_watchdog_do_restart() {
    local restart_count="$1"
    local failure_reason="$2"
    local mypid=${BASHPID:-$$}

    # 获取管理锁, 避免与 agent_go 修复操作并发重启
    if _acquire_manage_lock 5; then
        # 二次检查: 持锁期间状态可能已改变
        if ! _get_pid >/dev/null 2>&1 || [[ -z "$(curl -s --max-time 5 "http://${LLAMA_HOST:-127.0.0.1}:${LLAMA_PORT:-8081}/v1/models" 2>/dev/null)" ]]; then
            cmd_restart
            _write_watchdog_state true "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$((restart_count + 1))" "$failure_reason" "$mypid"
            _log_lifecycle_event "watchdog_auto_restart" "profile=$(_current_config_name) reason=$failure_reason restart_count=$((restart_count + 1))"
        else
            info "watchdog 获取锁后发现后端已恢复, 跳过重启"
        fi
        _release_manage_lock
    else
        warn "watchdog 无法获取管理锁, 跳过本次自动重启"
    fi
}

cmd_stop_watchdog() {
    if [[ -f "$WATCHDOG_PIDFILE" ]]; then
        local pid
        pid=$(cat "$WATCHDOG_PIDFILE" 2>/dev/null) || return 0
        if kill -0 "$pid" 2>/dev/null; then
            info "停止 watchdog (PID: $pid)..."
            kill "$pid" 2>/dev/null || true
            rm -f "$WATCHDOG_PIDFILE"
            _write_watchdog_state false "" 0 "" $$
            info "✅ Watchdog 已停止"
        else
            warn "Watchdog 未在运行，清理 PID 文件"
            rm -f "$WATCHDOG_PIDFILE"
            _write_watchdog_state false "" 0 "" $$
        fi
    else
        warn "Watchdog 未运行"
        _write_watchdog_state false "" 0 "" $$
    fi
}

# ============================================================
# Watchdog 状态查询 (R5): 输出 JSON
# ============================================================
cmd_watchdog_status() {
    python3 - <<PY
import json, os, sys

state_path = "$WATCHDOG_STATE_PATH"
result = {
    "enabled": False,
    "running": False,
    "pid": None,
    "last_restart_at": "",
    "restart_count_1h": 0,
    "last_failure_reason": "",
}

try:
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
except (OSError, json.JSONDecodeError):
    pass
else:
    result["enabled"] = bool(state.get("enabled", False))
    result["pid"] = state.get("pid")
    result["last_restart_at"] = state.get("last_restart_at", "")
    result["restart_count_1h"] = int(state.get("restart_count_1h", 0))
    result["last_failure_reason"] = state.get("last_failure_reason", "")

pid = result["pid"]
if pid:
    try:
        os.kill(int(pid), 0)
        result["running"] = True
    except (OSError, ValueError):
        pass

print(json.dumps(result, ensure_ascii=False))
PY
}

# ============================================================
# 智能路由管理命令
# ============================================================
cmd_route_force_local() {
    local sid="${1:-}"
    if [[ -z "$sid" ]]; then
        error "Usage: ./manage.sh route-force-local <session_id>"
        return 1
    fi
    info "Forcing session $sid to local..."
    curl -sf -X POST "http://127.0.0.1:${PORT:-4000}/admin/route/force-local" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$sid\"}" && echo "" || warn "Proxy not reachable on port ${PORT:-4000}"
}

cmd_route_force_cloud() {
    local sid="${1:-}"
    if [[ -z "$sid" ]]; then
        error "Usage: ./manage.sh route-force-cloud <session_id>"
        return 1
    fi
    info "Forcing session $sid to cloud..."
    curl -sf -X POST "http://127.0.0.1:${PORT:-4000}/admin/route/force-cloud" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$sid\"}" && echo "" || warn "Proxy not reachable on port ${PORT:-4000}"
}

# ============================================================
# 快速启动向导
# ============================================================
cmd_wizard() {
    echo ""
    echo -e "  ${CYAN}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "  ${CYAN}║${NC}       ${BOLD}llama.cpp 快速启动向导${NC}                  ${CYAN}║${NC}"
    echo -e "  ${CYAN}╚══════════════════════════════════════════════════╝${NC}"
    echo ""

    # --- 步骤 1: 选择运行模式 ---
    echo -e "${BOLD}步骤 1/4: 选择运行模式${NC}"
    echo "  1) 本地模式 (推荐) — 使用本地 GPU 运行模型，免费且隐私安全"
    echo "  2) 云端模式 — 使用 DeepSeek/OpenAI API，按 token 付费"
    echo ""
    local mode=""
    while [[ -z "$mode" ]]; do
        read -p "  请输入编号 [1/2]: " mode_choice
        case "$mode_choice" in
            1) mode="local"; echo "" ;;
            2) mode="cloud"; echo "" ;;
            *) echo "  请输入 1 或 2" ;;
        esac
    done

    # --- 步骤 2: 选择配置 ---
    echo -e "${BOLD}步骤 2/4: 选择配置${NC}"
    echo ""

    local configs=()
    local config_names=()
    local config_descs=()
    local config_memories=()
    local config_backends=()
    local idx=0

    for conf in "$CONFIG_DIR"/*.conf; do
        [[ -f "$conf" ]] || continue
        [[ "$(basename "$conf")" == "active.conf" ]] && continue
        [[ "$(basename "$conf")" == "secret.local.conf" ]] && continue

        local name desc memory backend
        name=$(basename "$conf" .conf)
        desc=$(grep "^CONFIG_DESC=" "$conf" 2>/dev/null | cut -d'"' -f2 || echo "-")
        memory=$(grep "^CONFIG_MEMORY=" "$conf" 2>/dev/null | cut -d'"' -f2 || echo "-")
        backend=$(grep "^LLAMA_BACKEND=" "$conf" 2>/dev/null | cut -d'"' -f2 || echo "llama-server")

        # 云端模式只显示 cloud 配置，本地模式只显示本地配置
        if [[ "$mode" == "cloud" ]]; then
            if [[ "$backend" != "cloud" && "$backend" != "deepseek-cloud" && "$backend" != "openai-cloud" ]]; then
                continue
            fi
        else
            if [[ "$backend" == "cloud" || "$backend" == "deepseek-cloud" || "$backend" == "openai-cloud" ]]; then
                continue
            fi
        fi

        configs[$idx]="$conf"
        config_names[$idx]="$name"
        config_descs[$idx]="$desc"
        config_memories[$idx]="$memory"
        config_backends[$idx]="$backend"
        idx=$((idx + 1))
    done

    if [[ ${#configs[@]} -eq 0 ]]; then
        if [[ "$mode" == "cloud" ]]; then
            error "未找到云端配置 (deepseek-chat.conf)"
            info "请确认 configs/ 目录下存在云端配置文件"
        else
            error "未找到本地配置"
            info "请确认 configs/ 目录下存在本地配置文件"
        fi
        return 1
    fi

    for ((i=0; i<${#configs[@]}; i++)); do
        local marker=""
        local active_name
        active_name=$(_current_config_name)
        if [[ "${config_names[$i]}" == "$active_name" ]]; then
            marker=" ${GREEN}(当前)${NC}"
        fi
        echo -e "  $((i+1))) ${CYAN}${config_names[$i]}${NC}$marker"
        echo -e "     后端: ${config_backends[$i]}  |  用途: ${config_descs[$i]}  |  内存: ${config_memories[$i]}"
    done
    echo ""

    local selected_idx=""
    while [[ -z "$selected_idx" ]]; do
        read -p "  请输入编号 [1-${#configs[@]}]: " choice
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#configs[@]} )); then
            selected_idx=$((choice - 1))
        else
            echo "  请输入 1 到 ${#configs[@]} 之间的编号"
        fi
    done
    echo ""

    local selected_name="${config_names[$selected_idx]}"

    # --- 步骤 3: 选择 Profile ---
    echo -e "${BOLD}步骤 3/4: 选择压缩策略 (Profile)${NC}"
    echo "  1) balanced (推荐) — 日常 coding，兼顾质量与性能"
    echo "     压缩: smart | 截断: fifo | 保留: 40 轮对话"
    echo ""
    echo "  2) aggressive — 长上下文场景，优先控制 token 消耗"
    echo "     压缩: aggressive | 截断: fifo | 保留: 30 轮对话"
    echo ""
    echo "  3) conservative — 质量敏感任务，保留上下文完整性"
    echo "     压缩: conservative | 截断: rounds | 保留: 50 轮对话"
    echo ""

    local profile="balanced"
    while true; do
        read -p "  请输入编号 [1/2/3] (默认 1): " profile_choice
        case "${profile_choice:-1}" in
            1) profile="balanced"; break ;;
            2) profile="aggressive"; break ;;
            3) profile="conservative"; break ;;
            *) echo "  请输入 1, 2 或 3" ;;
        esac
    done
    echo ""

    # --- 步骤 4: 确认并启动 ---
    echo -e "${BOLD}步骤 4/4: 确认配置${NC}"
    echo "  配置:     ${CYAN}$selected_name${NC}"
    echo "  后端:     ${config_backends[$selected_idx]}"
    echo "  用途:     ${config_descs[$selected_idx]}"
    if [[ "$mode" == "local" ]]; then
        echo "  内存需求: ${config_memories[$selected_idx]}"
    fi
    echo "  Profile:  ${CYAN}$profile${NC}"
    echo ""

    read -p "  确认启动? [Y/n] " confirm
    if [[ "$confirm" =~ ^[Nn]$ ]]; then
        info "已取消"
        return 0
    fi
    echo ""

    # 执行切换和启动
    info "切换到配置: $selected_name"
    cmd_switch "$selected_name"

    export PROXY_COMPRESSION_PROFILE="$profile"

    if [[ "$mode" == "cloud" ]]; then
        info "启动云端模式..."
        cmd_start_cloud
    else
        info "启动本地模式..."
        cmd_start
    fi
}

# ============================================================
# 帮助信息
# ============================================================
cmd_help() {
    cat <<EOF
llama.cpp / Rapid-MLX 服务管理脚本

用法: ./manage.sh <命令> [选项]
       ./manage.sh --profile <profile> <命令>   # 指定压缩策略

全局选项:
  --profile <profile>   压缩策略: balanced (默认), aggressive, conservative
                        适用于: start, start-cloud, start-backend, restart

服务命令:
  start                启动后端和代理（根据当前配置）
  start-cloud          启动云端代理（DeepSeek/OpenAI，无需本地后端）
  stop                 停止后端和代理
  status               查询后端和代理状态
  restart              重启后端和代理
  reload               热重载代理配置（SIGHUP，不重启进程）
  start-backend        仅启动本地模型（独立于代理，用于热切换）
  stop-backend         仅停止本地模型（释放 GPU 内存）
  watchdog [--daemon]  监控后端健康状态，性能衰减时自动重启（--daemon 后台运行）
  stop-watchdog        停止 watchdog 后台进程
  watchdog-status      查看 watchdog 运行状态 (JSON)
  logs [N]             查看最后 N 行后端日志 (默认 50)
  proxy-logs [N]       查看最后 N 行代理日志 (默认 50)

调用契约:
  所有变更命令 (start/stop/restart/start-backend/stop-backend/switch/reload)
  在非交互模式下均不阻塞等待输入; 成功返回 0, 失败返回非 0;
  start/start-backend/reload/switch 具有幂等性; 命令内部包含硬超时保护。
  变更命令之间通过 .manage.lock 互斥, 防止 agent_go 与 watchdog 并发操作。

配置命令:
  list                 列出所有可用配置
  switch <name>        切换到指定配置（切换后需 reload 或 restart 生效）
  current              显示当前配置详情

模型目录:
  models               显示模型目录（providers/models/routes、key 就绪状态、hash）
  models-validate      校验 configs/models.json（坏文件非零退出）

快速启动:
  wizard               交互式向导，引导完成首次启动配置

维护命令:
  fix-template <dir>   修复模型的 chat_template (防止 system message 崩溃)

支持的后端:
  llama-server         标准 llama.cpp 后端 (GGUF)
  rapid-mlx            Rapid-MLX 后端 (MLX, Apple 优化)

配置文件位置: configs/*.conf
当前激活配置: configs/active.conf (软链接)

示例:
  ./manage.sh list                    # 查看所有配置
  ./manage.sh switch rapid-mlx-35b    # 切换到 Rapid-MLX
  ./manage.sh start                   # 用当前配置启动
  ./manage.sh start --profile aggressive  # 激进压缩模式启动
  ./manage.sh start-cloud             # 启动云端代理（DeepSeek）
  ./manage.sh restart                 重启（应用新配置，停启本地模型）
  ./manage.sh reload                  # 热重载（SIGHUP，不重启 proxy，~0.5s）
  ./manage.sh status                  # 查看运行状态
  ./manage.sh wizard                  # 快速启动向导

热切换示例（本地↔云端，不重启代理）:
  ./manage.sh switch deepseek-chat && ./manage.sh reload   # 本地→云端
  ./manage.sh stop-backend                                  # 可选:释放本地模型内存
  ./manage.sh switch rapid-mlx-35b && ./manage.sh reload   # 云端→本地
  ./manage.sh start-backend                                 # 启动本地模型

EOF
}

# ============================================================
# 解析 --profile 全局参数
# ============================================================
_parse_global_flags() {
    local args=("$@")
    local result=()
    local skip_next=false

    for ((i=0; i<${#args[@]}; i++)); do
        if $skip_next; then
            skip_next=false
            continue
        fi
        case "${args[$i]}" in
            --profile)
                local val="${args[$((i+1))]:-}"
                if [[ -z "$val" ]]; then
                    echo "ERROR_MISSING_ARG"
                    return 1
                fi
                case "$val" in
                    balanced|aggressive|conservative)
                        export PROXY_COMPRESSION_PROFILE="$val"
                        ;;
                    *)
                        echo "ERROR_INVALID_PROFILE:$val"
                        return 1
                        ;;
                esac
                skip_next=true
                ;;
            *)
                result+=("${args[$i]}")
                ;;
        esac
    done

    # 输出解析后的参数（不含 --profile 及其值）
    echo "${result[@]}"
}

_handle_parse_error() {
    local err="$1"
    case "$err" in
        ERROR_MISSING_ARG)
            error "--profile 需要参数: balanced|aggressive|conservative"
            ;;
        ERROR_INVALID_PROFILE:*)
            local val="${err#ERROR_INVALID_PROFILE:}"
            error "无效 profile: $val (可选: balanced, aggressive, conservative)"
            ;;
    esac
}

# ============================================================
# 主入口
# ============================================================
main() {
    # 解析全局标志（--profile 等）
    local parsed_args
    if ! parsed_args=$(_parse_global_flags "$@"); then
        _handle_parse_error "$parsed_args"
        exit 1
    fi
    # 转换为数组
    eval set -- "$parsed_args"

    case "${1:-help}" in
        start)
            _with_manage_lock cmd_start
            ;;
        start-cloud)
            _with_manage_lock cmd_start_cloud
            ;;
        stop)
            _with_manage_lock cmd_stop
            ;;
        status)
            cmd_status
            ;;
        restart)
            _with_manage_lock cmd_restart
            ;;
        reload)
            _with_manage_lock cmd_reload
            ;;
        route-force-local)
            cmd_route_force_local "$2"
            ;;
        route-force-cloud)
            cmd_route_force_cloud "$2"
            ;;
        start-backend)
            _with_manage_lock cmd_start_backend
            ;;
        stop-backend)
            _with_manage_lock cmd_stop_backend
            ;;
        logs)
            cmd_logs "${2:-50}"
            ;;
        proxy-logs)
            cmd_proxy_logs "${2:-50}"
            ;;
        list|configs)
            cmd_list
            ;;
        models)
            cmd_models
            ;;
        models-validate)
            cmd_models_validate
            ;;
        switch)
            _with_manage_lock cmd_switch "$2"
            ;;
        current)
            cmd_current
            ;;
        fix-template)
            cmd_fix_template "${2:-}" "${3:-}"
            ;;
        watchdog)
            cmd_watchdog "$2"
            ;;
        stop-watchdog)
            cmd_stop_watchdog
            ;;
        watchdog-status)
            cmd_watchdog_status
            ;;
        wizard)
            cmd_wizard
            ;;
        help|--help|-h)
            cmd_help
            ;;
        *)
            error "未知命令: $1"
            cmd_help
            exit 1
            ;;
    esac
}

main "$@"

# ============================================================
# Metal 内存实时监控
# ============================================================
cmd_monitor() {
    local interval="${1:-5}"
    info "Metal 内存监控 (每 ${interval}s 刷新, Ctrl+C 退出)"
    info "  后端: ${LLAMA_BACKEND:-rapid-mlx}:${LLAMA_PORT:-8081}"
    info ""
    printf "  %-12s %-12s %-12s %-8s %-8s\n" "TIME" "ACTIVE" "PEAK" "RUNNING" "MEM%"
    while true; do
        local line
        line=$(grep "Metal memory" "$SCRIPT_DIR/logs/llama-server.log" 2>/dev/null | tail -1)
        if [[ -n "$line" ]]; then
            local active peak running
            active=$(echo "$line" | grep -oE 'active=[0-9.]+GB' | cut -d= -f2)
            peak=$(echo "$line" | grep -oE 'peak=[0-9.]+GB' | cut -d= -f2)
            running=$(echo "$line" | grep -oE 'running=[0-9]+' | cut -d= -f2)
            # 计算内存占比（假定 36.2GB 上限）
            local pct
            pct=$(echo "$active" | awk '{printf "%.0f", $1/36.2*100}' 2>/dev/null || echo "?")
            printf "  %-12s %-12s %-12s %-8s %-8s\n" "$(date +%H:%M:%S)" "${active}GB" "${peak}GB" "${running}" "${pct}%"
        else
            printf "  %-12s %-12s\n" "$(date +%H:%M:%S)" "(无数据)"
        fi
        sleep "$interval"
    done
}
