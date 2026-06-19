#!/usr/bin/env bash
# ============================================================
# Integration test for Phase 2 semantic compression.
# Boots a mock OpenAI backend and the proxy once, then sends a
# request containing a large JSON tool_result and verifies:
#   1. The forwarded tool_result content is compressed (shorter).
#   2. The proxy metrics record semantic_compress with ratio < 1.0.
#
# Run via:
#     bash test/run_tests.sh --integration
# or directly:
#     bash test/integration/test_compress_integration.sh
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/itest_compress"
MOCK_PORT="${MOCK_PORT:-8090}"
PROXY_PORT="${PROXY_PORT:-4002}"
CAPTURE_PATH="$LOG_DIR/mock_capture_compress.jsonl"
PROXY_LOG="$LOG_DIR/proxy_compress.log"
MOCK_LOG="$LOG_DIR/mock_compress.log"
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
      -H "x-claude-code-session-id: itest-compress" \
      -d "$body" >>"$PROXY_LOG" 2>&1; then
    return 1
  fi
  return 0
}

# ============================================================
# Start mock backend
# ============================================================
info "Starting mock backend on :$MOCK_PORT"
MOCK_PLAIN_TEXT="Acknowledged" \
MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
  python3 "$REPO_ROOT/test/integration/mock_backend.py" "$MOCK_PORT" >>"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

# ============================================================
# Start proxy with semantic compression enabled
# ============================================================
info "Starting proxy on :$PROXY_PORT (semantic compression enabled)"
PROXY_METRICS_ENABLED=true \
PROXY_METRICS_DIR="logs/itest_compress" \
PROXY_LOG_PATH="$PROXY_LOG" \
PROXY_COMPRESS_ENABLED=true \
PROXY_COMPRESS_THRESHOLD=500 \
PROXY_COMPRESS_MODE=semantic \
PROXY_CLEAR_ENABLED=false \
PROXY_CTX_LIMIT_ENABLED=false \
PROXY_CACHE_ALIGN_HEAD=2 \
LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
PORT="$PROXY_PORT" \
  python3 "$REPO_ROOT/anthropic_proxy.py" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
wait_for_port "$PROXY_PORT" "proxy"

# ============================================================
# Build a request with a large JSON tool_result
# ============================================================
LARGE_JSON=$(python3 -c "
import json
items = [{'id': i, 'name': 'item-' + str(i), 'description': 'x' * 200} for i in range(50)]
print(json.dumps(items))
")
ORIG_LEN=${#LARGE_JSON}

BODY=$(python3 -c "
import json
items = [{'id': i, 'name': 'item-' + str(i), 'description': 'x' * 200} for i in range(50)]
large_json = json.dumps(items)
body = {
    'model': 'claude-3-5-sonnet-20241022',
    'max_tokens': 1024,
    'messages': [
        {'role': 'system', 'content': 'You are a coding assistant.'},
        {'role': 'user', 'content': 'Analyze this data.'},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call_1', 'name': 'Read', 'input': {'file_path': '/data/items.json'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_1', 'content': large_json}]},
        {'role': 'assistant', 'content': 'What is the summary?'}
    ]
}
print(json.dumps(body))
")

info "TC1: send request with large JSON tool_result"
if send_request "$BODY"; then
  pass "TC1 request returned 200"
else
  fail "TC1 request failed"
fi

sleep 0.5

# ============================================================
# Verify forwarded content is shorter
# ============================================================
info "TC2: verify forwarded tool_result content is compressed"
FORWARDED_LEN=$(python3 -c "
import json
with open('$CAPTURE_PATH') as f:
    for line in f:
        rec = json.loads(line)
        body = rec.get('body', {})
        for msg in body.get('messages', []):
            if msg.get('role') == 'tool':
                print(len(str(msg.get('content', ''))))
        break
" 2>/dev/null)

if [[ -n "$FORWARDED_LEN" ]]; then
  if [[ "$FORWARDED_LEN" -lt "$ORIG_LEN" ]]; then
    pass "TC2 forwarded length $FORWARDED_LEN < original $ORIG_LEN"
  else
    fail "TC2 forwarded length $FORWARDED_LEN not less than original $ORIG_LEN"
  fi
else
  fail "TC2 could not measure forwarded length"
fi

# ============================================================
# Verify metrics recorded semantic_compress
# ============================================================
info "TC3: verify semantic_compress recorded in metrics"
if [[ -f "$METRICS_PATH" ]]; then
  COMPRESS_METRIC=$(python3 -c "
import json
last = None
with open('$METRICS_PATH') as f:
    for line in f:
        last = json.loads(line)
if last and 'pipeline' in last and 'semantic_compress' in last['pipeline']:
    print(json.dumps(last['pipeline']['semantic_compress']))
else:
    print('missing')
" 2>/dev/null)
  if [[ "$COMPRESS_METRIC" != "missing" && "$COMPRESS_METRIC" != "" ]]; then
    RATIO=$(python3 -c "import sys, json; d=json.loads(sys.argv[1]); print(d.get('ratio', 1.0))" "$COMPRESS_METRIC")
    if python3 -c "import sys; r=float(sys.argv[1]); sys.exit(0 if r < 1.0 else 1)" "$RATIO"; then
      pass "TC3 semantic_compress ratio=$RATIO < 1.0"
    else
      fail "TC3 semantic_compress ratio=$RATIO not < 1.0"
    fi
  else
    fail "TC3 semantic_compress missing from metrics"
  fi
else
  fail "TC3 metrics file not found: $METRICS_PATH"
fi

# ============================================================
# Summary
# ============================================================
echo ""
if [[ $FAIL -eq 0 ]]; then
  echo -e "${GREEN}All $PASS compress integration tests passed.${NC}"
  exit 0
else
  echo -e "${RED}$FAIL failed, $PASS passed.${NC}"
  exit 1
fi
