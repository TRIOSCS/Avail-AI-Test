#!/usr/bin/env bash
# run_tests.sh — Safe, stable test runner for AvailAI
#
# Usage:
#   ./run_tests.sh              # Full suite in batches (memory-safe)
#   ./run_tests.sh quick        # Fast run, no coverage report
#   ./run_tests.sh coverage     # Full suite, fail if <100%
#   ./run_tests.sh file <path>  # Single file (e.g. tests/test_routers_rfq.py)
#   ./run_tests.sh match <pat>  # Run tests matching pattern (-k)
#   ./run_tests.sh failed       # Re-run only last-failed tests
#
# Safe by design:
#   - Uses in-memory SQLite (never touches production DB)
#   - Disables rate limiting and scheduler
#   - Ignores E2E/browser tests (run those separately)
#   - Runs in batches to avoid OOM with 195+ test files

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────
PROJECT_DIR="/root/availai"
COVERAGE_TARGET=100
BATCH_SIZE=40  # files per batch — keeps memory under control

# ── Colors ───────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; }
header(){ echo -e "\n${BOLD}═══ $* ═══${NC}"; }

# ── Safety checks ────────────────────────────────────────────────────
cd "$PROJECT_DIR" || { fail "Project directory not found: $PROJECT_DIR"; exit 1; }

if [[ ! -f "tests/conftest.py" ]]; then
    fail "tests/conftest.py not found — are you in the right directory?"
    exit 1
fi

# Ensure we never accidentally connect to production
export TESTING=1
export RATE_LIMIT_ENABLED=false
export PYTHONPATH="$PROJECT_DIR"

# Common pytest opts that suppress noisy log/stdout capture (saves memory)
COMMON_OPTS=(
    --ignore=tests/e2e
    --ignore=tests/test_browser_e2e.py
    --timeout=30
    -p no:logging
    -p no:cacheprovider
)

# ── Parse mode ───────────────────────────────────────────────────────
MODE="${1:-full}"
ARG="${2:-}"
USE_BATCH=false

case "$MODE" in
    quick|q)
        header "Quick Test Run (no coverage)"
        USE_BATCH=true
        PYTEST_EXTRA=( -q --tb=short --no-header )
        ;;
    coverage|cov|c)
        header "Coverage Check (target: ${COVERAGE_TARGET}%)"
        # Coverage needs all files in one run to aggregate correctly
        PYTEST_ARGS=( tests/ --tb=short -q --cov=app --cov-report=term-missing --cov-fail-under="$COVERAGE_TARGET" --override-ini="addopts=" "${COMMON_OPTS[@]}" )
        ;;
    file|f)
        if [[ -z "$ARG" ]]; then
            fail "Usage: ./run_tests.sh file <path>"
            exit 1
        fi
        if [[ ! -f "$ARG" ]]; then
            fail "File not found: $ARG"
            exit 1
        fi
        header "Single File: $ARG"
        PYTEST_ARGS=( "$ARG" -v --tb=short --timeout=30 --override-ini="addopts=" )
        ;;
    match|m|k)
        if [[ -z "$ARG" ]]; then
            fail "Usage: ./run_tests.sh match <pattern>"
            exit 1
        fi
        header "Pattern Match: -k '$ARG'"
        PYTEST_ARGS=( tests/ -v --tb=short -k "$ARG" --override-ini="addopts=" "${COMMON_OPTS[@]}" )
        ;;
    failed|lf)
        header "Re-run Last Failed"
        PYTEST_ARGS=( tests/ -v --tb=long --lf --override-ini="addopts=" "${COMMON_OPTS[@]}" )
        ;;
    full|"")
        header "Full Test Suite (batched, memory-safe)"
        USE_BATCH=true
        PYTEST_EXTRA=( -q --tb=short )
        ;;
    help|--help|-h)
        echo "Usage: ./run_tests.sh [mode] [arg]"
        echo ""
        echo "Modes:"
        echo "  full              Full suite in batches (default)"
        echo "  quick   | q       Fast run, no coverage, batched"
        echo "  coverage| cov     Full suite, fail if <${COVERAGE_TARGET}%"
        echo "  file    | f PATH  Single test file"
        echo "  match   | m PAT   Tests matching -k pattern"
        echo "  failed  | lf      Re-run only last-failed tests"
        echo "  help              This message"
        exit 0
        ;;
    *)
        fail "Unknown mode: $MODE (try: ./run_tests.sh help)"
        exit 1
        ;;
esac

# ── Run ──────────────────────────────────────────────────────────────
info "TESTING=1  RATE_LIMIT_ENABLED=false  PYTHONPATH=$PROJECT_DIR"

START=$(date +%s)

