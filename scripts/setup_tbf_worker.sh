#!/bin/bash
# Setup script for The Broker Forum (TBF) search worker on the host server.
# Run once on initial deployment. Installs Xvfb, Chrome, and the pinned Python deps.
#
# The host tbf/nc/ics workers run from the pinned-lockfile venv at /root/availai/.venv
# (built from requirements.txt) — the SAME pinned deps as the docker app/enrichment
# images. deploy.sh refreshes this venv on every deploy; this script bootstraps it.
#
# Usage: sudo bash scripts/setup_tbf_worker.sh

set -euo pipefail

REPO_DIR=/root/availai

echo "=== AVAIL TBF Worker Setup ==="

# Virtual display for headed Chrome
echo "Installing Xvfb..."
apt-get update -qq
apt-get install -y -qq xvfb

# Install Google Chrome (Patchright drives real Chrome via channel="chrome", not Chromium)
echo "Installing Google Chrome..."
if ! command -v google-chrome &>/dev/null; then
    wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list
    apt-get update -qq
    apt-get install -y -qq google-chrome-stable
else
    echo "Chrome already installed: $(google-chrome --version)"
fi

# Python deps — pinned-lockfile venv (requirements.txt), NOT ad-hoc pip installs.
echo "Building pinned-lockfile venv at ${REPO_DIR}/.venv..."
cd "${REPO_DIR}"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
# Patchright drives system Google Chrome (channel="chrome"); register it.
.venv/bin/patchright install chrome
echo "venv built; patchright $(.venv/bin/pip show patchright | awk '/^Version:/{print $2}')"

# Create browser profile directory (worker runs as root from /root/availai)
echo "Creating browser profile directory..."
mkdir -p /root/tbf_browser_profile

# Create log directory
mkdir -p /var/log/avail-tbf

# Install systemd services
echo "Installing systemd services..."
cp deploy/avail-xvfb.service /etc/systemd/system/
cp deploy/avail-tbf-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable avail-xvfb
systemctl enable avail-tbf-worker

# Secure .env file permissions (contains TBF credentials)
if [ -f "${REPO_DIR}/.env.tbf-worker" ]; then
    chmod 600 "${REPO_DIR}/.env.tbf-worker"
    echo ".env.tbf-worker permissions set to 600 (owner-only read/write)"
fi

echo ""
echo "=== Setup Complete ==="
echo "Next steps:"
echo "  1. Add TBF_USERNAME and TBF_PASSWORD to ${REPO_DIR}/.env.tbf-worker"
echo "  2. Start Xvfb:     sudo systemctl start avail-xvfb"
echo "  3. Start worker:   sudo systemctl start avail-tbf-worker"
echo "  4. Check status:   sudo systemctl status avail-tbf-worker"
echo "  5. View logs:      sudo journalctl -u avail-tbf-worker -f"
