#!/bin/bash
# Continuous model log monitor — runs every 30s
LOG="logs/live_monitor.log"
mkdir -p logs
exec >> "$LOG" 2>&1

echo "[monitor] started pid=$$ at $(date '+%Y-%m-%d %H:%M:%S')"

while true; do
  TS=$(date '+%Y-%m-%d %H:%M:%S')
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "[$TS] 模型服务持续监控快照"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  # 1. 进程状态
  echo "[进程状态]"
  ./tools/modelmon.sh 2>/dev/null | grep -E "rapid-mlx:|anthropic_proxy:|端口 8081:|端口 4000:|端口 8081 API:|端口 4000 API:" | sed 's/\x1b\[[0-9;]*m//g'

  # 2. 资源占用
  echo "[资源]"
  ps -o pid,ppid,%cpu,%mem,rss,etime,comm -p $(cat llama-server.pid 2>/dev/null) $(cat anthropic_proxy.pid 2>/dev/null) 2>/dev/null | sed 's/^/  /'

  # 3. 最近代理异常
  echo "[最近代理异常/告警]"
  tail -n 200 logs/anthropic_proxy.log 2>/dev/null | grep -iE "ERROR|WARN|TEXT LOOP|BLOCKER|timeout|oom|backend_unavailable|retryable" | tail -n 10 | sed 's/^/  /'

  # 4. 最近后端异常
  echo "[最近后端异常]"
  tail -n 200 logs/llama-server.log 2>/dev/null | grep -iE "ERROR|WARN|out of memory|RuntimeError|failed|exception" | tail -n 10 | sed 's/^/  /'

  # 5. 最新质量标记
  echo "[最新请求质量标记]"
  python3 - <<'PY' 2>/dev/null
import json, sys
try:
    with open('logs/proxy_metrics.jsonl','r') as f:
        lines=[json.loads(l) for l in f if l.strip()]
    if not lines:
        print("  (no metrics)")
        sys.exit()
    last=lines[-1]
    p=last['pipeline']
    lc=p.get('lifecycle_stage',{})
    print(f"  ts={last.get('ts')} session={last.get('session_id')} req_count={lc.get('request_count')}")
    print(f"  stage={lc.get('stage')} total_chars={lc.get('total_chars')}")
    print(f"  error_translation={p.get('error_translation')}")
    print(f"  loop_detect={p.get('loop_detect')}")
    print(f"  blocker_detect={p.get('blocker_detect')}")
    print(f"  quality_flags={last.get('quality_flags',[])}")
    cpr=p.get('common_prefix_ratio',{})
    print(f"  common_prefix_ratio={cpr.get('ratio'):.2%}" if cpr.get('ratio') is not None else "  common_prefix_ratio=(n/a)")
except Exception as e:
    print(f"  (parse error: {e})")
PY

  # 6. 最近请求速率（1 分钟内请求数）
  echo "[最近 60s 请求数]"
  python3 - <<'PY' 2>/dev/null
import json, datetime, sys
try:
    now=datetime.datetime.utcnow()
    count=0
    with open('logs/proxy_metrics.jsonl','r') as f:
        for line in f:
            if not line.strip(): continue
            obj=json.loads(line)
            ts=datetime.datetime.fromisoformat(obj['ts'].replace('Z','+00:00'))
            if (now-ts).total_seconds() <= 60:
                count+=1
    print(f"  {count} requests/min")
except Exception:
    print("  (n/a)")
PY

  echo "[下次采样] $(date -v+30S '+%H:%M:%S')"
  sleep 30
done
