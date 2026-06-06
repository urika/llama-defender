#!/usr/bin/env bash
# ============================================================
# Integration test matrix for blocker detection (Plan A).
# Boots a mock OpenAI backend and the proxy once, then runs a
# matrix of test cases that drive different request patterns
# and assert the [BLOCKER] user message is (or isn't) injected.
#
# Lives under test/integration/ — run via:
#     bash test/run_tests.sh --integration
# or directly:
#     bash test/integration/test_blocker_integration.sh
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

mkdir -p "$LOG_DIR"
: > "$CAPTURE_PATH"
: > "$PROXY_LOG"
: > "$MOCK_LOG"
# Clear metrics file too so the per-run summary is clean
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

# ============================================================
# Helper: send a request body to the proxy and refresh capture.
# ============================================================
send_request() {
  local body=$1
  : > "$CAPTURE_PATH"
  if ! curl -sf --max-time 30 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
      -H "Content-Type: application/json" \
      -H "x-api-key: test" \
      -H "anthropic-version: 2023-06-01" \
      -d "$body" >"$LOG_DIR/resp.json" 2>"$LOG_DIR/curl.err"; then
    cat "$LOG_DIR/curl.err"
    return 1
  fi
  sleep 0.3
  return 0
}

# Helper: read a single record from the mock capture file.
# Echoes the JSON record on stdout, or empty on failure.
read_capture() {
  if [[ -s "$CAPTURE_PATH" ]]; then
    head -1 "$CAPTURE_PATH"
  else
    echo ""
  fi
}

# ============================================================
# Assertion helpers (delegate to inline python so we keep
# string-list duality for OpenAI/Anthropic content).
# ============================================================

# assert_capture_blocker <expected: yes|no> <test_desc>
assert_capture_blocker() {
  local expected=$1 desc=$2
  local capture
  capture=$(read_capture)
  if [[ -z "$capture" ]]; then
    fail "$desc  (no capture)"
    return
  fi
  if EXPECTED="$expected" CAPTURE="$capture" python3 <<'PY' 2>/dev/null
import json, os, sys
expected = os.environ["EXPECTED"]
rec = json.loads(os.environ["CAPTURE"])
def content_text(m):
    c = m.get("content", "")
    if isinstance(c, str): return c
    if isinstance(c, list):
        return "".join(b.get("text","") for b in c if isinstance(b, dict))
    return ""
msgs = rec.get("body", {}).get("messages", [])
has = any(m.get("role") == "user" and "[BLOCKER]" in content_text(m) for m in msgs)
if expected == "yes":
    assert has, f"expected [BLOCKER] but not found in {len(msgs)} messages"
else:
    assert not has, f"unexpected [BLOCKER] in {len(msgs)} messages"
PY
  then
    pass "$desc"
  else
    fail "$desc"
  fi
}

# assert_blocker_run_length <N> <test_desc>
assert_blocker_run_length() {
  local expected=$1 desc=$2
  local capture
  capture=$(read_capture)
  if [[ -z "$capture" ]]; then fail "$desc (no capture)"; return; fi
  if EXPECTED="$expected" CAPTURE="$capture" python3 <<'PY' 2>/dev/null
import json, os, re
expected = int(os.environ["EXPECTED"])
rec = json.loads(os.environ["CAPTURE"])
def content_text(m):
    c = m.get("content", "")
    if isinstance(c, str): return c
    if isinstance(c, list):
        return "".join(b.get("text","") for b in c if isinstance(b, dict))
    return ""
for m in rec.get("body", {}).get("messages", []):
    if m.get("role") == "user" and "[BLOCKER]" in content_text(m):
        text = content_text(m)
        m_run = re.search(r"(\d+) times in a row", text)
        assert m_run, f"no 'N times' in: {text!r}"
        got = int(m_run.group(1))
        assert got == expected, f"expected run_length={expected}, got {got}"
        break
else:
    assert False, "no [BLOCKER] message"
PY
  then
    pass "$desc"
  else
    fail "$desc"
  fi
}

# assert_blocker_metadata <tool_name> <error_type> <test_desc>
assert_blocker_metadata() {
  local tool=$1 err=$2 desc=$3
  local capture
  capture=$(read_capture)
  if [[ -z "$capture" ]]; then fail "$desc (no capture)"; return; fi
  if TOOL="$tool" ERR="$err" CAPTURE="$capture" python3 <<'PY' 2>/dev/null
import json, os
rec = json.loads(os.environ["CAPTURE"])
def content_text(m):
    c = m.get("content", "")
    if isinstance(c, str): return c
    if isinstance(c, list):
        return "".join(b.get("text","") for b in c if isinstance(b, dict))
    return ""
for m in rec.get("body", {}).get("messages", []):
    if m.get("role") == "user" and "[BLOCKER]" in content_text(m):
        text = content_text(m)
        tool = os.environ["TOOL"]
        err  = os.environ["ERR"]
        assert tool in text, f"tool {tool!r} not in: {text!r}"
        assert err  in text, f"error {err!r} not in: {text!r}"
        break
else:
    assert False, "no [BLOCKER] message"
PY
  then
    pass "$desc"
  else
    fail "$desc"
  fi
}

