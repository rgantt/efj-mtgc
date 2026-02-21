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
        "import",
        help="Import AllPrintings.json into SQLite (cards, booster sheets, configs)",
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

    fetch_sealed_parser = data_sub.add_parser(
        "fetch-sealed-prices",
        help="Fetch sealed product prices from TCGCSV (TCGPlayer mirror)",
    )
    fetch_sealed_parser.add_argument(
        "--set-code",
        help="Only fetch prices for products in this set",
    )

    parser.set_defaults(func=run)


def run(args):
    """Run the data command."""
    if args.data_command == "fetch":
        fetch_allprintings(force=args.force)
    elif args.data_command == "fetch-prices":
        _fetch_prices(force=args.force)
    elif args.data_command == "import":
        from mtg_collector.db.connection import get_db_path
        db_path = get_db_path(getattr(args, "db_path", None))
        import_mtgjson(db_path)
    elif args.data_command == "import-prices":
        from mtg_collector.db.connection import get_db_path
        db_path = get_db_path(getattr(args, "db_path", None))
        import_prices(db_path)
    elif args.data_command == "check-prices":
        from mtg_collector.db.connection import get_db_path
        db_path = get_db_path(getattr(args, "db_path", None))
        check_prices(db_path, sample=args.sample)
    elif args.data_command == "fetch-sealed-prices":
        from mtg_collector.db.connection import get_db_path
        db_path = get_db_path(getattr(args, "db_path", None))
        fetch_sealed_prices(db_path, set_code=getattr(args, "set_code", None))
    else:
        print("Usage: mtg data {fetch|fetch-prices|import|import-prices|check-prices|fetch-sealed-prices} [options]")
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

    # Auto-import into SQLite
    try:
        from mtg_collector.db.connection import get_db_path
        db_path = get_db_path()
        import_mtgjson(db_path)
    except Exception as e:
        print(f"Warning: auto-import failed: {e}", file=sys.stderr)


