#!/usr/bin/env bash
# ============================================================
# 内存专项检查脚本
# 详细分析内存使用、大进程、内存压力
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
# 详细内存分解
# ============================================================
_mem_detail() {
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
total_pages = 3145728

def gb(p):
    return p * ps / (1024**3)

def pct(p):
    return p / total_pages * 100

items = [
    ('空闲', 'Pages free'),
    ('活跃', 'Pages active'),
    ('非活跃', 'Pages inactive'),
    ('推测性', 'Pages speculative'),
    ('内核锁定', 'Pages wired down'),
    ('可清除', 'Pages purgeable'),
    ('文件缓存', 'File-backed pages'),
    ('匿名内存', 'Anonymous pages'),
    ('被压缩(原始)', 'Pages stored in compressor'),
    ('压缩后占用', 'Pages occupied by compressor'),
]

print(f'{"类型":<20} {"页数":>10} {"大小":>8} {"占比":>6}')
print('-' * 48)
for label, key in items:
    v = pages.get(key, 0)
    print(f'{label:<20} {v:>10,} {gb(v):>7.1f}G {pct(v):>5.1f}%')

used = pages.get('Pages active',0) + pages.get('Pages inactive',0) + pages.get('Pages wired down',0) + pages.get('Pages occupied by compressor',0)
print('-' * 48)
print(f'{"估算已用":<20} {used:>10,} {gb(used):>7.1f}G {pct(used):>5.1f}%')
print(f'{"估算空闲":<20} {pages.get(\"Pages free\",0):>10,} {gb(pages.get(\"Pages free\",0)):>7.1f}G {pct(pages.get(\"Pages free\",0)):>5.1f}%')
"
}

# ============================================================
# 大进程详细
# ============================================================
_large_procs() {
    ps -ax -o pid,rss,vsz,pcpu,comm | grep -v "^PID" | awk '
    {
        name = $4
        for (i = 5; i <= NF; i++) name = name " " $i
        printf "  %-40s %6.0f MB %6.0f MB %5s%%  PID %s\n", name, $2/1024, $3/1024/1024, $4, $1
    }' | sort -k2 -nr | head -n 15
}

# ============================================================
# 内存压力
# ============================================================
_mem_pressure() {
    memory_pressure 2>/dev/null | head -n 20 || warn "memory_pressure 命令不可用"
}

# ============================================================
# 模型相关进程内存
# ============================================================
_model_mem() {
    echo "  后端/模型进程:"
    ps -ax -o pid,rss,comm | grep -E "llama-server|rapid-mlx|anthropic_proxy" | grep -v grep | awk '
    {
        name = $3
        for (i = 4; i <= NF; i++) name = name " " $i
        printf "    %-40s %6.0f MB  PID %s\n", name, $2/1024, $1
    }'
}

# ============================================================
# 主入口
# ============================================================
main() {
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "  ${CYAN}内存专项检查${NC}  $(date '+%H:%M:%S')"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    info "详细内存分解"
    _mem_detail
    echo ""
    info "模型相关进程"
    _model_mem
    echo ""
    info "Top 15 内存进程 (RSS / VSZ / CPU)"
    echo "  名称                                      RSS      VSZ    CPU   PID"
    _large_procs
    echo ""
    info "内存压力"
    _mem_pressure
}

main "$@"
