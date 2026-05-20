#!/usr/bin/env bash
# ============================================================
# 模型服务监控脚本
# 检查后端进程、下载进度、端口状态、API 健康
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ============================================================
# 检查后端进程
# ============================================================
_check_backend() {
    local pid
    if pid=$(pgrep -x "llama-server" 2>/dev/null | head -1); then
        local mem
        mem=$(ps -p "$pid" -o rss= 2>/dev/null | awk '{printf "%.1f", $1/1024}')
        echo -e "  llama-server: ${GREEN}运行中${NC} (PID: $pid, RSS: ${mem} MB)"
    elif pid=$(pgrep -f "rapid-mlx serve" 2>/dev/null | head -1); then
        local mem
        mem=$(ps -p "$pid" -o rss= 2>/dev/null | awk '{printf "%.1f", $1/1024}')
        echo -e "  rapid-mlx:    ${GREEN}运行中${NC} (PID: $pid, RSS: ${mem} MB)"
    else
        echo -e "  后端:         ${RED}未运行${NC}"
    fi
}

# ============================================================
# 检查代理进程
# ============================================================
_check_proxy() {
    local pid
    if pid=$(pgrep -f "anthropic_proxy.py" 2>/dev/null | head -1); then
        local mem
        mem=$(ps -p "$pid" -o rss= 2>/dev/null | awk '{printf "%.1f", $1/1024}')
        echo -e "  anthropic_proxy: ${GREEN}运行中${NC} (PID: $pid, RSS: ${mem} MB)"
    else
        echo -e "  代理:            ${RED}未运行${NC}"
    fi
}

# ============================================================
# 检查端口
# ============================================================
_check_ports() {
    for port in 8002 8081 4000; do
        if lsof -Pi ":$port" -sTCP:LISTEN >/dev/null 2>&1; then
            local name pid
            name=$(lsof -Pi ":$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $1}')
            pid=$(lsof -Pi ":$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $2}')
            echo -e "  端口 $port: ${GREEN}监听中${NC} ($name PID $pid)"
        else
            echo -e "  端口 $port: ${RED}未监听${NC}"
        fi
    done
}

# ============================================================
# 检查 API 健康
# ============================================================
_check_api() {
    local url model_info
    for url in "http://127.0.0.1:8081/v1/models" "http://127.0.0.1:8002/v1/models" "http://127.0.0.1:4000/v1/models"; do
        local port
        port=$(echo "$url" | cut -d':' -f3 | cut -d'/' -f1)
        model_info=$(curl -s --max-time 2 "$url" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',[{}])[0].get('id','?'))" 2>/dev/null || echo "无响应")
        if [[ "$model_info" != "无响应" ]]; then
            echo -e "  端口 $port API: ${GREEN}正常${NC} ($model_info)"
        else
            echo -e "  端口 $port API: ${RED}无响应${NC}"
        fi
    done
}

# ============================================================
# 模型下载进度
# ============================================================
_download_progress() {
    local cache_dir
    cache_dir="$HOME/.cache/huggingface/hub"
    local total_down=0
    local has_incomplete=false

    if [[ -d "$cache_dir" ]]; then
        while IFS= read -r -d '' f; do
            has_incomplete=true
            total_down=$((total_down + $(stat -f%z "$f" 2>/dev/null || echo 0)))
        done < <(find "$cache_dir" -name "*.incomplete" -print0 2>/dev/null)
    fi

    if [[ "$has_incomplete" == true ]]; then
        local gb
        gb=$(echo "scale=2; $total_down / 1024 / 1024 / 1024" | bc 2>/dev/null || echo "$((total_down / 1024 / 1024 / 1024))")
        echo -e "  HuggingFace 下载中: ${CYAN}${gb} GB${NC} (incomplete 文件)"
    else
        echo "  无正在下载的模型"
    fi
}

# ============================================================
# 主入口
# ============================================================
main() {
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "  ${CYAN}模型服务监控${NC}  $(date '+%H:%M:%S')"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    info "后端进程"
    _check_backend
    echo ""
    info "代理进程"
    _check_proxy
    echo ""
    info "端口监听"
    _check_ports
    echo ""
    info "API 健康"
    _check_api
    echo ""
    info "模型下载进度"
    _download_progress
}

main "$@"
