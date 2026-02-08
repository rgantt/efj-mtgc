"""Crack-a-pack web server: mtg crack-pack-server --port 8080"""

import json
import sys
import threading
import time
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

from mtg_collector.services.pack_generator import PackGenerator

# In-memory price cache: scryfall_id -> (timestamp, prices_dict)
_price_cache: dict[str, tuple[float, dict]] = {}
_PRICE_TTL = 86400  # 24 hours


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
            self._serve_html()
        elif path == "/api/sets":
            self._api_sets()
        elif path == "/api/products":
            params = parse_qs(parsed.query)
            set_code = params.get("set", [""])[0]
            self._api_products(set_code)
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
        else:
            self._send_json({"error": "Not found"}, 404)

    def _serve_html(self):
        html_path = self.static_dir / "crack_pack.html"
        content = html_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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

        # Attach prices from Scryfall
        scryfall_ids = [c["scryfall_id"] for c in result["cards"] if c.get("scryfall_id")]
        prices = _fetch_prices(scryfall_ids)
        for card in result["cards"]:
            card_prices = prices.get(card.get("scryfall_id"), {})
            if card.get("foil"):
                card["price"] = card_prices.get("usd_foil") or card_prices.get("usd")
            else:
                card["price"] = card_prices.get("usd") or card_prices.get("usd_foil")

        self._send_json(result)

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

    # Pre-warm AllPrintings.json in background thread
    warm_thread = threading.Thread(target=lambda: gen.data, daemon=True)
    warm_thread.start()

    static_dir = Path(__file__).resolve().parent.parent / "static"
    handler = partial(CrackPackHandler, gen, static_dir)

    server = HTTPServer(("", args.port), handler)
    print(f"Crack-a-Pack server running at http://localhost:{args.port}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
