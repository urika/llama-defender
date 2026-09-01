#!/usr/bin/env bash
# ============================================================
# Integration test for Phase 3 /status page enhancements.
# Boots a mock OpenAI backend and the proxy, sends a request,
# then verifies the /status HTML contains Context Optimization
# card fields.
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/test/lib/diag_cleanup.sh"
LOG_DIR="$REPO_ROOT/logs/itest_status"
MOCK_PORT="${MOCK_PORT:-8092}"
PROXY_PORT="${PROXY_PORT:-4004}"
MOCK_LOG="$LOG_DIR/mock.log"
PROXY_LOG="$LOG_DIR/proxy.log"
METRICS_PATH="$LOG_DIR/proxy_metrics.jsonl"

mkdir -p "$LOG_DIR"
: > "$MOCK_LOG"
: > "$PROXY_LOG"
rm -f "$METRICS_PATH"

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
  diag_cleanup "$REPO_ROOT" "itest-st"
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
# Start proxy with metrics enabled
# ============================================================
info "Starting proxy on :$PROXY_PORT"
PROXY_METRICS_ENABLED=true \
PROXY_METRICS_DIR="logs/itest_status" \
PROXY_LOG_PATH="$PROXY_LOG" \
PROXY_CLEAR_ENABLED=false \
PROXY_CTX_LIMIT_ENABLED=false \
PROXY_DYNAMIC_CONCURRENT_ENABLED=false \
LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
PORT="$PROXY_PORT" \
  python3 "$REPO_ROOT/anthropic_proxy.py" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
wait_for_port "$PROXY_PORT" "proxy"

# ============================================================
# TC1: send a request so metrics exist
# ============================================================
info "TC1: send a simple request"
BODY='{"model":"claude-3-5-sonnet-20241022","max_tokens":1024,"messages":[{"role":"system","content":"You are a coder"},{"role":"user","content":"hi"}]}'
if curl -sf --max-time 10 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: test" \
    -H "anthropic-version: 2023-06-01" \
    -d "$BODY" >/dev/null 2>&1; then
  pass "TC1 request returned successfully"
else
  fail "TC1 request failed"
fi

sleep 0.5

# ============================================================
# TC2: /status contains Context Optimization card
# ============================================================
info "TC2: verify /status Context Optimization card"
STATUS_HTML="$LOG_DIR/status.html"
if ! curl -sf --max-time 10 "http://127.0.0.1:$PROXY_PORT/status" -o "$STATUS_HTML"; then
  fail "TC2 could not fetch /status"
fi

for label in "Avg Prefix Ratio" "Avg Compression" "Loop Triggered" "Blocker Triggered" "Max Concurrent"; do
  if grep -q "$label" "$STATUS_HTML"; then
    pass "TC2 status page contains '$label'"
  else
    fail "TC2 status page missing '$label'"
  fi
done

# ============================================================
# TC3: /metrics endpoint returns valid schema (v2)
# ============================================================
info "TC3: verify /metrics endpoint returns valid schema"
METRICS_JSON="$LOG_DIR/metrics.json"
if curl -sf --max-time 10 "http://127.0.0.1:$PROXY_PORT/metrics" -o "$METRICS_JSON"; then
  SCHEMA=$(python3 -c "import json,sys; d=json.load(open('$METRICS_JSON')); print(d.get('schema',''))" 2>/dev/null)
  if [[ "$SCHEMA" == "v2" ]]; then
    pass "TC3 /metrics schema is v2"
  else
    fail "TC3 /metrics schema is '$SCHEMA', expected v2"
  fi
else
  fail "TC3 could not fetch /metrics"
fi

# ============================================================
# TC4: /api/status returns structured JSON (R1/R2)
# ============================================================
info "TC4: verify /api/status returns structured JSON"
STATUS_JSON="$LOG_DIR/status.json"
STATUS_CODE=$(curl -s -o "$STATUS_JSON" -w "%{http_code}" --max-time 10 "http://127.0.0.1:$PROXY_PORT/api/status")
if [[ "$STATUS_CODE" =~ ^(200|503)$ ]]; then
  pass "TC4 /api/status returned HTTP $STATUS_CODE"
  if python3 -c "import json,sys; d=json.load(open('$STATUS_JSON')); assert 'proxy' in d; assert 'backend' in d; assert 'state' in d; assert 'ready' in d; assert 'active_profile' in d" 2>/dev/null; then
    pass "TC4 /api/status has required schema fields"
  else
    fail "TC4 /api/status missing required schema fields"
  fi
  STATE=$(python3 -c "import json,sys; print(json.load(open('$STATUS_JSON')).get('state',''))" 2>/dev/null)
  if [[ "$STATE" =~ ^(healthy|starting|backend_down|proxy_down|model_drift|down)$ ]]; then
    pass "TC4 /api/status state is valid ($STATE)"
  else
    fail "TC4 /api/status state is '$STATE', expected valid enum"
  fi
else
  fail "TC4 could not fetch /api/status (HTTP $STATUS_CODE)"
fi

# ============================================================
# TC5: /api/profiles returns profile list with active flag (R6)
# ============================================================
info "TC5: verify /api/profiles returns structured list"
PROFILES_JSON="$LOG_DIR/profiles.json"
if curl -sf --max-time 10 "http://127.0.0.1:$PROXY_PORT/api/profiles" -o "$PROFILES_JSON"; then
  pass "TC5 /api/profiles returned JSON"
  if python3 -c "import json,sys; d=json.load(open('$PROFILES_JSON')); assert 'profiles' in d; assert len(d['profiles']) > 0; active=[p for p in d['profiles'] if p.get('active')]; assert len(active)==1; assert 'name' in active[0]; assert 'memory_gb' in active[0]" 2>/dev/null; then
    pass "TC5 /api/profiles schema is valid"
  else
    fail "TC5 /api/profiles schema invalid"
  fi
else
  fail "TC5 could not fetch /api/profiles"
fi

# ============================================================
# Summary
# ============================================================
echo ""
if [[ $FAIL -eq 0 ]]; then
  echo -e "${GREEN}All $PASS status integration tests passed.${NC}"
  exit 0
else
  echo -e "${RED}$FAIL failed, $PASS passed.${NC}"
  exit 1
fi
