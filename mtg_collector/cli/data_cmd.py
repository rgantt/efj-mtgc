"""Data management commands: mtg data fetch"""

import gzip
import shutil
import sys
import urllib.request
from pathlib import Path

MTGJSON_URL = "https://mtgjson.com/api/v5/AllPrintings.json.gz"


def get_data_dir() -> Path:
    """Get the data directory (~/.mtgc/)."""
    return Path.home() / ".mtgc"


def get_allprintings_path() -> Path:
    """Get the default path for AllPrintings.json."""
    return get_data_dir() / "AllPrintings.json"


def register(subparsers):
    """Register the data subcommand."""
    parser = subparsers.add_parser(
        "data",
        help="Manage MTGJSON data files",
    )
    data_sub = parser.add_subparsers(dest="data_command", metavar="<subcommand>")

    fetch_parser = data_sub.add_parser(
        "fetch",
        help="Download AllPrintings.json from MTGJSON",
    )
    fetch_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if file already exists",
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the data command."""
    if args.data_command == "fetch":
        _fetch(force=args.force)
    else:
        print("Usage: mtg data fetch [--force]")
        sys.exit(1)


def _fetch(force: bool = False):
    """Download AllPrintings.json from MTGJSON."""
    dest = get_allprintings_path()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        size_mb = dest.stat().st_size / (1024 * 1024)
        print(f"AllPrintings.json already exists ({size_mb:.0f} MB): {dest}")
        print("Use --force to re-download.")
        return

    gz_path = dest.parent / "AllPrintings.json.gz"

    print(f"Downloading {MTGJSON_URL} ...")
    try:
        urllib.request.urlretrieve(MTGJSON_URL, str(gz_path))
    except Exception as e:
        print(f"Error downloading: {e}", file=sys.stderr)
        sys.exit(1)

    print("Decompressing ...")
    with gzip.open(gz_path, "rb") as f_in:
        with open(dest, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    gz_path.unlink()

    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"Done! AllPrintings.json ({size_mb:.0f} MB) saved to: {dest}")
