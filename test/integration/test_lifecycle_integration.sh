#!/usr/bin/env bash
# ============================================================
# Integration test for R7 lifecycle event logging.
# Verifies that manage.sh switch writes valid JSONL events.
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/test/lib/diag_cleanup.sh"
LOG_DIR="$REPO_ROOT/logs/itest_lifecycle"
LIFECYCLE_EVENTS_PATH="$LOG_DIR/lifecycle_events.jsonl"

mkdir -p "$LOG_DIR"
: > "$LIFECYCLE_EVENTS_PATH"

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'
PASS=0; FAIL=0
pass() { echo -e "  ${GREEN}PASS${NC}  $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; FAIL=$((FAIL+1)); }
info() { echo -e "${CYAN}→${NC} $1"; }

cleanup() {
  set +e
  diag_cleanup "$REPO_ROOT" "itest-li"
  # Restore original active profile so the test is non-destructive
  if [[ -n "$CURRENT" ]] && [[ -f "$REPO_ROOT/configs/$CURRENT.conf" ]]; then
    ln -sf "$CURRENT.conf" "$REPO_ROOT/configs/active.conf" 2>/dev/null || true
  fi
}
trap cleanup EXIT

CURRENT=$(basename "$(readlink "$REPO_ROOT/configs/active.conf")" .conf 2>/dev/null || echo "rapid-mlx-35b-opt")

# Pick a different profile to switch to
OTHER=""
for conf in "$REPO_ROOT/configs"/*.conf; do
  [[ -f "$conf" ]] || continue
  name=$(basename "$conf" .conf)
  [[ "$name" == "active" ]] && continue
  [[ "$name" == "secret" ]] && continue
  [[ "$name" == "$CURRENT" ]] && continue
  OTHER="$name"
  break
done

if [[ -z "$OTHER" ]]; then
  warn "只有一个可用配置，跳过生命周期集成测试"
  exit 0
fi

info "TC1: switch to $OTHER should log profile_switch event"
LIFECYCLE_EVENTS_PATH="$LIFECYCLE_EVENTS_PATH" "$REPO_ROOT/manage.sh" switch "$OTHER" >/dev/null 2>&1
if [[ -s "$LIFECYCLE_EVENTS_PATH" ]]; then
  pass "TC1 lifecycle_events.jsonl created"
else
  fail "TC1 lifecycle_events.jsonl missing or empty"
fi

if python3 - <<PY 2>/dev/null
import json
with open("$LIFECYCLE_EVENTS_PATH") as f:
    for line in f:
        rec = json.loads(line)
        assert "ts" in rec
        assert "event" in rec
        assert "detail" in rec
        if rec["event"] == "profile_switch":
            break
    else:
        raise SystemExit(1)
PY
then
  pass "TC1 profile_switch event is valid JSONL"
else
  fail "TC1 profile_switch event invalid"
fi

info "TC2: switch back to $CURRENT should append another event"
LIFECYCLE_EVENTS_PATH="$LIFECYCLE_EVENTS_PATH" "$REPO_ROOT/manage.sh" switch "$CURRENT" >/dev/null 2>&1
COUNT=$(wc -l < "$LIFECYCLE_EVENTS_PATH" | tr -d ' ')
if [[ "$COUNT" -ge 2 ]]; then
  pass "TC2 lifecycle log appended (count=$COUNT)"
else
  fail "TC2 lifecycle log not appended (count=$COUNT)"
fi

# Restore original active.conf (already done by switching back)

echo ""
if [[ $FAIL -eq 0 ]]; then
  echo "Passed: $PASS"
  echo "Failed: 0"
  echo -e "${GREEN}All $PASS lifecycle integration tests passed.${NC}"
  exit 0
else
  echo "Passed: $PASS"
  echo "Failed: $FAIL"
  echo -e "${RED}$FAIL failed, $PASS passed.${NC}"
  exit 1
fi
