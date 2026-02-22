#!/bin/bash
# ================================================================
#  AvailAI — One-Shot Deploy Script
#  Run this on a fresh Ubuntu 24.04 DigitalOcean droplet.
#
#  Usage:
#    ssh root@YOUR_SERVER_IP
#    curl -sSL https://raw.githubusercontent.com/YOUR_USER/availai/main/scripts/deploy.sh | bash
#
#    OR if you already cloned the repo:
#    bash scripts/deploy.sh
# ================================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  AvailAI — Deployment Script${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo ""

# ----------------------------------------------------------------
# Step 1: Install Docker if needed
# ----------------------------------------------------------------
echo -e "${YELLOW}[1/6] Checking Docker...${NC}"
if ! command -v docker &> /dev/null; then
    echo "  Installing Docker..."
    apt-get update -qq
    apt-get install -y -qq curl git > /dev/null 2>&1
    curl -fsSL https://get.docker.com | sh > /dev/null 2>&1
    echo -e "  ${GREEN}✓ Docker installed${NC}"
else
    echo -e "  ${GREEN}✓ Docker already installed${NC}"
fi

if ! docker compose version &> /dev/null; then
    apt-get install -y -qq docker-compose-plugin > /dev/null 2>&1
    echo -e "  ${GREEN}✓ Docker Compose installed${NC}"
fi

# ----------------------------------------------------------------
# Step 2: Clone or update the repo
# ----------------------------------------------------------------
echo -e "${YELLOW}[2/6] Getting code...${NC}"
INSTALL_DIR="/root/availai"

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  Repo exists, pulling latest..."
    cd "$INSTALL_DIR"
    git pull
    echo -e "  ${GREEN}✓ Updated to latest${NC}"
else
    echo ""
    read -p "  Enter your GitHub repo URL (e.g. https://github.com/youruser/availai): " REPO_URL

    if [ -z "$REPO_URL" ]; then
        echo -e "${RED}  No repo URL provided. Exiting.${NC}"
        exit 1
    fi

    # If the repo is private, offer to set up credentials
    echo ""
    echo -e "  ${BLUE}Is this a private repo? If so, use a URL like:${NC}"
    echo -e "  ${BLUE}https://YOUR_GITHUB_TOKEN@github.com/youruser/availai${NC}"
    echo ""

    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    echo -e "  ${GREEN}✓ Code cloned${NC}"
fi

# ----------------------------------------------------------------
# Step 3: Collect settings
# ----------------------------------------------------------------
echo -e "${YELLOW}[3/6] Configuring settings...${NC}"

if [ -f "$INSTALL_DIR/.env" ]; then
    echo ""
    read -p "  .env already exists. Overwrite? (y/N): " OVERWRITE
    if [ "$OVERWRITE" != "y" ] && [ "$OVERWRITE" != "Y" ]; then
        echo -e "  ${GREEN}✓ Keeping existing .env${NC}"
        SKIP_ENV=true
    fi
fi

if [ "$SKIP_ENV" != "true" ]; then
    echo ""
    echo -e "  ${BLUE}I need a few things from you. Press Enter to skip optional ones.${NC}"
    echo ""

    # Domain
    read -p "  Your domain (e.g. app.yourdomain.com): " DOMAIN
    if [ -z "$DOMAIN" ]; then
        echo -e "  ${RED}Domain is required for HTTPS and Microsoft login.${NC}"
        read -p "  Your domain: " DOMAIN
        if [ -z "$DOMAIN" ]; then
            echo -e "${RED}  Cannot continue without a domain. Exiting.${NC}"
            exit 1
        fi
    fi

    # Microsoft Azure
    echo ""
    echo -e "  ${BLUE}Microsoft Azure App Registration${NC}"
    echo -e "  ${BLUE}(portal.azure.com → App registrations → your app)${NC}"
    echo -e "  ${BLUE}Redirect URI must be: https://${DOMAIN}/auth/callback${NC}"
    echo ""
    read -p "  Azure Client ID: " AZURE_CLIENT_ID
    read -p "  Azure Client Secret: " AZURE_CLIENT_SECRET
    read -p "  Azure Tenant ID: " AZURE_TENANT_ID

    # AI Key
    echo ""
    echo -e "  ${BLUE}Anthropic API Key (for AI-parsing vendor replies)${NC}"
    echo -e "  ${BLUE}Get one at: console.anthropic.com${NC}"
    read -p "  Anthropic API Key: " ANTHROPIC_KEY

    # Sourcing APIs
    echo ""
    echo -e "  ${BLUE}Sourcing APIs (optional — press Enter to skip)${NC}"
    read -p "  Octopart API Key: " OCTOPART_KEY
    read -p "  BrokerBin API Key: " BB_KEY
    read -p "  BrokerBin API Secret: " BB_SECRET

    # Generate secret
    SECRET_KEY=$(openssl rand -hex 32)

    # Write .env
    cat > "$INSTALL_DIR/.env" <<EOF
