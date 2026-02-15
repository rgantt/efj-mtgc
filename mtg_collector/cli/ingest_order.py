"""Ingest-order command: mtg ingest-order [FILE ...] [-f format] [--status ordered|owned] [--dry-run]"""

import sys

from mtg_collector.db import (
    CardRepository,
    CollectionRepository,
    OrderRepository,
    PrintingRepository,
    SetRepository,
    get_connection,
    init_db,
)
from mtg_collector.services.order_parser import detect_order_format, parse_order
from mtg_collector.services.order_resolver import commit_orders, resolve_orders
from mtg_collector.services.scryfall import ScryfallAPI


def register(subparsers):
    """Register the ingest-order subcommand."""
    parser = subparsers.add_parser(
        "ingest-order",
        help="Import cards from order data (TCGPlayer, Card Kingdom)",
        description=(
            "Parse order data from files or stdin, resolve cards via Scryfall, "
            "and add them to the collection linked to order records.\n\n"
            "Examples:\n"
            "  mtg ingest-order order.html\n"
            "  mtg ingest-order page1.html page2.html\n"
            "  pbpaste | mtg ingest-order\n"
            "  mtg ingest-order --dry-run order.html"
        ),
        formatter_class=lambda prog: __import__("argparse").RawDescriptionHelpFormatter(prog),
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help="Order file(s) to parse (reads stdin if none provided)",
    )
    parser.add_argument(
        "-f", "--format",
        choices=["auto", "tcg_text", "tcg_html", "ck_text"],
        default="auto",
        help="Order format (default: auto-detect)",
    )
    parser.add_argument(
        "--status",
        choices=["ordered", "owned"],
        default="ordered",
        help="Status for imported cards (default: ordered)",
    )
    parser.add_argument(
        "--source",
        default="order_import",
        help="Source identifier (default: order_import)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and resolve but don't commit to database",
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the ingest-order command."""
    # Read input
    if args.files:
        text = ""
        for filepath in args.files:
            with open(filepath) as f:
                text += f.read()
    else:
        text = sys.stdin.read()

    if not text.strip():
        print("Error: No input provided")
        sys.exit(1)

    # Detect format
    fmt = None if args.format == "auto" else args.format
    detected = detect_order_format(text) if fmt is None else fmt
    print(f"Format: {detected}")

    # Parse
    orders = parse_order(text, fmt)
    if not orders:
        print("No orders found in input")
        sys.exit(1)

    total_items = sum(len(o.items) for o in orders)
    print(f"Parsed {len(orders)} order(s) with {total_items} item(s)")
    print()

    # Show preview
    for order in orders:
        seller = order.seller_name or "Unknown seller"
        print(f"  {seller} — {len(order.items)} item(s)")
        if order.order_number:
            print(f"    Order: {order.order_number}")
        if order.total is not None:
            print(f"    Total: ${order.total:.2f}")
        for item in order.items[:5]:
            foil_tag = " [FOIL]" if item.foil else ""
            price_tag = f" ${item.price:.2f}" if item.price else ""
            print(f"    {item.quantity}x {item.card_name}{foil_tag}{price_tag}")
        if len(order.items) > 5:
            print(f"    ... and {len(order.items) - 5} more")
        print()

    # Initialize database and resolve
    conn = get_connection(args.db_path)
    init_db(conn)

    scryfall = ScryfallAPI()
    card_repo = CardRepository(conn)
    set_repo = SetRepository(conn)
    printing_repo = PrintingRepository(conn)
    collection_repo = CollectionRepository(conn)
    order_repo = OrderRepository(conn)

    print("Resolving cards via Scryfall...")
    resolved = resolve_orders(orders, scryfall, card_repo, set_repo, printing_repo, conn)

    # Show resolution results
    resolved_count = 0
    failed_count = 0
    for ro in resolved:
        for item in ro.items:
            if item.scryfall_id:
                resolved_count += item.parsed.quantity
            else:
                failed_count += item.parsed.quantity
                print(f"  FAILED: {item.error}")

    print(f"\nResolved: {resolved_count}, Failed: {failed_count}")

    if args.dry_run:
        print("\n[DRY RUN] No changes committed.")
        # Show what would happen
        for ro in resolved:
            seller = ro.parsed.seller_name or "Unknown"
            print(f"\n  {seller}:")
            for item in ro.items:
                if item.scryfall_id:
                    print(f"    {item.parsed.quantity}x {item.card_name} ({item.set_code} #{item.collector_number})")
        return

    if failed_count > 0:
        print(f"\nWarning: {failed_count} card(s) could not be resolved (non-MTG cards or not found)")
        print("Resolved cards will still be imported.\n")

    # Commit
    summary = commit_orders(
        resolved,
        order_repo,
        collection_repo,
        conn,
        status=args.status,
        source=args.source,
    )

    print("\nDone!")
    print(f"  Orders created: {summary['orders_created']}")
    print(f"  Cards added: {summary['cards_added']}")
    print(f"  Cards linked to existing: {summary['cards_linked']}")
    if summary["errors"]:
        print(f"  Errors: {len(summary['errors'])}")
