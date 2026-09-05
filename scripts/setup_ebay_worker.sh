#!/bin/bash
# Setup script for the eBay Browse API search worker on the host server.
# Run once on initial deployment.
#
# This worker is an HTTP API poller, NOT a browser automation: no Xvfb, no
# Chrome, no Patchright registration. It only needs the pinned-lockfile venv at
# /root/availai/.venv (built from requirements.txt) — the SAME pinned deps as
# the docker app/enrichment images. deploy.sh refreshes this venv on every
# deploy; this script bootstraps it.
#
# Usage: sudo bash scripts/setup_ebay_worker.sh

set -euo pipefail

REPO_DIR=/root/availai

echo "=== AVAIL eBay Worker Setup ==="

# Python deps — pinned-lockfile venv (requirements.txt), NOT ad-hoc pip installs.
echo "Building pinned-lockfile venv at ${REPO_DIR}/.venv..."
cd "${REPO_DIR}"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "venv built."

# Log directory
mkdir -p /var/log/avail-ebay

# Env file — tuning knobs only; credentials live in Settings -> Connectors.
if [ ! -f "${REPO_DIR}/.env.ebay-worker" ]; then
    cp "${REPO_DIR}/.env.ebay-worker.example" "${REPO_DIR}/.env.ebay-worker"
    echo "Created .env.ebay-worker from the example template"
fi
chmod 600 "${REPO_DIR}/.env.ebay-worker"
echo ".env.ebay-worker permissions set to 600 (owner-only read/write)"

# Install systemd service
echo "Installing systemd service..."
cp deploy/avail-ebay-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable avail-ebay-worker

echo ""
echo "=== Setup Complete ==="
echo "Next steps:"
echo "  1. Enter EBAY_CLIENT_ID + EBAY_CLIENT_SECRET in AVAIL:"
echo "     Settings -> Connectors -> eBay  (stored encrypted in the DB)"
echo "  2. Review tuning knobs in ${REPO_DIR}/.env.ebay-worker"
echo "  3. Start worker:   sudo systemctl start avail-ebay-worker"
echo "  4. Check status:   sudo systemctl status avail-ebay-worker"
echo "  5. View logs:      sudo journalctl -u avail-ebay-worker -f"
