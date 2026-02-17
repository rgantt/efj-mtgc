#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/mtgc"

cd "$REPO_DIR"

echo "==> Building container image..."
podman build -t mtgc -f Containerfile .

echo "==> Restarting service..."
systemctl --user restart mtgc

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
