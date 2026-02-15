#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/mtgc"
DATA_DIR="/var/lib/mtgc"
SERVICE_USER="mtgc"
UV="$REPO_DIR/.uv/bin/uv"

cd "$REPO_DIR"

echo "==> Syncing dependencies..."
sudo -u "$SERVICE_USER" "$UV" sync

# First-run setup: create data dir
if [ ! -d "$DATA_DIR" ]; then
    echo "==> First run: creating data directory..."
    sudo mkdir -p "$DATA_DIR"
    sudo chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
fi

# Ensure database + Scryfall cache exist
if [ ! -f "$DATA_DIR/collection.sqlite" ]; then
    echo "==> Running initial setup (DB + Scryfall cache)..."
    sudo -u "$SERVICE_USER" MTGC_HOME="$DATA_DIR" "$UV" run mtg setup
fi

# Ensure MTGJSON price data exists
if [ ! -f "$DATA_DIR/AllPricesToday.json" ]; then
    echo "==> Fetching price data..."
    sudo -u "$SERVICE_USER" MTGC_HOME="$DATA_DIR" "$UV" run mtg data fetch-prices
fi

# Install/update systemd service if changed
if ! diff -q deploy/mtgc.service /etc/systemd/system/mtgc.service &>/dev/null; then
    echo "==> Updating systemd service..."
    sudo cp deploy/mtgc.service /etc/systemd/system/mtgc.service
    sudo systemctl daemon-reload
fi

# Install/update nginx config if changed
NGINX_CHANGED=0
if ! diff -q deploy/mtgc-nginx.conf /etc/nginx/sites-available/mtgc &>/dev/null; then
    echo "==> Updating nginx config..."
    sudo cp deploy/mtgc-nginx.conf /etc/nginx/sites-available/mtgc
    sudo ln -sf /etc/nginx/sites-available/mtgc /etc/nginx/sites-enabled/
    NGINX_CHANGED=1
fi

echo "==> Restarting mtgc service..."
sudo systemctl restart mtgc

if [ "$NGINX_CHANGED" -eq 1 ]; then
    echo "==> Reloading nginx..."
    sudo nginx -t && sudo systemctl reload nginx
fi

# Health check
echo "==> Health check..."
for i in 1 2 3 4 5; do
    if curl -sf http://localhost:8081/ > /dev/null 2>&1; then
        echo "==> Health check passed"
        exit 0
    fi
    echo "    Attempt $i/5 failed, waiting 2s..."
    sleep 2
done

echo "==> Health check FAILED after 5 attempts"
exit 1
