"""
Tests for corner-based card identification via Claude Vision.

Given a photo of card corners, verify that Claude correctly extracts the
(rarity, collector_number, set, foil) quad for each card.

Test fixtures: tests/fixtures/<name>.jpeg + tests/fixtures/<name>.json
JSON sidecar format (for corner tests):
{
  "description": "...",
  "cards": [
    {"rarity": "C", "collector_number": "0075", "set": "EOE", "foil": false},
    ...
  ]
}

To run: pytest tests/test_corner_identification.py -v

Note: Requires ANTHROPIC_API_KEY. These tests cost real API credits.
"""

import json
import os
from pathlib import Path
from typing import List

import pytest


# =============================================================================
# Test Data Discovery
# =============================================================================

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Corner test fixtures are identified by having "corners" in the filename
CORNER_PHOTOS = []

if FIXTURES_DIR.exists():
    for photo_path in sorted(FIXTURES_DIR.glob("*corners*.jpeg")):
        json_path = photo_path.with_suffix(".json")
        if json_path.exists():
            with open(json_path) as f:
                data = json.load(f)
            CORNER_PHOTOS.append((
                str(photo_path),
                data.get("cards", []),
                data.get("description", photo_path.stem),
            ))


# =============================================================================
# Helpers
# =============================================================================

def normalize_cn(cn: str) -> str:
    """Strip leading zeros for comparison."""
    return cn.lstrip("0") or "0"


def quad_key(card: dict) -> str:
    """Canonical key for a card quad (for matching detected vs expected)."""
    return f"{card['set'].upper()}#{normalize_cn(card['collector_number'])}"


def format_quad(card: dict) -> str:
    """Human-readable quad."""
    foil = " foil" if card.get("foil") else ""
    return f"{card['rarity']} {card['collector_number']} {card['set']}{foil}"


# =============================================================================
# Tests
# =============================================================================

@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
@pytest.mark.skipif(
    not CORNER_PHOTOS,
    reason="No corner test fixtures found",
)
class TestCornerIdentification:
    """Test that Claude correctly reads card corner info from photos."""

    @pytest.mark.parametrize(
        "photo_path,expected_cards,description",
        CORNER_PHOTOS,
        ids=[Path(p[0]).stem for p in CORNER_PHOTOS],
    )
    def test_read_corners(self, photo_path, expected_cards, description):
        """Verify Claude extracts the correct quads from a corner photo."""
        from mtg_collector.services.claude import ClaudeVision

        claude = ClaudeVision()
        detected, skipped = claude.read_card_corners(photo_path)

        # --- Count check ---
        print(f"\n{'='*70}")
        print(f"Test: {description}")
        print(f"{'='*70}")
        print(f"Expected {len(expected_cards)} cards, detected {len(detected)}")

        # --- Build lookup maps ---
        expected_by_key = {}
        for card in expected_cards:
            key = quad_key(card)
            expected_by_key[key] = card

        detected_by_key = {}
        for card in detected:
            key = quad_key(card)
            detected_by_key[key] = card

        # --- Match and validate ---
        errors = []
        matched = []
        missing = []
        extra = []

        for key, exp in expected_by_key.items():
            det = detected_by_key.get(key)
            if det is None:
                missing.append(exp)
                errors.append(f"MISSING: {format_quad(exp)}")
                continue

            matched.append((exp, det))

            # Check rarity
            if det["rarity"].upper() != exp["rarity"].upper():
                errors.append(
                    f"{key}: rarity expected '{exp['rarity']}', "
                    f"got '{det['rarity']}'"
                )

            # Check foil
            if det.get("foil", False) != exp.get("foil", False):
                errors.append(
                    f"{key}: foil expected {exp.get('foil', False)}, "
                    f"got {det.get('foil', False)}"
                )

        for key, det in detected_by_key.items():
            if key not in expected_by_key:
                extra.append(det)
                errors.append(f"EXTRA (unexpected): {format_quad(det)}")

        # --- Report ---
        print(f"\nMatched: {len(matched)}/{len(expected_cards)}")
        if missing:
            print(f"Missing ({len(missing)}):")
            for m in missing:
                print(f"  - {format_quad(m)}")
        if extra:
            print(f"Extra ({len(extra)}):")
            for e in extra:
                print(f"  - {format_quad(e)}")

        print(f"\nDetailed comparison:")
        for exp, det in matched:
            key = quad_key(exp)
            foil_match = det.get("foil", False) == exp.get("foil", False)
            rarity_match = det["rarity"].upper() == exp["rarity"].upper()
            status = "OK" if (foil_match and rarity_match) else "FAIL"
            print(
                f"  [{status}] {key}: "
                f"rarity={det['rarity']}({'ok' if rarity_match else 'WANT ' + exp['rarity']}) "
                f"foil={det.get('foil', False)}({'ok' if foil_match else 'WANT ' + str(exp.get('foil', False))})"
            )

        assert len(detected) == len(expected_cards), (
            f"Count mismatch: expected {len(expected_cards)}, detected {len(detected)}\n"
            f"Expected: {[format_quad(c) for c in expected_cards]}\n"
            f"Detected: {[format_quad(c) for c in detected]}"
        )

        assert not errors, (
            f"Validation errors for {description}:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
