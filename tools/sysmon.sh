#!/usr/bin/env bash
# ============================================================
# 系统监控脚本
# 显示内存、CPU、磁盘、Top 进程等关键指标
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ============================================================
# 内存监控
# ============================================================
_mem_stat() {
    python3 -c "
import subprocess
out = subprocess.check_output(['vm_stat'], text=True)
pages = {}
for line in out.strip().split('\n'):
    if ':' in line:
        key, val = line.split(':', 1)
        key = key.strip()
        val = val.strip().rstrip('.')
        try:
            pages[key] = int(val)
        except:
            pass

ps = 16384
total = 48.0
active = pages.get('Pages active', 0) * ps / (1024**3)
inactive = pages.get('Pages inactive', 0) * ps / (1024**3)
wired = pages.get('Pages wired down', 0) * ps / (1024**3)
comp = pages.get('Pages occupied by compressor', 0) * ps / (1024**3)
free = pages.get('Pages free', 0) * ps / (1024**3)
used = active + inactive + wired + comp

print(f'总内存:    {total:.1f} GB')
print(f'已用:      {used:.1f} GB ({used/total*100:.1f}%)')
print(f'空闲:      {free:.1f} GB')
print(f'活跃:      {active:.1f} GB')
print(f'非活跃:    {inactive:.1f} GB')
print(f'内核锁定:  {wired:.1f} GB')
print(f'压缩占用:  {comp:.1f} GB')
"
}

# ============================================================
# Top 进程
# ============================================================
_top_procs() {
    ps -ax -o pid,rss,comm | grep -v "^PID" | awk '
    {
        name = $3
        for (i = 4; i <= NF; i++) name = name " " $i
        printf "  %-45s %6.0f MB  (PID %s)\n", name, $2/1024, $1
    }' | sort -k2 -nr | head -n 10
}

# ============================================================
# 磁盘
# ============================================================
_disk_stat() {
    df -h / | tail -1 | awk '{printf "  已用: %s / %s (%s)\n", $3, $2, $5}'
}

# ============================================================
# CPU 负载
# ============================================================
_cpu_load() {
    uptime | awk -F'load averages:' '{print $2}' | awk '{printf "  1min: %s | 5min: %s | 15min: %s\n", $1, $2, $3}'
}

# ============================================================
# 主入口
# ============================================================
main() {
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "  ${CYAN}系统监控报告${NC}  $(date '+%H:%M:%S')"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    info "内存"
    _mem_stat
    echo ""
    info "Top 10 内存进程"
    _top_procs
    echo ""
    info "磁盘"
    _disk_stat
    echo ""
    info "CPU 负载"
    _cpu_load
}

main "$@"
