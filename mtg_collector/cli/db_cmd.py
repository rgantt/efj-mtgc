"""Database management commands: mtg db init/refresh"""

from mtg_collector.db import get_connection, init_db, SCHEMA_VERSION


def register(subparsers):
    """Register the db subcommand."""
    db_parser = subparsers.add_parser("db", help="Database management commands")
    db_subparsers = db_parser.add_subparsers(dest="db_command", metavar="<subcommand>")

    # db init
    init_parser = db_subparsers.add_parser("init", help="Initialize or migrate database")
    init_parser.add_argument(
        "--force", action="store_true", help="Recreate tables even if they exist"
    )
    init_parser.set_defaults(func=run_init)

    # db refresh
    refresh_parser = db_subparsers.add_parser(
        "refresh", help="Re-fetch Scryfall data for cached printings"
    )
    refresh_parser.add_argument(
        "--all", action="store_true", help="Refresh all printings (default: only stale)"
    )
    refresh_parser.set_defaults(func=run_refresh)

    db_parser.set_defaults(func=lambda args: db_parser.print_help())


def run_init(args):
    """Initialize the database."""
    conn = get_connection(args.db_path)

    created = init_db(conn, force=args.force)

    if created:
        print(f"Database initialized at: {args.db_path}")
        print(f"Schema version: {SCHEMA_VERSION}")
    else:
        print(f"Database already up to date (version {SCHEMA_VERSION})")
        print(f"Location: {args.db_path}")


def run_refresh(args):
    """Refresh Scryfall data for cached printings."""
    from mtg_collector.db import get_connection, PrintingRepository, CardRepository, SetRepository
    from mtg_collector.services.scryfall import ScryfallAPI, cache_scryfall_data

    conn = get_connection(args.db_path)
    printing_repo = PrintingRepository(conn)
    card_repo = CardRepository(conn)
    set_repo = SetRepository(conn)
    api = ScryfallAPI()

    # Get all printings
    cursor = conn.execute("SELECT scryfall_id FROM printings")
    scryfall_ids = [row[0] for row in cursor]

    if not scryfall_ids:
        print("No printings cached. Nothing to refresh.")
        return

    print(f"Refreshing {len(scryfall_ids)} printing(s)...")

    refreshed = 0
    errors = 0

    for scryfall_id in scryfall_ids:
        data = api.get_card_by_id(scryfall_id)
        if data:
            cache_scryfall_data(api, card_repo, set_repo, printing_repo, data)
            refreshed += 1
        else:
            print(f"  Failed to fetch: {scryfall_id}")
            errors += 1

    conn.commit()
    print(f"Refreshed {refreshed} printing(s), {errors} error(s)")
