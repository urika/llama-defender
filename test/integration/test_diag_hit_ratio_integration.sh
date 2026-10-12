#!/usr/bin/env bash
# ============================================================
# Integration test for the diagnostics hit_ratio 口径 (L-72 fix).
#
# 背景:Splash 世代后端不返回 `timings`,只返回
#   `usage.prompt_tokens_details.cached_tokens` —— 那是前缀缓存**命中量**。
# 修正前该字段被直接当作 `prompt_processed_n`(实算 prefill 数)喂给
# diagnostics,于是上报的 `hit_ratio = 1 - cached/sent` 是**未命中率**
# (数值反转)。修正后:`prompt_processed_n = prompt_tokens - cached_tokens`。
#
# 本套件在 HTTP 层守住**调用点接线**——单测只覆盖 helper 本身,而本次缺陷
# 正出在接线(usage 回退分支把 cached 当 processed 传):
#   - 非流式 `_handle_non_streaming_response`:anthropic_proxy.py:1457
#   - 流式 usage 回退: anthropic_proxy.py:1824
#   (passthrough 回退 anthropic_proxy.py:2355 与流式同源同错,共用同一个
#    `set_prompt_tokens_from_usage()`;此处不另解发。)
#
# mock 发 prompt=1000 / cached=900 且**不发 timings** ⇒ 期望
#   prompt_processed_n = 100,HitRatio = 0.9
# 反转缺陷下会得到 processed=900 / hit_ratio=0.1 —— TC6 显式断言其不会出现。
#
# Run via:
#     bash test/run_tests.sh --integration
# or directly:
#     bash test/integration/test_diag_hit_ratio_integration.sh
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/test/lib/diag_cleanup.sh"
LOG_DIR="$REPO_ROOT/logs/itest"
MOCK_PORT="${MOCK_PORT:-8095}"
PROXY_PORT="${PROXY_PORT:-4007}"
SID="itest-diag-hit"
CAPTURE_PATH="$LOG_DIR/mock_capture.jsonl"
PROXY_LOG="$LOG_DIR/proxy.log"
MOCK_LOG="$LOG_DIR/mock.log"
METRICS_PATH="$LOG_DIR/proxy_metrics.jsonl"
NS_BODY="$LOG_DIR/diag_ns_body.json"      # 非流式响应体
SSE_BODY="$LOG_DIR/diag_sse_body.txt"     # 流式 SSE 原文
DIAG_SESSIONS="$REPO_ROOT/logs/diag/sessions.jsonl"

# mock 口径:sent=1000, cached=900 ⇒ 期望 processed=100, hit_ratio=0.9
MOCK_PROMPT=1000
MOCK_CACHED=900
EXP_PROCESSED=100
EXP_RATIO=0.9
INVERTED_RATIO=0.1

mkdir -p "$LOG_DIR"
: > "$CAPTURE_PATH"
: > "$PROXY_LOG"
: > "$MOCK_LOG"
rm -f "$METRICS_PATH" "$NS_BODY" "$SSE_BODY"

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

# 直接运行时清理本套件端口的残留占用(经 run_tests.sh 运行时由 harness 统一预清)
for port in "$MOCK_PORT" "$PROXY_PORT"; do
  if lsof -ti :"$port" >/dev/null 2>&1; then
    warn "port $port is busy; killing stale holder"
    lsof -ti :"$port" | xargs kill -9 2>/dev/null || true
    sleep 0.2
  fi
done

