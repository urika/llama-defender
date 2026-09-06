#!/usr/bin/env bash
# ============================================================
# Integration test: 墓碑召回端到端（2026-09-06）。
#
# 客户端历史改写把旧 tool_result 丢成悬空 tool_use → 代理
# （PROXY_TOMBSTONE_RECALL_ENABLED）按悬空 call_id 查 manifest 寄存 →
# recover_full_content 取回 → AUTO-RECALL 尾消息回填，到达后端。
#
# 断言面：
#   1. 后端实际收到的请求体含回填内容（marker + 锚点 r:<call_id>）
#   2. R13 归因头 X-Proxy-Feedback-Injected 携带 auto_recall
#   3. 同会话第二次同报文不再重复注入（per-session 去重）
#   4. 主链路组装事实：悬空 tool_use 被 stage 19 清理，无墓碑透传
#
# Run via:
#     bash test/run_tests.sh --integration
# or directly:
#     bash test/integration/test_tombstone_recall_integration.sh
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/test/lib/diag_cleanup.sh"
LOG_DIR="$REPO_ROOT/logs/itest_tomb"
MOCK_PORT="${MOCK_PORT:-8096}"
PROXY_PORT="${PROXY_PORT:-4008}"
SID="itesttmb"   # 注意: 代理侧会话 key 截断为前 8 字符, 植入文件名必须一致
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

PROXY_PID=""
MOCK_PID=""

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
      info "$name is up on :$port"; return 0
    fi
    sleep 0.1
  done
  fail "$name failed to start"
  tail -20 "$PROXY_LOG" 2>/dev/null; tail -20 "$MOCK_LOG" 2>/dev/null
  exit 1
}

MARKER="TOMB-RECALL-CONTENT-4213"

# 预植寄存（写入期压缩同款落盘）: manifest r:call_t1 + archive 原文
python3 - "$REPO_ROOT" "$SID" "$MARKER" <<'PYEOF'
import json, os, sys
from datetime import datetime
repo, sid, marker = sys.argv[1], sys.argv[2], sys.argv[3]
now = datetime.now().isoformat(timespec="seconds")
diag = os.path.join(repo, "logs", "diag")
man_dir = os.path.join(diag, "manifest"); os.makedirs(man_dir, exist_ok=True)
with open(os.path.join(man_dir, sid + ".jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps({
        "turn": 2, "reason": "compressed", "anchor": "r:call_t1",
        "kind": "tool_result", "role": "user", "tool": "", "handle": None,
        "size_chars": 4200, "head": marker + " head excerpt", "ts": now},
        ensure_ascii=False) + "\n")
arc_dir = os.path.join(diag, "archive"); os.makedirs(arc_dir, exist_ok=True)
payload = json.dumps({"messages": [{"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "call_t1",
     "content": [{"type": "text", "text": marker + " def loop(): return 42"}]}]}]},
    ensure_ascii=False)
with open(os.path.join(arc_dir, sid + ".jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps({"turn": 2, "ts": now, "payload": payload},
                       ensure_ascii=False) + "\n")
print("planted")
PYEOF

MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
  python3 "$REPO_ROOT/test/integration/mock_backend.py" "$MOCK_PORT" >>"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

PORT="$PROXY_PORT" \
LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
PROXY_METRICS_ENABLED=false \
PROXY_LOG_PATH="$PROXY_LOG" \
PROXY_DIAG_ENABLED=true \
PROXY_HBE_ENABLED=false \
PROXY_QUEUE_ENABLED=false \
PROXY_COMPRESS_ENABLED=false \
PROXY_CLEAR_ENABLED=false \
PROXY_ROUTE_ENABLED=false \
PROXY_PD_ENABLED=true \
PROXY_PD_MICRO_TURN_ENABLED=false \
PROXY_AUTO_RECALL_ENABLED=false \
PROXY_TOMBSTONE_RECALL_ENABLED=true \
PROXY_CTX_LIMIT_ENABLED=false \
  python3 "$REPO_ROOT/anthropic_proxy.py" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
wait_for_port "$PROXY_PORT" "proxy"