if [[ "$USE_BATCH" == true ]]; then
    # Collect test files, excluding e2e
    mapfile -t ALL_FILES < <(
        find tests/ -maxdepth 1 -name 'test_*.py' -not -name 'test_browser_e2e.py' | sort
        find tests/test_models/ tests/test_services/ -name 'test_*.py' 2>/dev/null | sort
    )

    TOTAL=${#ALL_FILES[@]}
    BATCHES=$(( (TOTAL + BATCH_SIZE - 1) / BATCH_SIZE ))

    info "$TOTAL test files in $BATCHES batches of $BATCH_SIZE"
    echo ""

    OVERALL_EXIT=0
    TOTAL_PASSED=0
    TOTAL_FAILED=0
    TOTAL_ERRORS=0
    FAILED_FILES=()

    for (( i=0; i<TOTAL; i+=BATCH_SIZE )); do
        BATCH_NUM=$(( i / BATCH_SIZE + 1 ))
        BATCH_FILES=("${ALL_FILES[@]:i:BATCH_SIZE}")
        BATCH_COUNT=${#BATCH_FILES[@]}

        echo -e "${BOLD}── Batch $BATCH_NUM/$BATCHES ($BATCH_COUNT files) ──${NC}"

        set +e
        # Strip ANSI colors for reliable parsing
        OUTPUT=$(pytest "${BATCH_FILES[@]}" "${COMMON_OPTS[@]}" "${PYTEST_EXTRA[@]}" --override-ini="addopts=" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
        BATCH_EXIT=$?
        set -e

        # Extract counts from pytest summary line (e.g. "1398 passed, 2 errors")
        SUMMARY=$(echo "$OUTPUT" | tail -5 | grep -E '(passed|failed|error)' | tail -1)
        PASSED=$(echo "$SUMMARY" | grep -oP '\d+(?= passed)' || echo "0")
        FAILED=$(echo "$SUMMARY" | grep -oP '\d+(?= failed)' || echo "0")
        ERRORS=$(echo "$SUMMARY" | grep -oP '\d+(?= error)' || echo "0")
        TOTAL_PASSED=$((TOTAL_PASSED + PASSED))
        TOTAL_FAILED=$((TOTAL_FAILED + FAILED))
        TOTAL_ERRORS=$((TOTAL_ERRORS + ERRORS))

        if [[ $BATCH_EXIT -eq 0 ]]; then
            ok "Batch $BATCH_NUM: ${PASSED} passed"
        elif [[ $BATCH_EXIT -eq 5 ]]; then
            warn "Batch $BATCH_NUM: no tests collected"
        else
            DETAIL="${PASSED} passed"
            [[ "$FAILED" != "0" ]] && DETAIL+=", ${FAILED} failed"
            [[ "$ERRORS" != "0" ]] && DETAIL+=", ${ERRORS} errors"
            fail "Batch $BATCH_NUM: $DETAIL"
            # Show only the failure/error summary lines
            echo "$OUTPUT" | grep -E "^(FAILED|ERROR) " || true
            OVERALL_EXIT=1
            # Track which files had failures/errors
            while IFS= read -r line; do
                FAILED_FILES+=("$line")
            done < <(echo "$OUTPUT" | grep -oP '^(FAILED|ERROR) \K[^: ]+' | sort -u || true)
        fi
    done

    EXIT_CODE=$OVERALL_EXIT

    END=$(date +%s)
    ELAPSED=$((END - START))
    MINUTES=$((ELAPSED / 60))
    SECS=$((ELAPSED % 60))

    echo ""
    header "Result"
    SUMMARY_LINE="${TOTAL_PASSED} passed"
    [[ $TOTAL_FAILED -gt 0 ]] && SUMMARY_LINE+=", ${TOTAL_FAILED} failed"
    [[ $TOTAL_ERRORS -gt 0 ]] && SUMMARY_LINE+=", ${TOTAL_ERRORS} errors"
    info "Total: ${SUMMARY_LINE}  (${MINUTES}m ${SECS}s)"

    if [[ $EXIT_CODE -eq 0 ]]; then
        ok "All tests passed"
    else
        fail "${TOTAL_FAILED} failed, ${TOTAL_ERRORS} errors"
        if [[ ${#FAILED_FILES[@]} -gt 0 ]]; then
            echo ""
            info "Failed in:"
            printf '  %s\n' "${FAILED_FILES[@]}"
        fi
        echo ""
        info "Troubleshooting:"
        echo "  ./run_tests.sh failed        # re-run only failures"
        echo "  ./run_tests.sh file <path>   # run a single file"
        echo "  pytest <file> -k <name> -s   # run one test with print output"
    fi
else
    # Single-shot run (file, match, failed, coverage)
    info "pytest ${PYTEST_ARGS[*]}"
    echo ""

    set +e
    pytest "${PYTEST_ARGS[@]}"
    EXIT_CODE=$?
    set -e

    END=$(date +%s)
    ELAPSED=$((END - START))
    MINUTES=$((ELAPSED / 60))
    SECS=$((ELAPSED % 60))

    echo ""
    header "Result"

    if [[ $EXIT_CODE -eq 0 ]]; then
        ok "All tests passed  (${MINUTES}m ${SECS}s)"
    elif [[ $EXIT_CODE -eq 5 ]]; then
        warn "No tests collected (${MINUTES}m ${SECS}s)"
    else
        fail "Tests failed with exit code $EXIT_CODE  (${MINUTES}m ${SECS}s)"
        echo ""
        info "Troubleshooting:"
        echo "  ./run_tests.sh failed        # re-run only failures"
        echo "  ./run_tests.sh file <path>   # run a single file"
        echo "  pytest <file> -k <name> -s   # run one test with print output"
    fi
fi

exit $EXIT_CODE
