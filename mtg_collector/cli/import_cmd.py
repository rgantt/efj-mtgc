"""Import command: mtg import"""

from mtg_collector.db import (
    get_connection,
    init_db,
    CardRepository,
    SetRepository,
    PrintingRepository,
    CollectionRepository,
)
from mtg_collector.importers import get_importer, detect_format, IMPORTERS
from mtg_collector.services.scryfall import ScryfallAPI


def register(subparsers):
    """Register the import subcommand."""
    parser = subparsers.add_parser(
        "import",
        help="Import cards from CSV",
        description="Import cards from external platform CSV exports.",
    )
    parser.add_argument("file", metavar="FILE", help="CSV file to import")
    parser.add_argument(
        "-f",
        "--format",
        choices=list(IMPORTERS.keys()) + ["auto"],
        default="auto",
        help="Import format (default: auto-detect)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview import without saving to database",
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the import command."""
    conn = get_connection(args.db_path)
    init_db(conn)

    # Detect or use specified format
    if args.format == "auto":
        try:
            format_name = detect_format(args.file)
            print(f"Auto-detected format: {format_name}")
        except ValueError as e:
            print(f"Error: {e}")
            return
    else:
        format_name = args.format

    importer = get_importer(format_name)

    # Initialize repositories
    card_repo = CardRepository(conn)
    set_repo = SetRepository(conn)
    printing_repo = PrintingRepository(conn)
    collection_repo = CollectionRepository(conn)
    api = ScryfallAPI()

    print(f"Importing from {args.file} ({importer.format_name} format)...")
    if args.dry_run:
        print("(Dry run - no changes will be saved)")
    print()

    result = importer.import_file(
        file_path=args.file,
        conn=conn,
        card_repo=card_repo,
        set_repo=set_repo,
        printing_repo=printing_repo,
        collection_repo=collection_repo,
        api=api,
        dry_run=args.dry_run,
    )

    print()
    print("=" * 50)
    print("IMPORT SUMMARY".center(50))
    print("=" * 50)
    print(f"Total rows:    {result.total_rows}")
    print(f"Cards added:   {result.cards_added}")
    print(f"Cards skipped: {result.cards_skipped}")

    if result.errors:
        print()
        print(f"Errors ({len(result.errors)}):")
        for error in result.errors[:10]:  # Show first 10 errors
            print(f"  - {error}")
        if len(result.errors) > 10:
            print(f"  ... and {len(result.errors) - 10} more")

    print("=" * 50)

    if args.dry_run:
        print("\nDry run complete. No changes were saved.")
    else:
        print(f"\nImport complete. Database: {args.db_path}")
