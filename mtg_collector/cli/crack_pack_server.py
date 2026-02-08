"""Crack-a-pack web server: mtg crack-pack-server --port 8080"""

import gzip
import json
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

from mtg_collector.cli.data_cmd import MTGJSON_PRICES_URL, get_allpricestoday_path, _download
from mtg_collector.db.connection import get_db_path
from mtg_collector.services.pack_generator import PackGenerator

# In-memory price cache: scryfall_id -> (timestamp, prices_dict)
_price_cache: dict[str, tuple[float, dict]] = {}
_PRICE_TTL = 86400  # 24 hours

# CK prices from AllPricesToday.json
_prices_data: dict | None = None
_prices_lock = threading.Lock()


def _load_prices():
    """Load AllPricesToday.json into memory."""
    global _prices_data
    path = get_allpricestoday_path()
    with open(path) as f:
        raw = json.load(f)
    with _prices_lock:
        _prices_data = raw.get("data", {})


def _get_local_price(uuid: str, foil: bool, provider: str) -> str | None:
    """Look up a retail price for a card UUID from AllPricesToday.json."""
    with _prices_lock:
        data = _prices_data
    if data is None:
        return None
    card_prices = data.get(uuid)
    if not card_prices:
        return None
    paper = card_prices.get("paper", {})
    prov = paper.get(provider, {})
    retail = prov.get("retail", {})
    price_type = "foil" if foil else "normal"
    prices_by_date = retail.get(price_type, {})
    if not prices_by_date:
        return None
    latest_date = max(prices_by_date.keys())
    return str(prices_by_date[latest_date])


def _get_ck_price(uuid: str, foil: bool) -> str | None:
    return _get_local_price(uuid, foil, "cardkingdom")


def _get_tcg_price(uuid: str, foil: bool) -> str | None:
    return _get_local_price(uuid, foil, "tcgplayer")


def _fetch_prices(scryfall_ids: list[str]) -> dict[str, dict]:
    """Fetch prices from Scryfall collection endpoint, using cache."""
    now = time.time()
    result = {}
    to_fetch = []

    for sid in scryfall_ids:
        if not sid:
            continue
        cached = _price_cache.get(sid)
        if cached and now - cached[0] < _PRICE_TTL:
            result[sid] = cached[1]
        else:
            to_fetch.append(sid)

    # Scryfall collection endpoint accepts max 75 identifiers per request
    for i in range(0, len(to_fetch), 75):
        batch = to_fetch[i:i + 75]
        resp = requests.post(
            "https://api.scryfall.com/cards/collection",
            json={"identifiers": [{"id": sid} for sid in batch]},
            headers={"User-Agent": "MTGCollectionTool/2.0"},
        )
        resp.raise_for_status()
        for card in resp.json().get("data", []):
            prices = card.get("prices", {})
            _price_cache[card["id"]] = (now, prices)
            result[card["id"]] = prices
        if i + 75 < len(to_fetch):
            time.sleep(0.1)  # rate limit

    return result


