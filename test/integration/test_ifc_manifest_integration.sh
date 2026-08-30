#!/usr/bin/env bash
# ============================================================
# Integration test for IFC/PDC batch-1 chain (R9.1/R10.1/R10.2).
#
# Boots a mock OpenAI backend + proxy once, forces fifo
# truncation with a small keep window, and verifies the whole
# information-plane chain end to end:
#   1. manifest 落盘: fifo_drop 索引行含锚/句柄/head 摘录
#   2. sessions.jsonl ifc 段: ile=true 且 kinds 含 unit_drop,
#      retention < 1(锚点差分识别丢轮)
#   3. ctx_recall 召回: 从落盘索引按内容词查回被截断单元
#      (FTS5 trigram 中文路径)
#
# 日志纪律: 会话键固定 itestifc(8 字符, 见 SID 注释); _LOG_DIR 无 env 覆盖(proxy_state
# 硬编码), 故结束时清理 diag 足迹(manifest/index/archive/ledger +
# sessions.jsonl 过滤), 不污染 Phase 2 效度数据集。
#
# Run via:
#     bash test/run_tests.sh --integration
# or directly:
#     bash test/integration/test_ifc_manifest_integration.sh
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/itest_ifc"
MOCK_PORT="${MOCK_PORT:-8091}"
PROXY_PORT="${PROXY_PORT:-4003}"
# 会话键约定截断 8 字符(R14 D5)——取恰好 8 字符避免文件名/记录键歧义
SID="itestifc"
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
warn() { echo -e "${YELLOW}⚠${NC} $1"; }

PROXY_PID=""; MOCK_PID=""

cleanup() {
  set +e
  [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null
  [[ -n "$MOCK_PID"  ]] && kill "$MOCK_PID"  2>/dev/null
  sleep 0.3
  [[ -n "$PROXY_PID" ]] && kill -9 "$PROXY_PID" 2>/dev/null
  [[ -n "$MOCK_PID"  ]] && kill -9 "$MOCK_PID"  2>/dev/null
  # ITEST_KEEP=1: 保留 diag 产物供调试(常规运行务必清理——防污染效度数据集)
  [[ -n "${ITEST_KEEP:-}" ]] && { warn "ITEST_KEEP=1: 跳过 diag 清理"; return; }
  # diag 足迹清理: 本测试会话键的四处落点 + sessions.jsonl 行过滤
  rm -f "$REPO_ROOT/logs/diag/manifest/$SID.jsonl" \
        "$REPO_ROOT/logs/diag/index/$SID.db" \
        "$REPO_ROOT/logs/diag/archive/$SID.jsonl" \
        "$REPO_ROOT/logs/diag/ledger/$SID.jsonl"
  python3 - "$REPO_ROOT/logs/diag/sessions.jsonl" "$SID" <<'PYEOF'
import sys
path, sid = sys.argv[1], sys.argv[2]
try:
    lines = open(path, encoding="utf-8").readlines()
except OSError:
    sys.exit(0)
kept = [l for l in lines if sid not in l[:400]]
if len(kept) != len(lines):
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(kept)
PYEOF
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
  tail -30 "$PROXY_LOG" 2>/dev/null; tail -30 "$MOCK_LOG" 2>/dev/null
  exit 1
}

send_request() {
  local body=$1
  curl -sf --max-time 30 -X POST "http://127.0.0.1:$PROXY_PORT/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: test" \
    -H "anthropic-version: 2023-06-01" \
    -H "x-claude-code-session-id: $SID" \
    -d "$body" >>"$PROXY_LOG" 2>&1
}

# ============================================================
# Start backend + proxy (fifo 强制小窗口)
# ============================================================
python3 "$REPO_ROOT/test/integration/mock_backend.py" "$MOCK_PORT" >>"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
wait_for_port "$MOCK_PORT" "mock backend"

info "Starting proxy on :$PROXY_PORT (fifo keep=6 强制截断)"
PORT="$PROXY_PORT" \
LLAMA_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
PROXY_METRICS_ENABLED=true \
PROXY_METRICS_DIR="logs/itest_ifc" \
PROXY_LOG_PATH="$PROXY_LOG" \
PROXY_DIAG_ENABLED=true \
PROXY_HBE_ENABLED=false \
PROXY_QUEUE_ENABLED=false \
PROXY_COMPRESS_ENABLED=true \
PROXY_COMPRESS_THRESHOLD=2000 \
PROXY_CLEAR_ENABLED=false \
PROXY_CTX_LIMIT_ENABLED=true \
PROXY_CTX_TRUNCATE_STRATEGY=fifo \
PROXY_CTX_KEEP_MESSAGES=6 \
PROXY_CTX_KEEP_HEAD=0 \
  python3 "$REPO_ROOT/anthropic_proxy.py" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
wait_for_port "$PROXY_PORT" "proxy"

# ============================================================
# 请求 1: 5 条消息(≤ keep=6,无截断)——基线视图必须包含"将被丢弃的内容"。
# ILE 锚点差分语义 = 上一轮见过的单元本轮消失;真实客户端每轮重发全量
# 历史,被截断内容必然先出现在上一轮视图中。
# ============================================================
REQ1=$(python3 -c "
import json
msgs = [
    {'role': 'user', 'content': '早期架构决策:放弃JWT改用session方案' + 'X' * 300},
    {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 't-ifc1', 'name': 'Read',
         'input': {'file_path': '/src/arch.py'}}]},
    {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 't-ifc1',
         'content': [{'type': 'text', 'text': 'ARCH = \"vault\"' * 60}]}]},
    {'role': 'assistant', 'content': 'ok1'},
    {'role': 'user', 'content': '继续1'},
]
print(json.dumps({
  'model': 'claude-3-5-sonnet-20241022', 'max_tokens': 64, 'stream': False,
  'messages': msgs}))")
