"""Ingest-corners command: mtg ingest-corners <image>"""

import sys
from pathlib import Path

from mtg_collector.db import (
    get_connection,
    init_db,
    CardRepository,
    SetRepository,
    PrintingRepository,
    CollectionRepository,
)
from mtg_collector.services.claude import ClaudeVision
from mtg_collector.services.scryfall import (
    ScryfallAPI,
    ensure_set_cached,
)
from mtg_collector.utils import normalize_condition
from mtg_collector.cli.ingest_ids import RARITY_MAP, resolve_and_add_ids


def register(subparsers):
    """Register the ingest-corners subcommand."""
    parser = subparsers.add_parser(
        "ingest-corners",
        help="Add cards from photos of card corners (bottom-left rarity/CN/set info)",
        description=(
            "Use Claude Vision to read rarity, collector number, and set code "
            "from photos of the bottom-left corners of cards, then add them to "
            "your collection."
        ),
    )
    parser.add_argument(
        "images",
        nargs="+",
        metavar="IMAGE",
        help="Image file(s) showing card corners",
    )
    parser.add_argument(
        "--source",
        default="corner_ingest",
        help="Source identifier for these cards (default: corner_ingest)",
    )
    parser.add_argument(
        "--condition",
        default="Near Mint",
        help="Condition for all cards (default: Near Mint)",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Review detected cards before adding (toggle foil, remove cards)",
    )
    parser.add_argument(
        "--source-image",
        default=None,
        help="Override source image path (default: use image file path being ingested)",
    )
    parser.set_defaults(func=run)