def import_mtgjson(db_path: str):
    """Import AllPrintings.json into SQLite (printings, booster sheets, configs)."""
    from mtg_collector.db.schema import init_db

    t0 = time.time()

    path = get_allprintings_path()
    if not path.exists():
        print(f"AllPrintings.json not found at {path}")
        print("Run: mtg data fetch")
        return

    print(f"Loading {path} ...")
    with open(path) as f:
        raw = json.load(f)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")  # defer FK checks for bulk import
    init_db(conn)

    # Clear existing data (idempotent re-import)
    conn.execute("DELETE FROM mtgjson_booster_configs")
    conn.execute("DELETE FROM mtgjson_booster_sheets")
    conn.execute("DELETE FROM mtgjson_printings")
    conn.execute("DELETE FROM mtgjson_uuid_map")
    conn.execute("DELETE FROM sealed_products")

    imported_at = now_iso()
    printing_rows = []
    uuid_map_rows = []
    sheet_rows = []
    config_rows = []
    sealed_rows = []
    set_count = 0
    sets_with_boosters = []

    data = raw.get("data", {})
    for set_code_raw, set_data in data.items():
        set_code = set_code_raw.lower()
        set_count += 1

        # Cards → mtgjson_printings + mtgjson_uuid_map
        for card in set_data.get("cards", []):
            uuid = card.get("uuid")
            if not uuid:
                continue
            number = card.get("number", "")
            identifiers = card.get("identifiers", {})
            purchase_urls = card.get("purchaseUrls", {})
            frame_effects = card.get("frameEffects")

            printing_rows.append((
                uuid,
                identifiers.get("scryfallId", ""),
                card.get("name", "Unknown"),
                set_code,
                number,
                card.get("rarity", ""),
                card.get("borderColor", "black"),
                1 if card.get("isFullArt", False) else 0,
                json.dumps(frame_effects) if frame_effects else None,
                purchase_urls.get("cardKingdom", ""),
                purchase_urls.get("cardKingdomFoil", ""),
                imported_at,
            ))
            uuid_map_rows.append((uuid, set_code, number))

        # Sealed products
        for sealed in set_data.get("sealedProduct", []):
            sealed_uuid = sealed.get("uuid")
            if not sealed_uuid:
                continue
            identifiers = sealed.get("identifiers", {})
            purchase_urls = sealed.get("purchaseUrls", {})
            contents = sealed.get("contents")
            sealed_rows.append((
                sealed_uuid,
                sealed.get("name", "Unknown"),
                set_code,
                sealed.get("category", "unknown"),
                sealed.get("subtype"),
                identifiers.get("tcgplayerProductId"),
                sealed.get("cardCount"),
                sealed.get("productSize"),
                sealed.get("releaseDate"),
                purchase_urls.get("tcgplayer"),
                purchase_urls.get("cardKingdom"),
                json.dumps(contents) if contents else None,
                imported_at,
            ))

        # Booster data
        booster = set_data.get("booster")
        if not booster:
            continue

        set_name = set_data.get("name", set_code_raw)
        sets_with_boosters.append((set_code, set_name))

        for product, product_data in booster.items():
            sheets = product_data.get("sheets", {})
            variants = product_data.get("boosters", [])

            # Sheets → mtgjson_booster_sheets
            for sheet_name, sheet in sheets.items():
                is_foil = 1 if sheet.get("foil", False) else 0
                for card_uuid, weight in sheet.get("cards", {}).items():
                    sheet_rows.append((
                        set_code, product, sheet_name, is_foil,
                        card_uuid, int(weight),
                    ))

            # Variants → mtgjson_booster_configs
            for variant_index, variant in enumerate(variants):
                variant_weight = variant.get("weight", 1)
                for sheet_name, card_count in variant.get("contents", {}).items():
                    config_rows.append((
                        set_code, product, variant_index, int(variant_weight),
                        sheet_name, int(card_count),
                    ))

    # Bulk insert
    print(f"  Inserting {len(printing_rows)} printings ...")
    conn.executemany(
        "INSERT OR IGNORE INTO mtgjson_printings "
        "(uuid, scryfall_id, name, set_code, number, rarity, border_color, "
        "is_full_art, frame_effects, ck_url, ck_url_foil, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        printing_rows,
    )

    conn.executemany(
        "INSERT OR IGNORE INTO mtgjson_uuid_map (uuid, set_code, collector_number) VALUES (?, ?, ?)",
        uuid_map_rows,
    )

    # Insert sets with booster data
    for sc, sn in sets_with_boosters:
        conn.execute(
            "INSERT OR IGNORE INTO sets (set_code, set_name) VALUES (?, ?)",
            (sc, sn),
        )

    print(f"  Inserting {len(sheet_rows)} booster sheet rows ...")
    conn.executemany(
        "INSERT INTO mtgjson_booster_sheets "
        "(set_code, product, sheet_name, is_foil, uuid, weight) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        sheet_rows,
    )

    print(f"  Inserting {len(config_rows)} booster config rows ...")
    conn.executemany(
        "INSERT INTO mtgjson_booster_configs "
        "(set_code, product, variant_index, variant_weight, sheet_name, card_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        config_rows,
    )

    print(f"  Inserting {len(sealed_rows)} sealed product rows ...")
    conn.executemany(
        "INSERT OR IGNORE INTO sealed_products "
        "(uuid, name, set_code, category, subtype, tcgplayer_product_id, "
        "card_count, product_size, release_date, purchase_url_tcgplayer, "
        "purchase_url_cardkingdom, contents_json, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        sealed_rows,
    )

    conn.commit()
    conn.close()

    elapsed = time.time() - t0
    print(f"  Sets: {set_count} total, {len(sets_with_boosters)} with boosters")
    print(f"  Printings: {len(printing_rows)}")
    print(f"  Booster sheet rows: {len(sheet_rows)}")
    print(f"  Booster config rows: {len(config_rows)}")
    print(f"  UUID map rows: {len(uuid_map_rows)}")
    print(f"  Sealed products: {len(sealed_rows)}")
    print(f"  Elapsed: {elapsed:.1f}s")


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


TCGCSV_GROUPS_URL = "https://tcgcsv.com/tcgplayer/1/groups"
TCGCSV_PRICES_URL = "https://tcgcsv.com/tcgplayer/1/{group_id}/prices"


