"""sample-ingest command: insert sample ingest records for UI testing."""

import hashlib
import json
import shutil
from pathlib import Path

from mtg_collector.db import get_connection, init_db
from mtg_collector.db.models import CardRepository, PrintingRepository
from mtg_collector.utils import get_mtgc_home, now_iso

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"

SAMPLES = [
    {
        "name": "Mox Emerald",
        "set_code": "2ed",
        "collector_number": "262",
        "fixture": "sample-mox-emerald.jpg",
        "stored_name": "sample_mox_emerald.jpg",
        "status": "DONE",
        "confidence": "high",
        "ocr_confidence": [0.95, 0.88],
    },
    {
        "name": "Archenemy's Charm",
        "set_code": "eoe",
        "collector_number": None,  # multiple printings — leave unresolved
        "fixture": "sample-archenemys-charm.jpeg",
        "stored_name": "sample_archenemys_charm.jpg",
        "status": "READY_FOR_DISAMBIGUATION",
        "confidence": "medium",
        "ocr_confidence": [0.92, 0.75],
    },
]


def register(subparsers):
    parser = subparsers.add_parser(
        "sample-ingest",
        help="Insert sample ingest data for UI testing",
    )
    parser.add_argument(
        "--nuke",
        action="store_true",
        help="Delete all ingest_images rows and their image files before inserting samples",
    )
    parser.set_defaults(func=run)


def _format_candidate(printing):
    """Format a Printing into the candidate shape the recent page expects."""
    data = printing.get_scryfall_data()
    if not data:
        return None

    image_uri = None
    if "image_uris" in data:
        image_uri = data["image_uris"].get("small") or data["image_uris"].get("normal")
    elif "card_faces" in data and data["card_faces"]:
        face = data["card_faces"][0]
        if "image_uris" in face:
            image_uri = face["image_uris"].get("small") or face["image_uris"].get("normal")

    prices = data.get("prices", {})
    price = prices.get("usd") or prices.get("usd_foil")

    return {
        "scryfall_id": data["id"],
        "name": data.get("name", "???"),
        "set_code": data.get("set", "???"),
        "set_name": data.get("set_name", ""),
        "collector_number": data.get("collector_number", "???"),
        "rarity": data.get("rarity", "unknown"),
        "image_uri": image_uri,
        "foil": "foil" in data.get("finishes", []),
        "finishes": data.get("finishes", []),
        "promo": data.get("promo", False),
        "full_art": data.get("full_art", False),
        "border_color": data.get("border_color", ""),
        "frame_effects": data.get("frame_effects", []),
        "price": price,
        "artist": data.get("artist", ""),
    }


def _nuke(conn, images_dir):
    """Delete all ingest_images rows and their image files."""
    rows = conn.execute("SELECT id, stored_name FROM ingest_images").fetchall()
    for row in rows:
        filepath = images_dir / row[1]
        if filepath.is_file():
            filepath.unlink()
    conn.execute("DELETE FROM ingest_images")
    conn.commit()
    print(f"  nuked {len(rows)} ingest_images rows")


def run(args):
    conn = get_connection(args.db_path)
    init_db(conn)

    card_repo = CardRepository(conn)
    printing_repo = PrintingRepository(conn)
    images_dir = get_mtgc_home() / "ingest_images"
    images_dir.mkdir(parents=True, exist_ok=True)

    if args.nuke:
        _nuke(conn, images_dir)

    ts = now_iso()

    for sample in SAMPLES:
        # Look up card in local DB
        cards = card_repo.search_cards_by_name(sample["name"], limit=1)
        if not cards:
            print(f"  error: card '{sample['name']}' not found in local DB — run 'mtg setup' first")
            continue

        card = cards[0]
        all_printings = printing_repo.get_by_oracle_id(card.oracle_id)

        # Filter to the target set
        set_printings = [p for p in all_printings if p.set_code == sample["set_code"]]
        if not set_printings:
            print(f"  error: no printings for '{sample['name']}' in set '{sample['set_code']}' — run 'mtg setup' first")
            continue

        # Build candidates list
        candidates = []
        for p in set_printings:
            c = _format_candidate(p)
            if c:
                candidates.append(c)

        if not candidates:
            print(f"  error: no scryfall data for '{sample['name']}' in '{sample['set_code']}'")
            continue

        # Resolve the specific printing for DONE cards
        if sample["collector_number"]:
            target = printing_repo.get_by_set_cn(sample["set_code"], sample["collector_number"])
            if not target:
                print(f"  error: printing {sample['set_code']}#{sample['collector_number']} not found")
                continue
            disambiguated = [target.scryfall_id]
            claude_cn = sample["collector_number"]
        else:
            disambiguated = [None]
            claude_cn = None

        # Build claude_result
        claude_entry = {
            "name": sample["name"],
            "set_code": sample["set_code"],
            "confidence": sample["confidence"],
            "fragment_indices": [0, 1],
        }
        if claude_cn:
            claude_entry["collector_number"] = claude_cn
        claude_result = [claude_entry]

        # Build ocr_result
        ocr_result = [
            {"text": sample["name"], "bbox": {"x": 50, "y": 30, "w": 200, "h": 25}, "confidence": sample["ocr_confidence"][0]},
            {"text": sample["set_code"], "bbox": {"x": 50, "y": 60, "w": 60, "h": 20}, "confidence": sample["ocr_confidence"][1]},
        ]

        # Copy fixture image to ingest_images dir
        fixture_path = FIXTURES_DIR / sample["fixture"]
        image_path = images_dir / sample["stored_name"]
        shutil.copy2(str(fixture_path), str(image_path))
        md5 = hashlib.md5(image_path.read_bytes()).hexdigest()

        # Insert the record
        conn.execute(
            """INSERT INTO ingest_images
               (filename, stored_name, md5, status, ocr_result, claude_result,
                scryfall_matches, disambiguated, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sample["stored_name"],
                sample["stored_name"],
                md5,
                sample["status"],
                json.dumps(ocr_result),
                json.dumps(claude_result),
                json.dumps([candidates]),
                json.dumps(disambiguated),
                ts,
                ts,
            ),
        )
        conn.commit()
        print(f"  inserted: {sample['stored_name']} ({sample['status']}, {len(candidates)} candidates)")

    conn.close()
    print("done")
