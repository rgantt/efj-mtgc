"""Ingest command: mtg ingest <image>"""

from pathlib import Path
from typing import List, Dict, Optional, Set as PySet

from mtg_collector.db import (
    get_connection,
    init_db,
    CardRepository,
    SetRepository,
    PrintingRepository,
    CollectionRepository,
)
from mtg_collector.db.models import CollectionEntry
from mtg_collector.services.claude import ClaudeVision
from mtg_collector.services.scryfall import (
    ScryfallAPI,
    cache_scryfall_data,
    ensure_set_cached,
    get_cached_set_cards,
)
from mtg_collector.utils import normalize_condition, normalize_finish


def register(subparsers):
    """Register the ingest subcommand."""
    parser = subparsers.add_parser(
        "ingest",
        help="Analyze card image(s) and add to collection",
        description="Use Claude Vision to identify cards in images and add them to your collection.",
    )
    parser.add_argument("images", nargs="+", metavar="IMAGE", help="Image file(s) to analyze")
    parser.add_argument(
        "--source",
        default="photo_ingest",
        help="Source identifier for these cards (default: photo_ingest)",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Batch mode: auto-select first match, no prompts",
    )
    parser.add_argument(
        "--set",
        dest="set_code_override",
        metavar="CODE",
        help="Override set code for all cards (use when auto-detection fails)",
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the ingest command."""
    # Initialize database if needed
    conn = get_connection(args.db_path)
    init_db(conn)

    # Initialize repositories
    card_repo = CardRepository(conn)
    set_repo = SetRepository(conn)
    printing_repo = PrintingRepository(conn)
    collection_repo = CollectionRepository(conn)

    # Initialize services
    claude = ClaudeVision()
    scryfall = ScryfallAPI()

    print("=" * 80)
    print("MTG CARD COLLECTION BUILDER".center(80))
    print("=" * 80)
    print()

    # Validate set override if provided
    set_override = None
    if args.set_code_override:
        set_override = scryfall.normalize_set_code(args.set_code_override)
        if not set_override:
            print(f"Error: Unknown set code '{args.set_code_override}'")
            print("Use a valid Scryfall set code (e.g., ECL, DMU, MKM)")
            return
        print(f"Using set override: {set_override.upper()}")
        # Pre-cache the override set
        ensure_set_cached(scryfall, set_override, card_repo, set_repo, printing_repo, conn)
        print()

    total_added = 0

    for image_path in args.images:
        if not Path(image_path).exists():
            print(f"Image not found: {image_path}")
            continue

        added = process_image(
            image_path=image_path,
            source=args.source,
            batch_mode=args.batch,
            set_override=set_override,
            claude=claude,
            scryfall=scryfall,
            card_repo=card_repo,
            set_repo=set_repo,
            printing_repo=printing_repo,
            collection_repo=collection_repo,
            conn=conn,
        )
        total_added += added

    print()
    print("=" * 80)
    print(f"Complete! Added {total_added} card(s) to collection.")
    print(f"Database: {args.db_path}")
    print("=" * 80)


def process_image(
    image_path: str,
    source: str,
    batch_mode: bool,
    set_override: Optional[str],
    claude: ClaudeVision,
    scryfall: ScryfallAPI,
    card_repo: CardRepository,
    set_repo: SetRepository,
    printing_repo: PrintingRepository,
    collection_repo: CollectionRepository,
    conn,
) -> int:
    """Process a single image. Returns count of cards added."""
    # Identify cards in image (now returns list of {name, set})
    cards_info = claude.identify_cards(image_path)

    if not cards_info:
        print("  No cards identified in image")
        return 0

    # Collect and normalize unique sets
    detected_sets = collect_and_normalize_sets(cards_info, set_override, scryfall)

    # Cache any sets that aren't already cached
    cache_detected_sets(detected_sets, scryfall, card_repo, set_repo, printing_repo, conn)

    # Load cached card lists for fuzzy matching
    set_card_cache = {}
    for set_code in detected_sets:
        if set_code:
            set_card_cache[set_code] = get_cached_set_cards(conn, set_code)

    added = 0

    for card_info in cards_info:
        card_name = card_info["name"]
        detected_set = card_info.get("_normalized_set")  # Set during normalization

        result = process_card(
            image_path=image_path,
            card_name=card_name,
            detected_set=detected_set,
            all_detected_sets=detected_sets,
            set_card_cache=set_card_cache,
            source=source,
            batch_mode=batch_mode,
            claude=claude,
            scryfall=scryfall,
            card_repo=card_repo,
            set_repo=set_repo,
            printing_repo=printing_repo,
            collection_repo=collection_repo,
        )
        if result:
            added += 1

    conn.commit()
    return added


def collect_and_normalize_sets(
    cards_info: List[Dict],
    set_override: Optional[str],
    scryfall: ScryfallAPI,
) -> PySet[str]:
    """
    Collect and normalize set codes from card info.

    Updates cards_info in place with '_normalized_set' key.
    Returns set of unique normalized set codes.
    """
    detected_sets: PySet[str] = set()

    if set_override:
        # Use override for all cards
        for card in cards_info:
            card["_normalized_set"] = set_override
        detected_sets.add(set_override)
        return detected_sets

    # First pass: normalize set codes we can identify
    unresolved_cards = []
    resolved_set = None

    for card in cards_info:
        raw_set = card.get("set")
        if raw_set:
            normalized = scryfall.normalize_set_code(raw_set)
            if normalized:
                card["_normalized_set"] = normalized
                detected_sets.add(normalized)
                resolved_set = normalized  # Remember for unresolved cards
            else:
                print(f"  Warning: Unknown set code '{raw_set}' for '{card['name']}'")
                unresolved_cards.append(card)
        else:
            unresolved_cards.append(card)

    # Second pass: try to use resolved set for unresolved cards
    # (Claude might not read set on every card, but they're likely from same set)
    if unresolved_cards and resolved_set:
        print(f"  Inferring set {resolved_set.upper()} for {len(unresolved_cards)} card(s) without detected set")
        for card in unresolved_cards:
            card["_normalized_set"] = resolved_set

    return detected_sets


def cache_detected_sets(
    sets: PySet[str],
    scryfall: ScryfallAPI,
    card_repo: CardRepository,
    set_repo: SetRepository,
    printing_repo: PrintingRepository,
    conn,
) -> None:
    """Ensure all detected sets are cached locally."""
    for set_code in sets:
        if set_code:
            ensure_set_cached(scryfall, set_code, card_repo, set_repo, printing_repo, conn)


def process_card(
    image_path: str,
    card_name: str,
    detected_set: Optional[str],
    all_detected_sets: PySet[str],
    set_card_cache: Dict[str, List[Dict]],
    source: str,
    batch_mode: bool,
    claude: ClaudeVision,
    scryfall: ScryfallAPI,
    card_repo: CardRepository,
    set_repo: SetRepository,
    printing_repo: PrintingRepository,
    collection_repo: CollectionRepository,
) -> bool:
    """Process a single card. Returns True if added to collection."""
    print()
    print("-" * 80)
    print(f"Processing: {card_name}" + (f" (set: {detected_set.upper()})" if detected_set else ""))
    print("-" * 80)

    # Get card details from image
    details = claude.get_card_details(image_path, card_name)

    # Try fuzzy matching against detected set's cached card list
    printings = []
    matched_name = None
    matched_set = None

    # First try the detected set
    if detected_set and detected_set in set_card_cache:
        cached_cards = set_card_cache[detected_set]
        matched_card = scryfall.fuzzy_match_in_set(
            card_name, detected_set, cached_cards=cached_cards
        )
        if matched_card:
            matched_name = matched_card["name"]
            matched_set = detected_set

    # If not found in detected set, try other detected sets
    if not matched_name:
        for other_set in all_detected_sets:
            if other_set != detected_set and other_set in set_card_cache:
                cached_cards = set_card_cache[other_set]
                matched_card = scryfall.fuzzy_match_in_set(
                    card_name, other_set, cached_cards=cached_cards
                )
                if matched_card:
                    matched_name = matched_card["name"]
                    matched_set = other_set
                    print(f"    Found in alternate set: {other_set.upper()}")
                    break

    # Search for printings using matched name
    if matched_name and matched_set:
        # For DFCs, use the front face name for searching
        if " // " in matched_name:
            search_name = matched_name.split(" // ")[0]
        else:
            search_name = matched_name

        # Search for printings in this set
        printings = scryfall.search_card(
            search_name,
            set_code=matched_set,
            collector_number=details.get("collector_number"),
            fuzzy=False,
        )
        if printings:
            # Prefer printings from matched set
            set_printings = [p for p in printings if p.get("set", "").lower() == matched_set.lower()]
            if set_printings:
                printings = set_printings

    # Fall back to global Scryfall search if set-based search didn't work
    if not printings:
        printings = scryfall.search_card(
            card_name,
            set_code=details.get("set_code"),
            collector_number=details.get("collector_number"),
        )

    if not printings:
        print(f"  No printings found on Scryfall for '{card_name}'")
        return False

    # Select correct printing
    selected = select_printing(
        scryfall=scryfall,
        name=card_name,
        printings=printings,
        hint_set=detected_set or details.get("set_code"),
        hint_cn=details.get("collector_number"),
        batch_mode=batch_mode,
    )

    if not selected:
        return False

    # Cache Scryfall data
    cache_scryfall_data(scryfall, card_repo, set_repo, printing_repo, selected)

    # Add to collection
    finish = normalize_finish("foil" if details.get("foil") else "nonfoil")
    condition = normalize_condition(details.get("condition", "Near Mint"))

    entry = CollectionEntry(
        id=None,
        scryfall_id=selected["id"],
        finish=finish,
        condition=condition,
        source=source,
    )

    entry_id = collection_repo.add(entry)
    print(f"  Added to collection (ID: {entry_id}): {selected['name']} ({selected['set'].upper()} #{selected.get('collector_number', 'N/A')})")

    return True


def select_printing(
    scryfall: ScryfallAPI,
    name: str,
    printings: List[Dict],
    hint_set: Optional[str],
    hint_cn: Optional[str],
    batch_mode: bool,
) -> Optional[Dict]:
    """Select the correct printing, using hints or user input."""
    if not printings:
        return None

    # Single result - use it
    if len(printings) == 1:
        return printings[0]

    # Try to narrow down using hints
    if hint_set:
        filtered = [p for p in printings if p.get("set", "").lower() == hint_set.lower()]
        if len(filtered) == 1:
            print(f"  Matched by set: {hint_set.upper()}")
            return filtered[0]
        elif len(filtered) > 1:
            printings = filtered

    if hint_cn and hint_set:
        filtered = [
            p
            for p in printings
            if p.get("set", "").lower() == hint_set.lower()
            and p.get("collector_number") == hint_cn
        ]
        if len(filtered) == 1:
            print(f"  Matched by set + collector number: {hint_set.upper()} #{hint_cn}")
            return filtered[0]
        elif len(filtered) > 1:
            printings = filtered

    # Batch mode - auto-select first
    if batch_mode:
        print(f"  Auto-selected (batch mode): {scryfall.format_card_info(printings[0])}")
        return printings[0]

    # Interactive selection
    print(f"\n  Multiple printings found for '{name}':")
    print(f"  {'-' * 95}")
    for idx, card in enumerate(printings, 1):
        print(f"  {idx:3d}. {scryfall.format_card_info(card)}")
    print(f"  {'-' * 95}")

    while True:
        try:
            choice = input(f"  Select printing (1-{len(printings)}) or 's' to skip: ").strip().lower()

            if choice == "s":
                print("  Skipped")
                return None

            choice_num = int(choice)
            if 1 <= choice_num <= len(printings):
                selected = printings[choice_num - 1]
                print(f"  Selected: {scryfall.format_card_info(selected)}")
                return selected
            else:
                print(f"  Invalid choice. Enter 1-{len(printings)} or 's'")
        except ValueError:
            print("  Invalid input. Enter a number or 's'")
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled")
            return None
