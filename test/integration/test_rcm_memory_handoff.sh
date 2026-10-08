#!/usr/bin/env bash
# ============================================================
# Integration test for R-CM-1/R-CM-2 (cross-session memory + auto handoff).
#
# 隔离影子代理（工作树代码）+ mock 后端，独立端口/状态目录，不触碰生产。
# 端到端验证：
#   1. POST /admin/memory 写入 → 会话首请求尾部注入 [System: MEMORY …]
#   2. 同会话第二次请求不再注入（首请求守卫）
#   3. POST /admin/handoff 生成 → 目标会话首请求注入 [System: HANDOFF …]
#   4. 同会话第二次不再注入
#   5. GET /api/memory、GET /api/handoff 端点可读
#   注入证据取 mock 后端捕获的实际请求体（模型实际所见）。
#
# 会话键固定 itestrcma/itestrcmb（8 字符）。
# Run:  bash test/run_tests.sh --integration
#   或: bash test/integration/test_rcm_memory_handoff.sh
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/test/lib/diag_cleanup.sh"
LOG_DIR="$REPO_ROOT/logs/itest_rcm"
MOCK_PORT="${MOCK_PORT:-8093}"
PROXY_PORT="${PROXY_PORT:-4005}"
SID_A="itestrcma"
SID_B="itestrcmb"
PROJECT="itest"
CAPTURE_PATH="$LOG_DIR/mock_capture.jsonl"
PROXY_LOG="$LOG_DIR/proxy.log"
MOCK_LOG="$LOG_DIR/mock.log"

