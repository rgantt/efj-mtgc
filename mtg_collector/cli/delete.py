"""Delete command: mtg delete <id>"""

from mtg_collector.db import CollectionRepository, get_connection, init_db


def register(subparsers):
    """Register the delete subcommand."""
    parser = subparsers.add_parser(
        "delete",
        help="Remove a card from your collection",
        description="Delete a specific entry from your collection.",
    )
    parser.add_argument("id", type=int, help="Collection entry ID to delete")
    parser.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompt"
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the delete command."""
    conn = get_connection(args.db_path)
    init_db(conn)

    collection_repo = CollectionRepository(conn)

    # Get entry to show what we're deleting
    entry = collection_repo.get(args.id)

    if not entry:
        print(f"No collection entry found with ID: {args.id}")
        return

    # Get card name for confirmation
    cursor = conn.execute(
        """
        SELECT c.name, p.set_code, p.collector_number
        FROM collection col
        JOIN printings p ON col.scryfall_id = p.scryfall_id
        JOIN cards c ON p.oracle_id = c.oracle_id
        WHERE col.id = ?
        """,
        (args.id,),
    )
    row = cursor.fetchone()

    if row:
        card_desc = f"{row[0]} ({row[1].upper()} #{row[2]})"
    else:
        card_desc = f"Scryfall ID: {entry.scryfall_id}"

    if not args.yes:
        print(f"About to delete: {card_desc}")
        print(f"  Finish: {entry.finish}, Condition: {entry.condition}")
        confirm = input("Are you sure? (y/N): ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return

    conn.execute("DELETE FROM ingest_lineage WHERE collection_id = ?", (args.id,))
    deleted = collection_repo.delete(args.id)
    conn.commit()

    if deleted:
        print(f"Deleted entry #{args.id}: {card_desc}")
    else:
        print(f"Failed to delete entry #{args.id}")
