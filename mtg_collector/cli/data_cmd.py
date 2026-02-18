"""Data management commands: mtg data fetch / import-prices / check-prices"""

import gzip
import json
import shutil
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

from mtg_collector.utils import get_mtgc_home, now_iso

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

    data_sub.add_parser(
        "import-prices",
        help="Import AllPricesToday.json into SQLite prices table",
    )

    check_parser = data_sub.add_parser(
        "check-prices",
        help="Spot-check SQLite prices against AllPricesToday.json",
    )
    check_parser.add_argument(
        "--sample",
        type=int,
        default=10,
        help="Number of random cards to check (default: 10)",
    )

    parser.set_defaults(func=run)


def run(args):
    """Run the data command."""
    if args.data_command == "fetch":
        fetch_allprintings(force=args.force)
    elif args.data_command == "fetch-prices":
        _fetch_prices(force=args.force)
    elif args.data_command == "import-prices":
        from mtg_collector.db.connection import get_db_path
        db_path = get_db_path(getattr(args, "db_path", None))
        import_prices(db_path)
    elif args.data_command == "check-prices":
        from mtg_collector.db.connection import get_db_path
        db_path = get_db_path(getattr(args, "db_path", None))
        check_prices(db_path, sample=args.sample)
    else:
        print("Usage: mtg data {fetch|fetch-prices|import-prices|check-prices} [options]")
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
    """Download AllPricesToday.json from MTGJSON, then auto-import into SQLite."""
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

    # Auto-import into SQLite
    try:
        from mtg_collector.db.connection import get_db_path
        db_path = get_db_path()
        import_prices(db_path)
    except Exception as e:
        print(f"Warning: auto-import failed: {e}", file=sys.stderr)