# ============================================================
# Test cases
# ============================================================
# Each test is a self-contained bash function. It builds a body,
# calls send_request, then runs one or more assertions. Failures
# are recorded but do not abort the script (set -uo pipefail only).

# Helper: build a 2-failed-Read body
two_failing_reads_body() {
  cat <<'JSON'
{
  "model": "claude-sonnet-4-6", "max_tokens": 256, "stream": false,
  "messages": [
    {"role":"user","content":"read /nope.py"},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/nope.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t1",
       "content":"<tool_use_error>File does not exist. /nope.py</tool_use_error>"}
    ]},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t2","name":"Read","input":{"file_path":"/nope.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t2",
       "content":"<tool_use_error>File does not exist. /nope.py</tool_use_error>"}
    ]}
  ]
}
JSON
}

test_positive_file_not_found() {
  info "TC1: 2 consecutive file_not_found → expect [BLOCKER]"
  local body
  body=$(two_failing_reads_body)
  send_request "$body" || { fail "TC1 curl failed"; return; }
  assert_capture_blocker "yes" "TC1.1 [BLOCKER] injected"
  assert_blocker_metadata "Read" "file_not_found" "TC1.2 metadata = Read/file_not_found"
  assert_blocker_run_length "2" "TC1.3 run_length=2 in message"
}

test_positive_wasted() {
  info "TC2: 2 consecutive 'Wasted call' → expect [BLOCKER] (error_type=wasted)"
  local body
  body=$(cat <<'JSON'
{
  "model": "claude-sonnet-4-6", "max_tokens": 256, "stream": false,
  "messages": [
    {"role":"user","content":"read /foo.py again"},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/foo.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t1",
       "content":"<tool_use_error>Wasted call - file unchanged since last read</tool_use_error>"}
    ]},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t2","name":"Read","input":{"file_path":"/foo.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t2",
       "content":"<tool_use_error>Wasted call - file unchanged since last read</tool_use_error>"}
    ]}
  ]
}
JSON
)
  send_request "$body" || { fail "TC2 curl failed"; return; }
  assert_capture_blocker "yes" "TC2.1 [BLOCKER] injected"
  assert_blocker_metadata "Read" "wasted" "TC2.2 metadata = Read/wasted"
}

test_positive_input_validation() {
  info "TC3: 2 consecutive InputValidationError → expect [BLOCKER] (error_type=input_validation)"
  local body
  body=$(cat <<'JSON'
{
  "model": "claude-sonnet-4-6", "max_tokens": 256, "stream": false,
  "messages": [
    {"role":"user","content":"run git status"},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t1","name":"Bash","input":{"command":"git statuz"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t1",
       "content":"InputValidationError: missing required parameter 'command'"}
    ]},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t2","name":"Bash","input":{"command":""}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t2",
       "content":"InputValidationError: missing required parameter 'command'"}
    ]}
  ]
}
JSON
)
  send_request "$body" || { fail "TC3 curl failed"; return; }
  assert_capture_blocker "yes" "TC3.1 [BLOCKER] injected"
  assert_blocker_metadata "Bash" "input_validation" "TC3.2 metadata = Bash/input_validation"
}

test_positive_run_length_3() {
  info "TC4: 3 consecutive file_not_found → expect run_length=3 in message"
  local body
  body=$(cat <<'JSON'
{
  "model": "claude-sonnet-4-6", "max_tokens": 256, "stream": false,
  "messages": [
    {"role":"user","content":"read /nope.py"},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/nope.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t1",
       "content":"<tool_use_error>File does not exist. /nope.py</tool_use_error>"}
    ]},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t2","name":"Read","input":{"file_path":"/nope.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t2",
       "content":"<tool_use_error>File does not exist. /nope.py</tool_use_error>"}
    ]},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t3","name":"Read","input":{"file_path":"/nope.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t3",
       "content":"<tool_use_error>File does not exist. /nope.py</tool_use_error>"}
    ]}
  ]
}
JSON
)
  send_request "$body" || { fail "TC4 curl failed"; return; }
  assert_capture_blocker "yes" "TC4.1 [BLOCKER] injected"
  assert_blocker_run_length "3" "TC4.2 run_length=3 in message"
}