mkdir -p "$LOG_DIR"
: > "$CAPTURE_PATH"
: > "$PROXY_LOG"
: > "$MOCK_LOG"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
PASS=0; FAIL=0
pass() { echo -e "  ${GREEN}PASS${NC}  $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; FAIL=$((FAIL+1)); }
info() { echo -e "${CYAN}→${NC} $1"; }

PROXY_PID=""; MOCK_PID=""
cleanup() {
  set +e
  diag_cleanup "$REPO_ROOT" "itestrcm"
  [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null
  [[ -n "$MOCK_PID"  ]] && kill "$MOCK_PID"  2>/dev/null
  sleep 0.3
  [[ -n "$PROXY_PID" ]] && kill -9 "$PROXY_PID" 2>/dev/null
  [[ -n "$MOCK_PID"  ]] && kill -9 "$MOCK_PID"  2>/dev/null
}
trap cleanup EXIT

wait_for_port() {
  local port=$1 name=$2
  for _ in $(seq 1 60); do
    if curl -sf --max-time 1 "http://127.0.0.1:$port/v1/models" >/dev/null 2>&1; then
      info "$name up on :$port"; return 0
    fi
    sleep 0.1
  done
  fail "$name failed to start on :$port"
  tail -30 "$PROXY_LOG" 2>/dev/null; tail -30 "$MOCK_LOG" 2>/dev/null
  exit 1
}

# send_request <sid> <user-text>
send_request() {
  local sid=$1 text=$2
  curl -sf --max-time 30 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: test" \
    -H "anthropic-version: 2023-06-01" \
    -H "x-claude-code-session-id: $sid" \
    -d "{\"model\":\"mock\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"$text\"}]}" \
    >/dev/null 2>&1
}

# ============================================================
# Boot mock backend + shadow proxy (R-CM on, isolated state)
# ============================================================
MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
  python3 "$REPO_ROOT/test/integration/mock_backend.py" "$MOCK_PORT" >>"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

info "Starting shadow proxy on :$PROXY_PORT (R-CM-1/R-CM-2 on, isolated state)"
PORT="$PROXY_PORT" \
LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
PROXY_METRICS_ENABLED=false \
PROXY_LOG_PATH="$PROXY_LOG" \
PROXY_DIAG_ENABLED=false \
PROXY_HBE_ENABLED=false \
PROXY_QUEUE_ENABLED=false \
PROXY_COMPRESS_ENABLED=false \
PROXY_CLEAR_ENABLED=false \
PROXY_CTX_ENGINE_ENABLED="${RCM_ENGINE:-false}" \
PROXY_MEMORY_ENABLED=true \
PROXY_MEMORY_DB_PATH="$LOG_DIR/memory.db" \
PROXY_MEMORY_DEFAULT_PROJECT="$PROJECT" \
PROXY_HANDOFF_ENABLED=true \
PROXY_HANDOFF_DIR="$LOG_DIR/handoff" \
  python3 "$REPO_ROOT/anthropic_proxy.py" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
wait_for_port "$PROXY_PORT" "proxy"
BASE="http://127.0.0.1:$PROXY_PORT"

# ============================================================
# Seed memory (R-CM-1)
# ============================================================
info "POST /admin/memory (seed decision)"
SEED=$(curl -sf --max-time 5 -XPOST "$BASE/admin/memory" \
  -H 'Content-Type: application/json' \
  -d "{\"text\":\"决定使用 drizzle ORM 方案\",\"kind\":\"decision\",\"project\":\"$PROJECT\"}")
if echo "$SEED" | grep -q '"ok": true'; then
  pass "memory seeded: $SEED"
else
  fail "memory seed failed: $SEED"
fi

# ============================================================
# R-CM-1 injection + guard (session A, 2 requests)
# ============================================================
info "session A req1 (expect MEMORY injected)"
send_request "$SID_A" "drizzle 怎么配置"
info "session A req2 (expect NO re-inject)"
send_request "$SID_A" "drizzle 再讲讲"

# ============================================================
# R-CM-2 generate + injection + guard (session B)
# ============================================================
info "POST /admin/handoff (session B)"
HO=$(curl -sf --max-time 5 -XPOST "$BASE/admin/handoff" \
  -H 'Content-Type: application/json' \
  -d "{\"session_key\":\"$SID_B\",\"goal\":\"验证自动交接\",\"decisions\":[\"采用方案 X\"]}")
if echo "$HO" | grep -q '"stored": true'; then
  pass "handoff generated for $SID_B"
else
  fail "handoff generation failed: $HO"
fi
info "session B req1 (expect HANDOFF injected)"
send_request "$SID_B" "继续"
info "session B req2 (expect NO re-inject)"
send_request "$SID_B" "继续2"

# ============================================================
# GET endpoints
# ============================================================
MEM=$(curl -sf --max-time 5 "$BASE/api/memory?project=$PROJECT")
if echo "$MEM" | grep -q "drizzle"; then pass "GET /api/memory returns seeded item"
else fail "GET /api/memory missing item: $MEM"; fi

HOG=$(curl -sf --max-time 5 "$BASE/api/handoff?session=$SID_B")
if echo "$HOG" | grep -q "验证自动交接"; then pass "GET /api/handoff returns record"
else fail "GET /api/handoff failed: $HOG"; fi

# ============================================================
# Assert injections in mock-captured backend requests (4 lines, in order)
# ============================================================
# engine-off: 注入为 view-only 单轮 → req2 不应再出现。
# engine-on : 注入持久进 canonical（跨轮存活，设计预期）→ req2 仍在，但
#             **计数不得增长**（守卫防重复注入），即 req2 计数 == req1 计数。
info "Verifying injected blocks in captured backend request bodies (engine=${RCM_ENGINE:-false})"
RESULT=$(RCM_ENGINE="${RCM_ENGINE:-false}" python3 - "$CAPTURE_PATH" <<'PY'
import json, os, sys
path = sys.argv[1]
engine = os.environ.get("RCM_ENGINE", "false").lower() in ("1", "true", "yes")
lines = [l for l in open(path, encoding="utf-8") if l.strip()]
def count(i, token):
    if i >= len(lines):
        return 0
    try:
        s = json.dumps(json.loads(lines[i]), ensure_ascii=False)
    except Exception:
        s = lines[i]
    return s.count(token)
out = {
    "n": len(lines),
    "a1": count(0, "System: MEMORY"),
    "a2": count(1, "System: MEMORY"),
    "b1": count(2, "System: HANDOFF"),
    "b2": count(3, "System: HANDOFF"),
}
# engine-off 期望 a2=b2=0；engine-on 期望 a2==a1 且 b2==b1（无增长）
out["ok_a"] = (out["a2"] == out["a1"]) if engine else (out["a2"] == 0)
out["ok_b"] = (out["b2"] == out["b1"]) if engine else (out["b2"] == 0)
print(json.dumps(out))
PY
)
echo "    capture: $RESULT"
if echo "$RESULT" | grep -q '"n": 4'; then pass "mock backend received exactly 4 requests"
else fail "unexpected capture count: $RESULT"; fi
if echo "$RESULT" | grep -qE '"a1": [1-9]'; then pass "session A req1 injected MEMORY block"
else fail "session A req1 missing MEMORY block"; fi
if echo "$RESULT" | grep -q '"ok_a": true'; then pass "session A guard holds (no duplicate injection)"
else fail "session A guard broken: $RESULT"; fi
if echo "$RESULT" | grep -qE '"b1": [1-9]'; then pass "session B req1 injected HANDOFF block"
else fail "session B req1 missing HANDOFF block"; fi
if echo "$RESULT" | grep -q '"ok_b": true'; then pass "session B guard holds (no duplicate injection)"
else fail "session B guard broken: $RESULT"; fi

# ============================================================
echo ""
info "R-CM integration: $PASS passed, $FAIL failed"
rm -rf "$LOG_DIR" 2>/dev/null || true
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
