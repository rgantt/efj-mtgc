"""OCR benchmark: compare OCR engines on card photos.

Runs each engine by setting OCR_ENGINE before importing ocr module.

Usage:
    uv run python scripts/ocr_benchmark.py
"""

import importlib
import json
import os
import sqlite3
import textwrap
import time
from pathlib import Path

from mtg_collector.utils import get_mtgc_home

DB = get_mtgc_home() / "collection.sqlite"
IMG_DIR = get_mtgc_home() / "ingest_images"
RAW_DIR = Path(__file__).parent / "ocr_benchmark_raw"

CARDS = [
    ("camera_2026-02-17T13-43-26_238.jpg", "City Pigeon"),
    ("camera_2026-02-17T13-50-57_312.jpg", "Thwip!"),
    ("camera_2026-02-17T13-43-06_233.jpg", "Benalish Hero"),
]

ENGINES = ["paddle", "easyocr"]

COL = 50
SEP = " │ "


def sort_fragments_spatial(fragments: list[dict]) -> list[dict]:
    """Sort fragments top-to-bottom, left-to-right using bounding boxes."""
    return sorted(fragments, key=lambda f: (f["bbox"]["y"], f["bbox"]["x"]))


def run_engine(engine: str, image_path: str) -> tuple[list[dict], float]:
    """Force-load the OCR module with a specific engine, run it, return (fragments, elapsed)."""
    import mtg_collector.services.ocr as ocr_module

    # Reset module state so it re-initializes with the new engine
    os.environ["OCR_ENGINE"] = engine
    ocr_module._ENGINE = engine
    ocr_module._ocr = None
    if engine == "paddle":
        from paddleocr import PaddleOCR  # noqa: ensure it's imported for _get_ocr
    importlib.reload(ocr_module)

    t0 = time.monotonic()
    fragments = ocr_module.run_ocr_with_boxes(image_path)
    elapsed = time.monotonic() - t0
    return sort_fragments_spatial(fragments), elapsed


def wrap(text: str, width: int) -> list[str]:
    lines = []
    for paragraph in text.split("\n"):
        lines.extend(textwrap.wrap(paragraph, width) or [""])
    return lines


def side_by_side(left_title: str, left_lines: list[str], right_title: str, right_lines: list[str]):
    print(f"{'─' * COL}───{'─' * COL}")
    print(f"{left_title:<{COL}}{SEP}{right_title}")
    print(f"{'─' * COL}───{'─' * COL}")
    max_lines = max(len(left_lines), len(right_lines))
    for i in range(max_lines):
        l = left_lines[i] if i < len(left_lines) else ""
        r = right_lines[i] if i < len(right_lines) else ""
        print(f"{l:<{COL}}{SEP}{r}")
    print()


def get_ref_text(conn, card_name: str) -> str:
    row = conn.execute(
        "SELECT name, type_line, mana_cost, oracle_text FROM cards WHERE name = ?",
        (card_name,),
    ).fetchone()
    name, type_line, mana_cost, oracle_text = row
    parts = [name]
    if mana_cost:
        parts.append(mana_cost)
    parts.append(type_line)
    parts.append("")
    parts.append(oracle_text)
    return "\n".join(parts)


def write_raw(engine_name: str, filename: str, fragments: list[dict]):
    path = RAW_DIR / engine_name / filename.replace(".jpg", ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fragments, indent=2))


def main():
    conn = sqlite3.connect(DB)
    timings = []  # (engine, card_name, seconds)

    for engine in ENGINES:
        print(f"\n{'=' * 40} {engine} {'=' * 40}")
        for filename, card_name in CARDS:
            image_path = str(IMG_DIR / filename)
            ref_text = get_ref_text(conn, card_name)
            right = wrap(ref_text, COL)

            fragments, elapsed = run_engine(engine, image_path)
            timings.append((engine, card_name, elapsed))
            write_raw(engine, filename, fragments)

            ocr_text = "\n".join(f["text"] for f in fragments)
            left = wrap(ocr_text, COL)
            side_by_side(f"{engine}: {card_name}", left, f"Scryfall: {card_name}", right)

    conn.close()

    # Timing table
    print(f"\n{'═' * 60}")
    print(f"{'ENGINE':<15} {'CARD':<25} {'TIME (s)':>10}")
    print(f"{'─' * 15} {'─' * 25} {'─' * 10}")
    for engine, card, secs in timings:
        print(f"{engine:<15} {card:<25} {secs:>10.2f}")
    print(f"{'─' * 15} {'─' * 25} {'─' * 10}")
    for engine in ENGINES:
        avg = sum(s for e, _, s in timings if e == engine) / len(CARDS)
        print(f"{engine:<15} {'AVERAGE':<25} {avg:>10.2f}")


if __name__ == "__main__":
    main()
