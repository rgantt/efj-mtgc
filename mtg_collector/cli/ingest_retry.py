"""Retry stuck ingest images: mtg ingest-retry"""

import sqlite3

from mtg_collector.db import get_connection
from mtg_collector.db.schema import init_db


def register(subparsers):
    """Register the ingest-retry subcommand."""
    parser = subparsers.add_parser(
        "ingest-retry",
        help="Reset stuck PROCESSING (and optionally ERROR) ingest images so they reprocess on next server start",
    )
    parser.add_argument(
        "--errors", action="store_true",
        help="Also reset ERROR images back to READY_FOR_OCR",
    )
    parser.set_defaults(func=run)


def run(args):
    """Reset stuck images to READY_FOR_OCR."""
    from mtg_collector.utils import now_iso

    conn = get_connection(args.db_path)
    init_db(conn)

    now = now_iso()

    # Reset PROCESSING images
    cursor = conn.execute(
        "SELECT id, filename, updated_at FROM ingest_images WHERE status='PROCESSING'"
    )
    processing = cursor.fetchall()

    if processing:
        print(f"Found {len(processing)} stuck PROCESSING image(s):")
        for row in processing:
            print(f"  #{row['id']}  {row['filename']}  (last updated: {row['updated_at']})")
        conn.execute(
            "UPDATE ingest_images SET status='READY_FOR_OCR', updated_at=? WHERE status='PROCESSING'",
            (now,),
        )
    else:
        print("No stuck PROCESSING images found.")

    # Optionally reset ERROR images
    if args.errors:
        cursor = conn.execute(
            "SELECT id, filename, error_message FROM ingest_images WHERE status='ERROR'"
        )
        errors = cursor.fetchall()

        if errors:
            print(f"\nFound {len(errors)} ERROR image(s):")
            for row in errors:
                msg = (row['error_message'] or '')[:80]
                print(f"  #{row['id']}  {row['filename']}  ({msg})")
            conn.execute(
                "UPDATE ingest_images SET status='READY_FOR_OCR', error_message=NULL, updated_at=? WHERE status='ERROR'",
                (now,),
            )
        else:
            print("\nNo ERROR images found.")

    conn.commit()
    conn.close()

    total = len(processing) + (len(errors) if args.errors and errors else 0)
    if total:
        print(f"\nReset {total} image(s) to READY_FOR_OCR.")
        print("Restart the server (mtg crack-pack-server) to reprocess them.")
