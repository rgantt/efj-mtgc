#!/usr/bin/env python3
"""Tool-using Claude agent for MTG card identification from photos.

Usage:
    uv run python scripts/agent_ingest.py photo.jpg
    uv run python scripts/agent_ingest.py photo.jpg --max-calls 12

Output:
    Trace lines prefixed with [AGENT], [TOOL CALL], [TOOL RESULT] → stderr
    Final JSON → stdout
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtg_collector.services.agent import run_agent
from mtg_collector.services.ocr import run_ocr_with_boxes


def main():
    parser = argparse.ArgumentParser(
        description="Tool-using Claude agent for MTG card identification from photos."
    )
    parser.add_argument("image", help="Path to card photo")
    parser.add_argument(
        "--max-calls",
        type=int,
        default=None,
        help="Maximum tool calls (default: scales with fragment count, min 8)",
    )
    args = parser.parse_args()

    if not Path(args.image).exists():
        print(f"Error: file not found: {args.image}", file=sys.stderr)
        sys.exit(1)

    fragments = run_ocr_with_boxes(args.image)
    cards = run_agent(args.image, ocr_fragments=fragments, max_calls=args.max_calls)
    print(json.dumps({"cards": cards}, indent=2))


if __name__ == "__main__":
    main()
