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

    # db recache
    recache_parser = db_subparsers.add_parser(
        "recache", help="Fix non-English printings and clear set cache"
    )
    recache_parser.set_defaults(func=run_recache)

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


def run_recache(args):
    """Fix non-English printings in collection and clear set cache."""
    import json
    from mtg_collector.db import get_connection, init_db, PrintingRepository, CardRepository, SetRepository
    from mtg_collector.services.scryfall import ScryfallAPI, cache_scryfall_data

    conn = get_connection(args.db_path)
    init_db(conn)
    api = ScryfallAPI()
    card_repo = CardRepository(conn)
    set_repo = SetRepository(conn)
    printing_repo = PrintingRepository(conn)

    # Step 1: Find non-English printings referenced by collection
    cursor = conn.execute("""
        SELECT DISTINCT p.scryfall_id, p.set_code, p.collector_number, p.raw_json
        FROM collection c
        JOIN printings p ON c.scryfall_id = p.scryfall_id
        WHERE p.raw_json IS NOT NULL
          AND json_extract(p.raw_json, '$.lang') != 'en'
    """)
    non_english = cursor.fetchall()

    if non_english:
        print(f"Found {len(non_english)} non-English printing(s) in collection. Fixing...")
        conn.execute("PRAGMA foreign_keys = OFF")
        fixed = 0
        for row in non_english:
            old_id = row["scryfall_id"]
            set_code = row["set_code"]
            cn = row["collector_number"]
            old_lang = json.loads(row["raw_json"]).get("lang", "?")
            old_name = json.loads(row["raw_json"]).get("name", "?")

            # Fetch English version via /cards/{set}/{cn} (returns English by default)
            en_data = api.get_card_by_set_cn(set_code, cn)
            if not en_data:
                print(f"  SKIP: {old_name} ({set_code.upper()} #{cn}) — English version not found")
                continue

            new_id = en_data["id"]
            if new_id == old_id:
                # Already English despite raw_json saying otherwise — skip
                continue

            # Delete the old non-English printing first (unique constraint on set_code+cn)
            conn.execute("DELETE FROM printings WHERE scryfall_id = ?", (old_id,))

            # Cache the English printing
            cache_scryfall_data(api, card_repo, set_repo, printing_repo, en_data)

            # Update collection entries to point to English printing
            conn.execute(
                "UPDATE collection SET scryfall_id = ? WHERE scryfall_id = ?",
                (new_id, old_id),
            )

            print(f"  Fixed: {old_name} ({set_code.upper()} #{cn}) [{old_lang} -> en]")
            fixed += 1

        conn.execute("PRAGMA foreign_keys = ON")
        print(f"Fixed {fixed} printing(s)")
    else:
        print("No non-English printings found in collection.")

    # Step 2: Delete all printings not referenced by collection (cache cleanup)
    cursor = conn.execute("""
        DELETE FROM printings
        WHERE scryfall_id NOT IN (SELECT DISTINCT scryfall_id FROM collection)
    """)
    print(f"Cleaned {cursor.rowcount} cached printing(s) not in collection")

    # Step 3: Clear cache flags on all sets
    conn.execute("UPDATE sets SET cards_fetched_at = NULL")
    print("Cleared set cache flags (will re-cache on next use)")

    conn.commit()
    print("Done!")


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