# AvailAI Configuration — Generated $(date)

APP_URL=https://${DOMAIN}

# Microsoft Azure
AZURE_CLIENT_ID=${AZURE_CLIENT_ID}
AZURE_CLIENT_SECRET=${AZURE_CLIENT_SECRET}
AZURE_TENANT_ID=${AZURE_TENANT_ID}

# AI Parsing
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}

# Sourcing APIs
OCTOPART_API_KEY=${OCTOPART_KEY}
BROKERBIN_API_KEY=${BB_KEY}
BROKERBIN_API_SECRET=${BB_SECRET}

# Database (don't change)
DATABASE_URL=postgresql://availai:availai@db:5432/availai

# Security
SECRET_KEY=${SECRET_KEY}

# Behavior
OUTREACH_COOLDOWN_DAYS=30
POLL_INTERVAL_MINUTES=5
AUTO_SIGHTING_CONFIDENCE=0.7
EOF

    echo -e "  ${GREEN}✓ .env created${NC}"
fi

# ----------------------------------------------------------------
# Step 4: Configure Caddy (HTTPS)
# ----------------------------------------------------------------
echo -e "${YELLOW}[4/6] Setting up HTTPS...${NC}"

# Get domain from .env if we skipped the prompts
if [ -z "$DOMAIN" ]; then
    DOMAIN=$(grep APP_URL "$INSTALL_DIR/.env" | sed 's|APP_URL=https://||' | sed 's|APP_URL=http://||')
fi

cat > "$INSTALL_DIR/Caddyfile" <<EOF
${DOMAIN} {
    reverse_proxy app:8000
}
EOF
echo -e "  ${GREEN}✓ Caddy configured for ${DOMAIN}${NC}"

# ----------------------------------------------------------------
# Step 5: Open firewall
# ----------------------------------------------------------------
echo -e "${YELLOW}[5/6] Opening firewall...${NC}"
if command -v ufw &> /dev/null; then
    ufw allow 80/tcp > /dev/null 2>&1 || true
    ufw allow 443/tcp > /dev/null 2>&1 || true
    ufw allow 22/tcp > /dev/null 2>&1 || true
    echo -e "  ${GREEN}✓ Ports 80, 443, 22 open${NC}"
else
    echo -e "  ${GREEN}✓ No firewall to configure${NC}"
fi

# ----------------------------------------------------------------
# Step 6: Build and launch
# ----------------------------------------------------------------
echo -e "${YELLOW}[6/6] Building and launching...${NC}"
cd "$INSTALL_DIR"

# Stop existing containers if any
docker compose down 2>/dev/null || true

# Build and start
docker compose up -d --build

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✓ AvailAI is launching!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Waiting for containers to start..."
sleep 10

# Check if containers are running
if docker compose ps | grep -q "running"; then
    echo ""
    echo -e "  ${GREEN}✓ All containers are running${NC}"
    echo ""
    docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
    echo ""
    echo -e "  ${BLUE}Your app is live at:${NC}"
    echo -e "  ${GREEN}  https://${DOMAIN}${NC}"
    echo ""
    echo -e "  ${BLUE}Note: HTTPS certificate takes ~30 seconds on first load.${NC}"
    echo -e "  ${BLUE}If it doesn't load right away, wait a minute and try again.${NC}"
    echo ""
    echo -e "  ${YELLOW}Useful commands:${NC}"
    echo -e "    View logs:      ${GREEN}docker compose logs -f app${NC}"
    echo -e "    Restart:        ${GREEN}docker compose restart${NC}"
    echo -e "    Stop:           ${GREEN}docker compose down${NC}"
    echo -e "    Update code:    ${GREEN}cd /root/availai && git pull && docker compose up -d --build${NC}"
    echo ""
else
    echo ""
    echo -e "  ${RED}⚠ Something might be wrong. Checking logs...${NC}"
    echo ""
    docker compose logs --tail=20
    echo ""
    echo -e "  ${YELLOW}Try: docker compose logs -f app${NC}"
fi
