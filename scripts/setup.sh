#!/bin/bash
# AvailAI Setup Script
# Run this on a fresh Ubuntu 24.04 DigitalOcean droplet
set -e

echo "================================================"
echo "  AvailAI Setup — Installing everything..."
echo "================================================"
echo ""

# --- Install Docker ---
if ! command -v docker &> /dev/null; then
    echo "→ Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    echo "  ✓ Docker installed"
else
    echo "  ✓ Docker already installed"
fi

# --- Install Docker Compose plugin ---
if ! docker compose version &> /dev/null; then
    echo "→ Installing Docker Compose..."
    apt-get install -y docker-compose-plugin
    echo "  ✓ Docker Compose installed"
else
    echo "  ✓ Docker Compose already installed"
fi

# --- Create .env from template if it doesn't exist ---
if [ ! -f .env ]; then
    cp .env.example .env
    # Generate random secrets
    SESSION_SECRET=$(openssl rand -hex 32)
    PG_PASSWORD=$(openssl rand -hex 16)
    sed -i "s|SESSION_SECRET=change-me-to-random-string|SESSION_SECRET=${SESSION_SECRET}|" .env
    sed -i "s|POSTGRES_PASSWORD=availai|POSTGRES_PASSWORD=${PG_PASSWORD}|" .env
    sed -i "s|postgresql://availai:availai@db:5432/availai|postgresql://availai:${PG_PASSWORD}@db:5432/availai|" .env
    echo "  ✓ Created .env with random session secret and database password"
    echo ""
    echo "================================================"
    echo "  IMPORTANT: Edit .env with your settings!"
    echo "  Run:  nano .env"
    echo "================================================"
else
    echo "  ✓ .env already exists"
fi

# --- Create Caddyfile if it doesn't exist ---
if [ ! -f Caddyfile ]; then
    cp Caddyfile.example Caddyfile
    echo "  ✓ Created Caddyfile"
    echo ""
    echo "================================================"
    echo "  IMPORTANT: Edit Caddyfile with your domain!"
    echo "  Run:  nano Caddyfile"
    echo "================================================"
else
    echo "  ✓ Caddyfile already exists"
fi

# --- Open firewall ports ---
if command -v ufw &> /dev/null; then
    ufw allow 80/tcp > /dev/null 2>&1 || true
    ufw allow 443/tcp > /dev/null 2>&1 || true
    echo "  ✓ Firewall ports 80 and 443 opened"
fi

echo ""
echo "================================================"
echo "  Setup complete! Next steps:"
echo ""
echo "  1. Edit your settings:  nano .env"
echo "  2. Edit your domain:    nano Caddyfile"
echo "  3. Launch:              docker compose up -d"
echo "================================================"
