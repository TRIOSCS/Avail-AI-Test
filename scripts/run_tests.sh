#!/usr/bin/env bash
# Run full AVAIL test suite (unit + integration, excludes E2E).
# Usage: ./scripts/run_tests.sh [pytest-args...]
# Example: ./scripts/run_tests.sh -x -v  # fail fast, verbose

set -e
cd "$(dirname "$0")/.."

# Ensure dev deps installed (pytest, pytest-asyncio, pytest-cov, pytest-timeout)
pip install -q -r requirements-dev.txt 2>/dev/null || true

# Run tests: ignore E2e (Playwright), use 60s timeout per test
exec python3 -m pytest tests/ \
  --ignore=tests/e2e \
  --ignore=tests/test_browser_e2e.py \
  -o addopts="--timeout=60 --timeout-method=thread" \
  "$@"
