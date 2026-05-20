#!/usr/bin/env bash
# ============================================================
# End-to-end test for anthropic_proxy.py <tools> fallback.
# Hits the live proxy on 4000 (which forwards to llama-server:8081).
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_URL="${PROXY_URL:-http://127.0.0.1:4000}"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:8081}"
MODEL="${E2E_MODEL:-claude-sonnet-4-6}"
TIMEOUT="${E2E_TIMEOUT:-90}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo -e "${GREEN}✅ PASS${NC} $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo -e "${RED}❌ FAIL${NC} $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
info() { echo -e "${CYAN}→${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }

# ============================================================
# Preflight checks
# ============================================================
preflight() {
    info "Preflight: backend $BACKEND_URL"
    if ! curl -s --max-time 5 "$BACKEND_URL/v1/models" >/dev/null; then
        fail "Backend not reachable at $BACKEND_URL"
        exit 1
    fi
    info "Preflight: proxy $PROXY_URL"
    if ! curl -s --max-time 5 "$PROXY_URL/v1/models" >/dev/null; then
        fail "Proxy not reachable at $PROXY_URL"
        exit 1
    fi
    info "Both reachable."
}

# ============================================================
# Test 1: non-streaming tool call
# ============================================================
test_non_streaming_tool_call() {
    local name="Test 1: non-streaming tool call"
    info "$name"
    local req body
    body=$(cat <<'JSON'
{
  "model": "MODEL_PLACEHOLDER",
  "max_tokens": 256,
  "tools": [{
    "name": "get_weather",
    "description": "Get current weather for a city",
    "input_schema": {
      "type": "object",
      "properties": {
        "city": {"type": "string"},
        "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}
      },
      "required": ["city"]
    }
  }],
  "messages": [
    {"role": "user", "content": "What is the weather in Beijing in celsius?"}
  ]
}
JSON
)
    body="${body//MODEL_PLACEHOLDER/$MODEL}"
    local resp
    if ! resp=$(curl -s --max-time "$TIMEOUT" -X POST "$PROXY_URL/v1/messages" \
        -H "Content-Type: application/json" \
        -H "x-api-key: test" \
        -H "anthropic-version: 2023-06-01" \
        -d "$body"); then
        fail "$name (curl error)"
        return
    fi
    # Assert with python: at least one tool_use block with name=get_weather and city=Beijing,
    # and stop_reason == "tool_use".
    if python3 - "$resp" <<'PY'
import json, sys
resp = json.loads(sys.argv[1])
content = resp.get("content", [])
tool_blocks = [b for b in content if b.get("type") == "tool_use"]
assert tool_blocks, f"no tool_use block; content={content}"
gw = [b for b in tool_blocks if b.get("name") == "get_weather"]
assert gw, f"no get_weather tool_use; got names={[b.get('name') for b in tool_blocks]}"
city = gw[0].get("input", {}).get("city", "")
assert "Beijing" in city or "北京" in city, f"unexpected city={city!r}"
assert resp.get("stop_reason") == "tool_use", f"stop_reason={resp.get('stop_reason')!r}"
PY
    then
        pass "$name"
    else
        fail "$name (assertion failed)"
        echo "    response: ${resp:0:500}..."
    fi
}

# ============================================================
# Test 2: streaming tool call
# ============================================================
test_streaming_tool_call() {
    local name="Test 2: streaming tool call"
    info "$name"
    local body
    body=$(cat <<'JSON'
{
  "model": "MODEL_PLACEHOLDER",
  "max_tokens": 256,
  "stream": true,
  "tools": [{
    "name": "get_weather",
    "description": "Get current weather for a city",
    "input_schema": {
      "type": "object",
      "properties": {
        "city": {"type": "string"}
      },
      "required": ["city"]
    }
  }],
  "messages": [
    {"role": "user", "content": "What is the weather in Tokyo?"}
  ]
}
JSON
)
    body="${body//MODEL_PLACEHOLDER/$MODEL}"
    local raw
    if ! raw=$(curl -sN --max-time "$TIMEOUT" -X POST "$PROXY_URL/v1/messages" \
        -H "Content-Type: application/json" \
        -H "x-api-key: test" \
        -H "anthropic-version: 2023-06-01" \
        -d "$body"); then
        fail "$name (curl error)"
        return
    fi
    if python3 - "$raw" <<'PY'
import json, sys, re
sse = sys.argv[1]
events = []
for block in sse.split("\n\n"):
    block = block.strip()
    if not block: continue
    ev_type, data = None, None
    for line in block.splitlines():
        if line.startswith("event: "): ev_type = line[7:]
        elif line.startswith("data: "): data = line[6:]
    if ev_type and data:
        try:
            events.append((ev_type, json.loads(data)))
        except json.JSONDecodeError:
            pass

# Verify required event sequence is present.
types = [t for t, _ in events]
assert "message_start" in types, f"no message_start; types={types}"
assert "message_stop" in types, f"no message_stop"
# A tool_use content_block_start must appear.
tool_starts = [d for t, d in events
               if t == "content_block_start"
               and d.get("content_block", {}).get("type") == "tool_use"]
assert tool_starts, f"no tool_use content_block_start; types={types}"
# A get_weather name should be in the tool_use block.
names = [d["content_block"].get("name") for d in tool_starts]
assert "get_weather" in names, f"get_weather not in tool names: {names}"
# stop_reason in message_delta must be tool_use.
md = [d for t, d in events if t == "message_delta"]
assert md, "no message_delta"
sr = md[-1].get("delta", {}).get("stop_reason")
assert sr == "tool_use", f"stop_reason={sr!r}"
PY
    then
        pass "$name"
    else
        fail "$name (assertion failed)"
        echo "    raw first 600 chars: ${raw:0:600}..."
    fi
}

# ============================================================
# Test 3: regression — plain chat with no tools must still work
# ============================================================
test_plain_chat_regression() {
    local name="Test 3: plain chat regression (no tools)"
    info "$name"
    local body
    body=$(cat <<'JSON'
{
  "model": "MODEL_PLACEHOLDER",
  "max_tokens": 32,
  "messages": [
    {"role": "user", "content": "Say hello in one word."}
  ]
}
JSON
)
    body="${body//MODEL_PLACEHOLDER/$MODEL}"
    local resp
    if ! resp=$(curl -s --max-time "$TIMEOUT" -X POST "$PROXY_URL/v1/messages" \
        -H "Content-Type: application/json" \
        -H "x-api-key: test" \
        -H "anthropic-version: 2023-06-01" \
        -d "$body"); then
        fail "$name (curl error)"
        return
    fi
    if python3 - "$resp" <<'PY'
import json, sys
resp = json.loads(sys.argv[1])
content = resp.get("content", [])
tool_blocks = [b for b in content if b.get("type") == "tool_use"]
text_blocks = [b for b in content if b.get("type") == "text"]
assert not tool_blocks, f"unexpected tool_use blocks in plain response: {tool_blocks}"
assert text_blocks, "no text block in plain response"
assert resp.get("stop_reason") in ("end_turn", "max_tokens"), \
    f"unexpected stop_reason={resp.get('stop_reason')!r}"
PY
    then
        pass "$name"
    else
        fail "$name (assertion failed)"
        echo "    response: ${resp:0:300}..."
    fi
}

# ============================================================
# Main
# ============================================================
echo -e "${CYAN}=== Anthropic Proxy E2E Tool-Call Fallback Tests ===${NC}"
echo "  Proxy:   $PROXY_URL"
echo "  Backend: $BACKEND_URL"
echo "  Model:   $MODEL"
echo ""

preflight
echo ""

test_non_streaming_tool_call
test_streaming_tool_call
test_plain_chat_regression

echo ""
echo -e "${CYAN}=== Summary ===${NC}"
echo -e "  Passed: ${GREEN}${PASS_COUNT}${NC}"
echo -e "  Failed: ${RED}${FAIL_COUNT}${NC}"

if [[ $FAIL_COUNT -eq 0 ]]; then
    exit 0
else
    exit 1
fi
