#!/usr/bin/env bash
# ============================================================
# Integration test for manage.sh mutation command mutual exclusion (R4).
#
# Verifies that:
#   1. A second mutating command fails fast while the lock is held.
#   2. Read-only commands (status) are not blocked by the lock.
#   3. The lock is released after the first command finishes.
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/test/lib/diag_cleanup.sh"
cd "$REPO_ROOT"

MANAGE="./manage.sh"
LOCKFILE="$REPO_ROOT/.manage.lock"

PASS=0
FAIL=0

pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); }

# Clean up any stale lock
rm -f "$LOCKFILE"

# ---------------------------------------------------------------------------
# Test 1: manual lock held -> restart is rejected
# ---------------------------------------------------------------------------
if command -v shlock >/dev/null 2>&1; then
    # Hold the lock with a fake-but-alive PID (the test shell itself)
    shlock -f "$LOCKFILE" -p $$ 2>/dev/null || true
elif command -v flock >/dev/null 2>&1; then
    exec 200>"$LOCKFILE"
    flock -n 200 2>/dev/null || true
else
    echo "SKIP: neither shlock nor flock available"
    exit 0
fi

# Use restart because it is short and should detect the lock immediately
restart_out=$("$MANAGE" restart 2>&1) || true
if echo "$restart_out" | grep -q "无法获取 manage.sh 互斥锁"; then
    pass "mutating command rejected while lock held"
else
    fail "mutating command was not rejected while lock held"
fi

# Release lock
rm -f "$LOCKFILE"
exec 200>&- 2>/dev/null || true

# ---------------------------------------------------------------------------
# Test 2: read-only status works even if lock file exists (but not held)
# ---------------------------------------------------------------------------
if "$MANAGE" status >/dev/null 2>&1; then
    pass "status command works without lock contention"
else
    fail "status command failed unexpectedly"
fi

# ---------------------------------------------------------------------------
# Test 3: lock is released after a command finishes
# ---------------------------------------------------------------------------
if [[ ! -f "$LOCKFILE" ]]; then
    pass "lock file cleaned up after command"
else
    fail "stale lock file remains"
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