def fetch_sealed_prices(db_path: str, set_code: str = None, conn: sqlite3.Connection = None):
    """Fetch sealed product prices from TCGCSV and import into sealed_prices."""
    import datetime

    from mtg_collector.db.schema import init_db

    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

    today = datetime.date.today().isoformat()

    # Step 1: Fetch and cache TCGCSV groups
    print("Fetching TCGCSV groups...")
    req = urllib.request.Request(TCGCSV_GROUPS_URL, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req) as resp:
        groups_data = json.loads(resp.read())

    groups = groups_data.get("results", [])
    print(f"  {len(groups)} groups from TCGCSV")

    # Upsert groups into tcgplayer_groups
    for g in groups:
        abbr = g.get("abbreviation", "")
        mapped_set_code = abbr.lower() if abbr else None
        conn.execute(
            """INSERT OR REPLACE INTO tcgplayer_groups
               (group_id, set_code, name, abbreviation, published_on, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (g["groupId"], mapped_set_code, g["name"], abbr, g.get("publishedOn"), now_iso()),
        )
    conn.commit()

    # Step 2: Find which groups have sealed products we care about
    if set_code:
        sealed_rows = conn.execute(
            "SELECT tcgplayer_product_id, set_code FROM sealed_products WHERE tcgplayer_product_id IS NOT NULL AND set_code = ?",
            (set_code.lower(),),
        ).fetchall()
    else:
        sealed_rows = conn.execute(
            "SELECT tcgplayer_product_id, set_code FROM sealed_products WHERE tcgplayer_product_id IS NOT NULL"
        ).fetchall()

    # Map set_code -> set of tcgplayer_product_ids
    product_ids_by_set = {}
    for row in sealed_rows:
        product_ids_by_set.setdefault(row["set_code"], set()).add(row["tcgplayer_product_id"])

    # Map set_code -> group_id via tcgplayer_groups
    group_rows = conn.execute(
        "SELECT group_id, set_code FROM tcgplayer_groups WHERE set_code IS NOT NULL"
    ).fetchall()
    set_to_group = {r["set_code"]: r["group_id"] for r in group_rows}

    # Build list of (group_id, product_ids) to fetch
    groups_to_fetch = []
    for sc, pids in product_ids_by_set.items():
        gid = set_to_group.get(sc)
        if gid:
            groups_to_fetch.append((gid, pids))

    print(f"  {len(product_ids_by_set)} sets with sealed products, {len(groups_to_fetch)} mapped to TCGCSV groups")

    if not groups_to_fetch:
        print("No groups to fetch prices for.")
        if own_conn:
            conn.close()
        return {"groups_fetched": 0, "prices_for_today": 0}

    # Step 3: Fetch prices for each group
    groups_fetched = 0

    for gid, target_pids in groups_to_fetch:
        url = TCGCSV_PRICES_URL.format(group_id=gid)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req) as resp:
                price_data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"  Group {gid}: HTTP {e.code}, skipping")
            time.sleep(0.1)
            continue

        groups_fetched += 1
        results = price_data.get("results", [])

        for entry in results:
            pid = str(entry.get("productId", ""))
            if pid not in target_pids:
                continue
            if entry.get("subTypeName") != "Normal":
                continue

            conn.execute(
                """INSERT OR IGNORE INTO sealed_prices
                   (tcgplayer_product_id, low_price, mid_price, high_price,
                    market_price, direct_low_price, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (pid, entry.get("lowPrice"), entry.get("midPrice"),
                 entry.get("highPrice"), entry.get("marketPrice"),
                 entry.get("directLowPrice"), today),
            )

        time.sleep(0.1)  # rate limit courtesy

    conn.commit()

    row_count = conn.execute(
        "SELECT COUNT(*) FROM sealed_prices WHERE observed_at = ?", (today,)
    ).fetchone()[0]

    print(f"  Fetched prices from {groups_fetched} groups")
    print(f"  {row_count} sealed product prices for {today}")
    if own_conn:
        conn.close()

    return {"groups_fetched": groups_fetched, "prices_for_today": row_count}
