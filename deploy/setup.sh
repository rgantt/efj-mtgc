#!/usr/bin/env bash
#
# Set up an MTGC container instance (rootless Podman).
# No sudo required — runs entirely as the current user.
# Run from within the repo clone for this instance.
#
# Prerequisites (one-time, requires sudo):
#   sudo apt install podman
#   loginctl enable-linger $USER
#
# Usage:
#   bash deploy/setup.sh <instance> [port]
#
# Examples:
#   bash deploy/setup.sh prod 8081        # explicit port
#   bash deploy/setup.sh feature-xyz      # auto-assigns next free port
#
# Env file:
#   Copies from ~/.config/mtgc/default.env if it exists (set this up once
#   with your API key). Falls back to .env.example (needs manual editing).
#
set -euo pipefail

# Ensure XDG_RUNTIME_DIR is set (required for systemctl --user).
# CI runners and non-interactive sessions often lack this.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

if [ $# -lt 1 ]; then
    echo "Usage: bash deploy/setup.sh <instance> [port]"
    echo "Example: bash deploy/setup.sh prod 8081"
    exit 1
fi

INSTANCE="$1"
SERVICE_NAME="mtgc-${INSTANCE}"
QUADLET_DIR="$HOME/.config/containers/systemd"
MTGC_CONFIG="$HOME/.config/mtgc"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Port assignment ---

if [ $# -ge 2 ]; then
    PORT="$2"
else
    # Let the OS assign an available port at container start
    PORT=0
fi

echo "==> MTGC deployment setup"
echo "    Instance: $INSTANCE"
if [ "$PORT" = "0" ]; then
    echo "    Port:     (auto-assign)"
else
    echo "    Port:     $PORT"
fi
echo "    Service:  $SERVICE_NAME"
echo "    Repo:     $REPO_DIR"

# --- Prerequisites ---

if ! command -v podman &>/dev/null; then
    echo "ERROR: podman not found. Install it first:"
    echo "  sudo apt install podman"
    exit 1
fi

echo "    podman: $(podman --version)"

if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
    echo "WARNING: linger not enabled — services will stop when you log out."
    echo "  Fix with: loginctl enable-linger $USER  (may need sudo)"
fi

# --- Env file ---

ENV_FILE="${MTGC_CONFIG}/${INSTANCE}.env"
if [ ! -f "$ENV_FILE" ]; then
    mkdir -p "$MTGC_CONFIG"
    if [ -f "${MTGC_CONFIG}/default.env" ]; then
        echo "==> Creating $ENV_FILE from default.env..."
        cp "${MTGC_CONFIG}/default.env" "$ENV_FILE"
    else
        echo "==> Creating $ENV_FILE from .env.example..."
        echo "    NOTE: Set ANTHROPIC_API_KEY in $ENV_FILE before starting."
        echo "    (Create ~/.config/mtgc/default.env to skip this for future instances.)"
        cp "$REPO_DIR/.env.example" "$ENV_FILE"
    fi
    chmod 600 "$ENV_FILE"
else
    echo "    $ENV_FILE already exists, skipping"
fi

# --- Build container image ---

echo "==> Building container image (mtgc:$INSTANCE)..."
podman build -t "mtgc:${INSTANCE}" -f "$REPO_DIR/Containerfile" "$REPO_DIR"

# --- Generate and install Quadlet ---

QUADLET_FILE="${QUADLET_DIR}/${SERVICE_NAME}.container"
echo "==> Installing Quadlet: $QUADLET_FILE"
mkdir -p "$QUADLET_DIR"

# When PORT=0 (auto-assign), use ":8081" so Podman picks an available host port.
# Otherwise use "PORT:8081" to bind a specific host port.
if [ "$PORT" = "0" ]; then
    PORT_MAPPING=":8081"
else
    PORT_MAPPING="${PORT}:8081"
fi

sed \
    -e "s|{{INSTANCE}}|${INSTANCE}|g" \
    -e "s|{{PORT}}:8081|${PORT_MAPPING}|g" \
    "$REPO_DIR/deploy/mtgc.container" > "$QUADLET_FILE"

systemctl --user daemon-reload

echo ""
echo "==> Setup complete!"
echo ""
echo "  Start:      systemctl --user start $SERVICE_NAME"
echo "  Port:       podman port systemd-${SERVICE_NAME}"
echo "  Init data:  podman exec -it systemd-${SERVICE_NAME} mtg setup"
echo "  Logs:       journalctl --user -u $SERVICE_NAME -f"
echo "  Teardown:   bash deploy/teardown.sh $INSTANCE"
