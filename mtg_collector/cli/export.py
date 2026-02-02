"""Export command: mtg export"""

from mtg_collector.db import get_connection, init_db
from mtg_collector.exporters import get_exporter, EXPORTERS


def register(subparsers):
    """Register the export subcommand."""
    parser = subparsers.add_parser(
        "export",
        help="Export collection to CSV",
        description="Export your collection to various platform formats.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        required=True,
        help="Output file path",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=list(EXPORTERS.keys()),
        default="moxfield",
        help="Export format (default: moxfield)",
    )
    parser.add_argument(
        "--set",
        dest="set_code",
        metavar="CODE",
        help="Filter by set code",
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        help="Filter by card name (partial match)",
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the export command."""
    conn = get_connection(args.db_path)
    init_db(conn)

    exporter = get_exporter(args.format)

    filters = {}
    if args.set_code:
        filters["set_code"] = args.set_code
    if args.name:
        filters["name"] = args.name

    count = exporter.export(conn, args.output, filters if filters else None)

    if count > 0:
        print(f"Exported {count} card(s) to {args.output}")
        print(f"Format: {exporter.format_name}")
    else:
        print("No cards to export (collection is empty or filters matched nothing)")
