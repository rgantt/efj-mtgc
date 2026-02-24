"""Setup command: mtg setup — bootstrap a working installation."""

import shutil

from mtg_collector.db import SCHEMA_VERSION, close_connection, get_connection, init_db


def register(subparsers):
    """Register the setup subcommand."""
    parser = subparsers.add_parser(
        "setup",
        help="Initialize database, cache Scryfall data, and optionally load demo data",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Load demo fixture data (~50 cards) for testing/staging",
    )
    parser.add_argument(
        "--skip-cache",
        action="store_true",
        help="Skip Scryfall bulk data caching",
    )
    parser.add_argument(
        "--skip-data",
        action="store_true",
        help="Skip MTGJSON AllPrintings.json download",
    )
    parser.add_argument(
        "--skip-prices",
        action="store_true",
        help="Skip MTGJSON price data download and import",
    )
    parser.add_argument(
        "--from-fixture",
        metavar="PATH",
        help="Copy a pre-built fixture DB instead of downloading data",
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the setup command."""
    db_path = args.db_path

    if args.from_fixture:
        _run_from_fixture(args)
        return

    # Step 1: Initialize database
    print("=== Step 1: Database ===")
    conn = get_connection(db_path)
    created = init_db(conn)
    if created:
        print(f"  Database initialized (v{SCHEMA_VERSION}) at: {db_path}")
    else:
        print(f"  Database already up to date (v{SCHEMA_VERSION})")
    close_connection()

    # Step 2: Cache Scryfall data
    if args.skip_cache:
        print("\n=== Step 2: Scryfall cache (skipped) ===")
    else:
        print("\n=== Step 2: Scryfall cache ===")
        from mtg_collector.cli.cache_cmd import cache_all
        cache_all(force=False, db_path=db_path)

    # Step 3: Fetch MTGJSON data + import into SQLite
    if args.skip_data:
        print("\n=== Step 3: MTGJSON data (skipped) ===")
    else:
        print("\n=== Step 3: MTGJSON data ===")
        from mtg_collector.cli.data_cmd import fetch_allprintings, import_mtgjson
        fetch_allprintings(force=False)
        # fetch_allprintings auto-runs import_mtgjson on fresh download;
        # run it explicitly in case the file already existed
        import_mtgjson(db_path)

    # Step 3b: Fetch + import MTGJSON prices
    if args.skip_data or args.skip_prices:
        print("\n=== Step 3b: MTGJSON prices (skipped) ===")
    else:
        print("\n=== Step 3b: MTGJSON prices ===")
        from mtg_collector.cli.data_cmd import _fetch_prices, import_prices
        _fetch_prices(force=False)
        import_prices(db_path)

    # Step 3c: Import TCGCSV sealed products (supplements MTGJSON catalog)
    if args.skip_data:
        print("\n=== Step 3c: TCGCSV sealed products (skipped) ===")
    else:
        print("\n=== Step 3c: TCGCSV sealed products ===")
        from mtg_collector.cli.data_cmd import import_sealed_products
        import_sealed_products(db_path)

    # Step 4: Load demo data
    if args.demo:
        print("\n=== Step 4: Demo data ===")
        print("  NOTE: Demo data is for testing/staging only.")
        conn = get_connection(db_path)
        init_db(conn)

        from mtg_collector.cli.demo_data import load_demo_data
        loaded = load_demo_data(conn)
        if loaded:
            print("  Demo data loaded successfully.")
        else:
            print("  Demo data already loaded (skipping).")
        conn.close()

    print("\nSetup complete!")


def _run_from_fixture(args):
    """Fast setup path: copy a pre-built fixture DB, apply migrations, optionally load demo data."""
    db_path = args.db_path
    fixture_path = args.from_fixture

    print("=== Step 1: Copy fixture DB ===")
    shutil.copy2(fixture_path, db_path)
    print(f"  Copied {fixture_path} -> {db_path}")

    print("\n=== Step 2: Apply migrations ===")
    conn = get_connection(db_path)
    created = init_db(conn)
    if created:
        print(f"  Database migrated to v{SCHEMA_VERSION}")
    else:
        print(f"  Database already up to date (v{SCHEMA_VERSION})")
    close_connection()

    if args.demo:
        print("\n=== Step 3: Demo data ===")
        conn = get_connection(db_path)
        init_db(conn)

        from mtg_collector.cli.demo_data import load_demo_data
        loaded = load_demo_data(conn)
        if loaded:
            print("  Demo data loaded successfully.")
        else:
            print("  Demo data already loaded (skipping).")
        conn.close()

    print("\nSetup complete!")
