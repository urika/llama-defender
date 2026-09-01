#!/usr/bin/env bash
# ============================================================
# Integration test for watchdog structured status output (R5).
#
# Verifies that:
#   1. watchdog-status returns valid JSON with enabled=false when not running.
#   2. Starting watchdog --daemon updates status to enabled=true, running=true.
#   3. Stopping watchdog updates status to enabled=false.
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/test/lib/diag_cleanup.sh"
cd "$REPO_ROOT"

MANAGE="./manage.sh"
STATE_FILE="$REPO_ROOT/logs/watchdog_state.json"

PASS=0
FAIL=0

pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); }

# Make sure no watchdog is running
"$MANAGE" stop-watchdog 2>/dev/null || true
rm -f "$STATE_FILE"

# ---------------------------------------------------------------------------
# Test 1: not running -> enabled=false
# ---------------------------------------------------------------------------
out=$("$MANAGE" watchdog-status 2>&1) || rc=$?
rc=${rc:-0}
if echo "$out" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('enabled') is False" 2>/dev/null; then
    pass "watchdog-status shows enabled=false when not running"
else
    fail "watchdog-status did not show enabled=false: $out"
fi

# ---------------------------------------------------------------------------
# Test 2: start daemon -> enabled=true, running=true
# ---------------------------------------------------------------------------
# Use a very long interval so it doesn't do anything during the test
WATCHDOG_INTERVAL=600 "$MANAGE" watchdog --daemon >/dev/null 2>&1 || rc=$?
rc=${rc:-0}
if [[ $rc -ne 0 ]]; then
    fail "watchdog --daemon failed to start"
else
    # Give daemon a moment to write state file
    sleep 1
    out=$("$MANAGE" watchdog-status 2>&1) || rc=$?
    rc=${rc:-0}
    if echo "$out" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('enabled') is True and d.get('running') is True" 2>/dev/null; then
        pass "watchdog-status shows enabled=true, running=true after daemon start"
    else
        fail "watchdog-status did not show running daemon: $out"
    fi
fi

# ---------------------------------------------------------------------------
# Test 3: stop -> enabled=false
# ---------------------------------------------------------------------------
"$MANAGE" stop-watchdog >/dev/null 2>&1 || true
sleep 0.5
out=$("$MANAGE" watchdog-status 2>&1) || rc=$?
rc=${rc:-0}
if echo "$out" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('enabled') is False" 2>/dev/null; then
    pass "watchdog-status shows enabled=false after stop"
else
    fail "watchdog-status did not show enabled=false after stop: $out"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Passed: $PASS"
echo "Failed: $FAIL"

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