def _ensure_uuid_map(conn: sqlite3.Connection):
    """Build mtgjson_uuid_map from AllPrintings.json if empty."""
    count = conn.execute("SELECT COUNT(*) FROM mtgjson_uuid_map").fetchone()[0]
    if count > 0:
        return

    path = get_allprintings_path()
    if not path.exists():
        print(f"AllPrintings.json not found at {path} — cannot build UUID map")
        return

    print("Building UUID map from AllPrintings.json ...")
    with open(path) as f:
        raw = json.load(f)

    rows = []
    for set_code, set_data in raw.get("data", {}).items():
        for card in set_data.get("cards", []):
            uuid = card.get("uuid")
            number = card.get("number")
            if uuid and number:
                rows.append((uuid, set_code.lower(), number))

    conn.executemany(
        "INSERT OR IGNORE INTO mtgjson_uuid_map (uuid, set_code, collector_number) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    print(f"  UUID map populated: {len(rows)} entries")


def import_prices(db_path: str):
    """Import AllPricesToday.json into SQLite prices table."""
    from mtg_collector.db.schema import init_db

    t0 = time.time()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    _ensure_uuid_map(conn)

    prices_path = get_allpricestoday_path()
    if not prices_path.exists():
        print(f"AllPricesToday.json not found at {prices_path}")
        print("Run: mtg data fetch-prices")
        conn.close()
        return

    print(f"Loading {prices_path} ...")
    with open(prices_path) as f:
        raw = json.load(f)

    data = raw.get("data", {})

    # Build lookup from uuid_map
    uuid_rows = conn.execute("SELECT uuid, set_code, collector_number FROM mtgjson_uuid_map").fetchall()
    uuid_map = {r[0]: (r[1], r[2]) for r in uuid_rows}

    uuid_total = 0
    uuid_mapped = 0
    uuid_unmapped = 0
    price_rows = []
    dates_seen = set()

    # Provider name mapping: MTGJSON key → our source name
    provider_map = {
        "cardkingdom": "cardkingdom",
        "tcgplayer": "tcgplayer",
    }

    for uuid, card_prices in data.items():
        uuid_total += 1
        mapping = uuid_map.get(uuid)
        if not mapping:
            uuid_unmapped += 1
            continue
        uuid_mapped += 1
        set_code, collector_number = mapping

        paper = card_prices.get("paper", {})
        for provider_key, source_name in provider_map.items():
            prov = paper.get(provider_key, {})
            retail = prov.get("retail", {})
            for price_type in ("normal", "foil"):
                prices_by_date = retail.get(price_type, {})
                for date_str, price_val in prices_by_date.items():
                    if price_val is not None:
                        price_rows.append((
                            set_code, collector_number, source_name,
                            price_type, float(price_val), date_str,
                        ))
                        dates_seen.add(date_str)

    print(f"  Inserting {len(price_rows)} price rows ...")
    conn.executemany(
        "INSERT OR IGNORE INTO prices (set_code, collector_number, source, price_type, price, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        price_rows,
    )

    conn.commit()

    # Log the fetch
    dates_list = sorted(dates_seen)
    conn.execute(
        "INSERT INTO price_fetch_log (fetched_at, source_file, dates_imported, uuid_total, uuid_mapped, uuid_unmapped, rows_inserted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), str(prices_path), json.dumps(dates_list), uuid_total, uuid_mapped, uuid_unmapped, len(price_rows)),
    )
    conn.commit()

    elapsed = time.time() - t0
    print(f"  Dates imported: {', '.join(dates_list) if dates_list else 'none'}")
    print(f"  UUIDs: {uuid_total} total, {uuid_mapped} mapped, {uuid_unmapped} unmapped")
    print(f"  Price rows: {len(price_rows)} (INSERT OR IGNORE)")
    print(f"  Elapsed: {elapsed:.1f}s")

    conn.close()


def check_prices(db_path: str, sample: int = 10):
    """Spot-check SQLite prices against AllPricesToday.json source."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Pick N random cards from collection that have printings
    cards = conn.execute("""
        SELECT p.set_code, p.collector_number, c.finish
        FROM collection c
        JOIN printings p ON c.scryfall_id = p.scryfall_id
        ORDER BY RANDOM()
        LIMIT ?
    """, (sample,)).fetchall()

    if not cards:
        print("No cards in collection to check.")
        conn.close()
        return

    # Load AllPricesToday.json for comparison
    prices_path = get_allpricestoday_path()
    if not prices_path.exists():
        print(f"AllPricesToday.json not found at {prices_path}")
        conn.close()
        return

    with open(prices_path) as f:
        raw = json.load(f)
    json_data = raw.get("data", {})

    # Build reverse map: (set_code, cn) → uuid
    uuid_rows = conn.execute("SELECT uuid, set_code, collector_number FROM mtgjson_uuid_map").fetchall()
    reverse_map = {}
    for r in uuid_rows:
        key = (r["set_code"], r["collector_number"])
        reverse_map[key] = r["uuid"]

    print(f"Checking {len(cards)} cards...\n")
    for card in cards:
        sc = card["set_code"].lower()
        cn = card["collector_number"]
        finish = card["finish"]
        price_type = "foil" if finish in ("foil", "etched") else "normal"

        print(f"  {sc}/{cn} ({finish}):")

        # SQLite price
        row = conn.execute(
            "SELECT source, price FROM latest_prices WHERE set_code = ? AND collector_number = ? AND price_type = ?",
            (sc, cn, price_type),
        ).fetchall()
        sqlite_prices = {r["source"]: r["price"] for r in row}

        # JSON price
        uuid = reverse_map.get((sc, cn))
        json_prices = {}
        if uuid and uuid in json_data:
            paper = json_data[uuid].get("paper", {})
            for provider in ("cardkingdom", "tcgplayer"):
                retail = paper.get(provider, {}).get("retail", {})
                by_date = retail.get(price_type, {})
                if by_date:
                    latest = max(by_date.keys())
                    json_prices[provider] = float(by_date[latest])

        for provider in ("cardkingdom", "tcgplayer"):
            sq = sqlite_prices.get(provider)
            js = json_prices.get(provider)
            match = "MATCH" if sq == js else "MISMATCH"
            print(f"    {provider}: sqlite={sq}  json={js}  [{match}]")

    conn.close()
