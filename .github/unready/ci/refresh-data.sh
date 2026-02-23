#!/usr/bin/env bash
set -euo pipefail

# Refreshes the mtgc-ci-data volume with latest card database and prices.
#
# Runs on the macOS HOST (not inside Podman) so the AllPrintings.json import
# has access to full host RAM instead of competing with prod/staging containers
# inside the 8GB Podman VM.
#
# Requires: uv, git, and a checkout of efj-mtgc somewhere on the host.
#
# Usage: ci/refresh-data.sh <repo_checkout_path> [branch]
# Example:
#   bash ci/refresh-data.sh ~/efj-mtgc
#   bash ci/refresh-data.sh ~/efj-mtgc sqlite-pack-generator
# Cron:
#   0 3 * * 0  bash /path/to/ci/refresh-data.sh /path/to/efj-mtgc

REPO_CHECKOUT="${1:?Usage: refresh-data.sh <repo_checkout_path> [branch]}"
BRANCH="${2:-main}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Temp dir for the data files (avoids clobbering the user's ~/.mtgc)
STAGING_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGING_DIR"' EXIT

echo "=== Step 1: Update repo (branch: $BRANCH) ==="
cd "$REPO_CHECKOUT"
git fetch origin
git checkout "$BRANCH"
git rebase "origin/$BRANCH"
uv sync --group dev

echo ""
echo "=== Step 2: Fetch + import data on host ==="
export MTGC_HOME="$STAGING_DIR"

# Fetch AllPrintings.json (download only, ~130MB compressed → ~514MB)
# Use --force to always re-download for freshness.
# The auto-import after fetch handles the SQLite import.
uv run mtg data fetch --force

# Fetch + import prices
uv run mtg data fetch-prices

echo ""
echo "=== Step 3: Copy into Podman volume ==="
ls -lh "$STAGING_DIR/"

# Use a lightweight helper container to copy host files into the named volume.
# Mount the host staging dir read-only, the volume read-write.
podman run --rm \
    -v "$STAGING_DIR":/src:ro \
    -v mtgc-ci-data:/data \
    docker.io/library/alpine:3.19 sh -c '
        cp /src/AllPrintings.json /data/
        cp /src/AllPricesToday.json /data/
        cp /src/collection.sqlite /data/
        echo "Files copied into mtgc-ci-data:"
        ls -lh /data/
    '

echo ""
echo "=== Done ==="
