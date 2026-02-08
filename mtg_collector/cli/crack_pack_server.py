"""Crack-a-pack web server: mtg crack-pack-server --port 8080"""

import gzip
import json
import shutil
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


def _get_ck_price(uuid: str, foil: bool) -> str | None:
    """Look up CardKingdom retail price for a card UUID."""
    with _prices_lock:
        data = _prices_data
    if data is None:
        return None
    card_prices = data.get(uuid)
    if not card_prices:
        return None
    paper = card_prices.get("paper", {})
    ck = paper.get("cardkingdom", {})
    retail = ck.get("retail", {})
    price_type = "foil" if foil else "normal"
    prices_by_date = retail.get(price_type, {})
    if not prices_by_date:
        return None
    # Get the most recent date's price
    latest_date = max(prices_by_date.keys())
    return str(prices_by_date[latest_date])


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

    if to_fetch:
        resp = requests.post(
            "https://api.scryfall.com/cards/collection",
            json={"identifiers": [{"id": sid} for sid in to_fetch]},
            headers={"User-Agent": "MTGCollectionTool/2.0"},
        )
        resp.raise_for_status()
        for card in resp.json().get("data", []):
            prices = card.get("prices", {})
            _price_cache[card["id"]] = (now, prices)
            result[card["id"]] = prices

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

    def __init__(self, generator: PackGenerator, static_dir: Path, *args, **kwargs):
        self.generator = generator
        self.static_dir = static_dir
        super().__init__(*args, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_homepage()
        elif path == "/crack":
            self._serve_static("crack_pack.html")
        elif path == "/api/sets":
            self._api_sets()
        elif path == "/api/products":
            params = parse_qs(parsed.query)
            set_code = params.get("set", [""])[0]
            self._api_products(set_code)
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
    parser.set_defaults(func=run)


def run(args):
    """Run the crack-pack-server command."""
    mtgjson_path = Path(args.mtgjson) if args.mtgjson else None

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
    handler = partial(CrackPackHandler, gen, static_dir)

    server = HTTPServer(("", args.port), handler)
    print(f"Server running at http://localhost:{args.port}")
    print(f"Crack-a-Pack: http://localhost:{args.port}/crack")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