test_negative_one_failure() {
  info "TC5: 1 file_not_found only → expect NO [BLOCKER] (below threshold)"
  local body
  body=$(cat <<'JSON'
{
  "model": "claude-sonnet-4-6", "max_tokens": 256, "stream": false,
  "messages": [
    {"role":"user","content":"read /nope.py"},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/nope.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t1",
       "content":"<tool_use_error>File does not exist. /nope.py</tool_use_error>"}
    ]}
  ]
}
JSON
)
  send_request "$body" || { fail "TC5 curl failed"; return; }
  assert_capture_blocker "no" "TC5.1 no [BLOCKER] injected"
}

test_negative_mixed_types() {
  info "TC6: mixed error types (wasted + file_not_found) → expect NO [BLOCKER]"
  local body
  body=$(cat <<'JSON'
{
  "model": "claude-sonnet-4-6", "max_tokens": 256, "stream": false,
  "messages": [
    {"role":"user","content":"read /foo.py then /bar.py"},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/foo.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t1",
       "content":"<tool_use_error>Wasted call - file unchanged</tool_use_error>"}
    ]},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t2","name":"Read","input":{"file_path":"/bar.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t2",
       "content":"<tool_use_error>File does not exist. /bar.py</tool_use_error>"}
    ]}
  ]
}
JSON
)
  send_request "$body" || { fail "TC6 curl failed"; return; }
  assert_capture_blocker "no" "TC6.1 no [BLOCKER] (mixed types break the run)"
}

test_negative_recovery() {
  info "TC7: 2 errors → 1 success → 1 error → expect NO [BLOCKER] (success breaks run)"
  local body
  body=$(cat <<'JSON'
{
  "model": "claude-sonnet-4-6", "max_tokens": 256, "stream": false,
  "messages": [
    {"role":"user","content":"explore these files"},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/a.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t1",
       "content":"<tool_use_error>File does not exist. /a.py</tool_use_error>"}
    ]},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t2","name":"Read","input":{"file_path":"/b.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t2",
       "content":"<tool_use_error>File does not exist. /b.py</tool_use_error>"}
    ]},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t3","name":"Read","input":{"file_path":"/c.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t3",
       "content":"def c(): return 1\n"}
    ]},
    {"role":"assistant","content":[
      {"type":"tool_use","id":"t4","name":"Read","input":{"file_path":"/d.py"}}
    ]},
    {"role":"user","content":[
      {"type":"tool_result","tool_use_id":"t4",
       "content":"<tool_use_error>File does not exist. /d.py</tool_use_error>"}
    ]}
  ]
}
JSON
)
  send_request "$body" || { fail "TC7 curl failed"; return; }
  assert_capture_blocker "no" "TC7.1 no [BLOCKER] (success in middle breaks run)"
}

# ============================================================
# Main
# ============================================================
echo -e "${CYAN}=== Blocker integration test matrix ===${NC}"
echo "  Mock:    http://127.0.0.1:$MOCK_PORT"
echo "  Proxy:   http://127.0.0.1:$PROXY_PORT"
echo "  Capture: $CAPTURE_PATH"
echo "  Logs:    $LOG_DIR"
echo ""

info "Booting mock backend on :$MOCK_PORT"
MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
  python3 "$SCRIPT_DIR/mock_backend.py" "$MOCK_PORT" >"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

info "Booting proxy on :$PROXY_PORT"
PROXY_BLOCKER_ENABLED=true \
PROXY_BLOCKER_THRESHOLD=2 \
PROXY_CLEAR_ENABLED=false \
PROXY_CTX_LIMIT_ENABLED=false \
PROXY_LOOP_THRESHOLD=99 \
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

test_positive_file_not_found
test_positive_wasted
test_positive_input_validation
test_positive_run_length_3
test_negative_one_failure
test_negative_mixed_types
test_negative_recovery

echo ""
echo -e "${CYAN}=== Summary ===${NC}"
echo -e "  Passed: ${GREEN}$PASS${NC}"
echo -e "  Failed: ${RED}$FAIL${NC}"
echo -e "  Logs:   $LOG_DIR"
echo ""

# On full pass, also dump proxy_metrics.jsonl summary for observability.
if [[ $FAIL -eq 0 && -f "$LOG_DIR/proxy_metrics.jsonl" ]]; then
  info "Metrics summary (per request):"
  python3 -c "
import json
with open('$LOG_DIR/proxy_metrics.jsonl') as f:
    for i, line in enumerate(f, 1):
        m = json.loads(line)
        qf = m.get('quality_flags', [])
        bd = m.get('pipeline', {}).get('blocker_detect', {})
        trig = 'TRIG' if bd.get('triggered') else '----'
        rl   = bd.get('run_length', '-')
        et   = bd.get('error_type', '-') or '-'
        tn   = bd.get('tool_name', '-') or '-'
        print(f'  req#{i}: blocker={trig} tool={tn:<10} err={et:<18} run_len={rl}  flags={qf}')
"
fi

if [[ $FAIL -eq 0 ]]; then
  exit 0
else
  exit 1
fi
