"""Stats command: mtg stats"""

from mtg_collector.db import get_connection, init_db, CollectionRepository


def register(subparsers):
    """Register the stats subcommand."""
    parser = subparsers.add_parser(
        "stats",
        help="Show collection statistics",
        description="Display summary statistics about your collection.",
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the stats command."""
    conn = get_connection(args.db_path)
    init_db(conn)

    collection_repo = CollectionRepository(conn)
    stats = collection_repo.stats()

    print()
    print("=" * 50)
    print("COLLECTION STATISTICS".center(50))
    print("=" * 50)
    print()

    print(f"Total Cards:       {stats['total_cards']:,}")
    print(f"Unique Cards:      {stats['unique_cards']:,}")
    print(f"Unique Printings:  {stats['unique_printings']:,}")
    print()

    if stats["by_finish"]:
        print("By Finish:")
        for finish, count in sorted(stats["by_finish"].items()):
            print(f"  {finish:<10} {count:,}")
        print()

    if stats["by_condition"]:
        print("By Condition:")
        for condition, count in sorted(stats["by_condition"].items()):
            print(f"  {condition:<20} {count:,}")
        print()

    if stats["by_source"]:
        print("By Source:")
        for source, count in sorted(stats["by_source"].items()):
            print(f"  {source:<20} {count:,}")
        print()

    if stats["total_value"] > 0:
        print(f"Total Value:       ${stats['total_value']:,.2f}")
        print()

    # Top sets
    cursor = conn.execute(
        """
        SELECT p.set_code, s.set_name, COUNT(*) as cnt
        FROM collection c
        JOIN printings p ON c.scryfall_id = p.scryfall_id
        JOIN sets s ON p.set_code = s.set_code
        GROUP BY p.set_code
        ORDER BY cnt DESC
        LIMIT 10
        """
    )
    rows = cursor.fetchall()

    if rows:
        print("Top Sets:")
        for row in rows:
            print(f"  {row[0].upper():<6} {row[1][:35]:<35} {row[2]:,}")
        print()

    print("=" * 50)
