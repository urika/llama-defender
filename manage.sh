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
}

# 加载当前激活配置
_load_config

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
: "${RAPID_MLX_TOOL_PARSER:=qwen3_coder}"
: "${RAPID_MLX_REASONING_PARSER:=qwen3}"
: "${RAPID_MLX_ENABLE_PREFIX_CACHE:=true}"
: "${RAPID_MLX_KV_QUANTIZATION:=false}"
: "${RAPID_MLX_KV_QUANT_BITS:=8}"

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
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

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
            elif [[ "$comm" == "rapid-mlx" ]]; then
                echo "rapid-mlx"
                return 0
            fi
        fi
    fi
    # fallback: search process
    if pgrep -f "rapid-mlx" >/dev/null 2>&1; then
        echo "rapid-mlx"
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
    if [[ "$backend" == "rapid-mlx" ]]; then
        pid=$(pgrep -f "rapid-mlx" 2>/dev/null | head -1)
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
    if lsof -Pi ":$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
        local pid
        pid=$(lsof -Pi ":$port" -sTCP:LISTEN -t 2>/dev/null | head -1)
        error "端口 $port 已被占用 (PID: $pid)"
        ps -p "$pid" -o pid,comm,args 2>/dev/null | tail -1
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

    info "启动 Rapid-MLX..."
    info "  配置: ${CYAN}$(_current_config_name)${NC}"
    info "  模型: $LLAMA_MODEL"
    info "  地址: $LLAMA_HOST:$LLAMA_PORT"
    info "  工具解析: $RAPID_MLX_TOOL_PARSER"
    info "  推理解析: $RAPID_MLX_REASONING_PARSER"
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
        --reasoning-parser "$RAPID_MLX_REASONING_PARSER"
        --no-thinking
        --log-level INFO
    )

    if [[ "$RAPID_MLX_ENABLE_PREFIX_CACHE" == "true" ]]; then
        args+=(--enable-prefix-cache)
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

    nohup rapid-mlx "${args[@]}" >> "$LOGFILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PIDFILE"

    info "进程已启动 (PID: $new_pid)，等待就绪..."
    _wait_for_ready "$new_pid" "Rapid-MLX"
}

# ============================================================
# 启动代理
# ============================================================
_start_proxy() {
    local proxy_pid
    if proxy_pid=$(_get_proxy_pid 2>/dev/null); then
        warn "代理已在运行 (PID: $proxy_pid)"
        return 0
    fi

    _check_port "$PROXY_PORT" || return 1

    # 使用配置中的 LLAMA_BASE_URL 如果有的话，否则用本地
    local base_url="${LLAMA_BASE_URL:-http://$LLAMA_HOST:$LLAMA_PORT/v1}"

    info "启动 anthropic_proxy.py..."
    info "  地址: $PROXY_HOST:$PROXY_PORT"
    info "  后端: $base_url"

    LLAMA_BASE_URL="$base_url" \
    LLAMA_API_KEY="${LLAMA_API_KEY:-sk-1234}" \
    MODEL_NAME="${MODEL_NAME:-}" \
    PORT="$PROXY_PORT" \
    HOST="$PROXY_HOST" \
    PROXY_LOG_PATH="$PROXY_LOGFILE" \
    PROXY_MAX_CONCURRENT="${PROXY_MAX_CONCURRENT:-1}" \
    PROXY_CLEAR_ENABLED="${PROXY_CLEAR_ENABLED:-true}" \
    PROXY_CLEAR_THRESHOLD="${PROXY_CLEAR_THRESHOLD:-30000}" \
    PROXY_TOOL_KEEP="${PROXY_TOOL_KEEP:-5}" \
    PROXY_CONTENT_TOOLS_FALLBACK="${PROXY_CONTENT_TOOLS_FALLBACK:-true}" \
    nohup python3 "$SCRIPT_DIR/anthropic_proxy.py" >> "$PROXY_LOGFILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PROXY_PIDFILE"

    info "代理进程已启动 (PID: $new_pid)，等待就绪..."

    local i
    for i in {1..30}; do
        if ! kill -0 "$new_pid" 2>/dev/null; then
            error "代理进程已退出！查看日志: $PROXY_LOGFILE"
            tail -n 20 "$PROXY_LOGFILE" 2>/dev/null
            rm -f "$PROXY_PIDFILE"
            return 1
        fi

        if curl -s --max-time 2 "http://$PROXY_HOST:$PROXY_PORT/v1/models" >/dev/null 2>&1; then
            info "✅ anthropic_proxy.py 就绪 (PID: $new_pid)"
            return 0
        fi

        sleep 1
        if (( i % 10 == 0 )); then
            echo "  等待代理就绪... ($i/30)"
        fi
    done

    warn "代理启动超时，但进程仍在运行 (PID: $new_pid)"
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
        rapid-mlx)
            _start_rapid_mlx || true
            ;;
        cloud|deepseek-cloud|openai-cloud)
            # 云模式：直接启动代理，不启动本地后端
            _start_proxy
            return $?
            ;;
        llama-server|*)
            _start_llama_server || true
            ;;
    esac

    # 后端就绪后启动代理（即使 wait 返回失败，只要进程还活着就尝试）
    if _get_pid >/dev/null 2>&1; then
        _start_proxy
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
                return 0
            fi

            error "无法停止进程 (PID: $pid)"
            return 1
        fi
    fi

    warn "后端服务未在运行"
    rm -f "$PIDFILE"
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
    cmd_stop || true
    sleep 2
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
        warn "切换配置后需要重启才能生效"
        read -p "是否先停止服务再切换? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            cmd_stop || true
        fi
    fi

    ln -sf "$target.conf" "$ACTIVE_CONF"
    info "配置已切换为: ${CYAN}$target${NC}"

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
# 帮助信息
# ============================================================
cmd_help() {
    cat <<EOF
llama.cpp / Rapid-MLX 服务管理脚本

用法: ./manage.sh <命令> [选项]

服务命令:
  start                启动后端和代理（根据当前配置）
  start-cloud          启动云端代理（DeepSeek/OpenAI，无需本地后端）
  stop                 停止后端和代理
  status               查询后端和代理状态
  restart              重启后端和代理
  logs [N]             查看最后 N 行后端日志 (默认 50)
  proxy-logs [N]       查看最后 N 行代理日志 (默认 50)

配置命令:
  list                 列出所有可用配置
  switch <name>        切换到指定配置
  current              显示当前配置详情

支持的后端:
  llama-server         标准 llama.cpp 后端 (GGUF)
  rapid-mlx            Rapid-MLX 后端 (MLX, Apple 优化)

配置文件位置: configs/*.conf
当前激活配置: configs/active.conf (软链接)

示例:
  ./manage.sh list                    # 查看所有配置
  ./manage.sh switch rapid-mlx-35b    # 切换到 Rapid-MLX
  ./manage.sh start                   # 用当前配置启动
  ./manage.sh start-cloud             # 启动云端代理（DeepSeek）
  ./manage.sh restart                 # 重启（应用新配置）
  ./manage.sh status                  # 查看运行状态

EOF
}

# ============================================================
# 主入口
# ============================================================
main() {
    case "${1:-help}" in
        start)
            cmd_start
            ;;
        start-cloud)
            cmd_start_cloud
            ;;
        stop)
            cmd_stop
            ;;
        status)
            cmd_status
            ;;
        restart)
            cmd_restart
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
        switch)
            cmd_switch "$2"
            ;;
        current)
            cmd_current
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
