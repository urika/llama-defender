#!/usr/bin/env bash
# ============================================================
# Integration test: recall hints reach the model's visible surface.
#
# 欠拉根因回归防线(2026-09-01): L1/L2 的价值在于指引进入发送 payload——
# 单测只断言函数返回值, 本测试断言 mock 后端实际收到的请求体(capture)。
#
# 场景:
#   1. L1: 长对话触发 fifo 截断 → 折叠占位符(含 ctx_recall 指引)到达后端
#   2. L2: 历史含 Wasted call tool_result → 改写后的 ctx_recall 指引到达后端
#
# Run via:
#     bash test/run_tests.sh --integration
# or directly:
#     bash test/integration/test_recall_hint_integration.sh
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/test/lib/diag_cleanup.sh"
LOG_DIR="$REPO_ROOT/logs/itest_hint"
MOCK_PORT="${MOCK_PORT:-8094}"
PROXY_PORT="${PROXY_PORT:-4006}"
SID="itesthint"
CAPTURE_PATH="$LOG_DIR/mock_capture.jsonl"
PROXY_LOG="$LOG_DIR/proxy.log"
MOCK_LOG="$LOG_DIR/mock.log"

mkdir -p "$LOG_DIR"
: > "$CAPTURE_PATH"; : > "$PROXY_LOG"; : > "$MOCK_LOG"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
PASS=0; FAIL=0
pass() { echo -e "  ${GREEN}PASS${NC}  $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; FAIL=$((FAIL+1)); }
info() { echo -e "${CYAN}→${NC} $1"; }

PROXY_PID=""; MOCK_PID=""
cleanup() {
  set +e
  diag_cleanup "$REPO_ROOT" "itesthin" "itestp3"
  [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null
  [[ -n "$MOCK_PID"  ]] && kill "$MOCK_PID"  2>/dev/null
  sleep 0.3
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

MOCK_CAPTURE_PATH="$CAPTURE_PATH" \
  python3 "$REPO_ROOT/test/integration/mock_backend.py" "$MOCK_PORT" >>"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

PORT="$PROXY_PORT" \
LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
PROXY_METRICS_ENABLED=false \
PROXY_LOG_PATH="$PROXY_LOG" \
PROXY_DIAG_ENABLED=false \
PROXY_HBE_ENABLED=false \
PROXY_QUEUE_ENABLED=false \
PROXY_COMPRESS_ENABLED=false \
PROXY_CLEAR_ENABLED=false \
PROXY_ROUTE_ENABLED=false \
PROXY_PD_ENABLED=true \
PROXY_PD_MICRO_TURN_ENABLED=false \
PROXY_CTX_LIMIT_ENABLED=true \
PROXY_CTX_TRUNCATE_STRATEGY=fifo \
PROXY_CTX_KEEP_MESSAGES=6 \
PROXY_CTX_KEEP_HEAD=0 \
  python3 "$REPO_ROOT/anthropic_proxy.py" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
wait_for_port "$PROXY_PORT" "proxy"

send() {
  local body=$1 tag=$2
  curl -sf --max-time 30 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
    -H "Content-Type: application/json" -H "x-api-key: test" \
    -H "anthropic-version: 2023-06-01" \
    -H "x-claude-code-session-id: $SID" \
    -d "$body" > /dev/null 2>>"$PROXY_LOG" || fail "$tag: 请求失败"
}

# ============================================================
# 场景 1 (L1): 12 条消息 + keep=6 → 必触发截断, 占位符应带指引
# ============================================================
info "场景 1: L1 折叠占位符指引到达后端"
: > "$CAPTURE_PATH"
REQ1=$(python3 -c "
import json
msgs = [{'role': 'user', 'content': 'seed ' + 'x' * 100}]
for i in range(6):
    msgs.append({'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': f't{i}', 'name': 'Read',
         'input': {'file_path': f'/src/f{i}.py'}}]})
    msgs.append({'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': f't{i}',
         'content': [{'type': 'text', 'text': 'data ' + 'y' * 200}]}]})
msgs.append({'role': 'user', 'content': 'tail question'})
print(json.dumps({'model': 'claude-3-5-sonnet-20241022', 'max_tokens': 32,
                  'stream': False, 'messages': msgs}))
")
send "$REQ1" "场景1"

python3 - "$CAPTURE_PATH" <<'PYEOF'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").readlines()
assert lines, "capture 为空——请求未达后端"
body = json.loads(lines[-1])["body"]
text = json.dumps(body.get("messages", []), ensure_ascii=False)
assert "Context folded" in text, "未见折叠占位符(截断未触发?)"
assert "ctx_recall" in text, "占位符到达后端但不含 ctx_recall 指引"
PYEOF
if [[ $? -eq 0 ]]; then pass "L1: 折叠占位符 + ctx_recall 指引到达后端 payload"; else fail "L1: 占位符或指引未到达后端"; fi

# ============================================================
# 场景 2 (L2): 历史 tool_result 含 Wasted call → 改写指引到达后端
# ============================================================
info "场景 2: L2 wasted-call 提示改指向到达后端"
: > "$CAPTURE_PATH"
REQ2=$(python3 -c "
import json
msgs = [
    {'role': 'user', 'content': 'read the file'},
    {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'tw1', 'name': 'Read',
         'input': {'file_path': '/src/app.py'}}]},
    {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'tw1',
         'content': [{'type': 'text', 'text': 'Wasted call: file unchanged'}]}]},
    {'role': 'assistant', 'content': 'ok, noted'},
    {'role': 'user', 'content': 'continue'},
]
print(json.dumps({'model': 'claude-3-5-sonnet-20241022', 'max_tokens': 32,
                  'stream': False, 'messages': msgs}))