def _review_cards(resolved):
    """Interactive review: let user toggle foil or remove cards before committing."""
    print()
    print("=" * 70)
    print("REVIEW MODE".center(70))
    print("=" * 70)

    while True:
        print()
        for i, r in enumerate(resolved, 1):
            foil_marker = " *FOIL*" if r["foil"] else ""
            print(
                f"  {i:3d}. {r['name']:<35s} "
                f"{r['set_code'].upper()} #{r['cn_display']:<5s} "
                f"[{r['rarity_code']}]{foil_marker}"
            )

        print()
        print("Commands: <number> toggle foil, 'd<number>' remove card, 'a' accept, 'q' quit")

        try:
            choice = input("  > ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return False

        if choice == "q":
            print("  Cancelled.")
            return False

        if choice == "a":
            return True

        # Delete command: d<number>
        if choice.startswith("d"):
            try:
                idx = int(choice[1:])
                if 1 <= idx <= len(resolved):
                    removed = resolved.pop(idx - 1)
                    print(f"  Removed: {removed['name']}")
                    if not resolved:
                        print("  No cards remaining.")
                        return False
                else:
                    print(f"  Invalid number. Enter 1-{len(resolved)}.")
            except ValueError:
                print("  Invalid input. Use d<number> to remove (e.g. d3).")
            continue

        # Toggle foil
        try:
            idx = int(choice)
            if 1 <= idx <= len(resolved):
                r = resolved[idx - 1]
                r["foil"] = not r["foil"]
                status = "FOIL" if r["foil"] else "nonfoil"
                print(f"  Toggled: {r['name']} -> {status}")
            else:
                print(f"  Invalid number. Enter 1-{len(resolved)}.")
        except ValueError:
            print("  Invalid input.")


def run(args):
    """Run the ingest-corners command."""
    # Validate image paths
    for image_path in args.images:
        if not Path(image_path).exists():
            print(f"Error: Image not found: {image_path}")
            sys.exit(1)

    # Initialize Claude Vision
    claude = ClaudeVision()

    # Read corners from all images
    all_detections = []
    for image_path in args.images:
        detections = claude.read_card_corners(image_path)
        if not detections:
            print(f"  Warning: No card corners detected in {image_path}")
            continue
        for d in detections:
            d["_source_image"] = image_path
        all_detections.extend(detections)

    if not all_detections:
        print("Error: No card corners detected in any image.")
        sys.exit(1)

    print()
    print(f"Detected {len(all_detections)} card(s) from {len(args.images)} image(s):")
    for d in all_detections:
        foil_tag = " foil" if d.get("foil") else ""
        print(f"  {d['rarity']} {d['collector_number']} {d['set']}{foil_tag}")
    print()

    # Validate rarity codes
    for d in all_detections:
        if d["rarity"] not in RARITY_MAP:
            print(
                f"  Warning: Unexpected rarity '{d['rarity']}' for "
                f"{d['set']} #{d['collector_number']} — treating as common"
            )
            d["rarity"] = "C"

    # Initialize Scryfall and normalize set codes
    scryfall = ScryfallAPI()

    unique_sets = {}
    for d in all_detections:
        raw = d["set"]
        if raw.lower() not in unique_sets:
            normalized = scryfall.normalize_set_code(raw)
            if not normalized:
                print(f"Error: Unknown set code '{raw}'")
                sys.exit(1)
            unique_sets[raw.lower()] = normalized
        d["set_code"] = unique_sets[raw.lower()]

    # Initialize database
    conn = get_connection(args.db_path)
    init_db(conn)

    card_repo = CardRepository(conn)
    set_repo = SetRepository(conn)
    printing_repo = PrintingRepository(conn)
    collection_repo = CollectionRepository(conn)

    # Cache each unique set once
    for set_code in set(unique_sets.values()):
        ensure_set_cached(scryfall, set_code, card_repo, set_repo, printing_repo, conn)

    condition = normalize_condition(args.condition)

    # If review mode, resolve cards first for display, then let user edit
    if args.review:
        from mtg_collector.services.scryfall import cache_scryfall_data

        resolved = []
        failed = []

        for d in all_detections:
            set_code = d["set_code"]
            cn_raw = d["collector_number"]
            cn_stripped = cn_raw.lstrip("0") or "0"

            printing = printing_repo.get_by_set_cn(set_code, cn_stripped)
            if not printing:
                printing = printing_repo.get_by_set_cn(set_code, cn_raw)

            card_data = None
            if printing and printing.raw_json:
                card_data = printing.get_scryfall_data()

            if not card_data:
                card_data = scryfall.get_card_by_set_cn(set_code, cn_stripped)
                if not card_data:
                    card_data = scryfall.get_card_by_set_cn(set_code, cn_raw)

            if not card_data:
                label = f"{d['rarity']} {cn_raw} {set_code.upper()}"
                print(f"  FAILED: {label} — card not found")
                failed.append(label)
                continue

            cache_scryfall_data(scryfall, card_repo, set_repo, printing_repo, card_data)

            resolved.append({
                "card_data": card_data,
                "set_code": set_code,
                "cn_display": card_data.get("collector_number", cn_raw),
                "name": card_data.get("name", "Unknown"),
                "rarity_code": d["rarity"],
                "foil": d.get("foil", False),
                "_source_image": d.get("_source_image"),
            })

        if failed:
            conn.rollback()
            print()
            print(f"FAILED — {len(failed)} card(s) could not be resolved:")
            for f in failed:
                print(f"  - {f}")
            print("No cards were added. Fix the issues above and try again.")
            sys.exit(1)

        if not _review_cards(resolved):
            conn.rollback()
            sys.exit(0)

        # Build entries from reviewed data
        from mtg_collector.utils import normalize_finish
        from mtg_collector.db.models import CollectionEntry

        added = 0
        for r in resolved:
            finish = normalize_finish("foil" if r["foil"] else "nonfoil")
            si = args.source_image if args.source_image else r.get("_source_image")
            entry = CollectionEntry(
                id=None,
                scryfall_id=r["card_data"]["id"],
                finish=finish,
                condition=condition,
                source=args.source,
                source_image=si,
            )
            entry_id = collection_repo.add(entry)
            print(
                f"  Added (ID: {entry_id}): {r['name']} "
                f"({r['set_code'].upper()} #{r['cn_display']}) [{finish}]"
            )
            added += 1

        conn.commit()
        print()
        print(f"Done! Added {added} card(s) to collection.")
        return

    # Non-review mode: build entries and use shared resolve_and_add_ids
    entries = []
    for d in all_detections:
        entries.append({
            "rarity_code": d["rarity"],
            "rarity": RARITY_MAP[d["rarity"]],
            "collector_number": d["collector_number"],
            "set_code": d["set_code"],
            "foil": d.get("foil", False),
        })

    # Default source_image: CLI override, or the first image path
    si = args.source_image if args.source_image else args.images[0]

    added, failed = resolve_and_add_ids(
        entries=entries,
        scryfall=scryfall,
        card_repo=card_repo,
        set_repo=set_repo,
        printing_repo=printing_repo,
        collection_repo=collection_repo,
        conn=conn,
        condition=condition,
        source=args.source,
        source_image=si,
    )

    if failed:
        conn.rollback()
        print()
        print(f"FAILED — {len(failed)} card(s) could not be resolved:")
        for f in failed:
            print(f"  - {f}")
        print("No cards were added. Fix the issues above and try again.")
        sys.exit(1)
    else:
        conn.commit()
        print()
        print(f"Done! Added {added} card(s) to collection.")
