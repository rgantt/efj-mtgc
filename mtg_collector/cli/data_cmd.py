"""Data management commands: mtg data fetch"""

import gzip
import shutil
import sys
import urllib.request
from pathlib import Path

from mtg_collector.utils import get_mtgc_home

_USER_AGENT = "MTGCollectionTool/2.0"

def _download(url: str, dest: Path):
    """Download a URL to a file with proper User-Agent."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)

MTGJSON_URL = "https://mtgjson.com/api/v5/AllPrintings.json.gz"
MTGJSON_PRICES_URL = "https://mtgjson.com/api/v5/AllPricesToday.json.gz"


def get_allprintings_path() -> Path:
    """Get the default path for AllPrintings.json."""
    return get_mtgc_home() / "AllPrintings.json"


def get_allpricestoday_path() -> Path:
    """Get the default path for AllPricesToday.json."""
    return get_mtgc_home() / "AllPricesToday.json"


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

    fetch_prices_parser = data_sub.add_parser(
        "fetch-prices",
        help="Download AllPricesToday.json from MTGJSON",
    )
    fetch_prices_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if file already exists",
    )

    parser.set_defaults(func=run)


def run(args):
    """Run the data command."""
    if args.data_command == "fetch":
        fetch_allprintings(force=args.force)
    elif args.data_command == "fetch-prices":
        _fetch_prices(force=args.force)
    else:
        print("Usage: mtg data {fetch|fetch-prices} [--force]")
        sys.exit(1)


def fetch_allprintings(force: bool = False):
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
    _download(MTGJSON_URL, gz_path)

    print("Decompressing ...")
    with gzip.open(gz_path, "rb") as f_in:
        with open(dest, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    gz_path.unlink()

    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"Done! AllPrintings.json ({size_mb:.0f} MB) saved to: {dest}")


def _fetch_prices(force: bool = False):
    """Download AllPricesToday.json from MTGJSON."""
    dest = get_allpricestoday_path()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        size_mb = dest.stat().st_size / (1024 * 1024)
        print(f"AllPricesToday.json already exists ({size_mb:.0f} MB): {dest}")
        print("Use --force to re-download.")
        return

    gz_path = dest.parent / "AllPricesToday.json.gz"

    print(f"Downloading {MTGJSON_PRICES_URL} ...")
    _download(MTGJSON_PRICES_URL, gz_path)

    print("Decompressing ...")
    with gzip.open(gz_path, "rb") as f_in:
        with open(dest, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    gz_path.unlink()

    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"Done! AllPricesToday.json ({size_mb:.0f} MB) saved to: {dest}")
