#!/usr/bin/env bash
# ============================================================
# Integration test for Phase 1 Cache Aligner + common_prefix_ratio.
# Boots a mock OpenAI backend and the proxy once, then sends two
# consecutive requests with the same prefix and verifies:
#   1. The proxy logs a non-zero common_prefix_ratio on the 2nd request.
#   2. The first N messages are preserved in the forwarded prompt.
#
# Run via:
#     bash test/run_tests.sh --integration
# or directly:
#     bash test/integration/test_cache_align_integration.sh
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/itest"
MOCK_PORT="${MOCK_PORT:-8089}"
PROXY_PORT="${PROXY_PORT:-4001}"
CAPTURE_PATH="$LOG_DIR/mock_capture.jsonl"
PROXY_LOG="$LOG_DIR/proxy.log"
MOCK_LOG="$LOG_DIR/mock.log"
METRICS_PATH="$LOG_DIR/proxy_metrics.jsonl"

mkdir -p "$LOG_DIR"
: > "$CAPTURE_PATH"
: > "$PROXY_LOG"
: > "$MOCK_LOG"
rm -f "$METRICS_PATH"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'
PASS=0; FAIL=0
pass() { echo -e "  ${GREEN}PASS${NC}  $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; FAIL=$((FAIL+1)); }
info() { echo -e "${CYAN}→${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }

PROXY_PID=""
MOCK_PID=""

cleanup() {
  set +e
  [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null
  [[ -n "$MOCK_PID"  ]] && kill "$MOCK_PID"  2>/dev/null
  sleep 0.2
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

send_request() {
  local body=$1
  : > "$CAPTURE_PATH"
  if ! curl -sf --max-time 30 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
      -H "Content-Type: application/json" \
      -H "x-api-key: test" \
      -H "anthropic-version: 2023-06-01" \
      -H "x-claude-code-session-id: itest-cache-align" \
      -d "$body" >>"$PROXY_LOG" 2>&1; then
    return 1
  fi
  return 0
}

# ============================================================
# Start mock backend
# ============================================================
info "Starting mock backend on :$MOCK_PORT"
MOCK_PLAIN_TEXT="Hello from mock" \
MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
  python3 "$REPO_ROOT/test/integration/mock_backend.py" "$MOCK_PORT" >>"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

# ============================================================
# Start proxy with Cache Aligner enabled
# ============================================================
info "Starting proxy on :$PROXY_PORT (Cache Aligner enabled)"
PROXY_METRICS_ENABLED=true \
PROXY_METRICS_DIR="$LOG_DIR" \
PROXY_LOG_PATH="$PROXY_LOG" \
PROXY_CACHE_ALIGN_ENABLED=true \
PROXY_CACHE_ALIGN_HEAD=4 \
PROXY_CTX_LIMIT_ENABLED=true \
PROXY_CTX_TRUNCATE_STRATEGY=rounds \
LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
PORT="$PROXY_PORT" \
  python3 "$REPO_ROOT/anthropic_proxy.py" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
wait_for_port "$PROXY_PORT" "proxy"

# ============================================================
# Test cases
# ============================================================
info "TC1: first request establishes baseline"
BODY1='{
  "model": "claude-3-5-sonnet-20241022",
  "max_tokens": 1024,
  "messages": [
    {"role": "system", "content": "You are a helpful coding assistant."},
    {"role": "user", "content": "Project: todo-app. Use the Read tool to inspect files."},
    {"role": "assistant", "content": "Understood."},
    {"role": "user", "content": "Read src/main.py"}
  ],
  "tools": [
    {"name": "Read", "description": "Read a file", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}
  ]
}'
if send_request "$BODY1"; then
  pass "TC1 first request returned 200"
else
  fail "TC1 first request failed"
fi

info "TC2: second request with same prefix should show high common_prefix_ratio"
BODY2='{
  "model": "claude-3-5-sonnet-20241022",
  "max_tokens": 1024,
  "messages": [
    {"role": "system", "content": "You are a helpful coding assistant."},
    {"role": "user", "content": "Project: todo-app. Use the Read tool to inspect files."},
    {"role": "assistant", "content": "Understood."},
    {"role": "user", "content": "Read src/app.py"}
  ],
  "tools": [
    {"name": "Read", "description": "Read a file", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}
  ]
}'
if send_request "$BODY2"; then
  pass "TC2 second request returned 200"
else
  fail "TC2 second request failed"
fi

# Wait for metrics flush
sleep 0.5

info "TC3: verify common_prefix_ratio recorded in metrics"
if [[ -f "$METRICS_PATH" ]]; then
  RATIO=$(python3 -c "
import json, sys
last = None
with open('$METRICS_PATH') as f:
    for line in f:
        last = json.loads(line)
if last and 'pipeline' in last and 'common_prefix_ratio' in last['pipeline']:
    print(last['pipeline']['common_prefix_ratio']['ratio'])
else:
    print('missing')
" 2>/dev/null)
  if [[ "$RATIO" != "missing" && "$RATIO" != "" ]]; then
    if python3 -c "import sys; r=float(sys.argv[1]); sys.exit(0 if r > 0 else 1)" "$RATIO"; then
      pass "TC3 common_prefix_ratio=$RATIO > 0"
    else
      fail "TC3 common_prefix_ratio=$RATIO is not > 0"
    fi
  else
    fail "TC3 common_prefix_ratio missing from metrics"
  fi
else
  fail "TC3 metrics file not found: $METRICS_PATH"
fi

info "TC4: verify first message is preserved as system in forwarded prompt"
if [[ -f "$CAPTURE_PATH" ]]; then
  FIRST_ROLE=$(python3 -c "
import json
with open('$CAPTURE_PATH') as f:
    for line in f:
        rec = json.loads(line)
        body = rec.get('body', {})
        msgs = body.get('messages', [])
        if msgs:
            print(msgs[0].get('role', 'none'))
        break
" 2>/dev/null)
  if [[ "$FIRST_ROLE" == "system" ]]; then
    pass "TC4 first forwarded message role is system"
  else
    fail "TC4 first forwarded message role is '$FIRST_ROLE', expected system"
  fi
else
  fail "TC4 mock capture file not found"
fi

# ============================================================
# Summary
# ============================================================
echo ""
if [[ $FAIL -eq 0 ]]; then
  echo -e "${GREEN}All $PASS cache-align integration tests passed.${NC}"
  exit 0
else
  echo -e "${RED}$FAIL failed, $PASS passed.${NC}"
  exit 1
fi
