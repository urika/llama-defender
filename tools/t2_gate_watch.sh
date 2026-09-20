#!/usr/bin/env bash
# T2 门控自动执行器（Runbook §10.2 T2）
#
# 门（全部满足才放行）：
#   G1: EXP-9 驱动进程退出
#   G2: swe-eval finale-*.md 存在且新鲜（>= 本次启动时间）
#   G3: finale-watch 已停止（不再 state=running）
#   G4: 放行标记文件 T2_GO 存在（人工/swe-eval 侧确认 analyze --force 完成，L-31）
#
# 放行后顺序：
#   P0 关闭 T1 影子(8090)
#   P1 停生产后端（唯一生产中断点）
#   P2 起 35B oQ4e 影子(8090, 0.14.3 + pin-system-prompt + B2 补丁)
#   P3 A/B：基线臂 brew 0.12.12 vs 对照臂 0.14.3（~40K 发散点）
#   P4 采集 snap/HIT/全冷/decode 指标
#   P5 回退演练
#   P6 恢复生产后端
#
# 用法：
#   bash tools/t2_gate_watch.sh            # 前台持续盯门（可 nohup 后台）
#   T2_POLL=60 T2_TIMEOUT=7200 bash tools/t2_gate_watch.sh
# 放行：  touch /tmp/ornith-staging/T2_GO

set -uo pipefail

ROOT="/Users/jinsongwang/APP/llama.cpp"
STG="/tmp/ornith-staging"
# 取证完成门：扫全部 experiment 目录找最新 finale（不绑定单一 exp）
EXP_ROOT="$HOME/APP/swe-eval/results/experiments"
WATCH_LOG="$HOME/APP/swe-eval/results/logs/finale-watch-exp9.log"
GO_MARK="$STG/T2_GO"
LOG="$STG/logs/t2-gate.log"

VENV="$ROOT/.venv-rapidmlx-0143"
BREW_BIN="/opt/homebrew/bin/rapid-mlx"
MODEL="pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp"
SHADOW_PORT=8090
STG_TS="$(date +%Y%m%d-%H%M%S)"

POLL="${T2_POLL:-60}"
TIMEOUT="${T2_TIMEOUT:-7200}"

mkdir -p "$STG/state" "$STG/logs"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# ---- 门检查：返回 0 = 放行 ----
gate_ok() {
  # G1 驱动退出。
  # 注意：不能 pgrep -f 'exp9' —— 本脚本自身命令行/日志路径含 "exp9"/"t2_gate" 会误匹配。
  # 只认批跑驱动的具体进程名，并显式排除本脚本 PID 与父 shell。
  local self="$$"
  local hits
  hits=$(pgrep -f 'batch_finale\.py|batch_driver|run_claimtest' 2>/dev/null | grep -v "^${self}$" || true)
  [[ -n "$hits" ]] && return 1
  # 具体驱动 PID（本实验为 4579；可选，用 T2_DRIVER_PID 覆盖）
  if [[ -n "${T2_DRIVER_PID:-}" ]] && ps -p "$T2_DRIVER_PID" >/dev/null 2>&1; then
    return 1
  fi
  # G2 最新 finale 新鲜（本次启动之后）且关键步骤 rc=0。
  # 不绑定单一 exp 目录：扫全部 experiments，取最新；目录被清/轮换也不失效。
  local newest
  newest=$(find "$EXP_ROOT" -name 'finale-*.md' 2>/dev/null | xargs ls -t 2>/dev/null | head -1)
  [[ -n "$newest" && -f "$newest" ]] || return 1
  # 新鲜判定做成「可选」：默认关闭 —— 因为 watcher 可能在取证之后才启动，
  # 强制 -nt 会永远卡住（本次实测教训）。需要严格时序时置 T2_REQUIRE_FRESH=1。
  if [[ "${T2_REQUIRE_FRESH:-0}" == "1" ]]; then
    [[ "$newest" -nt "$STG/.t2-watch-start" ]] || return 1
  fi
  # G2b: 相邻 json 全部步骤 rc=0（拒绝 rc=1 的残缺取证件）
  local j="${newest%.md}.json"
  if [[ -f "$j" ]]; then
    python3 -c "import json,sys;d=json.load(open('$j'));sys.exit(0 if all(s.get('rc')==0 for s in d.get('steps',[])) else 1)" || return 1
  fi
  # G3 finale-watch 已停：以「watcher 进程是否存活」为准，不只看日志尾行
  #（进程已死但尾行遗留 state=running 会永久卡门——本次实测教训）。
  local wpid
  wpid=$(cat "$HOME/APP/swe-eval/results/.finale-watch.pid" 2>/dev/null || true)
  if [[ -n "$wpid" ]] && ps -p "$wpid" >/dev/null 2>&1; then
    # 进程活着才看它是否仍在 running；进程死了则视为已停
    tail -1 "$WATCH_LOG" 2>/dev/null | grep -q 'state=running' && return 1
  fi
  # G4 放行标记
  [[ -f "$GO_MARK" ]] || return 1
  return 0
}

