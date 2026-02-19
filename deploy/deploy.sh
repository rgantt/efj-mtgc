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

# Ensure XDG_RUNTIME_DIR is set (required for systemctl --user).
# CI runners and non-interactive sessions often lack this.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

QUADLET_FILE="$HOME/.config/containers/systemd/${SERVICE_NAME}.container"

# If Quadlet doesn't exist yet, delegate to setup.sh for initial install
if [ ! -f "$QUADLET_FILE" ]; then
    echo "==> No Quadlet found for $INSTANCE, running initial setup..."
    bash "$SCRIPT_DIR/setup.sh" "$INSTANCE"
    echo "==> Starting $SERVICE_NAME..."
    systemctl --user start "$SERVICE_NAME"
else
    echo "==> Building container image (mtgc:$INSTANCE)..."
    podman build -t "mtgc:${INSTANCE}" -f Containerfile .

    echo "==> Reloading systemd (picks up Quadlet changes)..."
    systemctl --user daemon-reload

    echo "==> Restarting $SERVICE_NAME..."
    systemctl --user restart "$SERVICE_NAME"
fi
# Wait briefly for the container to start, then discover the assigned port
sleep 2
PORT_LINE=$(podman port "systemd-${SERVICE_NAME}" 8081/tcp 2>/dev/null || true)
PORT=$(echo "$PORT_LINE" | grep -oP ':\K[0-9]+' | head -1)
if [ -z "$PORT" ]; then
    echo "==> Could not determine port. Check: podman port systemd-${SERVICE_NAME}"
    exit 1
fi
echo "==> Listening on port $PORT"
echo "==> Health check: $SERVICE_NAME (port $PORT)..."
for i in 1 2 3 4 5; do
    if curl -skf "https://localhost:${PORT}/" > /dev/null 2>&1; then
        echo "==> Health check passed"
        exit 0
    fi
    echo "    Attempt $i/5 failed, waiting 2s..."
    sleep 2
done

echo "==> Health check FAILED after 5 attempts"
exit 1