# 报文: 悬空 tool_use（无配对 result，且不在末条 assistant）
REQ=$(python3 -c "
import json
msgs = [
    {'role': 'user', 'content': [{'type': 'text', 'text': 'start'}]},
    {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'call_t1', 'name': 'Read',
         'input': {'file_path': '/repo/src/loop_file.py'}}]},
    {'role': 'user', 'content': [{'type': 'text', 'text': 'next'}]},
    {'role': 'assistant', 'content': [
        {'type': 'text', 'text': 'thinking'}]},
]
print(json.dumps({'model': 'claude-3-5-sonnet-20241022', 'max_tokens': 32,
                  'stream': False, 'messages': msgs}))
")

# ============================================================
info "场景 1: 悬空调用 → 寄存回填到达后端 + 归因头"
: > "$CAPTURE_PATH"
curl -sf --max-time 30 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
  -H "Content-Type: application/json" -H "x-api-key: test" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-claude-code-session-id: $SID" \
  -d "$REQ" -D "$LOG_DIR/resp_headers.txt" > /dev/null 2>>"$PROXY_LOG" \
  || fail "场景1: 请求失败"
sleep 0.3

python3 - "$CAPTURE_PATH" "$MARKER" <<'PYEOF'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").readlines()
assert lines, "capture 为空"
body = json.loads(lines[-1])["body"]
text = json.dumps(body.get("messages", []), ensure_ascii=False)
assert "AUTO-RECALL" in text, "回填尾消息未到达后端"
assert sys.argv[2] in text, "寄存原文未到达后端(恢复链断)"
assert "r:call_t1" in text, "锚点提示未到达后端"
sys.exit(0)
PYEOF
if [[ $? -eq 0 ]]; then pass "回填内容+锚点到达后端(主动代答端到端)"; else fail "回填链断"; fi

grep -qi "X-Proxy-Feedback-Injected:.*auto_recall" "$LOG_DIR/resp_headers.txt" \
  && pass "R13 归因头携带 auto_recall" || fail "归因头缺 auto_recall"

python3 - "$CAPTURE_PATH" <<'PYEOF'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").readlines()
body = json.loads(lines[-1])["body"]
text = json.dumps(body.get("messages", []), ensure_ascii=False)
# stage 19 清理悬空 tool_use → 后端不应看到该调用的 tool_calls
assert "call_t1" not in text or "AUTO-RECALL" in text, "悬空调用裸透传"
assert "Tool result was not provided" not in text, "墓碑透传到后端"
sys.exit(0)
PYEOF
if [[ $? -eq 0 ]]; then pass "悬空调用被清理, 无墓碑透传(组装事实)"; else fail "悬空调用/墓碑形态异常"; fi

# ============================================================
info "场景 2: 同会话同调用去重（报文差异化以避开 body 去重窗口）"
: > "$CAPTURE_PATH"
REQ2=$(python3 -c "
import json
msgs = [
    {'role': 'user', 'content': [{'type': 'text', 'text': 'start'}]},
    {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'call_t1', 'name': 'Read',
         'input': {'file_path': '/repo/src/loop_file.py'}}]},
    {'role': 'user', 'content': [{'type': 'text', 'text': 'next'}]},
    {'role': 'assistant', 'content': [
        {'type': 'text', 'text': 'thinking'}]},
    {'role': 'user', 'content': [{'type': 'text', 'text': 'followup'}]},
]
print(json.dumps({'model': 'claude-3-5-sonnet-20241022', 'max_tokens': 32,
                  'stream': False, 'messages': msgs}))
")
curl -sf --max-time 30 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
  -H "Content-Type: application/json" -H "x-api-key: test" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-claude-code-session-id: $SID" \
  -d "$REQ2" > /dev/null 2>>"$PROXY_LOG" || fail "场景2: 请求失败"
sleep 0.3
python3 - "$CAPTURE_PATH" <<'PYEOF'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").readlines()
assert lines, "capture 为空"
text = json.dumps(json.loads(lines[-1])["body"].get("messages", []),
                  ensure_ascii=False)
assert "AUTO-RECALL" not in text, "第二次请求重复注入(去重失效)"
sys.exit(0)
PYEOF
if [[ $? -eq 0 ]]; then pass "第二次请求不重复注入(per-session 去重)"; else fail "去重失效"; fi

echo ""
if (( FAIL == 0 )); then
  echo -e "${GREEN}=== tombstone-recall integration: ALL PASS ($PASS) ===${NC}"
  exit 0
else
  echo -e "${RED}=== tombstone-recall integration: $FAIL FAILED / $PASS passed ===${NC}"
  exit 1
fi