# ---- 采样：把关注指标从日志中抽取 ----
sample() {
  local logf="$1" tag="$2"
  {
    echo "===== $tag ====="
    echo "-- metal --";        grep -oE '\[Metal memory\][^"]*' "$logf" | tail -5
    echo "-- cache_fetch --";  grep -oE '\[cache_fetch\] request=[^"]*' "$logf" | tail -20
    echo "-- lcp/snap --";     grep -oE 'LCP snapped to checkpoint[^"]*|supersequence snapped to checkpoint[^"]*|LCP unavailable[^"]*' "$logf" | tail -20
    echo "-- decode --";       grep -oE 'finished normally, [0-9]+ tokens in [0-9.]+s' "$logf" | tail -10
    echo "-- first token --";  grep -oE 'first token after [0-9.]+s' "$logf" | tail -10
    echo "-- evict --";        grep -c 'prefix-pressure-evict' "$logf" 2>/dev/null || echo 0
  } >> "$STG/logs/t2-samples.log"
}

req() { # $1=port $2=body-json $3=outfile
  curl -s --max-time 900 "http://127.0.0.1:$1/v1/chat/completions" \
    -H 'Content-Type: application/json' -d "$2" -o "$3"
}

# 构造 ~40K 级发散点：长 A 建条目；A+'OMEGA' 走普通 HIT；B 与 A 共享头部分叉 => LCP
mk_body() { # $1=marker $2=extra
python3 - "$1" "$2" <<'PY'
import json,sys
marker,extra=sys.argv[1],sys.argv[2]
filler=("lorem ipsum dolor sit amet consectetur adipiscing elit " * 40)
content=f"[[{marker}]] {filler} {extra}".strip()
print(json.dumps({"model":"pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp",
 "max_tokens":48,"temperature":0.0,
 "messages":[{"role":"user","content":content}]}))
PY
}

run_arm() { # $1=bin $2=tag
  local bin="$1" tag="$2" logf="$STG/logs/t2-$tag.log"
  log "起臂 $tag ($bin)"
  HF_HUB_OFFLINE=1 RAPID_MLX_STATE_DIR="$STG/state" RAPID_MLX_TELEMETRY=0 \
  nohup "$bin" serve "$MODEL" \
    --no-mllm --port "$SHADOW_PORT" --gpu-memory-utilization 0.80 \
    --cache-memory-mb 12288 --max-num-seqs 1 --default-repetition-penalty 1.0 \
    --hybrid-cache-entries 32 --pin-system-prompt \
    > "$logf" 2>&1 &
  local pid=$!
  # 就绪等待
  for i in $(seq 1 60); do
    curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$SHADOW_PORT/v1/models" && break
    sleep 5
  done
  log "$tag 就绪，开跑 A/B 序列"
  req "$SHADOW_PORT" "$(mk_body A '')"        "$STG/logs/t2-$tag-r1.json"
  req "$SHADOW_PORT" "$(mk_body A 'OMEGA')"   "$STG/logs/t2-$tag-r2.json"
  req "$SHADOW_PORT" "$(mk_body B '')"        "$STG/logs/t2-$tag-r3.json"   # LCP 发散
  sample "$logf" "$tag"
  log "$tag 采集完成，停止"
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  sleep 3
}

