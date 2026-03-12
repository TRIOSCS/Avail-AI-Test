#!/usr/bin/env bash
# scripts/bootstrap-dev.sh — Dev/runtime bootstrap for cloud and local agents.
# Installs Python dev/test dependencies and frontend dependencies so backend
# pytest and frontend intake tests run without manual setup.
# Called by: engineers, cloud startup scripts, env setup agents.
# Depends on: python3, pip, npm, requirements-dev.txt, package.json.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Bootstrap: validating required tools"
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v npm >/dev/null || { echo "npm is required"; exit 1; }

echo "==> Bootstrap: upgrading pip"
python3 -m pip install --upgrade pip

echo "==> Bootstrap: installing Python dev/test dependencies"
if ! python3 -m pip install -r requirements-dev.txt; then
  echo "==> Fallback: installing Python deps into user site-packages"
  python3 -m pip install --user -r requirements-dev.txt
fi

echo "==> Bootstrap: installing frontend dependencies"
npm install

echo "==> Bootstrap: validating pytest availability"
python3 -m pytest --version

echo "==> Bootstrap: validating frontend test script"
npm run test:frontend -- --test-name-pattern="^$" >/dev/null 2>&1 || true
npm run test:frontend

echo "==> Bootstrap complete"
echo "    python3 -m pytest and npm run test:frontend are ready"
