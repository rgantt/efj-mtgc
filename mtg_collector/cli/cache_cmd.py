"""Cache management commands: mtg cache all"""

import json
import sys

from mtg_collector.db import get_connection, init_db
from mtg_collector.db.models import CardRepository, PrintingRepository, SetRepository
from mtg_collector.services.scryfall import ScryfallAPI
from mtg_collector.utils import get_mtgc_home

_BULK_DATA_URL = "https://api.scryfall.com/bulk-data"


def register(subparsers):
    """Register the cache subcommand."""
    parser = subparsers.add_parser(
        "cache",
        help="Manage local Scryfall cache",
    )
    cache_sub = parser.add_subparsers(dest="cache_command", metavar="<subcommand>")

    all_parser = cache_sub.add_parser(
        "all",
        help="Download and cache all cards from Scryfall bulk data",
    )
    all_parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess all sets, even those already cached",
    )

    parser.set_defaults(func=run)


def run(args):
    """Run the cache command."""
    if args.cache_command == "all":
        cache_all(force=args.force, db_path=args.db_path)
    else:
        print("Usage: mtg cache all [--force]")
        sys.exit(1)


def cache_all(force: bool, db_path: str):
    """Download Scryfall bulk data and cache all cards/sets/printings."""
    conn = get_connection(db_path)
    init_db(conn)

    card_repo = CardRepository(conn)
    set_repo = SetRepository(conn)
    printing_repo = PrintingRepository(conn)
    api = ScryfallAPI()

    # Step 1: Fetch and upsert all sets
    print("Fetching set list from Scryfall...")
    all_sets = api.get_all_sets()
    print(f"  {len(all_sets)} sets found")

    for s in all_sets:
        set_model = api.to_set_model(s)
        set_repo.upsert(set_model)
    conn.commit()

    # Step 2: Build skip set (already-cached set codes)
    if force:
        cached_sets = set()
        print("  --force: will reprocess all sets")
    else:
        cursor = conn.execute(
            "SELECT set_code FROM sets WHERE cards_fetched_at IS NOT NULL"
        )
        cached_sets = {row["set_code"] for row in cursor}
        print(f"  {len(cached_sets)} sets already cached")

    # Step 3: Get bulk data download URL
    print("Fetching bulk data metadata...")
    resp = api.session.get(_BULK_DATA_URL)
    resp.raise_for_status()
    bulk_meta = resp.json()

    download_uri = None
    for entry in bulk_meta.get("data", []):
        if entry.get("type") == "default_cards":
            download_uri = entry["download_uri"]
            break

    if not download_uri:
        print("Error: Could not find default_cards bulk data entry")
        sys.exit(1)

    # Step 4: Stream-download bulk JSON to temp file
    tmp_dir = get_mtgc_home()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / "bulk-default-cards.json"

    print(f"Downloading bulk data to {tmp_path}...")
    with api.session.get(download_uri, stream=True) as resp:
        resp.raise_for_status()
        total = resp.headers.get("Content-Length")
        total_mb = int(total) / (1024 * 1024) if total else None

        downloaded = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                f.write(chunk)
                downloaded += len(chunk)
                mb = downloaded / (1024 * 1024)
                if total_mb:
                    print(f"\r  {mb:.0f} / {total_mb:.0f} MB", end="", flush=True)
                else:
                    print(f"\r  {mb:.0f} MB", end="", flush=True)
    print()  # newline after progress

    # Step 5: Parse and process cards
    print("Processing bulk data...")
    with open(tmp_path, "r") as f:
        cards_data = json.load(f)

    total_cards = len(cards_data)
    print(f"  {total_cards} cards in bulk data")

    processed = 0
    skipped = 0
    new_set_codes = set()

    for card_data in cards_data:
        set_code = card_data.get("set")

        # Skip cards from already-cached sets
        if set_code in cached_sets:
            skipped += 1
            continue

        # Skip cards without oracle_id (tokens, etc.)
        if "oracle_id" not in card_data:
            continue

        # Skip non-English cards
        if card_data.get("lang", "en") != "en":
            continue

        card = api.to_card_model(card_data)
        card_repo.upsert(card)

        printing = api.to_printing_model(card_data)
        printing_repo.upsert(printing)

        new_set_codes.add(set_code)
        processed += 1

        # Commit every 5000 cards and print progress
        if processed % 5000 == 0:
            conn.commit()
            print(f"  Processed {processed} cards...")

    # Final commit for remaining cards
    conn.commit()

    # Step 6: Mark newly processed sets as cached
    for sc in new_set_codes:
        set_repo.mark_cards_cached(sc)
    conn.commit()

    # Step 7: Clean up temp file
    tmp_path.unlink(missing_ok=True)

    # Summary
    print("\nDone!")
    print(f"  Cards processed: {processed}")
    print(f"  Cards skipped (already cached): {skipped}")
    print(f"  New sets cached: {len(new_set_codes)}")
    print(f"  Previously cached sets: {len(cached_sets)}")
