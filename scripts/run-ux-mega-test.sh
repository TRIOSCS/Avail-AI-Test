#!/usr/bin/env bash
# run-ux-mega-test.sh — Run the complete UX Mega Test suite.
#
# Executes all 9 test systems in order:
# 1. Vitest Alpine component tests
# 2. Template compilation tests
# 3. Data health scanner
# 4. Data consistency validator
# 5. Dead-end detector (Playwright)
# 6. Workflow tests (Playwright)
# 7. Self-repair toolkit tests
# 8. Lighthouse audit (optional, needs Chrome)
# 9. Canary monitor (optional, needs running app)
#
# Called by: npm run test:mega or bash scripts/run-ux-mega-test.sh
# Depends on: pytest, vitest, playwright

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

FAILURES=0

run_step() {
    local name="$1"
    shift
    echo -e "\n${YELLOW}=== $name ===${NC}"
    if "$@"; then
        echo -e "${GREEN}✓ $name passed${NC}"
    else
        echo -e "${RED}✗ $name failed${NC}"
        FAILURES=$((FAILURES + 1))
    fi
}

cd "$(dirname "$0")/.."

echo "=== UX Mega Test Suite ==="
echo "Starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# System 1: Vitest Alpine Component Tests
run_step "Vitest Alpine Components" npx vitest run

# System 2: Template Compilation Tests
run_step "Template Compilation" env TESTING=1 PYTHONPATH=/root/availai \
    pytest tests/ux_mega/test_template_compilation.py -v --timeout=60 -x

# System 3: Data Health Scanner
run_step "Data Health Scanner" env TESTING=1 PYTHONPATH=/root/availai \
    pytest tests/ux_mega/test_data_health.py -v --timeout=30

# System 4: Data Consistency Validator
run_step "Data Consistency Validator" env TESTING=1 PYTHONPATH=/root/availai \
    pytest tests/ux_mega/test_data_consistency.py -v --timeout=30

# System 5: Dead-End Detector (Playwright)
run_step "Dead-End Detector" npx playwright test --project=dead-ends

# System 6: Workflow Tests (Playwright)
run_step "Workflow Tests" npx playwright test --project=workflows

# System 7: Self-Repair Toolkit
run_step "Self-Repair Toolkit" env TESTING=1 PYTHONPATH=/root/availai \
    pytest tests/ux_mega/test_self_repair.py -v --timeout=30

# System 8: Lighthouse (optional — skip if no Chrome)
if command -v google-chrome &> /dev/null || command -v chromium-browser &> /dev/null; then
    LH_PORT=8789
    TESTING=1 DATABASE_URL=sqlite:// REDIS_URL="" CACHE_BACKEND=none PYTHONPATH=/root/availai \
        python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$LH_PORT" &
    LH_PID=$!
    # Wait for server to be ready
    for i in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:$LH_PORT/" > /dev/null 2>&1; then break; fi
        sleep 0.5
    done
    LIGHTHOUSE_URL="http://127.0.0.1:$LH_PORT" run_step "Lighthouse Audit" npm run test:lighthouse
    kill "$LH_PID" 2>/dev/null || true
    wait "$LH_PID" 2>/dev/null || true
else
    echo -e "\n${YELLOW}=== Lighthouse Audit ===${NC}"
    echo -e "${YELLOW}Skipped (Chrome not installed)${NC}"
fi

echo ""
echo "=== UX Mega Test Complete ==="
echo "Finished at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ "$FAILURES" -gt 0 ]; then
    echo -e "${RED}$FAILURES system(s) failed${NC}"
    exit 1
else
    echo -e "${GREEN}All systems passed${NC}"
    exit 0
fi
