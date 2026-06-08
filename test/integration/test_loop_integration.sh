#!/usr/bin/env bash
# ============================================================
# Integration test for loop detection — cross-request session
# persistence and escalation behavior.
#
# Validates:
#   TC1: Level 1 — 3 identical tool_use calls → hint injected
#   TC2: Level 2 — 6+ identical calls → tool removed from list
#   TC3: Level 3 — 9+ identical calls → all tools stripped
#   TC4: De-escalation — session resets after clean request
#   TC5: Cross-request persistence — session remembers Level 2+
#
# Lives under test/integration/ — run via:
#     bash test/integration/test_loop_integration.sh
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/itest_loop"
MOCK_PORT="${MOCK_PORT:-8090}"
PROXY_PORT="${PROXY_PORT:-4002}"
CAPTURE_PATH="$LOG_DIR/mock_capture.jsonl"
PROXY_LOG="$LOG_DIR/proxy.log"
MOCK_LOG="$LOG_DIR/mock.log"

mkdir -p "$LOG_DIR"
: > "$CAPTURE_PATH"
: > "$PROXY_LOG"
: > "$MOCK_LOG"
rm -f "$LOG_DIR/proxy_metrics.jsonl"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'
PASS=0; FAIL=0
pass() { echo -e "  ${GREEN}PASS${NC}  $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; FAIL=$((FAIL+1)); }
info() { echo -e "${CYAN}→${NC} $1"; }

PROXY_PID=""
MOCK_PID=""
SESSION_ID="loop-test-$(date +%s)"

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
  exit 1
}

send_request() {
  local body=$1
  : > "$CAPTURE_PATH"
  if ! curl -sf --max-time 30 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
      -H "Content-Type: application/json" \
      -H "x-api-key: test" \
      -H "anthropic-version: 2023-06-01" \
      -H "x-claude-code-session-id: $SESSION_ID" \
      -d "$body" >"$LOG_DIR/resp.json" 2>"$LOG_DIR/curl.err"; then
    cat "$LOG_DIR/curl.err"
    return 1
  fi
  sleep 0.3
  return 0
}

read_capture() {
  if [[ -s "$CAPTURE_PATH" ]]; then
    head -1 "$CAPTURE_PATH"
  else
    echo ""
  fi
}

# Assert the captured request contains (or doesn't) a loop hint
assert_loop_hint() {
  local expected=$1 desc=$2
  local capture
  capture=$(read_capture)
  if [[ -z "$capture" ]]; then
    fail "$desc  (no capture)"
    return
  fi
  if EXPECTED="$expected" CAPTURE="$capture" python3 <<'PY' 2>/dev/null
import json, os
expected = os.environ["EXPECTED"]
rec = json.loads(os.environ["CAPTURE"])
def content_text(m):
    c = m.get("content", "")
    if isinstance(c, str): return c
    if isinstance(c, list):
        return "".join(b.get("text","") for b in c if isinstance(b, dict))
    return ""
msgs = rec.get("body", {}).get("messages", [])
has_hint = any(m.get("role") == "user" and "loop" in content_text(m).lower() for m in msgs)
if expected == "yes":
    assert has_hint, f"expected loop hint but not found"
else:
    assert not has_hint, f"unexpected loop hint found"
PY
  then
    pass "$desc"
  else
    fail "$desc"
  fi
}

# Assert the number of tools in the forwarded request
assert_tool_count() {
  local expected=$1 desc=$2
  local capture
  capture=$(read_capture)
  if [[ -z "$capture" ]]; then
    fail "$desc  (no capture)"
    return
  fi
  if EXPECTED="$expected" CAPTURE="$capture" python3 <<'PY' 2>/dev/null
import json, os
expected = int(os.environ["EXPECTED"])
rec = json.loads(os.environ["CAPTURE"])
tools = rec.get("body", {}).get("tools", [])
got = len(tools)
assert got == expected, f"expected {expected} tools, got {got}"
PY
  then
    pass "$desc"
  else
    fail "$desc"
  fi
}

