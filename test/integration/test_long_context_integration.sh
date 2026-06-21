#!/usr/bin/env bash
# ============================================================
# Long-context verification tests (TC-1, TC-2, TC-4)
#
# Validates the proxy's context management pipeline using a mock
# backend. Based on production log analysis findings.
#
# Test cases:
#   TC-1: Progressive context growth → lifecycle stage transitions
#   TC-2: Large request interception (413 + pre_truncate)
#   TC-4: Pipeline trigger verification (truncate/tool_clear/compress)
#
# Usage:
#   bash test/integration/test_long_context_integration.sh
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/itest_longctx"
MOCK_PORT="${MOCK_PORT:-8093}"
PROXY_PORT="${PROXY_PORT:-4005}"
METRICS_PATH="$LOG_DIR/proxy_metrics.jsonl"
MOCK_LOG="$LOG_DIR/mock.log"
PROXY_LOG="$LOG_DIR/proxy.log"
CAPTURE_PATH="$LOG_DIR/mock_capture.jsonl"

mkdir -p "$LOG_DIR"
: > "$MOCK_LOG"
: > "$PROXY_LOG"
: > "$CAPTURE_PATH"
touch "$METRICS_PATH"  # Create empty file so wc -l doesn't fail
: > "$METRICS_PATH"    # Truncate to empty

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'
PASS=0; FAIL=0; SKIP=0
pass() { echo -e "  ${GREEN}PASS${NC}  $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; FAIL=$((FAIL+1)); }
skip() { echo -e "  ${YELLOW}SKIP${NC}  $1"; SKIP=$((SKIP+1)); }
info() { echo -e "${CYAN}→${NC} $1"; }

PROXY_PID=""
MOCK_PID=""

cleanup() {
  set +e
  [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null
  [[ -n "$MOCK_PID"  ]] && kill "$MOCK_PID"  2>/dev/null
  sleep 0.3
  [[ -n "$PROXY_PID" ]] && kill -9 "$PROXY_PID" 2>/dev/null
  [[ -n "$MOCK_PID"  ]] && kill -9 "$MOCK_PID"  2>/dev/null
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
  [[ -f "$MOCK_LOG"  ]] && tail -30 "$MOCK_LOG"
  [[ -f "$PROXY_LOG" ]] && tail -30 "$PROXY_LOG"
  exit 1
}

# ============================================================
# Helper: send a request and capture response + metrics
# ============================================================
send_request() {
  local body_file=$1 label=$2
  local resp_file="$LOG_DIR/resp_${label}.json"
  local http_code
  # Count lines before request to find the new one after
  local before_lines
  before_lines=$(wc -l < "$METRICS_PATH" 2>/dev/null || echo 0)
  http_code=$(curl -s -o "$resp_file" -w "%{http_code}" \
    --max-time 30 \
    -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: test" \
    -H "anthropic-version: 2023-06-01" \
    -H "x-claude-code-session-id: itest-longctx" \
    -d @"$body_file" 2>/dev/null)
  # Wait for new metrics line(s) — poll up to 5s
  for i in $(seq 1 50); do
    local after_lines
    after_lines=$(wc -l < "$METRICS_PATH" 2>/dev/null || echo 0)
    if [[ "$after_lines" -gt "$before_lines" ]]; then
      sleep 0.3  # Buffer for multi-line metrics flush
      break
    fi
    sleep 0.1
  done
  echo "$http_code"
}

# Read all fields from the LAST metric record
get_last_metric_all() {
  tail -1 "$METRICS_PATH" 2>/dev/null | python3 -c "
import json, sys
try:
    m = json.loads(sys.stdin.read())
    p = m.get('pipeline', {})
    result = {
        'input_chars': m.get('input_chars', 0),
        'stage': '?',
        'truncate_applied': 'N/A',
        'tool_clear_applied': 'N/A',
        'tool_clear_cleared': 'N/A',
        'semantic_compress_enabled': 'N/A',
        'pre_truncate_triggered': 'N/A',
    }
    stage = p.get('lifecycle_stage', {})
    if isinstance(stage, dict):
        result['stage'] = stage.get('stage', '?')
    else:
        result['stage'] = str(stage)
    trunc = p.get('truncate', {})
    if isinstance(trunc, dict):
        result['truncate_applied'] = trunc.get('applied', 'N/A')
    clear = p.get('tool_clear', {})
    if isinstance(clear, dict):
        result['tool_clear_applied'] = clear.get('applied', 'N/A')
        result['tool_clear_cleared'] = clear.get('cleared', 'N/A')
    comp = p.get('semantic_compress', {})
    if isinstance(comp, dict):
        result['semantic_compress_enabled'] = comp.get('enabled', 'N/A')
    pre = p.get('pre_truncate', {})
    if isinstance(pre, dict):
        result['pre_truncate_triggered'] = pre.get('triggered', 'N/A')
    for k, v in result.items():
        print(f'{k}={v}')
except Exception as e:
    print(f'ERROR={e}')
" 2>/dev/null
}

# ============================================================
# Generate message payloads of various sizes
# ============================================================
gen_small_request() {
  # ~2K chars (INIT stage)
  python3 -c "
import json
msgs = [{'role':'user','content':'Write a hello world function'}]
print(json.dumps({'model':'claude-sonnet-4-6','max_tokens':100,'stream':False,'messages':msgs}))
"
}

gen_medium_request() {
  # ~50K chars (EXPANSION stage) — 20 tool_result pairs with ~2K each
  python3 -c "
import json
msgs = [{'role':'user','content':'Analyze this codebase'}]
for i in range(20):
    msgs.append({'role':'assistant','content':[{'type':'tool_use','id':f't{i}','name':'Read','input':{'file_path':f'/src/file_{i}.py'}}]})
    msgs.append({'role':'user','content':[{'type':'tool_result','tool_use_id':f't{i}','content':'def func_' + str(i) + '():\n    pass\n' * 60}]})
print(json.dumps({'model':'claude-sonnet-4-6','max_tokens':100,'stream':False,'messages':msgs}))
"
}

gen_large_request() {
  # ~300K chars (SATURATION stage) — 50 tool_result pairs with ~5K each
  python3 -c "
import json
msgs = [{'role':'user','content':'Refactor this module'}]
for i in range(50):
    msgs.append({'role':'assistant','content':[{'type':'tool_use','id':f't{i}','name':'Read','input':{'file_path':f'/src/module_{i}.py'}}]})
    content = 'line_%d: ' % i + 'x' * 4000 + '\n'
    msgs.append({'role':'user','content':[{'type':'tool_result','tool_use_id':f't{i}','content':content}]})
print(json.dumps({'model':'claude-sonnet-4-6','max_tokens':100,'stream':False,'messages':msgs}))
"
}

gen_oversized_request() {
  # >500KB body (should trigger 413)
  python3 -c "
import json
msgs = [{'role':'user','content':'x' * 600000}]
print(json.dumps({'model':'claude-sonnet-4-6','max_tokens':100,'stream':False,'messages':msgs}))
"
}

gen_many_messages_request() {
  # >30 messages but small total chars (should trigger fifo truncate)
  python3 -c "
import json
msgs = [{'role':'user','content':'Start'}]
for i in range(35):
    msgs.append({'role':'assistant','content':[{'type':'tool_use','id':f't{i}','name':'Bash','input':{'command':f'echo {i}'}}]})
    msgs.append({'role':'user','content':[{'type':'tool_result','tool_use_id':f't{i}','content':f'output {i}'}]})
print(json.dumps({'model':'claude-sonnet-4-6','max_tokens':100,'stream':False,'messages':msgs}))
"
}

# ============================================================
# Main
# ============================================================
echo -e "${CYAN}${BOLD}=== Long-Context Verification Tests ===${NC}"
echo "  Mock:    http://127.0.0.1:$MOCK_PORT"
echo "  Proxy:   http://127.0.0.1:$PROXY_PORT"
echo "  Metrics: $METRICS_PATH"
echo ""

# ---- Start mock backend ----
info "Booting mock backend on :$MOCK_PORT"
MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
MOCK_PLAIN_TEXT="Acknowledged" \
  python3 "$SCRIPT_DIR/mock_backend.py" "$MOCK_PORT" >"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

# ---- Start proxy with pipeline features ENABLED ----
info "Booting proxy on :$PROXY_PORT (pipeline enabled)"
PROXY_CLEAR_ENABLED=true \
PROXY_CLEAR_THRESHOLD=10000 \
PROXY_TOOL_KEEP=3 \
PROXY_COMPRESS_ENABLED=true \
PROXY_COMPRESS_THRESHOLD=2048 \
PROXY_CTX_LIMIT_ENABLED=true \
PROXY_CTX_TRUNCATE_STRATEGY=rounds \
PROXY_CTX_KEEP_ROUNDS=10 \
PROXY_OOM_SAFE_CHARS=500000 \
PROXY_MAX_REQUEST_BYTES=512000 \
PROXY_LOOP_THRESHOLD=99 \
PROXY_BLOCKER_ENABLED=false \
PROXY_DYNAMIC_CONCURRENT_ENABLED=false \
LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
PORT="$PROXY_PORT" \
HOST=127.0.0.1 \
PROXY_METRICS_ENABLED=true \
PROXY_METRICS_DIR="$LOG_DIR" \
PROXY_LOG_PATH="$PROXY_LOG" \
  python3 "$REPO_ROOT/anthropic_proxy.py" >/dev/null 2>&1 &
PROXY_PID=$!
wait_for_port "$PROXY_PORT" "proxy"

echo ""
info "Running test matrix"
echo ""

# ============================================================
# TC-2: Large request interception (413 + pre_truncate)
# ============================================================
echo -e "${CYAN}${BOLD}--- TC-2: Large Request Interception ---${NC}"

# TC-2.1: Oversized body → 413
info "TC-2.1: Body > 500KB → expect 413"
gen_oversized_request > "$LOG_DIR/body_413.json"
HTTP_CODE=$(send_request "$LOG_DIR/body_413.json" "tc21")
if [[ "$HTTP_CODE" == "413" ]]; then
  pass "TC-2.1: Oversized request rejected with 413 (got $HTTP_CODE)"
else
  fail "TC-2.1: Expected 413, got $HTTP_CODE"
fi

# TC-2.2: Large but under 500KB → should reach backend (not 413)
info "TC-2.2: Body < 500KB but >200K chars → expect 200 (not 413)"
gen_large_request > "$LOG_DIR/body_large.json"
BODY_SIZE=$(wc -c < "$LOG_DIR/body_large.json")
HTTP_CODE=$(send_request "$LOG_DIR/body_large.json" "tc22")
if [[ "$HTTP_CODE" == "200" ]]; then
  pass "TC-2.2: Large request passed 413 check (body=${BODY_SIZE}B, got $HTTP_CODE)"
else
  fail "TC-2.2: Expected 200, got $HTTP_CODE (body=${BODY_SIZE}B)"
fi

# TC-2.3: Verify pre_truncate NOT triggered (threshold=500K, input=205K < 500K)
METRICS=$(get_last_metric_all)
INPUT_CHARS=$(echo "$METRICS" | grep '^input_chars=' | cut -d= -f2)
STAGE=$(echo "$METRICS" | grep '^stage=' | cut -d= -f2)
if [[ "$STAGE" == "saturation" || "$STAGE" == "oom_danger" ]]; then
  pass "TC-2.3: Large request (${INPUT_CHARS} chars) reached saturation stage (not pre-truncated)"
else
  fail "TC-2.3: Expected saturation/oom_danger, got '$STAGE' (${INPUT_CHARS} chars)"
fi

echo ""

# ============================================================
# TC-4: Pipeline trigger verification
# ============================================================
echo -e "${CYAN}${BOLD}--- TC-4: Pipeline Trigger Verification ---${NC}"

# TC-4.1: Small request → INIT stage, no pipeline action
info "TC-4.1: Small request → INIT stage"
gen_small_request > "$LOG_DIR/body_small.json"
send_request "$LOG_DIR/body_small.json" "tc41" > /dev/null
METRICS=$(get_last_metric_all)
STAGE=$(echo "$METRICS" | grep '^stage=' | cut -d= -f2)
CHARS=$(echo "$METRICS" | grep '^input_chars=' | cut -d= -f2)
if [[ "$STAGE" == "init" ]]; then
  pass "TC-4.1: Small request (${CHARS} chars) → INIT stage"
else
  fail "TC-4.1: Expected INIT, got '$STAGE' (${CHARS} chars)"
fi

# TC-4.2: Large request with tool_results → tool_clear should trigger
info "TC-4.2: Large request with many tool_results → tool_clear"
gen_large_request > "$LOG_DIR/body_clear.json"
send_request "$LOG_DIR/body_clear.json" "tc42" > /dev/null
sleep 1  # Wait for all metrics to flush (large request may write multiple)
# Search ALL metrics for a record with tool_clear.applied=True
TC42_RESULT=$(python3 -c "
import json
with open('$METRICS_PATH') as f:
    for line in f:
        m = json.loads(line)
        p = m.get('pipeline', {})
        tc = p.get('tool_clear', {})
        if isinstance(tc, dict) and tc.get('applied') == True:
            stage = p.get('lifecycle_stage', {})
            s = stage.get('stage','?') if isinstance(stage, dict) else str(stage)
            print(f'PASS:{tc.get(\"cleared\",0)}:{s}:{m.get(\"input_chars\",0)}')
            break
    else:
        print('FAIL:0:?')
" 2>/dev/null)
TC42_STATUS=$(echo "$TC42_RESULT" | cut -d: -f1)
TC42_CLEARED=$(echo "$TC42_RESULT" | cut -d: -f2)
TC42_STAGE=$(echo "$TC42_RESULT" | cut -d: -f3)
TC42_CHARS=$(echo "$TC42_RESULT" | cut -d: -f4)
if [[ "$TC42_STATUS" == "PASS" ]]; then
  pass "TC-4.2: tool_clear applied ($TC42_CLEARED cleared, stage=$TC42_STAGE, chars=$TC42_CHARS)"
else
  fail "TC-4.2: tool_clear NOT applied in any metrics record"
fi

# TC-4.3: Pipeline summary — at least one L2-L5 action triggered during the test run
info "TC-4.3: Pipeline action summary (any L2-L5 triggered)"
TC43_RESULT=$(python3 -c "
import json
actions = {'tool_clear': 0, 'semantic_compress': 0, 'truncate': 0}
with open('$METRICS_PATH') as f:
    for line in f:
        m = json.loads(line)
        p = m.get('pipeline', {})
        tc = p.get('tool_clear', {})
        if isinstance(tc, dict) and tc.get('applied'):
            actions['tool_clear'] += 1
        sc = p.get('semantic_compress', {})
        if isinstance(sc, dict) and sc.get('enabled'):
            actions['semantic_compress'] += 1
        tr = p.get('truncate', {})
        if isinstance(tr, dict) and tr.get('applied'):
            actions['truncate'] += 1
total = sum(actions.values())
if total > 0:
    print(f'PASS:{total}:{actions}')
else:
    print(f'FAIL:0:{actions}')
" 2>/dev/null)
TC43_STATUS=$(echo "$TC43_RESULT" | cut -d: -f1)
TC43_TOTAL=$(echo "$TC43_RESULT" | cut -d: -f2)
TC43_DETAIL=$(echo "$TC43_RESULT" | cut -d: -f3)
if [[ "$TC43_STATUS" == "PASS" ]]; then
  pass "TC-4.3: $TC43_TOTAL pipeline actions triggered ($TC43_DETAIL)"
else
  fail "TC-4.3: No pipeline actions triggered ($TC43_DETAIL)"
fi

# TC-4.4: semantic_compress metric should exist
info "TC-4.4: semantic_compress metric present"
TC44_RESULT=$(python3 -c "
import json
with open('$METRICS_PATH') as f:
    for line in f:
        m = json.loads(line)
        p = m.get('pipeline', {})
        if 'semantic_compress' in p:
            sc = p['semantic_compress']
            print(f'present:{sc.get(\"enabled\", \"?\")}')
            break
    else:
        print('absent')
" 2>/dev/null)
if [[ "$TC44_RESULT" == present* ]]; then
  pass "TC-4.4: semantic_compress metric present ($TC44_RESULT)"
else
  fail "TC-4.4: semantic_compress metric missing"
fi

echo ""

# ============================================================
# TC-1: Progressive context growth → lifecycle stages
# ============================================================
echo -e "${CYAN}${BOLD}--- TC-1: Lifecycle Stage Transitions ---${NC}"

# TC-1.1: Small → INIT
info "TC-1.1: Small context → INIT"
gen_small_request > "$LOG_DIR/body_init.json"
send_request "$LOG_DIR/body_init.json" "tc11" > /dev/null
METRICS=$(get_last_metric_all)
STAGE=$(echo "$METRICS" | grep '^stage=' | cut -d= -f2)
if [[ "$STAGE" == "init" ]]; then
  pass "TC-1.1: INIT stage confirmed"
else
  fail "TC-1.1: Expected INIT, got '$STAGE'"
fi

# TC-1.2: Medium → GROWTH or EXPANSION
info "TC-1.2: Medium context → GROWTH/EXPANSION"
gen_medium_request > "$LOG_DIR/body_growth.json"
send_request "$LOG_DIR/body_growth.json" "tc12" > /dev/null
METRICS=$(get_last_metric_all)
STAGE=$(echo "$METRICS" | grep '^stage=' | cut -d= -f2)
CHARS=$(echo "$METRICS" | grep '^input_chars=' | cut -d= -f2)
if [[ "$STAGE" == "growth" || "$STAGE" == "expansion" ]]; then
  pass "TC-1.2: $STAGE stage confirmed (${CHARS} chars)"
else
  fail "TC-1.2: Expected growth/expansion, got '$STAGE' (${CHARS} chars)"
fi

# TC-1.3: Large → SATURATION or beyond
info "TC-1.3: Large context → SATURATION+"
gen_large_request > "$LOG_DIR/body_saturation.json"
send_request "$LOG_DIR/body_saturation.json" "tc13" > /dev/null
METRICS=$(get_last_metric_all)
STAGE=$(echo "$METRICS" | grep '^stage=' | cut -d= -f2)
CHARS=$(echo "$METRICS" | grep '^input_chars=' | cut -d= -f2)
if [[ "$STAGE" == "saturation" || "$STAGE" == "oom_danger" || "$STAGE" == "pre_trunc" ]]; then
  pass "TC-1.3: $STAGE stage confirmed (${CHARS} chars)"
else
  fail "TC-1.3: Expected saturation+, got '$STAGE' (${CHARS} chars)"
fi

echo ""

# ============================================================
# Summary
# ============================================================
echo -e "${CYAN}${BOLD}=== Summary ===${NC}"
echo -e "  Passed: ${GREEN}$PASS${NC}"
echo -e "  Failed: ${RED}$FAIL${NC}"
echo -e "  Skipped: ${YELLOW}$SKIP${NC}"
echo -e "  Metrics: $METRICS_PATH"
echo ""

if [[ $FAIL -eq 0 ]]; then
  exit 0
else
  exit 1
fi
