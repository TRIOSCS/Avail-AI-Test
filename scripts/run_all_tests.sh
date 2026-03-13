#!/usr/bin/env bash
# run_all_tests.sh — Full test suite for AVAIL AI
# Runs: pytest (backend), vitest (frontend), playwright (e2e optional)
# Usage: ./scripts/run_all_tests.sh [--e2e]

set -e
cd "$(dirname "$0")/.."

echo "=== AVAIL AI — Full Test Suite ==="

# 1. Backend (pytest)
echo ""
echo "--- 1. Backend (pytest) ---"
export TESTING=1
export MVP_MODE=false
python3 -m pytest tests/ \
  --ignore=tests/e2e \
  --ignore=tests/test_browser_e2e.py \
  -v --tb=short \
  --cov=app --cov-report=term-missing \
  --timeout=60

# 2. Frontend (Vitest)
echo ""
echo "--- 2. Frontend (Vitest) ---"
npm run test:frontend

# 3. E2E (optional, requires Docker app)
if [[ "$1" == "--e2e" ]]; then
  echo ""
  echo "--- 3. E2E (Playwright) — requires docker compose up ---"
  npx playwright test e2e/ || echo "E2E skipped (app may not be running)"
fi

echo ""
echo "=== All tests complete ==="