# Build a request with N consecutive identical Read tool_uses + tool_results
# and M tools defined. Uses Python to ensure valid JSON.
build_loop_body() {
  local n=$1 tool_count=$2
  python3 -c "
import json
n = $n
tc = $tool_count
tools = [{'type':'custom','name':'Read','description':'Read file','input_schema':{'type':'object','properties':{'file_path':{'type':'string'}}}}]
tools += [{'type':'custom','name':f'Tool{i}','description':f'Tool {i}','input_schema':{'type':'object','properties':{'x':{'type':'string'}}}} for i in range(1, tc+1)]
msgs = [{'role':'user','content':'read file'}]
for i in range(1, n+1):
    msgs.append({'role':'assistant','content':[{'type':'tool_use','id':f't{i}','name':'Read','input':{'file_path':'/loop.py'}}]})
    msgs.append({'role':'user','content':[{'type':'tool_result','tool_use_id':f't{i}','content':'file contents here'}]})
print(json.dumps({'model':'claude-sonnet-4-6','max_tokens':256,'stream':False,'tools':tools,'messages':msgs}))
" 2>/dev/null
}

# ============================================================
# Test cases
# ============================================================

test_level1_hint() {
  info "TC1: 3 identical Read calls → Level 1 loop hint"
  local body
  body=$(build_loop_body 3 10)
  send_request "$body" || { fail "TC1 curl failed"; return; }
  assert_loop_hint "yes" "TC1.1 loop hint injected"
}

test_level2_tool_removed() {
  info "TC2: 6 identical Read calls → Level 2, tool removed from list"
  local body
  body=$(build_loop_body 6 10)
  send_request "$body" || { fail "TC2 curl failed"; return; }
  assert_tool_count 10 "TC2.1 Read tool removed (11→10)"
}

test_level3_all_tools_stripped() {
  info "TC3: 9 identical Read calls → Level 3, all tools stripped"
  local body
  body=$(build_loop_body 9 10)
  send_request "$body" || { fail "TC3 curl failed"; return; }
  assert_tool_count 0 "TC3.1 all tools stripped"
}

test_no_loop_below_threshold() {
  info "TC4: 2 identical calls → no loop intervention"
  local body
  body=$(build_loop_body 2 10)
  send_request "$body" || { fail "TC4 curl failed"; return; }
  assert_loop_hint "no" "TC4.1 no loop hint"
  assert_tool_count 11 "TC4.2 all tools preserved"
}

test_cross_request_persistence() {
  info "TC5: cross-request — Level 2 persists across requests"
  local body1 body2
  SESSION_ID="persist-test-$(date +%s)"
  body1=$(build_loop_body 6 10)
  send_request "$body1" || { fail "TC5.1 curl failed"; return; }
  assert_tool_count 10 "TC5.1 first request: Read removed (11→10)"

  : > "$CAPTURE_PATH"
  body2=$(build_loop_body 2 10)
  send_request "$body2" || { fail "TC5.2 curl failed"; return; }
  assert_loop_hint "yes" "TC5.2 second request: persistent warning even with low count"
  assert_tool_count 11 "TC5.3 second request: tools restored (warning only, no removal)"
}

# ============================================================
# Main
# ============================================================
echo -e "${CYAN}=== Loop detection integration test ===${NC}"
echo "  Mock:    http://127.0.0.1:$MOCK_PORT"
echo "  Proxy:   http://127.0.0.1:$PROXY_PORT"
echo "  Session: $SESSION_ID"
echo ""

info "Booting mock backend on :$MOCK_PORT"
MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
  python3 "$SCRIPT_DIR/mock_backend.py" "$MOCK_PORT" >"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

info "Booting proxy on :$PROXY_PORT"
PROXY_BLOCKER_ENABLED=false \
PROXY_CLEAR_ENABLED=false \
PROXY_CTX_LIMIT_ENABLED=false \
PROXY_DEDUP_WINDOW=0 \
PROXY_LOOP_THRESHOLD=3 \
PROXY_LOOP_LEVEL2=6 \
PROXY_LOOP_LEVEL3=9 \
PROXY_TOOL_FILTER_ENABLED=false \
PROXY_MAX_CONCURRENT=1 \
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

test_no_loop_below_threshold
test_level1_hint
test_level2_tool_removed
test_level3_all_tools_stripped
test_cross_request_persistence

echo ""
echo -e "${CYAN}=== Summary ===${NC}"
echo -e "  Passed: ${GREEN}$PASS${NC}"
echo -e "  Failed: ${RED}$FAIL${NC}"
echo -e "  Logs:   $LOG_DIR"
echo ""

if [[ $FAIL -eq 0 ]]; then
  exit 0
else
  exit 1
fi
