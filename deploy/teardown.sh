#!/usr/bin/env bash
#
# Tear down an MTGC container instance.
# Stops the service, removes the Quadlet, and cleans up the image.
# Data volume and env file are preserved unless --purge is passed.
#
# Usage:
#   bash deploy/teardown.sh <instance>          # keep data
#   bash deploy/teardown.sh <instance> --purge  # remove everything
#
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: bash deploy/teardown.sh <instance> [--purge]"
    exit 1
fi

INSTANCE="$1"
PURGE="${2:-}"
SERVICE_NAME="mtgc-${INSTANCE}"
QUADLET_DIR="$HOME/.config/containers/systemd"
QUADLET_FILE="${QUADLET_DIR}/${SERVICE_NAME}.container"

if [ ! -f "$QUADLET_FILE" ]; then
    echo "ERROR: No Quadlet found for instance '$INSTANCE'"
    echo "    Expected: $QUADLET_FILE"
    exit 1
fi

echo "==> Tearing down $SERVICE_NAME..."

# Stop and disable
systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true

# Remove Quadlet
rm -f "$QUADLET_FILE"
systemctl --user daemon-reload

# Remove image
podman rmi "mtgc:${INSTANCE}" 2>/dev/null || true

echo "    Service stopped and Quadlet removed."

if [ "$PURGE" = "--purge" ]; then
    # Remove data volume
    podman volume rm "systemd-${SERVICE_NAME}-data" 2>/dev/null || true
    echo "    Data volume removed."

    # Remove env file
    rm -f "$HOME/.config/mtgc/${INSTANCE}.env"
    echo "    Env file removed."

    echo "==> Purge complete — all traces of $INSTANCE removed."
else
    echo "    Data volume and env file preserved."
    echo "    Run with --purge to remove everything."
fi
