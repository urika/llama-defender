#!/usr/bin/env bash
# ============================================================
# diag_cleanup.sh — 集成测试共享状态清理(测试足迹不污染生产 diag)。
#
# 用法(在集成测试脚本内):
#   source "$REPO_ROOT/test/lib/diag_cleanup.sh"
#   trap cleanup EXIT
#   # cleanup() 内部调用: diag_cleanup <SID1> [SID2 ...]
#
# 行为:
#   - 删除各会话键控存储: manifest/index/archive/ledger/<SID>.*
#   - 全局流 sessions.jsonl 行过滤(含 SID 的行)
#   - ITEST_KEEP=1: 保留全部产物供调试(常规运行务必清理——
#     防 Phase 2 效度数据集被测试足迹污染, 2026-08-30 exp-1 事故)
# ============================================================

diag_cleanup() {
  local repo="$1"; shift
  local sids="$@"
  set +e
  local sid
  for sid in $sids; do
    rm -f "$repo/logs/diag/manifest/$sid.jsonl" \
          "$repo/logs/diag/manifest/$sid.jsonl.prev" \
          "$repo/logs/diag/index/$sid.db" \
          "$repo/logs/diag/index/$sid.db.prev" \
          "$repo/logs/diag/archive/$sid.jsonl" \
          "$repo/logs/diag/archive/$sid.jsonl.prev" \
          "$repo/logs/diag/orig/$sid.jsonl" \
          "$repo/logs/diag/ledger/$sid.jsonl"
  done
  if [[ "${ITEST_KEEP:-}" == "1" ]]; then
    echo -e "  \033[1;33m⚠\033[0m ITEST_KEEP=1: 跳过 diag 清理(调试模式)" >&2
    return 0
  fi
  python3 - "$repo/logs/diag/sessions.jsonl" $sids <<'PYEOF'
import sys
path = sys.argv[1]
sids = sys.argv[2:]
try:
    lines = open(path, encoding="utf-8").readlines()
except OSError:
    sys.exit(0)

def hits(line):
    head = line[:400]
    return any(sid in head for sid in sids)

kept = [l for l in lines if not hits(l)]
if len(kept) != len(lines):
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(kept)
PYEOF
}