cleanup() {
  set +e
  diag_cleanup "$REPO_ROOT" "$SID"
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

# 断言两个浮点数近似相等(默认容差 1e-6)
approx_eq() {
  python3 -c "
import sys
a, b, tol = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
sys.exit(0 if abs(a - b) <= tol else 1)
" "$1" "$2" "${3:-1e-6}"
}

# ============================================================
# Start mock backend (Splash 世代:cached_tokens,无 timings)
# ============================================================
info "Starting mock backend on :$MOCK_PORT (prompt=$MOCK_PROMPT cached=$MOCK_CACHED, no timings)"
MOCK_USAGE_PROMPT="$MOCK_PROMPT" \
MOCK_USAGE_CACHED="$MOCK_CACHED" \
MOCK_PLAIN_TEXT="Hello from mock" \
MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
  python3 "$REPO_ROOT/test/integration/mock_backend.py" "$MOCK_PORT" >>"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

# ============================================================
# Start proxy with diagnostics enabled
# ============================================================
info "Starting proxy on :$PROXY_PORT (PROXY_DIAG_ENABLED=true)"
PROXY_DIAG_ENABLED=true \
PROXY_METRICS_ENABLED=true \
PROXY_METRICS_DIR="$LOG_DIR" \
PROXY_LOG_PATH="$PROXY_LOG" \
LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
PORT="$PROXY_PORT" \
  python3 "$REPO_ROOT/anthropic_proxy.py" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
wait_for_port "$PROXY_PORT" "proxy"

BASE_BODY='{"model":"claude-3-5-sonnet-20241022","max_tokens":128,"messages":[{"role":"user","content":"hi"}]}'

# ============================================================
# TC1: 非流式请求 —— 三处回退中的 `_handle_non_streaming_response` 路径
# ============================================================
info "TC1: 非流式请求 → usage.cache_read_input_tokens 已贯通"
NS_RC=0
curl -s --max-time 30 -o "$NS_BODY" -w '%{http_code}' \
  -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: test" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-claude-code-session-id: $SID" \
  -d "$BASE_BODY" > "$LOG_DIR/diag_ns_code.txt" 2>>"$PROXY_LOG" || NS_RC=$?
NS_CODE="$(cat "$LOG_DIR/diag_ns_code.txt" 2>/dev/null)"
if [[ "$NS_RC" -eq 0 && "$NS_CODE" == "200" ]]; then
  pass "TC1 非流式请求返回 200"
else
  fail "TC1 非流式请求失败 (rc=$NS_RC http=$NS_CODE)"
fi
NS_CACHE_READ=$(python3 -c "
import json
try:
    print(json.load(open('$NS_BODY')).get('usage', {}).get('cache_read_input_tokens'))
except Exception:
    print('missing')
" 2>/dev/null)
if [[ "$NS_CACHE_READ" == "$MOCK_CACHED" ]]; then
  pass "TC1 响应 usage.cache_read_input_tokens=$NS_CACHE_READ (cached 语义未被曲解)"
else
  fail "TC1 usage.cache_read_input_tokens=$NS_CACHE_READ, 期望 $MOCK_CACHED"
fi

# ============================================================
# TC2-TC4: 流式请求 —— SSE 尾注 `: x-proxy-diag {...}`
# ============================================================
info "TC2-TC4: 流式请求 → 尾注 prompt_processed_n / hit_ratio 口径"
STREAM_BODY=$(python3 -c "
import json, sys
b = json.loads('''$BASE_BODY''')
b['stream'] = True
print(json.dumps(b))
")
S_RC=0
curl -s -N --max-time 30 -o "$SSE_BODY" \
  -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: test" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-claude-code-session-id: $SID" \
  -d "$STREAM_BODY" >>"$PROXY_LOG" 2>&1 || S_RC=$?
if [[ "$S_RC" -eq 0 ]] && grep -q "x-proxy-diag" "$SSE_BODY"; then
  pass "TC2 流式请求返回 SSE 且带 x-proxy-diag 尾注"
else
  fail "TC2 流式请求缺少 x-proxy-diag 尾注 (rc=$S_RC)"
fi

TAIL_JSON=$(grep -m1 "^: x-proxy-diag " "$SSE_BODY" 2>/dev/null | sed 's/^: x-proxy-diag //')
TAIL_PROCESSED=$(python3 -c "
import json, sys
try:
    print(json.loads(sys.argv[1]).get('prompt_processed_n'))
except Exception:
    print('missing')
" "$TAIL_JSON" 2>/dev/null)
TAIL_SENT=$(python3 -c "
import json, sys
try:
    print(json.loads(sys.argv[1]).get('prompt_sent_n'))
except Exception:
    print('missing')
" "$TAIL_JSON" 2>/dev/null)
TAIL_RATIO=$(python3 -c "
import json, sys
try:
    print(json.loads(sys.argv[1]).get('hit_ratio'))
except Exception:
    print('missing')
" "$TAIL_JSON" 2>/dev/null)

if [[ "$TAIL_PROCESSED" == "$EXP_PROCESSED" && "$TAIL_SENT" == "$MOCK_PROMPT" ]]; then
  pass "TC3 尾注 prompt_processed_n=$TAIL_PROCESSED (=$MOCK_PROMPT-$MOCK_CACHED 实算 prefill), sent_n=$TAIL_SENT"
else
  fail "TC3 尾注 prompt_processed_n=$TAIL_PROCESSED / sent_n=$TAIL_SENT, 期望 $EXP_PROCESSED / $MOCK_PROMPT(收到 cached 即为口径反转)"
fi

if [[ "$TAIL_RATIO" != "missing" ]] && approx_eq "$TAIL_RATIO" "$EXP_RATIO" 1e-3; then
  pass "TC4 尾注 hit_ratio=$TAIL_RATIO = 命中率 $MOCK_CACHED/$MOCK_PROMPT"
else
  fail "TC4 尾注 hit_ratio=$TAIL_RATIO, 期望 $EXP_RATIO"
fi

if [[ "$TAIL_RATIO" != "missing" ]] && ! approx_eq "$TAIL_RATIO" "$INVERTED_RATIO" 1e-2; then
  pass "TC5 尾注 hit_ratio 未反转 (≠$INVERTED_RATIO)"
else
  fail "TC5 尾注 hit_ratio=$TAIL_RATIO —— 仍为 1-cached/sent 的未命中率(口径反转)"
fi

# ============================================================
# TC6: sessions.jsonl per-turn 记录 —— 持久化口径(用户在会话 series 里读到的字段)
# ============================================================
sleep 0.6
info "TC6: sessions.jsonl 两条 turn 的 prompt_processed_tokens / hit_ratio"
if [[ -f "$DIAG_SESSIONS" ]]; then
  TURNS_OUT="$LOG_DIR/diag_turns.txt"
  python3 - "$DIAG_SESSIONS" "$SID" >"$TURNS_OUT" <<'PY'
import json, sys
path, sid = sys.argv[1], sys.argv[2]
recs = []
try:
    for line in open(path, encoding="utf-8"):
        if sid in line[:400]:
            recs.append(json.loads(line))
except OSError:
    pass
def g(r, k):
    return r.get(k) if r else "missing"
r1 = recs[0] if len(recs) > 0 else None
r2 = recs[1] if len(recs) > 1 else None
print(g(r1, "prompt_processed_tokens"), g(r1, "hit_ratio"),
      g(r2, "prompt_processed_tokens"), g(r2, "hit_ratio"))
PY
  read -r T1_PROC T1_RATIO T2_PROC T2_RATIO < "$TURNS_OUT"
  T1_PROC="${T1_PROC:-missing}"; T1_RATIO="${T1_RATIO:-missing}"
  T2_PROC="${T2_PROC:-missing}"; T2_RATIO="${T2_RATIO:-missing}"
  if [[ "$T1_PROC" == "$EXP_PROCESSED" && "$T1_RATIO" != "missing" ]] && approx_eq "$T1_RATIO" "$EXP_RATIO" 1e-3; then
    pass "TC6 非流式 turn1 记录 processed=$T1_PROC hit_ratio=$T1_RATIO (anthropic_proxy.py:1457 接线)"
  else
    fail "TC6 非流式 turn1 记录 processed=$T1_PROC hit_ratio=$T1_RATIO, 期望 $EXP_PROCESSED / $EXP_RATIO"
  fi
  if [[ "$T2_PROC" == "$EXP_PROCESSED" && "$T2_RATIO" != "missing" ]] && approx_eq "$T2_RATIO" "$EXP_RATIO" 1e-3; then
    pass "TC6 流式 turn2 记录 processed=$T2_PROC hit_ratio=$T2_RATIO (anthropic_proxy.py:1824 接线)"
  else
    fail "TC6 流式 turn2 记录 processed=$T2_PROC hit_ratio=$T2_RATIO, 期望 $EXP_PROCESSED / $EXP_RATIO"
  fi
else
  fail "TC6 diag sessions.jsonl 不存在: $DIAG_SESSIONS"
fi

# ============================================================
# Summary
# ============================================================
echo ""
if [[ $FAIL -eq 0 ]]; then
  echo -e "${GREEN}All $PASS diag-hit-ratio integration tests passed.${NC}"
  exit 0
else
  echo -e "${RED}$FAIL failed, $PASS passed.${NC}"
  exit 1
fi
