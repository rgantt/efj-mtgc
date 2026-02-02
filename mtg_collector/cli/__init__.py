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
    from mtg_collector.cli import db_cmd, list_cmd, show, edit, delete, stats, export

    modules = [db_cmd, list_cmd, show, edit, delete, stats, export]

    # Try to import modules that require external dependencies
    try:
        from mtg_collector.cli import ingest
        modules.append(ingest)
    except ImportError as e:
        # anthropic not installed - ingest won't be available
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
