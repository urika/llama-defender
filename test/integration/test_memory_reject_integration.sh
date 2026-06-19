#!/usr/bin/env bash
# ============================================================
# Integration test for Phase 3 memory pressure active rejection.
# Boots a mock OpenAI backend and a proxy with a fake high-memory
# _get_system_memory, then verifies that /v1/messages returns 503
# with Retry-After header.
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/itest_memory"
MOCK_PORT="${MOCK_PORT:-8091}"
PROXY_PORT="${PROXY_PORT:-4003}"
MOCK_LOG="$LOG_DIR/mock.log"
PROXY_LOG="$LOG_DIR/proxy.log"

mkdir -p "$LOG_DIR"
: > "$MOCK_LOG"
: > "$PROXY_LOG"

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'
PASS=0; FAIL=0
pass() { echo -e "  ${GREEN}PASS${NC}  $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; FAIL=$((FAIL+1)); }
info() { echo -e "${CYAN}→${NC} $1"; }

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

# ============================================================
# Start mock backend
# ============================================================
info "Starting mock backend on :$MOCK_PORT"
MOCK_PLAIN_TEXT="Acknowledged" \
python3 "$REPO_ROOT/test/integration/mock_backend.py" "$MOCK_PORT" >>"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

# ============================================================
# Start proxy with memory pressure monkey-patch
# ============================================================
info "Starting proxy on :$PROXY_PORT (memory pressure 95%)"
PROXY_MEMORY_REJECT_THRESHOLD=90 \
PROXY_DYNAMIC_CONCURRENT_ENABLED=false \
PROXY_CLEAR_ENABLED=false \
PROXY_CTX_LIMIT_ENABLED=false \
LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
PORT="$PROXY_PORT" \
  python3 "$REPO_ROOT/test/integration/mock_proxy_memory_pressure.py" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
wait_for_port "$PROXY_PORT" "proxy"

# ============================================================
# TC1: request should be rejected with 503 + Retry-After
# ============================================================
info "TC1: request under memory pressure returns 503"
BODY='{"model":"claude-3-5-sonnet-20241022","max_tokens":1024,"messages":[{"role":"user","content":"hi"}]}'
RESPONSE_FILE="$LOG_DIR/curl_response.txt"
HEADERS_FILE="$LOG_DIR/curl_headers.txt"
rm -f "$RESPONSE_FILE" "$HEADERS_FILE"

HTTP_CODE=$(curl -s -o "$RESPONSE_FILE" -D "$HEADERS_FILE" -w "%{http_code}" \
  --max-time 10 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: test" \
  -H "anthropic-version: 2023-06-01" \
  -d "$BODY")

if [[ "$HTTP_CODE" == "503" ]]; then
  pass "TC1 HTTP status is 503"
else
  fail "TC1 expected 503, got $HTTP_CODE"
fi

if grep -qi "Retry-After" "$HEADERS_FILE"; then
  pass "TC1 Retry-After header present"
else
  fail "TC1 Retry-After header missing"
fi

if grep -q "backend_oom" "$RESPONSE_FILE"; then
  pass "TC1 error type is backend_oom"
else
  fail "TC1 error type missing in response"
fi

# ============================================================
# Summary
# ============================================================
echo ""
if [[ $FAIL -eq 0 ]]; then
  echo -e "${GREEN}All $PASS memory-reject integration tests passed.${NC}"
  exit 0
else
  echo -e "${RED}$FAIL failed, $PASS passed.${NC}"
  exit 1
fi
