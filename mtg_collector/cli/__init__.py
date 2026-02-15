"""CLI entry point and subcommand assembly."""

import argparse
import sys

from mtg_collector.db import get_db_path


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="mtg",
        description="MTG Card Collection Builder - Manage your Magic: The Gathering collection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--db",
        metavar="PATH",
        help="Database path (default: $HOME/.mtgc/collection.sqlite, or MTGC_DB env var)",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # Import subcommand modules and register them
    # These imports are done here to allow partial functionality
    # even if some dependencies (like anthropic) are missing
    from mtg_collector.cli import (
        cache_cmd,
        crack_pack,
        crack_pack_server,
        data_cmd,
        db_cmd,
        delete,
        edit,
        export,
        ingest_ids,
        ingest_order,
        ingest_retry,
        list_cmd,
        orders,
        setup_cmd,
        show,
        stats,
        wishlist,
    )

    modules = [db_cmd, data_cmd, cache_cmd, list_cmd, show, edit, delete, stats, export, ingest_ids, ingest_order, ingest_retry, orders, crack_pack, crack_pack_server, wishlist, setup_cmd]

    # Try to import modules that require external dependencies
    try:
        from mtg_collector.cli import ingest_corners
        modules.append(ingest_corners)
    except ImportError:
        # anthropic not installed - ingest-corners won't be available
        pass

    try:
        from mtg_collector.cli import ingest_ocr
        modules.append(ingest_ocr)
    except ImportError:
        # easyocr not installed - ingest-ocr won't be available
        pass

    try:
        from mtg_collector.cli import import_cmd
        modules.append(import_cmd)
    except ImportError:
        pass

    for module in modules:
        module.register(subparsers)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Resolve database path
    args.db_path = get_db_path(args.db)

    # Run the command
    args.func(args)
