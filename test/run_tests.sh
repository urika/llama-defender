#!/usr/bin/env bash
# ============================================================
# Unified test runner for the anthropic_proxy project.
#
# Layout:
#   test/unit/         — pure logic, no network (Python unittest)
#   test/integration/  — boots a mock backend, no real LLM
#   test/e2e/          — requires a running proxy + backend
#
# Usage:
#   bash test/run_tests.sh                  # default: --unit
#   bash test/run_tests.sh --unit
#   bash test/run_tests.sh --integration
#   bash test/run_tests.sh --e2e
#   bash test/run_tests.sh --all
#   bash test/run_tests.sh --fast           # alias for --unit (used by pre-commit)
#
# Exit code is 0 on full pass, 1 if any tier fails.
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

print_banner() { echo -e "${CYAN}${BOLD}=== $1 ===${NC}"; }
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }

TIER_RESULTS=()  # each entry: "tier|ok|detail"

record() {
  local tier=$1 ok=$2 detail=$3
  TIER_RESULTS+=("$tier|$ok|$detail")
}

# ------------------------------------------------------------
# Tier 1: unit tests
# ------------------------------------------------------------
run_unit() {
  print_banner "Unit tests (test/unit/)"
  local log="$REPO_ROOT/logs/unit_test.log"
  mkdir -p "$REPO_ROOT/logs"
  if python3 -m unittest discover \
        -s "$SCRIPT_DIR/unit" \
        -p 'test_*.py' \
        -v 2>&1 | tee "$log" | tail -5; then
    local n
    n=$(grep -c "^test_" "$log" 2>/dev/null || echo "?")
    record "unit" "ok" "$n tests passed"
  else
    record "unit" "fail" "see logs/unit_test.log"
  fi
}

# ------------------------------------------------------------
# Tier 2: integration tests (mock backend, no LLM)
# ------------------------------------------------------------
run_integration() {
  print_banner "Integration tests (test/integration/)"
  # Make sure no stale process holds the integration ports.
  for port in 8089 4001; do
    if lsof -ti :"$port" >/dev/null 2>&1; then
      warn "port $port is busy; killing stale holder"
      lsof -ti :"$port" | xargs kill -9 2>/dev/null || true
      sleep 0.3
    fi
  done
  local out
  out=$(bash "$SCRIPT_DIR/integration/test_blocker_integration.sh" 2>&1)
  local rc=$?
  # Echo the last 25 lines so the user sees the summary.
  echo "$out" | tail -25
  if [[ $rc -ne 0 ]]; then
    record "integration" "fail" "test_blocker_integration.sh exited $rc"
    return
  fi
  # Strip ANSI escape codes before parsing numbers.
  local passed failed
  passed=$(echo "$out" | sed $'s/\x1b\\[[0-9;]*[a-zA-Z]//g' | grep -E "Passed:" | tail -1 | grep -oE "[0-9]+" | head -1)
  failed=$(echo "$out" | sed $'s/\x1b\\[[0-9;]*[a-zA-Z]//g' | grep -E "Failed:" | tail -1 | grep -oE "[0-9]+" | head -1)
  if [[ "${failed:-0}" == "0" ]]; then
    record "integration" "ok" "all ${passed:-?} cases passed"
  else
    record "integration" "fail" "$failed of ${passed:-?} cases failed"
  fi
}

