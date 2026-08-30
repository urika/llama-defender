#!/usr/bin/env bash
# ============================================================
# Integration test for IFC-3 micro-turn re-dispatch (PDC 方案 B).
#
# mock 后端按序列响应: 第 1 个请求流出 ctx_recall 工具调用,
# 第 2 个请求(含 follow-up tool 消息)流出最终文本答案。
# 验证:
#   1. 客户端 SSE 输出恰好 1 个 message_start(微轮透明)
#   2. 输出不含 ctx_recall tool_use 块(调用被抑制)
#   3. 输出含最终答案文本
#   4. mock capture 显示 2 次后端请求, 第 2 次带 role:tool 消息
#   5. 关闭开关时回落路径 A: tool_use 正常发给客户端
#
# Run via:
#     bash test/run_tests.sh --integration
# or directly:
#     bash test/integration/test_micro_turn_integration.sh
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/itest_micro"
MOCK_PORT="${MOCK_PORT:-8093}"
PROXY_PORT="${PROXY_PORT:-4005}"
SID="itestmic"
CAPTURE_PATH="$LOG_DIR/mock_capture.jsonl"
PROXY_LOG="$LOG_DIR/proxy.log"
MOCK_LOG="$LOG_DIR/mock.log"
SSE_OUT="$LOG_DIR/client_sse.txt"
SSE_OUT_OFF="$LOG_DIR/client_sse_off.txt"
SEQ_FILE="$LOG_DIR/seq.json"

