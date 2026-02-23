#!/usr/bin/env bash
set -euo pipefail

# Usage: ci/sandbox-run.sh <mode> <issue_number> <repo_full_name>
# Env:   ANTHROPIC_API_KEY, GH_TOKEN
#
# Volumes (created automatically, refreshed via ci/refresh-data.sh):
#   mtgc-ci-workspace  — persistent git clone + .venv
#   mtgc-ci-data       — card database + price data (read-only)

MODE="$1"          # "plan" or "implement"
ISSUE_NUMBER="$2"  # e.g. "52"
REPO="$3"          # e.g. "thaen/efj-mtgc"
BASE_BRANCH="${4:-main}"  # optional, e.g. "sqlite-pack-generator"

if [[ "$MODE" != "plan" && "$MODE" != "implement" ]]; then
    echo "Error: mode must be 'plan' or 'implement', got '$MODE'" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_DIR/ci/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${MODE}-${ISSUE_NUMBER}-$(date +%Y%m%d-%H%M%S).log"

# For implement mode, require approval from thaen ("Make it so")
if [[ "$MODE" == "implement" ]]; then
    if ! gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" --jq '
        .[] | select(.user.login == "thaen") | .body
    ' | grep -q "Make it so"; then
        echo "Error: no 'Make it so' comment from thaen on issue #${ISSUE_NUMBER}. Aborting." >&2
        exit 1
    fi
    echo "Approval found. Proceeding with implementation."
fi

podman build -t mtgc:ci-sandbox -f "$REPO_DIR/Containerfile.ci-sandbox" "$REPO_DIR"

echo "Logging to $LOG_FILE"

podman run --rm \
    --memory=4g \
    -v mtgc-ci-workspace:/workspace \
    -v mtgc-ci-data:/data:ro \
    -e ANTHROPIC_API_KEY \
    -e GH_TOKEN \
    -e ISSUE_NUMBER="$ISSUE_NUMBER" \
    -e REPO_FULL_NAME="$REPO" \
    -e MODE="$MODE" \
    -e MTGC_HOME=/data \
    -e BASE_BRANCH="$BASE_BRANCH" \
    localhost/mtgc:ci-sandbox 2>&1 | tee "$LOG_FILE"