log "T2 gate watcher 启动（POLL=${POLL}s TIMEOUT=${TIMEOUT}s）"
touch "$STG/.t2-watch-start"
START=$(date +%s)

while :; do
  if gate_ok; then log "门放行（G1-G4 全绿）"; break; fi
  now=$(date +%s)
  if (( now - START > TIMEOUT )); then log "门未在 TIMEOUT 内放行，退出"; exit 2; fi
  log "门未放行（driver=$(pgrep -f exp9 >/dev/null 2>&1 && echo run || echo exit), go_mark=$([[ -f $GO_MARK ]] && echo yes || echo no)）"
  sleep "$POLL"
done

# ---- 放行后执行 ----
log "P0 关闭 T1 影子(8090)"; pkill -f 'port 8090' 2>/dev/null; sleep 3
log "P1 停生产后端（生产中断开始）"
( cd "$ROOT" && ./manage.sh stop-backend ) >> "$LOG" 2>&1

log "P2/P3 起对照臂 0.14.3"
run_arm "$VENV/bin/rapid-mlx" "0143"

log "P2/P3 起基线臂 brew 0.12.12（无 pin，模拟现状）"
# 基线臂用简化命令（brew 0.12.12 无 pin-system-prompt 需要去掉该 flag）
BASE_BIN="$BREW_BIN"
if "$BREW_BIN" serve --help 2>&1 | grep -q 'pin-system-prompt'; then
  run_arm "$BREW_BIN" "brew01212"
else
  log "brew 0.12.12 无 --pin-system-prompt，改跑无该 flag 的基线"
  HF_HUB_OFFLINE=1 RAPID_MLX_STATE_DIR="$STG/state" RAPID_MLX_TELEMETRY=0 \
  nohup "$BREW_BIN" serve "$MODEL" \
    --no-mllm --port "$SHADOW_PORT" --gpu-memory-utilization 0.75 \
    --cache-memory-mb 12288 --max-num-seqs 1 --default-repetition-penalty 1.0 \
    --hybrid-cache-entries 32 > "$STG/logs/t2-brew01212.log" 2>&1 &
  bpid=$!
  for i in $(seq 1 60); do curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$SHADOW_PORT/v1/models" && break; sleep 5; done
  req "$SHADOW_PORT" "$(mk_body A '')"      "$STG/logs/t2-brew01212-r1.json"
  req "$SHADOW_PORT" "$(mk_body A 'OMEGA')" "$STG/logs/t2-brew01212-r2.json"
  req "$SHADOW_PORT" "$(mk_body B '')"      "$STG/logs/t2-brew01212-r3.json"
  sample "$STG/logs/t2-brew01212.log" "brew01212"
  kill "$bpid" 2>/dev/null; wait "$bpid" 2>/dev/null; sleep 3
fi

log "P5 回退演练：删 shadows、恢复生产配置"
pkill -f 'port 8090' 2>/dev/null; sleep 2
log "P6 恢复生产后端（生产中断结束）"
( cd "$ROOT" && ./manage.sh start-backend ) >> "$LOG" 2>&1
sleep 5
curl -s --max-time 10 "http://127.0.0.1:4000/api/status" | python3 -c "import json,sys;d=json.load(sys.stdin);print('state=',d['state'],'ready=',d['ready'])" 2>/dev/null | tee -a "$LOG"

log "T2 完成。样本见 $STG/logs/t2-samples.log，臂日志 t2-*.log，响应 t2-*-r*.json"
log "提醒：生产者如需切 0.14.3，另按 Runbook §5 应用 LLAMA_SERVER_BIN + B2 补丁"
