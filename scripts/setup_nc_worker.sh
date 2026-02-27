#!/bin/bash
# Setup script for NetComponents search worker on DigitalOcean server.
# Run once on initial deployment. Installs Xvfb, Chrome, and Python deps.
#
# Usage: sudo bash scripts/setup_nc_worker.sh

set -euo pipefail

echo "=== AVAIL NC Worker Setup ==="

# Virtual display for headed Chrome
echo "Installing Xvfb..."
apt-get update -qq
apt-get install -y -qq xvfb

# Install Google Chrome (Patchright needs real Chrome, not Chromium)
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

# Python deps (in AVAIL's virtualenv)
echo "Installing Python dependencies..."
cd /home/avail/avail-ai
if [ -d ".venv" ]; then
    source .venv/bin/activate
    pip install -q patchright beautifulsoup4
    patchright install chrome
else
    echo "WARNING: No .venv found at /home/avail/avail-ai/.venv"
    echo "Install patchright and beautifulsoup4 manually in your Python environment."
fi

# Create browser profile directory
echo "Creating browser profile directory..."
mkdir -p /home/avail/nc_browser_profile
chown avail:avail /home/avail/nc_browser_profile

# Create log directory
mkdir -p /var/log/avail-nc
chown avail:avail /var/log/avail-nc

# Install systemd services
echo "Installing systemd services..."
cp deploy/avail-xvfb.service /etc/systemd/system/
cp deploy/avail-nc-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable avail-xvfb
systemctl enable avail-nc-worker

# Secure .env file permissions (contains NC credentials)
if [ -f /home/avail/avail-ai/.env ]; then
    chmod 600 /home/avail/avail-ai/.env
    chown avail:avail /home/avail/avail-ai/.env
    echo ".env permissions set to 600 (owner-only read/write)"
fi

echo ""
echo "=== Setup Complete ==="
echo "Next steps:"
echo "  1. Add NC_USERNAME and NC_PASSWORD to your .env file"
echo "  2. Start Xvfb:     sudo systemctl start avail-xvfb"
echo "  3. Start worker:   sudo systemctl start avail-nc-worker"
echo "  4. Check status:   sudo systemctl status avail-nc-worker"
echo "  5. View logs:      sudo journalctl -u avail-nc-worker -f"
