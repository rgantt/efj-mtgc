"""Edit command: mtg edit <id>"""

from mtg_collector.db import CollectionRepository, get_connection, init_db
from mtg_collector.utils import normalize_condition, normalize_finish


def register(subparsers):
    """Register the edit subcommand."""
    parser = subparsers.add_parser(
        "edit",
        help="Edit a collection entry",
        description="Modify attributes of a card in your collection.",
    )
    parser.add_argument("id", type=int, help="Collection entry ID")
    parser.add_argument("--finish", choices=["nonfoil", "foil", "etched"], help="Set finish")
    parser.add_argument(
        "--condition",
        metavar="COND",
        help="Set condition (NM, LP, MP, HP, D or full names)",
    )
    parser.add_argument("--language", metavar="LANG", help="Set language")
    parser.add_argument("--price", type=float, metavar="PRICE", help="Set purchase price")
    parser.add_argument("--source", metavar="SRC", help="Set source")
    parser.add_argument("--notes", metavar="TEXT", help="Set notes")
    parser.add_argument("--tags", metavar="TAGS", help="Set tags")
    parser.add_argument(
        "--status",
        choices=["owned", "ordered", "listed", "sold", "removed", "traded", "gifted", "lost"],
        help="Set lifecycle status",
    )
    parser.add_argument("--status-note", metavar="NOTE", help="Note for status change (logged in status_log)")
    parser.add_argument("--sale-price", type=float, metavar="PRICE", help="Set sale price (for sold cards)")
    parser.add_argument("--tradelist", type=int, choices=[0, 1], help="Set tradelist flag (deprecated, use --status)")
    parser.add_argument("--alter", type=int, choices=[0, 1], help="Set alter flag")
    parser.add_argument("--proxy", type=int, choices=[0, 1], help="Set proxy flag")
    parser.add_argument("--signed", type=int, choices=[0, 1], help="Set signed flag")
    parser.add_argument("--misprint", type=int, choices=[0, 1], help="Set misprint flag")
    parser.set_defaults(func=run)


def run(args):
    """Run the edit command."""
    conn = get_connection(args.db_path)
    init_db(conn)

    collection_repo = CollectionRepository(conn)

    entry = collection_repo.get(args.id)

    if not entry:
        print(f"No collection entry found with ID: {args.id}")
        return

    changes = []

    if args.finish:
        entry.finish = normalize_finish(args.finish)
        changes.append(f"finish={entry.finish}")

    if args.condition:
        entry.condition = normalize_condition(args.condition)
        changes.append(f"condition={entry.condition}")

    if args.language:
        entry.language = args.language
        changes.append(f"language={entry.language}")

    if args.price is not None:
        entry.purchase_price = args.price
        changes.append(f"purchase_price=${entry.purchase_price:.2f}")

    if args.source:
        entry.source = args.source
        changes.append(f"source={entry.source}")

    if args.notes is not None:
        entry.notes = args.notes
        changes.append("notes=...")

    if args.tags is not None:
        entry.tags = args.tags
        changes.append("tags=...")

    if args.status:
        entry.status = args.status
        changes.append(f"status={entry.status}")

    if args.sale_price is not None:
        entry.sale_price = args.sale_price
        changes.append(f"sale_price=${entry.sale_price:.2f}")

    if args.tradelist is not None:
        entry.tradelist = bool(args.tradelist)
        changes.append(f"tradelist={entry.tradelist}")

    if args.alter is not None:
        entry.alter = bool(args.alter)
        changes.append(f"alter={entry.alter}")

    if args.proxy is not None:
        entry.proxy = bool(args.proxy)
        changes.append(f"proxy={entry.proxy}")

    if args.signed is not None:
        entry.signed = bool(args.signed)
        changes.append(f"signed={entry.signed}")

    if args.misprint is not None:
        entry.misprint = bool(args.misprint)
        changes.append(f"misprint={entry.misprint}")

    if not changes:
        print("No changes specified. Use --help to see available options.")
        return

    collection_repo.update(entry, status_note=getattr(args, 'status_note', None))
    conn.commit()

    print(f"Updated entry #{args.id}: {', '.join(changes)}")