if send_request "$REQ1"; then pass "请求 1(基线) 成功"; else fail "请求 1(基线) 失败"; fi
sleep 0.5

# ============================================================
# 请求 2: 同一历史 + 3 条新消息 = 8 条 > keep=6 → fifo 保尾 6 条,
# 丢弃区(0..1)= 基线头部的动机文本与工具调用;基线尾部(ok1/继续1)存活
# → 差分报 ile=unit_drop(保尾丢头=真实截断;整视图滑出=task reset,不算)
# ============================================================
REQ2=$(python3 -c "
import json
msgs = [
    {'role': 'user', 'content': '早期架构决策:放弃JWT改用session方案' + 'X' * 300},
    {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 't-ifc1', 'name': 'Read',
         'input': {'file_path': '/src/arch.py'}}]},
    {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 't-ifc1',
         'content': [{'type': 'text', 'text': 'ARCH = \"vault\"' * 60}]}]},
    {'role': 'assistant', 'content': 'ok1'},
    {'role': 'user', 'content': '继续1'},
    {'role': 'assistant', 'content': 'ok2'},
    {'role': 'user', 'content': '继续2'},
    {'role': 'user', 'content': '最终问题:总结当前状态'},
]
print(json.dumps({
  'model': 'claude-3-5-sonnet-20241022', 'max_tokens': 64, 'stream': False,
  'messages': msgs}))")
if send_request "$REQ2"; then pass "请求 2(触发截断) 成功"; else fail "请求 2(触发截断) 失败"; fi
sleep 1.0

# ============================================================
# 断言 1: manifest 落盘(锚/句柄/head)
# ============================================================
MANIFEST_FILE="$REPO_ROOT/logs/diag/manifest/$SID.jsonl"
if [[ -f "$MANIFEST_FILE" ]]; then
  MCHK=$(python3 - "$MANIFEST_FILE" <<'PYEOF'
import json, sys
lines = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
anchors = {l.get("anchor") for l in lines}
has_tool = any(l.get("anchor") == "u:t-ifc1" and
               (l.get("handle") or {}).get("value") == "/src/arch.py" for l in lines)
has_result = any(l.get("anchor") == "r:t-ifc1" for l in lines)  # 软断言:孤儿配对修复可能后移
has_head = any("早期架构决策" in (l.get("head") or "") for l in lines)
has_reason = any(l.get("reason") == "fifo_drop" for l in lines)
print("OK" if (has_tool and has_head and has_reason and lines) else
      "MISS tool=%s result=%s head=%s reason=%s n=%d" %
      (has_tool, has_result, has_head, has_reason, len(lines)))
PYEOF
)
  if [[ "$MCHK" == "OK" ]]; then
    pass "manifest 落盘: fifo_drop 行含 u:/r: 锚、句柄 /src/arch.py、head 摘录"
  else
    fail "manifest 内容不完整: $MCHK"
  fi
else
  fail "manifest 文件未生成: $MANIFEST_FILE"
fi

# ============================================================
# 断言 2: sessions.jsonl ifc 段(ile/unit_drop/retention<1)
# ============================================================
SCHK=$(python3 - "$REPO_ROOT/logs/diag/sessions.jsonl" "$SID" <<'PYEOF'
import json, sys
path, sid = sys.argv[1], sys.argv[2]
recs = []
for l in open(path, encoding="utf-8"):
    try:
        r = json.loads(l)
    except ValueError:
        continue
    if r.get("session_key") == sid:
        recs.append(r)
if not recs:
    print("MISS no-records"); sys.exit()
last = recs[-1].get("ifc") or {}
ok = ("ifc" in recs[-1] and last.get("ile") is True
      and "unit_drop" in (last.get("ile_kinds") or [])
      and isinstance(last.get("retention"), (int, float))
      and last["retention"] < 1.0)
print("OK" if ok else "MISS last-ifc=%s n=%d" % (json.dumps(last, ensure_ascii=False), len(recs)))
PYEOF
)
if [[ "$SCHK" == "OK" ]]; then
  pass "ifc 段: ile=true, kinds 含 unit_drop, retention<1(锚点差分识别丢轮)"
else
  fail "ifc 段断言失败: $SCHK"
fi

# ============================================================
# 断言 3: ctx_recall 从落盘索引按内容词召回(中文 trigram)
# ============================================================
RCHK=$(cd "$REPO_ROOT" && python3 - <<'PYEOF'
import sys
sys.path.insert(0, ".")
import ctx_recall
hits = ctx_recall.lookup("itestifc", "早期架构")
tool_hits = ctx_recall.lookup("itestifc", "arch.py", kind="tool_use")
if hits and any(l.get("kind") == "text" for l in hits) and \
   any((l.get("handle") or {}).get("value") == "/src/arch.py" for l in tool_hits):
    print("OK")
else:
    print("MISS text=%d tool=%d" % (len(hits), len(tool_hits)))
PYEOF
)
if [[ "$RCHK" == "OK" ]]; then
  pass "ctx_recall 召回: 内容词「早期架构」与句柄 arch.py 均命中落盘索引"
else
  fail "ctx_recall 召回失败: $RCHK"
fi

echo ""
info "Summary: $PASS passed, $FAIL failed."
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
