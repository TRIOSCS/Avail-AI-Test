#!/usr/bin/env bash
# post-create.sh — runs once after devcontainer is built
# Installs dev dependencies and Playwright Chromium so pytest + Playwright
# work out of the box in /workspace.
set -euo pipefail

# System deps for Playwright Chromium and WeasyPrint
sudo apt-get update && sudo apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 fonts-unifont \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info \
    && sudo rm -rf /var/lib/apt/lists/*

# Install all dev/test dependencies (includes prod deps via -r requirements.txt)
pip install --no-cache-dir -r requirements-dev.txt

# Install Chromium for Playwright-based tests
python3 -m playwright install chromium

echo "Dev environment ready — run: python3 -m pytest tests/"
