"""Show command: mtg show <id>"""

from mtg_collector.db import (
    get_connection,
    init_db,
    CollectionRepository,
    PrintingRepository,
    CardRepository,
    SetRepository,
)


def register(subparsers):
    """Register the show subcommand."""
    parser = subparsers.add_parser(
        "show",
        help="Show details for a collection entry",
        description="Display full details for a specific card in your collection.",
    )
    parser.add_argument("id", type=int, help="Collection entry ID")
    parser.set_defaults(func=run)


def run(args):
    """Run the show command."""
    conn = get_connection(args.db_path)
    init_db(conn)

    collection_repo = CollectionRepository(conn)
    printing_repo = PrintingRepository(conn)
    card_repo = CardRepository(conn)
    set_repo = SetRepository(conn)

    entry = collection_repo.get(args.id)

    if not entry:
        print(f"No collection entry found with ID: {args.id}")
        return

    printing = printing_repo.get(entry.scryfall_id)
    card = card_repo.get(printing.oracle_id) if printing else None
    set_info = set_repo.get(printing.set_code) if printing else None

    print()
    print("=" * 60)
    print(f"Collection Entry #{entry.id}")
    print("=" * 60)

    if card:
        print(f"Card:            {card.name}")
        print(f"Type:            {card.type_line or 'N/A'}")
        print(f"Mana Cost:       {card.mana_cost or 'N/A'}")
        if card.oracle_text:
            print(f"Text:            {card.oracle_text[:60]}...")
    print()

    if printing:
        print(f"Set:             {set_info.set_name if set_info else printing.set_code.upper()} ({printing.set_code.upper()})")
        print(f"Collector #:     {printing.collector_number}")
        print(f"Rarity:          {printing.rarity or 'N/A'}")
        print(f"Artist:          {printing.artist or 'N/A'}")
        if printing.promo:
            print(f"Promo Types:     {', '.join(printing.promo_types) if printing.promo_types else 'Yes'}")
        if printing.frame_effects:
            print(f"Frame Effects:   {', '.join(printing.frame_effects)}")
        print(f"Available:       {', '.join(printing.finishes) if printing.finishes else 'N/A'}")
    print()

    print(f"Status:          {entry.status}")
    print(f"Finish:          {entry.finish}")
    print(f"Condition:       {entry.condition}")
    print(f"Language:        {entry.language}")
    print()

    print(f"Acquired:        {entry.acquired_at or 'N/A'}")
    print(f"Source:          {entry.source}")
    if entry.purchase_price is not None:
        print(f"Purchase Price:  ${entry.purchase_price:.2f}")
    if entry.sale_price is not None:
        print(f"Sale Price:      ${entry.sale_price:.2f}")
    if entry.notes:
        print(f"Notes:           {entry.notes}")
    if entry.tags:
        print(f"Tags:            {entry.tags}")
    print()

    flags = []
    if entry.alter:
        flags.append("Alter")
    if entry.proxy:
        flags.append("Proxy")
    if entry.signed:
        flags.append("Signed")
    if entry.misprint:
        flags.append("Misprint")
    if flags:
        print(f"Flags:           {', '.join(flags)}")

    # Status history
    history = collection_repo.get_status_history(entry.id)
    if history:
        print()
        print("Status History:")
        for h in history:
            from_s = h["from_status"] or "(new)"
            note_s = f" — {h['note']}" if h["note"] else ""
            print(f"  {h['changed_at']}  {from_s} → {h['to_status']}{note_s}")

    print()
    print(f"Scryfall ID:     {entry.scryfall_id}")
    print("=" * 60)
