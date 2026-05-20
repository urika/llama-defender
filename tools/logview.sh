#!/usr/bin/env bash
# ============================================================
# 日志查看脚本
# 统一查看后端和代理日志
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

usage() {
    cat <<EOF
用法: ./tools/logview.sh <命令> [选项]

命令:
  backend [N]      查看后端日志最后 N 行 (默认 50)
  proxy [N]        查看代理日志最后 N 行 (默认 50)
  errors [N]       查看后端日志中的错误最后 N 行 (默认 20)
  tail             实时跟踪后端和代理日志
  list             列出日志文件信息

示例:
  ./tools/logview.sh backend 100
  ./tools/logview.sh proxy 20
  ./tools/logview.sh tail
EOF
}

# ============================================================
# 查看后端日志
# ============================================================
cmd_backend() {
    local n="${1:-50}"
    local logfile="$LOG_DIR/llama-server.log"
    if [[ -f "$logfile" ]]; then
        info "后端日志 (最后 $n 行): $logfile"
        echo "---"
        tail -n "$n" "$logfile"
    else
        error "日志文件不存在: $logfile"
        return 1
    fi
}

# ============================================================
# 查看代理日志
# ============================================================
cmd_proxy() {
    local n="${1:-50}"
    local logfile="$LOG_DIR/anthropic_proxy.log"
    if [[ -f "$logfile" ]]; then
        info "代理日志 (最后 $n 行): $logfile"
        echo "---"
        tail -n "$n" "$logfile"
    else
        error "日志文件不存在: $logfile"
        return 1
    fi
}

# ============================================================
# 查看错误
# ============================================================
cmd_errors() {
    local n="${1:-20}"
    local logfile="$LOG_DIR/llama-server.log"
    if [[ -f "$logfile" ]]; then
        info "后端日志错误 (最后 $n 条): $logfile"
        echo "---"
        grep -n -i "error\|fail\|fatal\|exception" "$logfile" | tail -n "$n"
    else
        error "日志文件不存在: $logfile"
        return 1
    fi
}

# ============================================================
# 实时跟踪
# ============================================================
cmd_tail() {
    local backend_log="$LOG_DIR/llama-server.log"
    local proxy_log="$LOG_DIR/anthropic_proxy.log"
    info "实时跟踪日志 (按 Ctrl+C 退出)"
    if [[ -f "$backend_log" && -f "$proxy_log" ]]; then
        tail -f "$backend_log" "$proxy_log"
    elif [[ -f "$backend_log" ]]; then
        tail -f "$backend_log"
    elif [[ -f "$proxy_log" ]]; then
        tail -f "$proxy_log"
    else
        error "无日志文件可跟踪"
        return 1
    fi
}

# ============================================================
# 列出日志文件
# ============================================================
cmd_list() {
    info "日志文件列表: $LOG_DIR"
    if [[ -d "$LOG_DIR" ]]; then
        ls -lh "$LOG_DIR" | awk 'NR>1 {printf "  %-30s %8s\n", $NF, $5}'
    else
        error "日志目录不存在: $LOG_DIR"
        return 1
    fi
}

# ============================================================
# 主入口
# ============================================================
main() {
    case "${1:-help}" in
        backend)
            cmd_backend "${2:-50}"
            ;;
        proxy)
            cmd_proxy "${2:-50}"
            ;;
        errors)
            cmd_errors "${2:-20}"
            ;;
        tail)
            cmd_tail
            ;;
        list)
            cmd_list
            ;;
        help|--help|-h)
            usage
            ;;
        *)
            error "未知命令: $1"
            usage
            exit 1
            ;;
    esac
}

main "$@"
