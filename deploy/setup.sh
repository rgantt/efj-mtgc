#!/usr/bin/env bash
#
# One-time machine setup for MTGC deployment.
# Run this with sudo or as root. Creates a dedicated mtgc user,
# installs uv + the service, and downloads all required data.
#
set -euo pipefail

REPO_DIR="/opt/mtgc"
DATA_DIR="/var/lib/mtgc"
UV_DIR="/opt/mtgc/.uv"
SERVICE_USER="mtgc"

echo "==> MTGC deployment setup"
echo "    Code: $REPO_DIR"
echo "    Data: $DATA_DIR"
echo "    User: $SERVICE_USER"
echo ""

# --- Prerequisites ---

echo "==> Checking prerequisites..."

if ! command -v nginx &>/dev/null; then
    echo "ERROR: nginx not found. Install it first:"
    echo "  sudo apt install nginx"
    exit 1
fi

if ! command -v curl &>/dev/null; then
    echo "ERROR: curl not found. Install it first:"
    echo "  sudo apt install curl"
    exit 1
fi

if ! command -v git &>/dev/null; then
    echo "ERROR: git not found. Install it first:"
    echo "  sudo apt install git"
    exit 1
fi

echo "    nginx: $(which nginx)"

# --- Service user ---

if ! id "$SERVICE_USER" &>/dev/null; then
    echo "==> Creating $SERVICE_USER system user..."
    sudo useradd --system --shell /usr/sbin/nologin --home-dir "$DATA_DIR" "$SERVICE_USER"
fi

# --- Code directory ---

if [ ! -d "$REPO_DIR" ]; then
    echo "==> Cloning repository to $REPO_DIR..."
    sudo git clone https://github.com/thaen/efj-mtgc.git "$REPO_DIR"
    sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR"
fi

# --- Data directory (must exist before uv, which caches here) ---

if [ ! -d "$DATA_DIR" ]; then
    echo "==> Creating data directory..."
    sudo mkdir -p "$DATA_DIR"
fi
sudo chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"

# --- Install uv for mtgc user ---

if [ ! -x "$UV_DIR/bin/uv" ]; then
    echo "==> Installing uv for $SERVICE_USER..."
    sudo mkdir -p "$UV_DIR"
    sudo chown "$SERVICE_USER:$SERVICE_USER" "$UV_DIR"
    curl -LsSf https://astral.sh/uv/install.sh | sudo -u "$SERVICE_USER" env UV_INSTALL_DIR="$UV_DIR/bin" sh
fi

UV="$UV_DIR/bin/uv"
echo "    uv: $UV"

# --- Dependencies ---

echo "==> Installing Python dependencies..."
cd "$REPO_DIR"
sudo -u "$SERVICE_USER" "$UV" sync

echo "==> Running mtg setup (DB + Scryfall cache, ~500MB download)..."
sudo -u "$SERVICE_USER" MTGC_HOME="$DATA_DIR" "$UV" run mtg setup

echo "==> Fetching price data (~200MB download)..."
sudo -u "$SERVICE_USER" MTGC_HOME="$DATA_DIR" "$UV" run mtg data fetch-prices

# --- systemd service ---

echo "==> Installing systemd service..."
sudo cp "$REPO_DIR/deploy/mtgc.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mtgc

# --- nginx ---

echo "==> Installing nginx config..."
sudo cp "$REPO_DIR/deploy/mtgc-nginx.conf" /etc/nginx/sites-available/mtgc
sudo ln -sf /etc/nginx/sites-available/mtgc /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# --- Verify ---

echo ""
echo "==> Verifying..."
sleep 2

if curl -sf http://localhost:8081/ > /dev/null 2>&1; then
    echo "    mtgc service (port 8081): OK"
else
    echo "    mtgc service (port 8081): FAILED"
    echo "    Check: sudo journalctl -u mtgc -n 20 --no-pager"
fi

if curl -sf http://localhost:8082/ > /dev/null 2>&1; then
    echo "    nginx proxy (port 8082): OK"
else
    echo "    nginx proxy (port 8082): FAILED"
    echo "    Check: sudo nginx -t"
fi

echo ""
echo "==> Setup complete!"
