#!/bin/bash
# Runs scripts/update-file-index.py before git commit commands.
# Configured as a Claude Code PreToolUse hook in .claude/settings.json.

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [[ "$COMMAND" == *"git commit"* ]]; then
  python3 "$CLAUDE_PROJECT_DIR/scripts/update-file-index.py"
fi

exit 0