# ------------------------------------------------------------
# Tier 3: e2e tests (requires running proxy + backend)
# ------------------------------------------------------------
run_e2e() {
  print_banner "End-to-end tests (test/e2e/)"

  # Preflight: proxy must be reachable.
  local proxy="${PROXY_BASE:-http://127.0.0.1:4000}"
  if ! curl -sf --max-time 5 "$proxy/v1/models" >/dev/null 2>&1; then
    warn "proxy not reachable at $proxy — skipping e2e suite"
    warn "start it with: ./manage.sh start   (or)   ./manage.sh start-cloud"
    record "e2e" "skip" "proxy not reachable at $proxy"
    return 0
  fi
  # Preflight: backend (when not in cloud mode).
  if [[ "$proxy" != *"deepseek"* && "$proxy" != *"api."* ]]; then
    local backend="${BACKEND_URL:-http://127.0.0.1:8081}"
    if ! curl -sf --max-time 5 "$backend/v1/models" >/dev/null 2>&1; then
      warn "backend not reachable at $backend — e2e suite will likely fail"
    fi
  fi

  local log="$REPO_ROOT/logs/e2e_test.log"
  mkdir -p "$REPO_ROOT/logs"
  local overall_rc=0
  local out_a out_b
  out_a=$(PROXY_BASE="$proxy" python3 "$SCRIPT_DIR/e2e/test_proxy_integration.py" 2>&1)
  local rc_a=$?
  out_b=$(PROXY_URL="$proxy" bash "$SCRIPT_DIR/e2e/e2e_tools_fallback.sh" 2>&1)
  local rc_b=$?
  # Persist & display.
  {
    echo "==== proxy integration ===="
    echo "$out_a"
    echo ""
    echo "==== tool-call fallback ===="
    echo "$out_b"
  } > "$log"
  echo ""
  echo -e "${CYAN}--- e2e: proxy integration matrix ---${NC}"
  echo "$out_a" | tail -10
  echo ""
  echo -e "${CYAN}--- e2e: tool-call fallback ---${NC}"
  echo "$out_b" | tail -8

  if [[ $rc_a -ne 0 ]]; then
    overall_rc=1
    warn "test_proxy_integration.py exited $rc_a"
  fi
  if [[ $rc_b -ne 0 ]]; then
    overall_rc=1
    warn "e2e_tools_fallback.sh exited $rc_b"
  fi

  if [[ $overall_rc -eq 0 ]]; then
    record "e2e" "ok" "all e2e sub-suites passed"
  else
    record "e2e" "fail" "see logs/e2e_test.log"
  fi
}

# ------------------------------------------------------------
# Main dispatch
# ------------------------------------------------------------
usage() {
  cat <<'USAGE'
Usage: bash test/run_tests.sh [TIER]

Tiers:
  --unit          Pure logic tests (default if no flag given)
  --integration   Boots a mock backend, no LLM needed
  --e2e           Requires a running proxy + backend
  --all           Run all tiers in order (--unit then --integration then --e2e)
  --fast          Alias for --unit (used by pre-commit hook)
  -h, --help      Show this help

Environment:
  PROXY_BASE      Override proxy URL (default: http://127.0.0.1:4000)
  BACKEND_URL     Override backend URL (default: http://127.0.0.1:8081)
  SKIP_E2E=1      Skip the e2e tier when using --all
USAGE
}

main() {
  local mode="${1:---unit}"

  case "$mode" in
    -h|--help) usage; exit 0 ;;
    --unit)         run_unit ;;
    --integration)  run_integration ;;
    --e2e)          run_e2e ;;
    --all)
      run_unit
      echo ""
      run_integration
      echo ""
      if [[ "${SKIP_E2E:-0}" == "1" ]]; then
        warn "SKIP_E2E=1 — skipping e2e tier"
        record "e2e" "skip" "SKIP_E2E=1"
      else
        run_e2e
      fi
      ;;
    --fast) run_unit ;;
    *)
      echo "Unknown tier: $mode" >&2
      usage
      exit 2
      ;;
  esac

  # Summary
  echo ""
  print_banner "Summary"
  local total=0 failed=0
  for entry in "${TIER_RESULTS[@]}"; do
    local tier ok detail
    tier="${entry%%|*}"
    local rest="${entry#*|}"
    ok="${rest%%|*}"
    detail="${rest#*|}"
    total=$((total + 1))
    if [[ "$ok" == "ok" ]]; then
      ok   "  $tier: $detail"
    elif [[ "$ok" == "skip" ]]; then
      warn "  $tier: $detail"
    else
      fail "  $tier: $detail"
      failed=$((failed + 1))
    fi
  done

  echo ""
  if [[ $failed -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}All $total tier(s) passed.${NC}"
    exit 0
  else
    echo -e "${RED}${BOLD}$failed of $total tier(s) failed.${NC}"
    exit 1
  fi
}

main "$@"