")
send "$REQ2" "场景2"

python3 - "$CAPTURE_PATH" <<'PYEOF'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").readlines()
body = json.loads(lines[-1])["body"]
text = json.dumps(body.get("messages", []), ensure_ascii=False)
ok = ("ctx_recall" in text and "Bash cat" in text)
sys.exit(0 if ok else 1)
PYEOF
if [[ $? -eq 0 ]]; then pass "L2: wasted-call 改写为 ctx_recall 优先指引并到达后端"; else fail "L2: 改写指引未到达后端"; fi

# 原始 Wasted call 字样不应原样透传(已被改写)
python3 - "$CAPTURE_PATH" <<'PYEOF'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").readlines()
body = json.loads(lines[-1])["body"]
text = json.dumps(body.get("messages", []), ensure_ascii=False)
sys.exit(0 if "Wasted call: file unchanged" not in text else 1)
PYEOF
if [[ $? -eq 0 ]]; then pass "L2: 原始 Wasted call 字样已被改写(非透传)"; else fail "L2: Wasted call 原样透传"; fi

# ============================================================
# 场景 3 (状态注入 + 已知答案检索): 预植 manifest/archive →
# 注入 ctx_recall 调用(路径 A 改写) → 断言后端收到植入的已知内容。
# 独立会话键 itestplant: 代理内存无该会话 → 走磁盘加载(预植生效路径)。
# ============================================================
info "场景 3: 构造报文注入——预植记忆 + 已知答案检索"
SID2="itestp3"
MARKER="PLANT-KNOWN-CONTENT-9137"

python3 - "$REPO_ROOT" "$SID2" "$MARKER" <<'PYEOF'
import json, os, sys
from datetime import datetime
repo, sid, marker = sys.argv[1], sys.argv[2], sys.argv[3]
now = datetime.now().isoformat(timespec="seconds")
diag = os.path.join(repo, "logs", "diag")
# 预植 manifest: 一条 fifo_drop 索引行(anchor 可寻址, head 含查询词)
man_dir = os.path.join(diag, "manifest"); os.makedirs(man_dir, exist_ok=True)
with open(os.path.join(man_dir, sid + ".jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps({
        "turn": 3, "reason": "fifo_drop", "anchor": "r:tp1",
        "kind": "tool_result", "role": "user", "tool": "", "handle": None,
        "size_chars": 4200, "head": marker + " head excerpt for FTS",
        "ts": now}, ensure_ascii=False) + "\n")
# 预植 archive: payload 内嵌 tool_result(recover_full_content 按 tool_use_id 匹配)
arc_dir = os.path.join(diag, "archive"); os.makedirs(arc_dir, exist_ok=True)
payload = json.dumps({"messages": [{"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "tp1",
     "content": [{"type": "text",
                  "text": marker + " full body zebra quantum unique"}]}]}]},
    ensure_ascii=False)
with open(os.path.join(arc_dir, sid + ".jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps({"turn": 3, "ts": now, "payload": payload},
                       ensure_ascii=False) + "\n")
print("planted")
PYEOF

: > "$CAPTURE_PATH"
REQ3=$(python3 -c "
import json
msgs = [
    {'role': 'user', 'content': 'earlier context question'},
    {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'tc1', 'name': 'ctx_recall',
         'input': {'query': 'PLANT'}}]},
    {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'tc1',
         'content': [{'type': 'text',
                      'text': \"Error: unknown tool 'ctx_recall'\"}]}]},
    {'role': 'user', 'content': 'continue with recalled info'},
]
print(json.dumps({'model': 'claude-3-5-sonnet-20241022', 'max_tokens': 32,
                  'stream': False, 'messages': msgs}))
")
# 注意 header 换成注入会话键
curl -sf --max-time 30 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
  -H "Content-Type: application/json" -H "x-api-key: test" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-claude-code-session-id: $SID2" \
  -d "$REQ3" > /dev/null 2>>"$PROXY_LOG" || fail "场景3: 请求失败"

python3 - "$CAPTURE_PATH" "$MARKER" <<'PYEOF'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").readlines()
assert lines, "capture 为空"
body = json.loads(lines[-1])["body"]
text = json.dumps(body.get("messages", []), ensure_ascii=False)
assert "Error: unknown tool" not in text, "error result 未被改写(路径 A 未触发)"
assert sys.argv[2] in text, f"已知答案 {sys.argv[2]} 未到达后端(检索/恢复链断)"
sys.exit(0)
PYEOF
if [[ $? -eq 0 ]]; then pass "注入状态→代理检索→已知内容到达后端(检索正确性端到端)"; else fail "已知答案检索失败(植入/检索/改写链断)"; fi

# 恢复全文也应在改写结果中(recover_full_content 链)
python3 - "$CAPTURE_PATH" "$MARKER" <<'PYEOF'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").readlines()
body = json.loads(lines[-1])["body"]
text = json.dumps(body.get("messages", []), ensure_ascii=False)
sys.exit(0 if ("zebra quantum" in text and "fifo_drop" in text) else 1)
PYEOF
if [[ $? -eq 0 ]]; then pass "archive 全文恢复 + 索引行元数据一同到达后端"; else fail "全文恢复链断"; fi

echo ""
if (( FAIL == 0 )); then
  echo -e "${GREEN}=== recall-hint integration: ALL PASS ($PASS) ===${NC}"
  exit 0
else
  echo -e "${RED}=== recall-hint integration: $FAIL FAILED / $PASS passed ===${NC}"
  exit 1
fi
