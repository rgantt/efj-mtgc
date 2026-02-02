"""List command: mtg list"""

from mtg_collector.db import get_connection, init_db, CollectionRepository
from mtg_collector.utils import normalize_condition


def register(subparsers):
    """Register the list subcommand."""
    parser = subparsers.add_parser(
        "list",
        help="List cards in your collection",
        description="Query and display cards in your collection with optional filters.",
    )
    parser.add_argument("--set", dest="set_code", metavar="CODE", help="Filter by set code")
    parser.add_argument("--name", metavar="NAME", help="Filter by card name (partial match)")
    parser.add_argument("--foil", action="store_true", help="Show only foil/etched cards")
    parser.add_argument("--nonfoil", action="store_true", help="Show only non-foil cards")
    parser.add_argument(
        "--condition",
        metavar="COND",
        help="Filter by condition (NM, LP, MP, HP, D)",
    )
    parser.add_argument("--source", metavar="SRC", help="Filter by source")
    parser.add_argument(
        "--limit", type=int, default=50, metavar="N", help="Maximum results (default: 50)"
    )
    parser.add_argument("--offset", type=int, default=0, metavar="N", help="Skip first N results")
    parser.set_defaults(func=run)


def run(args):
    """Run the list command."""
    conn = get_connection(args.db_path)
    init_db(conn)

    collection_repo = CollectionRepository(conn)

    # Resolve foil filter
    foil = None
    if args.foil:
        foil = True
    elif args.nonfoil:
        foil = False

    # Normalize condition if provided
    condition = None
    if args.condition:
        condition = normalize_condition(args.condition)

    entries = collection_repo.list_all(
        set_code=args.set_code,
        name=args.name,
        foil=foil,
        condition=condition,
        source=args.source,
        limit=args.limit,
        offset=args.offset,
    )

    if not entries:
        print("No cards found matching your criteria.")
        return

    # Print header
    print(f"{'ID':>6}  {'Name':<30}  {'Set':<6}  {'#':<5}  {'Finish':<8}  {'Condition':<18}  {'Source':<15}")
    print("-" * 110)

    for e in entries:
        print(
            f"{e['id']:>6}  "
            f"{e['name'][:30]:<30}  "
            f"{e['set_code'].upper():<6}  "
            f"{e['collector_number'][:5]:<5}  "
            f"{e['finish']:<8}  "
            f"{e['condition']:<18}  "
            f"{e['source'][:15]:<15}"
        )

    print("-" * 110)
    print(f"Showing {len(entries)} card(s)")

    total = collection_repo.count()
    if total > len(entries):
        print(f"(Total in collection: {total})")
