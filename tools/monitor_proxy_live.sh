#!/bin/bash
# 实时代理监控脚本 — 用于 Claude 编程任务期间的持续观测
# 用法: ./tools/monitor_proxy_live.sh [间隔秒数]

set -euo pipefail

INTERVAL="${1:-10}"
LOG="logs/anthropic_proxy.log"
METRICS="logs/proxy_metrics.jsonl"
BACKEND_LOG="logs/llama-server.log"

colors() {
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
}
colors

# 获取自启动以来的新 metrics
count_new_metrics() {
    python3 -c "
import json
from collections import Counter

cutoff = '$(date +%Y-%m-%d)T00:00:00'
records = []
with open('$METRICS') as f:
    for line in f:
        d = json.loads(line)
        if d.get('ts', '') >= cutoff:
            records.append(d)

if not records:
    print('No metrics today')
    exit(0)

statuses = Counter(r.get('status') for r in records)
loops = sum(1 for r in records if 'loop_injected' in r.get('quality_flags', []))
blockers = sum(1 for r in records if r.get('pipeline', {}).get('blocker', {}).get('triggered'))
drops = sum(1 for r in records if 'high_drop_ratio' in r.get('quality_flags', []))
errors = sum(1 for r in records if r.get('pipeline', {}).get('error', {}).get('classified'))

print(f'Total: {len(records)} | ', end='')
for s, c in sorted(statuses.items()):
    pct = c/len(records)*100
    color = 'GREEN' if s == 200 else 'RED' if s == 500 else 'YELLOW'
    print(f'{s}:{c}({pct:.0f}%) ', end='')
print(f'| Loops:{loops} Blockers:{blockers} Drops:{drops} ErrCls:{errors}')
" 2>/dev/null || echo "metrics parse error"
}

# 最近的 Claude session 活动
claude_activity() {
    python3 -c "
import json, re
from collections import Counter

# Find most active session today
cutoff = '$(date +%Y-%m-%d)T00:00:00'
sessions = Counter()
with open('$METRICS') as f:
    for line in f:
        d = json.loads(line)
        if d.get('ts', '') >= cutoff:
            sid = d.get('session_id', '')
            if sid and len(sid) > 10:
                sessions[sid] += 1

if not sessions:
    print('No Claude sessions today')
    exit(0)

main_sid = sessions.most_common(1)[0][0]
print(f'Active session: {main_sid[:16]} ({sessions[main_sid]} requests)')

# Analyze last 50 proxy log lines for this session
actions = Counter()
tool_calls = Counter()
with open('$LOG') as f:
    lines = f.readlines()

for line in lines[-200:]:
    if main_sid[:8] not in line:
        continue
    if 'tool_use' in line or 'tool_result' in line:
        m = re.search(r'\"name\":\"(\w+)\"', line)
        if m:
            tool_calls[m.group(1)] += 1
    for action in ['Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep']:
        if action in line and 'cleared' not in line and 'filter' not in line:
            actions[action] += 1

if actions:
    print('Semantic actions:', ' '.join(f'{k}:{v}' for k, v in actions.most_common(6)))
if tool_calls:
    print('Tool calls:', ' '.join(f'{k}:{v}' for k, v in tool_calls.most_common(6)))
" 2>/dev/null || echo "log parse error"
}

# 后端健康
backend_health() {
    if grep -q "Resource limit (499000)\|Insufficient Memory" "$BACKEND_LOG" 2>/dev/null; then
        oom_count=$(grep -c "Resource limit (499000)\|Insufficient Memory" "$BACKEND_LOG" 2>/dev/null)
        echo -e "${RED}OOM events total: $oom_count${NC}"
    else
        echo -e "${GREEN}No OOM detected${NC}"
    fi
    
    if grep -q "Memory pressure" "$BACKEND_LOG" 2>/dev/null; then
        pressure=$(grep -c "Memory pressure" "$BACKEND_LOG" 2>/dev/null)
        echo -e "${YELLOW}Memory pressure events: $pressure${NC}"
    fi
}

# 主循环
echo -e "${BLUE}=== Proxy Live Monitor ===${NC}"
echo "Interval: ${INTERVAL}s | Press Ctrl+C to stop"
echo ""

while true; do
    clear
    echo -e "${BLUE}$(date '+%Y-%m-%d %H:%M:%S')${NC}"
    echo ""
    
    echo -e "${YELLOW}[Metrics Today]${NC}"
    count_new_metrics
    echo ""
    
    echo -e "${YELLOW}[Claude Activity]${NC}"
    claude_activity
    echo ""
    
    echo -e "${YELLOW}[Backend Health]${NC}"
    backend_health
    echo ""
    
    echo -e "${YELLOW}[Recent Proxy Log]${NC}"
    tail -8 "$LOG" | grep -E "REQ_SUMMARY|LOOP|BLOCKER|500|ERROR|cleared|filter" || tail -4 "$LOG"
    
    sleep "$INTERVAL"
done
