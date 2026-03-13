#!/usr/bin/env bash
# Run full pytest suite (excludes E2E/Playwright).
# Usage: ./scripts/run_tests.sh [pytest args...]
# Examples:
#   ./scripts/run_tests.sh                    # Full run
#   ./scripts/run_tests.sh -x                 # Stop on first failure
#   ./scripts/run_tests.sh -k "avail_score"   # Run matching tests only
#   ./scripts/run_tests.sh --cov=app          # With coverage

set -e
cd "$(dirname "$0")/.."
export TESTING=1
export MVP_MODE=false
export PYTHONPATH="$PWD"

exec python3 -m pytest tests/ \
  --ignore=tests/e2e \
  --ignore=tests/test_browser_e2e.py \
  -v \
  --tb=short \
  "$@"
