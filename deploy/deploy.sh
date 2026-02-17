#!/usr/bin/env bash
#
# Rebuild and restart a single MTGC instance.
# Run from within the repo clone for this instance.
#
# Usage:
#   bash deploy/deploy.sh <instance>
#
# Example:
#   bash deploy/deploy.sh prod
#
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: bash deploy/deploy.sh <instance>"
    echo "Example: bash deploy/deploy.sh prod"
    exit 1
fi

INSTANCE="$1"
SERVICE_NAME="mtgc-${INSTANCE}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_DIR"

echo "==> Building container image (mtgc:$INSTANCE)..."
podman build -t "mtgc:${INSTANCE}" -f Containerfile .

echo "==> Restarting $SERVICE_NAME..."
systemctl --user restart "$SERVICE_NAME"

# Health check
QUADLET_FILE="$HOME/.config/containers/systemd/${SERVICE_NAME}.container"
PORT=$(grep -oP 'PublishPort=\K[0-9]+' "$QUADLET_FILE")
echo "==> Health check: $SERVICE_NAME (port $PORT)..."
for i in 1 2 3 4 5; do
    if curl -sf "http://localhost:${PORT}/" > /dev/null 2>&1; then
        echo "==> Health check passed"
        exit 0
    fi
    echo "    Attempt $i/5 failed, waiting 2s..."
    sleep 2
done

echo "==> Health check FAILED after 5 attempts"
exit 1
