"""Shared utilities for MTG Collector."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def now_iso() -> str:
    """Return current time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_json_array(value: Optional[str]) -> list:
    """Parse a JSON array string, returning empty list for None/empty."""
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def to_json_array(value: Optional[list]) -> Optional[str]:
    """Convert a list to JSON string, returning None for empty/None."""
    if not value:
        return None
    return json.dumps(value)


def normalize_condition(condition: str) -> str:
    """Normalize condition strings to standard format."""
    condition = condition.strip()

    # Common abbreviations
    abbrevs = {
        "NM": "Near Mint",
        "LP": "Lightly Played",
        "MP": "Moderately Played",
        "HP": "Heavily Played",
        "D": "Damaged",
        "DM": "Damaged",
        "DMG": "Damaged",
        "PL": "Lightly Played",
        "SP": "Lightly Played",  # Slightly Played -> Lightly Played
        "EX": "Lightly Played",  # Excellent -> Lightly Played
        "GD": "Lightly Played",  # Good -> Lightly Played
        "VG": "Lightly Played",  # Very Good -> Lightly Played
    }

    upper = condition.upper()
    if upper in abbrevs:
        return abbrevs[upper]

    # Already in full form
    valid = ["Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged"]
    for v in valid:
        if v.lower() == condition.lower():
            return v

    # Default to Near Mint if unrecognized
    return "Near Mint"


def normalize_finish(finish: str) -> str:
    """Normalize finish strings to standard format."""
    finish = finish.strip().lower()

    if finish in ("foil", "f", "yes", "true", "1"):
        return "foil"
    elif finish in ("etched", "e"):
        return "etched"
    else:
        return "nonfoil"


def store_source_image(image_path: str) -> str:
    """
    Copy a source image into ~/.mtgc/source_images/ and return the stored path.

    If the image is already in the source_images dir, returns its path as-is.
    """
    src = Path(image_path).expanduser().resolve()
    dest_dir = Path.home() / ".mtgc" / "source_images"
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / src.name

    # Avoid copying over itself
    if dest.resolve() == src:
        return str(dest)

    # Error on name collision with different content — likely a duplicate ingest
    if dest.exists():
        if dest.read_bytes() == src.read_bytes():
            return str(dest)
        raise FileExistsError(
            f"Source image '{src.name}' already exists in {dest_dir} with different content. "
            f"This may be a duplicate ingest — check before proceeding."
        )

    shutil.copy2(str(src), str(dest))
    return str(dest)


def format_box(title: str, width: int = 100) -> str:
    """Format a box title for CLI output."""
    return f"{'═' * width}\n{title.center(width)}\n{'═' * width}"