def _download_prices():
    """Download AllPricesToday.json.gz and decompress it."""
    dest = get_allpricestoday_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    gz_path = dest.parent / "AllPricesToday.json.gz"

    _download(MTGJSON_PRICES_URL, gz_path)

    with gzip.open(gz_path, "rb") as f_in:
        with open(dest, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    gz_path.unlink()
    _load_prices()


class CrackPackHandler(BaseHTTPRequestHandler):
    """HTTP handler for crack-a-pack web UI."""

    def __init__(self, generator: PackGenerator, static_dir: Path, db_path: str, *args, **kwargs):
        self.generator = generator
        self.static_dir = static_dir
        self.db_path = db_path
        super().__init__(*args, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_homepage()
        elif path == "/crack":
            self._serve_static("crack_pack.html")
        elif path == "/sheets":
            self._serve_static("explore_sheets.html")
        elif path == "/collection":
            self._serve_static("collection.html")
        elif path == "/api/sets":
            self._api_sets()
        elif path == "/api/products":
            params = parse_qs(parsed.query)
            set_code = params.get("set", [""])[0]
            self._api_products(set_code)
        elif path == "/api/sheets":
            params = parse_qs(parsed.query)
            set_code = params.get("set", [""])[0]
            product = params.get("product", [""])[0]
            self._api_sheets(set_code, product)
        elif path == "/api/collection":
            params = parse_qs(parsed.query)
            self._api_collection(params)
        elif path == "/api/prices-status":
            self._api_prices_status()
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/generate":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            self._api_generate(data)
        elif parsed.path == "/api/fetch-prices":
            self._api_fetch_prices()
        else:
            self._send_json({"error": "Not found"}, 404)

    _CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
    }

    def _serve_homepage(self):
        self._serve_static("index.html")

    def _serve_static(self, filename: str):
        filepath = self.static_dir / filename
        if not filepath.resolve().is_relative_to(self.static_dir.resolve()):
            self._send_json({"error": "Not found"}, 404)
            return
        if not filepath.is_file():
            self._send_json({"error": "Not found"}, 404)
            return
        content = filepath.read_bytes()
        content_type = self._CONTENT_TYPES.get(filepath.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _api_sets(self):
        sets = self.generator.list_sets()
        self._send_json([{"code": code, "name": name} for code, name in sets])

    def _api_products(self, set_code: str):
        if not set_code:
            self._send_json({"error": "Missing 'set' parameter"}, 400)
            return
        products = self.generator.list_products(set_code)
        self._send_json(products)

    def _api_sheets(self, set_code: str, product: str):
        if not set_code or not product:
            self._send_json({"error": "Missing 'set' or 'product' parameter"}, 400)
            return
        result = self.generator.get_sheet_data(set_code, product)

        # Attach local prices
        for sheet in result["sheets"].values():
            for card in sheet["cards"]:
                uuid = card.get("uuid", "")
                foil = card.get("foil", False)
                card["ck_price"] = _get_ck_price(uuid, foil)
                card["tcg_price"] = _get_tcg_price(uuid, foil)

        self._send_json(result)

    def _api_generate(self, data: dict):
        set_code = data.get("set_code", "")
        product = data.get("product", "")
        if not set_code or not product:
            self._send_json({"error": "Missing set_code or product"}, 400)
            return
        seed = data.get("seed")
        if seed is not None:
            seed = int(seed)
        result = self.generator.generate_pack(set_code, product, seed=seed)

        # Attach TCG prices from Scryfall
        scryfall_ids = [c["scryfall_id"] for c in result["cards"] if c.get("scryfall_id")]
        prices = _fetch_prices(scryfall_ids)
        for card in result["cards"]:
            card_prices = prices.get(card.get("scryfall_id"), {})
            if card.get("foil"):
                card["tcg_price"] = card_prices.get("usd_foil") or card_prices.get("usd")
            else:
                card["tcg_price"] = card_prices.get("usd") or card_prices.get("usd_foil")

            # Attach CK price from AllPricesToday
            card["ck_price"] = _get_ck_price(card.get("uuid", ""), card.get("foil", False))

        self._send_json(result)

    def _api_collection(self, params: dict):
        """Return aggregated collection data with optional search/sort/filter."""
        q = params.get("q", [""])[0]
        sort = params.get("sort", ["name"])[0]
        order = params.get("order", ["asc"])[0]
        filter_colors = params.get("filter_color", [])
        filter_rarities = params.get("filter_rarity", [])
        filter_sets = params.get("filter_set", [])
        filter_type = params.get("filter_type", [""])[0]
        filter_finish = params.get("filter_finish", [])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        where_clauses = []
        sql_params = []

        if q:
            where_clauses.append("card.name LIKE ?")
            sql_params.append(f"%{q}%")

        if filter_colors:
            color_conditions = []
            for color in filter_colors:
                if color == "C":
                    color_conditions.append("(card.colors IS NULL OR card.colors = '[]')")
                else:
                    color_conditions.append("card.colors LIKE ?")
                    sql_params.append(f'%"{color}"%')
            where_clauses.append(f"({' OR '.join(color_conditions)})")

        if filter_rarities:
            placeholders = ",".join("?" * len(filter_rarities))
            where_clauses.append(f"p.rarity IN ({placeholders})")
            sql_params.extend(filter_rarities)

        if filter_sets:
            placeholders = ",".join("?" * len(filter_sets))
            where_clauses.append(f"p.set_code IN ({placeholders})")
            sql_params.extend(filter_sets)

        if filter_type:
            where_clauses.append("card.type_line LIKE ?")
            sql_params.append(f"%{filter_type}%")

        if filter_finish:
            placeholders = ",".join("?" * len(filter_finish))
            where_clauses.append(f"c.finish IN ({placeholders})")
            sql_params.extend(filter_finish)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # Map sort param to SQL column
        sort_map = {
            "name": "card.name",
            "cmc": "card.cmc",
            "rarity": "CASE p.rarity WHEN 'common' THEN 0 WHEN 'uncommon' THEN 1 WHEN 'rare' THEN 2 WHEN 'mythic' THEN 3 ELSE 4 END",
            "set": "p.set_code",
            "color": "card.color_identity",
            "qty": "qty",
            "price": "0",  # sorted client-side since prices are attached after
            "collector_number": "CAST(p.collector_number AS INTEGER)",
        }
        sort_col = sort_map.get(sort, "card.name")
        order_dir = "DESC" if order == "desc" else "ASC"

        query = f"""
            SELECT
                card.name, card.type_line, card.mana_cost, card.cmc,
                card.colors, card.color_identity,
                p.set_code, s.set_name, p.collector_number, p.rarity,
                p.scryfall_id, p.image_uri, p.artist,
                p.frame_effects, p.border_color, p.full_art, p.promo,
                p.promo_types, p.finishes,
                c.finish, c.condition,
                COUNT(*) as qty
            FROM collection c
            JOIN printings p ON c.scryfall_id = p.scryfall_id
            JOIN cards card ON p.oracle_id = card.oracle_id
            JOIN sets s ON p.set_code = s.set_code
            WHERE {where_sql}
            GROUP BY p.scryfall_id, c.finish, c.condition
            ORDER BY {sort_col} {order_dir}, card.name ASC
        """

        cursor = conn.execute(query, sql_params)
        rows = cursor.fetchall()

        results = []
        for row in rows:
            card = {
                "name": row["name"],
                "type_line": row["type_line"],
                "mana_cost": row["mana_cost"],
                "cmc": row["cmc"],
                "colors": row["colors"],
                "color_identity": row["color_identity"],
                "set_code": row["set_code"],
                "set_name": row["set_name"],
                "collector_number": row["collector_number"],
                "rarity": row["rarity"],
                "scryfall_id": row["scryfall_id"],
                "image_uri": row["image_uri"],
                "artist": row["artist"],
                "frame_effects": row["frame_effects"],
                "border_color": row["border_color"],
                "full_art": bool(row["full_art"]),
                "promo": bool(row["promo"]),
                "promo_types": row["promo_types"],
                "finishes": row["finishes"],
                "finish": row["finish"],
                "condition": row["condition"],
                "qty": row["qty"],
            }
            # Attach prices from AllPricesToday
            foil = card["finish"] in ("foil", "etched")
            # We don't have uuid here, but we can look up by scryfall_id in the MTGJSON data
            # For now, attach Scryfall-based prices
            card["tcg_price"] = None
            card["ck_price"] = None
            card["ck_url"] = None
            results.append(card)

        # Batch fetch Scryfall prices
        scryfall_ids = list({r["scryfall_id"] for r in results})
        prices = _fetch_prices(scryfall_ids)
        for card in results:
            card_prices = prices.get(card["scryfall_id"], {})
            foil = card["finish"] in ("foil", "etched")
            if foil:
                card["tcg_price"] = card_prices.get("usd_foil") or card_prices.get("usd")
            else:
                card["tcg_price"] = card_prices.get("usd") or card_prices.get("usd_foil")

        conn.close()
        self._send_json(results)

    def _api_prices_status(self):
        path = get_allpricestoday_path()
        if path.exists():
            mtime = path.stat().st_mtime
            last_modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            self._send_json({"available": True, "last_modified": last_modified})
        else:
            self._send_json({"available": False, "last_modified": None})

    def _api_fetch_prices(self):
        try:
            _download_prices()
            path = get_allpricestoday_path()
            mtime = path.stat().st_mtime
            last_modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            self._send_json({"available": True, "last_modified": last_modified})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Quieter logging — just method and path
        sys.stderr.write(f"{args[0]}\n")


def register(subparsers):
    """Register the crack-pack-server subcommand."""
    parser = subparsers.add_parser(
        "crack-pack-server",
        help="Start the crack-a-pack web UI",
        description="Start a local web server for the crack-a-pack visual UI.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to serve on (default: 8080)",
    )
    parser.add_argument(
        "--mtgjson",
        default=None,
        help="Path to AllPrintings.json (default: ~/.mtgc/AllPrintings.json)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to collection SQLite database (default: ~/.mtgc/collection.sqlite)",
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the crack-pack-server command."""
    mtgjson_path = Path(args.mtgjson) if args.mtgjson else None
    db_path = get_db_path(getattr(args, "db", None))

    gen = PackGenerator(mtgjson_path)

    # Verify required data files exist
    allprintings = gen.mtgjson_path
    if not allprintings.exists():
        print(f"Error: AllPrintings.json not found: {allprintings}", file=sys.stderr)
        print("Run: mtg data fetch", file=sys.stderr)
        sys.exit(1)

    prices_path = get_allpricestoday_path()
    if not prices_path.exists():
        print(f"Error: AllPricesToday.json not found: {prices_path}", file=sys.stderr)
        print("Run: mtg data fetch-prices", file=sys.stderr)
        sys.exit(1)

    # Pre-warm AllPrintings.json in background thread
    warm_thread = threading.Thread(target=lambda: gen.data, daemon=True)
    warm_thread.start()

    # Load CK prices in background thread
    prices_thread = threading.Thread(target=_load_prices, daemon=True)
    prices_thread.start()

    static_dir = Path(__file__).resolve().parent.parent / "static"
    handler = partial(CrackPackHandler, gen, static_dir, db_path)

    server = HTTPServer(("", args.port), handler)
    print(f"Server running at http://localhost:{args.port}")
    print(f"Crack-a-Pack: http://localhost:{args.port}/crack")
    print(f"Explore Sheets: http://localhost:{args.port}/sheets")
    print(f"Collection: http://localhost:{args.port}/collection")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