mkdir -p "$LOG_DIR"
: > "$CAPTURE_PATH"; : > "$PROXY_LOG"; : > "$MOCK_LOG"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
PASS=0; FAIL=0
pass() { echo -e "  ${GREEN}PASS${NC}  $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; FAIL=$((FAIL+1)); }
info() { echo -e "${CYAN}→${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }

PROXY_PID=""; MOCK_PID=""

count_matches() { local pat=$1 f=$2; local n; n=$(grep -c -- "$pat" "$f" 2>/dev/null); echo "${n:-0}"; }

cleanup() {
  set +e
  [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null
  [[ -n "$MOCK_PID"  ]] && kill "$MOCK_PID"  2>/dev/null
  sleep 0.3
  [[ -n "$PROXY_PID" ]] && kill -9 "$PROXY_PID" 2>/dev/null
  [[ -n "$MOCK_PID"  ]] && kill -9 "$MOCK_PID"  2>/dev/null
  rm -f "$REPO_ROOT/logs/diag/manifest/$SID.jsonl" \
        "$REPO_ROOT/logs/diag/index/$SID.db" \
        "$REPO_ROOT/logs/diag/archive/$SID.jsonl" \
        "$REPO_ROOT/logs/diag/ledger/$SID.jsonl"
  python3 - "$REPO_ROOT/logs/diag/sessions.jsonl" "$SID" <<'PYEOF'
import sys
path, sid = sys.argv[1], sys.argv[2]
try:
    lines = open(path, encoding="utf-8").readlines()
except OSError:
    sys.exit(0)
kept = [l for l in lines if sid not in l[:400]]
if len(kept) != len(lines):
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(kept)
PYEOF
}
trap cleanup EXIT

wait_for_port() {
  local port=$1 name=$2
  for i in $(seq 1 50); do
    if curl -sf --max-time 1 "http://127.0.0.1:$port/v1/models" >/dev/null 2>&1; then
      info "$name is up on :$port"
      return 0
    fi
    sleep 0.1
  done
  fail "$name failed to start on :$port"
  tail -30 "$PROXY_LOG" 2>/dev/null; tail -30 "$MOCK_LOG" 2>/dev/null
  exit 1
}

start_proxy() {
  local extra_env_name=$1  # "on" | "off"
  local mt_flag="false"
  [[ "$extra_env_name" == "on" ]] && mt_flag="true"
  PORT="$PROXY_PORT" \
  LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
  PROXY_METRICS_ENABLED=false \
  PROXY_LOG_PATH="$PROXY_LOG" \
  PROXY_DIAG_ENABLED=true \
  PROXY_HBE_ENABLED=false \
  PROXY_QUEUE_ENABLED=false \
  PROXY_COMPRESS_ENABLED=false \
  PROXY_CLEAR_ENABLED=false \
  PROXY_CTX_LIMIT_ENABLED=false \
  PROXY_ROUTE_ENABLED=false \
  PROXY_PD_ENABLED=true \
  PROXY_PD_MICRO_TURN_ENABLED="$mt_flag" \
    python3 "$REPO_ROOT/anthropic_proxy.py" >>"$PROXY_LOG" 2>&1 &
  PROXY_PID=$!
  wait_for_port "$PROXY_PORT" "proxy(micro=$mt_flag)"
}

send_stream() {
  local outfile=$1
  curl -sN --max-time 30 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: test" \
    -H "anthropic-version: 2023-06-01" \
    -H "x-claude-code-session-id: $SID" \
    -d '{
      "model": "claude-3-5-sonnet-20241022",
      "max_tokens": 128,
      "stream": true,
      "messages": [{"role": "user", "content": "之前的架构决策是什么"}]
    }' > "$outfile" 2>>"$PROXY_LOG"
}

# ============================================================
# mock 序列: ctx_recall 调用 → 最终文本
# ============================================================
python3 -c "
import json
seq = [
    {'tool_call': {'id': 'call_mt1', 'name': 'ctx_recall',
                   'arguments': json.dumps({'query': '架构 决策'})}},
    {'text': '根据召回: 早期架构决策是 session 方案(放弃 JWT)。'},
]
json.dump(seq, open('$SEQ_FILE', 'w'), ensure_ascii=False)
"

MOCK_SEQ_FILE="$SEQ_FILE" \
MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
  python3 "$REPO_ROOT/test/integration/mock_backend.py" "$MOCK_PORT" >>"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

# ============================================================
# 场景 1: 微轮开启
# ============================================================
info "场景 1: PROXY_PD_MICRO_TURN_ENABLED=true"
start_proxy on
: > "$CAPTURE_PATH"
send_stream "$SSE_OUT"

n_start=$(count_matches '^event: message_start' "$SSE_OUT")
n_recall=$(count_matches 'ctx_recall' "$SSE_OUT")
n_tooluse=$(count_matches '"type":"tool_use"' "$SSE_OUT")
n_answer=$(count_matches 'JWT' "$SSE_OUT")
n_stop=$(count_matches '^event: message_stop' "$SSE_OUT")
n_captured=$(wc -l < "$CAPTURE_PATH" | tr -d ' ')

if [[ "$n_start" == "1" ]]; then pass "客户端恰好看到 1 个 message_start(微轮透明)"; else fail "message_start 数=$n_start(期望 1)"; tail -5 "$SSE_OUT"; fi
if [[ "$n_recall" == "0" && "$n_tooluse" == "0" ]]; then pass "ctx_recall 调用被抑制(客户端未见 tool_use)"; else fail "客户端看到 ctx_recall/tool_use(recall=$n_recall, tool_use=$n_tooluse)"; fi
if [[ "$n_answer" == "1" ]]; then pass "最终答案文本已送达"; else fail "未见最终答案文本"; fi
if [[ "$n_stop" == "1" ]]; then pass "恰好 1 个 message_stop"; else fail "message_stop 数=$n_stop(期望 1)"; fi
if [[ "$n_captured" == "2" ]]; then pass "后端收到 2 次请求(微轮重派)"; else fail "后端请求数=$n_captured(期望 2)"; fi

# 第 2 次请求应含 role:tool 的 follow-up
if python3 - "$CAPTURE_PATH" <<'PYEOF'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").readlines()
ok = False
if len(lines) >= 2:
    body = json.loads(lines[1])["body"]
    msgs = body.get("messages", [])
    roles = [m.get("role") for m in msgs]
    ok = "assistant" in roles and "tool" in roles
sys.exit(0 if ok else 1)
PYEOF
then pass "第 2 次后端请求携带 assistant(tool_calls)+tool follow-up 消息对"; else fail "follow-up 消息对缺失"; fi

# 代理日志应有 MICRO_TURN 记录
if grep -q "MICRO_TURN" "$PROXY_LOG"; then pass "代理日志记录 [MICRO_TURN] 重派"; else fail "日志未见 MICRO_TURN"; fi

kill "$PROXY_PID" 2>/dev/null; wait "$PROXY_PID" 2>/dev/null; PROXY_PID=""
sleep 0.5

# ============================================================
# 场景 2: 微轮关闭 → 回落路径 A(tool_use 正常发给客户端)
# ============================================================
info "场景 2: PROXY_PD_MICRO_TURN_ENABLED=false(回落路径 A)"
# 重启 mock 重置序列(场景 1 已消费前 2 个 spec)
kill "$MOCK_PID" 2>/dev/null; wait "$MOCK_PID" 2>/dev/null; MOCK_PID=""
sleep 0.3
MOCK_SEQ_FILE="$SEQ_FILE" \
MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
  python3 "$REPO_ROOT/test/integration/mock_backend.py" "$MOCK_PORT" >>"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend(restarted)"
: > "$CAPTURE_PATH"; : > "$PROXY_LOG"
start_proxy off
send_stream "$SSE_OUT_OFF"

n_start_off=$(count_matches '^event: message_start' "$SSE_OUT_OFF")
n_recall_off=$(count_matches 'ctx_recall' "$SSE_OUT_OFF")
n_captured_off=$(wc -l < "$CAPTURE_PATH" | tr -d ' ')

if [[ "$n_start_off" == "1" ]]; then pass "关闭时正常 1 个 message_start"; else fail "message_start=$n_start_off"; fi
if [[ "$n_recall_off" -ge 1 ]]; then pass "关闭时 ctx_recall tool_use 正常发给客户端(路径 A)"; else fail "关闭时未见 tool_use"; fi
if [[ "$n_captured_off" == "1" ]]; then pass "关闭时后端仅 1 次请求(无重派)"; else fail "后端请求数=$n_captured_off(期望 1)"; fi

echo ""
if (( FAIL == 0 )); then
  echo -e "${GREEN}=== micro-turn integration: ALL PASS ($PASS) ===${NC}"
  exit 0
else
  echo -e "${RED}=== micro-turn integration: $FAIL FAILED / $PASS passed ===${NC}"
  exit 1
fi
