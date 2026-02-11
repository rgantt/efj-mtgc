"""Crack-a-pack web server: mtg crack-pack-server --port 8080"""

import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid as uuid_mod
from datetime import datetime, timezone
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
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


# ── Ingest session state ──
_ingest_sessions: dict = {}
_ingest_lock = threading.Lock()

_INGEST_IMAGES_DIR = None  # Set in run()


def _get_ingest_images_dir() -> Path:
    global _INGEST_IMAGES_DIR
    if _INGEST_IMAGES_DIR is None:
        from mtg_collector.utils import get_mtgc_home
        _INGEST_IMAGES_DIR = get_mtgc_home() / "ingest_images"
    _INGEST_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return _INGEST_IMAGES_DIR


def _md5_file(filepath: str) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_card_crop(fragments, indices, image_w=None, image_h=None):
    """Compute union bounding box of fragment indices with 10% buffer, constrained to 63:88."""
    if not indices:
        return None
    xs, ys, xws, yhs = [], [], [], []
    for i in indices:
        if i < len(fragments):
            b = fragments[i]["bbox"]
            xs.append(b["x"])
            ys.append(b["y"])
            xws.append(b["x"] + b["w"])
            yhs.append(b["y"] + b["h"])
    if not xs:
        return None
    x1, y1 = min(xs), min(ys)
    x2, y2 = max(xws), max(yhs)
    w, h = x2 - x1, y2 - y1
    # Add 10% buffer
    bx, by = w * 0.1, h * 0.1
    x1 -= bx
    y1 -= by
    w += 2 * bx
    h += 2 * by
    # Constrain to 63:88 aspect ratio
    target_ratio = 63 / 88
    current_ratio = w / h if h > 0 else target_ratio
    if current_ratio > target_ratio:
        # Too wide, increase height
        new_h = w / target_ratio
        y1 -= (new_h - h) / 2
        h = new_h
    else:
        # Too tall, increase width
        new_w = h * target_ratio
        x1 -= (new_w - w) / 2
        w = new_w
    # Clamp to image bounds
    if image_w and image_h:
        x1 = max(0, x1)
        y1 = max(0, y1)
        if x1 + w > image_w:
            w = image_w - x1
        if y1 + h > image_h:
            h = image_h - y1
    return {"x": round(x1), "y": round(y1), "w": round(w), "h": round(h)}


def _compute_name_bbox(fragments, indices, image_w=None, image_h=None):
    """Compute union bounding box of fragment indices with 10% buffer, no aspect-ratio constraint."""
    if not indices:
        return None
    xs, ys, xws, yhs = [], [], [], []
    for i in indices:
        if i < len(fragments):
            b = fragments[i]["bbox"]
            xs.append(b["x"])
            ys.append(b["y"])
            xws.append(b["x"] + b["w"])
            yhs.append(b["y"] + b["h"])
    if not xs:
        return None
    x1, y1 = min(xs), min(ys)
    x2, y2 = max(xws), max(yhs)
    w, h = x2 - x1, y2 - y1
    # Add 10% buffer
    bx, by = w * 0.1, h * 0.1
    x1 -= bx
    y1 -= by
    w += 2 * bx
    h += 2 * by
    # Clamp to image bounds
    if image_w and image_h:
        x1 = max(0, x1)
        y1 = max(0, y1)
        if x1 + w > image_w:
            w = image_w - x1
        if y1 + h > image_h:
            h = image_h - y1
    return {"x": round(x1), "y": round(y1), "w": round(w), "h": round(h)}


def _merge_nearby_fragments(fragments, gap_threshold=2.0):
    """Merge OCR fragments whose bounding boxes are within gap_threshold pixels of each other.

    Uses union-find to group fragments, then merges each group into a single
    fragment with combined text (left-to-right) and union bounding box.
    """
    n = len(fragments)
    if n == 0:
        return fragments

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a

    # Check all pairs for proximity
    for i in range(n):
        bi = fragments[i]["bbox"]
        i_x1, i_y1 = bi["x"], bi["y"]
        i_x2, i_y2 = i_x1 + bi["w"], i_y1 + bi["h"]
        for j in range(i + 1, n):
            bj = fragments[j]["bbox"]
            j_x1, j_y1 = bj["x"], bj["y"]
            j_x2, j_y2 = j_x1 + bj["w"], j_y1 + bj["h"]

            # Gap = distance between nearest edges; negative means overlap
            gap_x = max(i_x1 - j_x2, j_x1 - i_x2, 0)
            gap_y = max(i_y1 - j_y2, j_y1 - i_y2, 0)

            if gap_x <= gap_threshold and gap_y <= gap_threshold:
                union(i, j)

    # Group by root
    groups: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, []).append(i)

    merged = []
    for indices in groups.values():
        # Sort left-to-right by x position so text reads naturally
        indices.sort(key=lambda i: fragments[i]["bbox"]["x"])
        text = " ".join(fragments[i]["text"] for i in indices)
        confidence = min(fragments[i]["confidence"] for i in indices)

        xs = [fragments[i]["bbox"]["x"] for i in indices]
        ys = [fragments[i]["bbox"]["y"] for i in indices]
        x2s = [fragments[i]["bbox"]["x"] + fragments[i]["bbox"]["w"] for i in indices]
        y2s = [fragments[i]["bbox"]["y"] + fragments[i]["bbox"]["h"] for i in indices]

        merged.append({
            "text": text,
            "bbox": {
                "x": min(xs),
                "y": min(ys),
                "w": max(x2s) - min(xs),
                "h": max(y2s) - min(ys),
            },
            "confidence": round(confidence, 3),
        })

    # Sort top-to-bottom, left-to-right
    merged.sort(key=lambda f: (f["bbox"]["y"], f["bbox"]["x"]))
    return merged


def _format_candidates(raw_cards):
    """Format raw Scryfall card dicts into the candidate shape the client expects."""
    formatted = []
    for c in raw_cards:
        image_uri = None
        if "image_uris" in c:
            image_uri = c["image_uris"].get("small") or c["image_uris"].get("normal")
        elif "card_faces" in c and c["card_faces"]:
            face = c["card_faces"][0]
            if "image_uris" in face:
                image_uri = face["image_uris"].get("small") or face["image_uris"].get("normal")

        prices = c.get("prices", {})
        price = prices.get("usd") or prices.get("usd_foil")

        formatted.append({
            "scryfall_id": c["id"],
            "name": c.get("name", "???"),
            "set_code": c.get("set", "???"),
            "set_name": c.get("set_name", ""),
            "collector_number": c.get("collector_number", "???"),
            "rarity": c.get("rarity", "unknown"),
            "image_uri": image_uri,
            "foil": "foil" in c.get("finishes", []),
            "finishes": c.get("finishes", []),
            "promo": c.get("promo", False),
            "full_art": c.get("full_art", False),
            "border_color": c.get("border_color", ""),
            "frame_effects": c.get("frame_effects", []),
            "price": price,
        })
    return formatted


def _log_ingest(msg):
    sys.stderr.write(f"[INGEST] {msg}\n")
    sys.stderr.flush()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


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
        params = parse_qs(parsed.query)

        if path == "/":
            self._serve_homepage()
        elif path == "/crack":
            self._serve_static("crack_pack.html")
        elif path == "/sheets":
            self._serve_static("explore_sheets.html")
        elif path == "/collection":
            self._serve_static("collection.html")
        elif path == "/ingestor-ocr":
            self._serve_static("ingest.html")
        elif path == "/api/sets":
            self._api_sets()
        elif path == "/api/products":
            set_code = params.get("set", [""])[0]
            self._api_products(set_code)
        elif path == "/api/sheets":
            set_code = params.get("set", [""])[0]
            product = params.get("product", [""])[0]
            self._api_sheets(set_code, product)
        elif path == "/api/collection":
            self._api_collection(params)
        elif path == "/api/settings":
            self._api_get_settings()
        elif path == "/api/prices-status":
            self._api_prices_status()
        # Ingest SSE endpoint: /api/ingest/process/{session_id}/{image_idx}
        elif path.startswith("/api/ingest/process/"):
            parts = path.split("/")
            if len(parts) == 6:
                sid, img_idx = parts[4], parts[5]
                force = params.get("force", ["0"])[0] == "1"
                self._api_ingest_process_sse(sid, int(img_idx), force)
            else:
                self._send_json({"error": "Invalid path"}, 400)
        elif path.startswith("/api/ingest/image/"):
            filename = path[len("/api/ingest/image/"):]
            self._api_ingest_serve_image(filename)
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/generate":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            self._api_generate(data)
        elif path == "/api/fetch-prices":
            self._api_fetch_prices()
        elif path == "/api/ingest/session":
            self._api_ingest_create_session()
        elif path == "/api/ingest/upload":
            self._api_ingest_upload()
        elif path == "/api/ingest/set-count":
            self._api_ingest_set_count()
        elif path == "/api/ingest/confirm":
            self._api_ingest_confirm()
        elif path == "/api/ingest/skip":
            self._api_ingest_skip()
        elif path == "/api/ingest/next-card":
            self._api_ingest_next_card()
        elif path == "/api/ingest/search-card":
            self._api_ingest_search_card()
        elif path == "/api/ingest/submit-clicked-names":
            self._api_ingest_submit_clicked_names()
        elif path == "/api/ingest/next-name":
            self._api_ingest_next_name()
        elif path == "/api/ingest/confirm-name":
            self._api_ingest_confirm_name()
        elif path == "/api/ingest/skip-name":
            self._api_ingest_skip_name()
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/settings":
            self._api_put_settings()
        else:
            self._send_json({"error": "Not found"}, 404)

    _CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
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
        filter_types = params.get("filter_type[]", [])
        filter_subtypes = params.get("filter_subtype[]", [])
        filter_finish = params.get("filter_finish", [])
        filter_badges = params.get("filter_badge[]", [])
        filter_cmc_min = params.get("filter_cmc_min", [""])[0]
        filter_cmc_max = params.get("filter_cmc_max", [""])[0]

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

        if filter_types:
            type_conditions = []
            for t in filter_types:
                type_conditions.append("card.type_line LIKE ?")
                sql_params.append(f"%{t}%")
            where_clauses.append(f"({' OR '.join(type_conditions)})")

        if filter_subtypes:
            subtype_conditions = []
            for st in filter_subtypes:
                subtype_conditions.append("card.type_line LIKE ?")
                sql_params.append(f"%{st}%")
            where_clauses.append(f"({' OR '.join(subtype_conditions)})")

        if filter_finish:
            placeholders = ",".join("?" * len(filter_finish))
            where_clauses.append(f"c.finish IN ({placeholders})")
            sql_params.extend(filter_finish)

        if filter_badges:
            badge_conditions = []
            for badge in filter_badges:
                if badge == "borderless":
                    badge_conditions.append("p.border_color = 'borderless'")
                elif badge == "showcase":
                    badge_conditions.append("p.frame_effects LIKE '%showcase%'")
                elif badge == "extendedart":
                    badge_conditions.append("p.frame_effects LIKE '%extendedart%'")
                elif badge == "fullart":
                    badge_conditions.append("p.full_art = 1")
                elif badge == "promo":
                    badge_conditions.append("p.promo = 1")
            if badge_conditions:
                where_clauses.append(f"({' OR '.join(badge_conditions)})")

        if filter_cmc_min:
            where_clauses.append("card.cmc >= ?")
            sql_params.append(float(filter_cmc_min))

        if filter_cmc_max:
            where_clauses.append("card.cmc <= ?")
            sql_params.append(float(filter_cmc_max))

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # Map sort param to SQL column
        sort_map = {
            "name": "card.name",
            "cmc": "card.cmc",
            "rarity": "CASE p.rarity WHEN 'common' THEN 0 WHEN 'uncommon' THEN 1 WHEN 'rare' THEN 2 WHEN 'mythic' THEN 3 ELSE 4 END",
            "set": "p.set_code",
            "color": "card.color_identity",
            "qty": "qty",
            "price": "card.name",  # sorted client-side since prices are attached after
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
            card["tcg_price"] = None
            card["ck_price"] = None
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

            # CK price and URL via MTGJSON uuid lookup
            uuid = self.generator.get_uuid_for_scryfall_id(card["scryfall_id"])
            if uuid:
                card["ck_price"] = _get_ck_price(uuid, foil)
            card["ck_url"] = self.generator.get_ck_url(card["scryfall_id"], foil)

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

    def _api_get_settings(self):
        from mtg_collector.db.schema import init_db
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        conn.close()
        self._send_json({row["key"]: row["value"] for row in rows})

    def _api_put_settings(self):
        from mtg_collector.db.schema import init_db
        data = self._read_json_body()
        if data is None:
            return
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        for key, value in data.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (str(key), str(value)),
            )
        conn.commit()
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        conn.close()
        self._send_json({row["key"]: row["value"] for row in rows})

    def _api_fetch_prices(self):
        try:
            _download_prices()
            path = get_allpricestoday_path()
            mtime = path.stat().st_mtime
            last_modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            self._send_json({"available": True, "last_modified": last_modified})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # ── Ingest API endpoints ──

    def _api_ingest_create_session(self):
        sid = uuid_mod.uuid4().hex[:12]
        with _ingest_lock:
            _ingest_sessions[sid] = {"images": []}
        _log_ingest(f"Session created: {sid}")
        self._send_json({"session_id": sid})

    def _api_ingest_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        # Parse multipart form data
        boundary = content_type.split("boundary=")[1].strip()
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Simple multipart parser
        uploaded = []
        session_id = None
        parts = body.split(f"--{boundary}".encode())

        for part in parts:
            if not part or part.strip() == b"--" or part.strip() == b"":
                continue

            # Split headers from content
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            header_bytes = part[:header_end]
            file_content = part[header_end + 4:]
            # Remove trailing \r\n
            if file_content.endswith(b"\r\n"):
                file_content = file_content[:-2]

            header_str = header_bytes.decode("utf-8", errors="replace")

            # Check if it's session_id field
            if 'name="session_id"' in header_str:
                session_id = file_content.decode("utf-8").strip()
                continue

            # Check if it's a file
            name_match = re.search(r'name="([^"]+)"', header_str)
            filename_match = re.search(r'filename="([^"]+)"', header_str)
            if not filename_match:
                continue

            original_name = filename_match.group(1)
            ext = Path(original_name).suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                continue

            # Save file
            stored_name = f"{uuid_mod.uuid4().hex[:12]}{ext}"
            dest = _get_ingest_images_dir() / stored_name
            dest.write_bytes(file_content)

            md5 = _md5_file(str(dest))
            file_size = len(file_content)

            with _ingest_lock:
                session = _ingest_sessions.get(session_id)
                if session is None:
                    self._send_json({"error": "Invalid session"}, 400)
                    return
                idx = len(session["images"])
                session["images"].append({
                    "filename": original_name,
                    "stored_name": stored_name,
                    "card_count": 1,
                    "md5": md5,
                    "force_ingest": False,
                    "ocr_result": None,
                    "claude_result": None,
                    "scryfall_matches": None,
                    "crops": None,
                    "disambiguated": None,
                })

            _log_ingest(f"Upload: {original_name} -> {stored_name} ({file_size} bytes, MD5={md5})")
            uploaded.append({
                "filename": original_name,
                "stored_name": stored_name,
                "index": idx,
                "md5": md5,
            })

        self._send_json({"uploaded": uploaded})

    def _api_ingest_set_count(self):
        data = self._read_json_body()
        if data is None:
            return
        sid = data.get("session_id")
        idx = data.get("image_idx")
        count = data.get("card_count", 1)
        force = data.get("force_ingest", False)
        mode = data.get("mode")
        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if session and 0 <= idx < len(session["images"]):
                session["images"][idx]["card_count"] = int(count)
                session["images"][idx]["force_ingest"] = bool(force)
                if mode:
                    session["images"][idx]["mode"] = mode
        self._send_json({"ok": True})

    def _api_ingest_serve_image(self, filename):
        # Sanitize filename
        if "/" in filename or "\\" in filename or ".." in filename:
            self._send_json({"error": "Invalid filename"}, 400)
            return
        filepath = _get_ingest_images_dir() / filename
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

    def _api_ingest_process_sse(self, sid, img_idx, force):
        """SSE endpoint: process one image through OCR -> Claude -> Scryfall."""
        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if not session or img_idx >= len(session["images"]):
                self._send_json({"error": "Invalid session or image"}, 400)
                return
            img = session["images"][img_idx]

        # Set up SSE response
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send_event(event_type, data):
            payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            try:
                self.wfile.write(payload.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        try:
            mode = img.get("mode", "full")
            if mode == "click_names":
                self._process_image_click_names_sse(sid, img_idx, img, force, send_event)
            elif mode == "names":
                self._process_image_names_sse(sid, img_idx, img, force, send_event)
            else:
                self._process_image_sse(sid, img_idx, img, force, send_event)
        except Exception as e:
            _log_ingest(f"Error processing image {img_idx}: {e}")
            send_event("error", {"message": str(e)})

        send_event("done", {})

    def _process_image_sse(self, sid, img_idx, img, force, send_event):
        """Process a single image: OCR -> Claude -> Scryfall, streaming SSE events."""
        from mtg_collector.services.ocr import run_ocr_with_boxes
        from mtg_collector.services.claude import ClaudeVision
        from mtg_collector.services.scryfall import ScryfallAPI, cache_scryfall_data
        from mtg_collector.cli.ingest_ocr import _build_scryfall_query
        from mtg_collector.db.schema import init_db
        from mtg_collector.utils import now_iso

        image_path = str(_get_ingest_images_dir() / img["stored_name"])
        md5 = img["md5"]
        card_count = img["card_count"]

        _log_ingest(f"Processing image {img_idx}: {img['filename']} (MD5={md5}, count={card_count}, force={force})")

        # Check cache
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)

        ocr_fragments = None
        claude_cards = None

        if not force:
            cache_row = conn.execute(
                "SELECT ocr_result, claude_result FROM ingest_cache WHERE image_md5 = ?",
                (md5,),
            ).fetchone()
            if cache_row:
                _log_ingest(f"Cache hit for MD5={md5}")
                ocr_fragments = json.loads(cache_row["ocr_result"])
                send_event("cached", {"step": "ocr"})
                send_event("ocr_complete", {"fragment_count": len(ocr_fragments), "fragments": ocr_fragments})

                if cache_row["claude_result"]:
                    claude_cards = json.loads(cache_row["claude_result"])
                    send_event("cached", {"step": "claude"})
                    send_event("claude_complete", {"card_count": len(claude_cards), "cards": claude_cards})

        # Step 1: OCR
        if ocr_fragments is None:
            send_event("status", {"message": "Running OCR..."})
            t0 = time.time()
            raw_fragments = run_ocr_with_boxes(image_path)
            elapsed = time.time() - t0
            _log_ingest(f"OCR complete: {len(raw_fragments)} fragments in {elapsed:.1f}s")
            ocr_fragments = _merge_nearby_fragments(raw_fragments)
            _log_ingest(f"Merged {len(raw_fragments)} -> {len(ocr_fragments)} fragments")
            send_event("ocr_complete", {"fragment_count": len(ocr_fragments), "fragments": ocr_fragments})

        # Step 2: Claude extraction
        if claude_cards is None:
            send_event("status", {"message": "Calling Claude..."})
            t0 = time.time()
            claude = ClaudeVision()
            claude_cards, usage = claude.extract_cards_from_ocr_with_positions(
                ocr_fragments, card_count,
                status_callback=lambda msg: send_event("status", {"message": msg}),
            )
            elapsed = time.time() - t0
            token_info = {}
            if usage:
                token_info = {"in": usage.input_tokens, "out": usage.output_tokens}
            _log_ingest(f"Claude complete: {len(claude_cards)} cards in {elapsed:.1f}s, tokens={token_info}")
            send_event("claude_complete", {
                "card_count": len(claude_cards),
                "cards": claude_cards,
                "tokens": token_info,
            })

            # Validate card count matches user expectation
            if len(claude_cards) != card_count:
                msg = (f"Expected {card_count} card(s) but Claude found {len(claude_cards)}. "
                       f"Discard this image and take a better photo.")
                _log_ingest(f"Card count mismatch: {msg}")
                send_event("count_mismatch", {"message": msg, "expected": card_count, "found": len(claude_cards)})
                send_event("done", {})
                return

        # Save to cache
        conn.execute(
            """INSERT OR REPLACE INTO ingest_cache
               (image_md5, image_path, card_count, ocr_result, claude_result, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (md5, image_path, card_count, json.dumps(ocr_fragments),
             json.dumps(claude_cards), now_iso()),
        )
        conn.commit()

        # Step 3: Scryfall resolution
        send_event("status", {"message": "Querying Scryfall..."})
        scryfall = ScryfallAPI()

        from mtg_collector.db.models import PrintingRepository
        printing_repo = PrintingRepository(conn)

        all_matches = []
        all_crops = []

        for ci, card_info in enumerate(claude_cards):
            set_code, cn_or_query = _build_scryfall_query(card_info, {})
            candidates = []

            # Direct lookup
            if set_code and cn_or_query:
                cn_raw = cn_or_query
                cn_stripped = cn_raw.lstrip("0") or "0"
                card_data = scryfall.get_card_by_set_cn(set_code, cn_stripped)
                if not card_data:
                    card_data = scryfall.get_card_by_set_cn(set_code, cn_raw)
                if card_data:
                    candidates = [card_data]

            # Fallback: search by name
            if not candidates:
                name = card_info.get("name")
                search_set = card_info.get("set_code")
                if name:
                    candidates = scryfall.search_card(name, set_code=search_set)

            # Last resort: raw query
            if not candidates and cn_or_query and not set_code:
                try:
                    url = f"{scryfall.BASE_URL}/cards/search"
                    params = {"q": cn_or_query, "unique": "prints"}
                    response = scryfall._request_with_retry("GET", url, params=params)
                    response.raise_for_status()
                    data = response.json()
                    if data.get("object") == "list" and data.get("data"):
                        candidates = data["data"]
                except Exception:
                    pass

            formatted = _format_candidates(candidates)

            all_matches.append(formatted)
            _log_ingest(f"Scryfall card {ci}: {len(formatted)} candidates for '{card_info.get('name', '???')}'")

            # Compute crop from fragment indices
            frag_indices = card_info.get("fragment_indices", [])
            crop = _compute_card_crop(ocr_fragments, frag_indices)
            all_crops.append(crop)

        # Check lineage for already-ingested cards
        lineage_rows = conn.execute(
            "SELECT card_index FROM ingest_lineage WHERE image_md5 = ?",
            (md5,),
        ).fetchall()
        already_ingested = {row["card_index"] for row in lineage_rows}

        # Update session state
        disambiguated = []
        for ci in range(len(claude_cards)):
            if ci in already_ingested:
                disambiguated.append("already_ingested")
            else:
                disambiguated.append(None)

        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if session and img_idx < len(session["images"]):
                session["images"][img_idx]["ocr_result"] = ocr_fragments
                session["images"][img_idx]["claude_result"] = claude_cards
                session["images"][img_idx]["scryfall_matches"] = all_matches
                session["images"][img_idx]["crops"] = all_crops
                session["images"][img_idx]["disambiguated"] = disambiguated

        # Build matches_ready payload
        cards_payload = []
        for ci, card_info in enumerate(claude_cards):
            cards_payload.append({
                "card_info": card_info,
                "candidates": all_matches[ci] if ci < len(all_matches) else [],
                "crop": all_crops[ci] if ci < len(all_crops) else None,
                "already_ingested": ci in already_ingested,
            })

        send_event("matches_ready", {"cards": cards_payload})
        conn.close()

    def _process_image_names_sse(self, sid, img_idx, img, force, send_event):
        """Process a single image in names-only mode: OCR -> Claude names -> Scryfall search."""
        from mtg_collector.services.ocr import run_ocr_with_boxes
        from mtg_collector.services.claude import ClaudeVision
        from mtg_collector.services.scryfall import ScryfallAPI
        from mtg_collector.db.schema import init_db
        from mtg_collector.utils import now_iso

        image_path = str(_get_ingest_images_dir() / img["stored_name"])
        md5 = img["md5"]
        card_count = img["card_count"]
        cache_key = md5 + ":names"

        _log_ingest(f"Processing image {img_idx} (names mode): {img['filename']} (MD5={md5}, count={card_count}, force={force})")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)

        ocr_fragments = None
        claude_cards = None

        if not force:
            cache_row = conn.execute(
                "SELECT ocr_result, claude_result FROM ingest_cache WHERE image_md5 = ?",
                (cache_key,),
            ).fetchone()
            if cache_row:
                _log_ingest(f"Cache hit for names MD5={cache_key}")
                ocr_fragments = json.loads(cache_row["ocr_result"])
                send_event("cached", {"step": "ocr"})
                send_event("ocr_complete", {"fragment_count": len(ocr_fragments), "fragments": ocr_fragments})

                if cache_row["claude_result"]:
                    claude_cards = json.loads(cache_row["claude_result"])
                    send_event("cached", {"step": "claude"})
                    send_event("claude_complete", {"card_count": len(claude_cards), "cards": claude_cards})

        # Step 1: OCR
        if ocr_fragments is None:
            send_event("status", {"message": "Running OCR..."})
            t0 = time.time()
            raw_fragments = run_ocr_with_boxes(image_path)
            elapsed = time.time() - t0
            _log_ingest(f"OCR complete: {len(raw_fragments)} fragments in {elapsed:.1f}s")
            ocr_fragments = _merge_nearby_fragments(raw_fragments)
            _log_ingest(f"Merged {len(raw_fragments)} -> {len(ocr_fragments)} fragments")
            send_event("ocr_complete", {"fragment_count": len(ocr_fragments), "fragments": ocr_fragments})

        # Step 2: Claude name extraction
        if claude_cards is None:
            send_event("status", {"message": "Calling Claude (names mode)..."})
            t0 = time.time()
            claude = ClaudeVision()
            claude_cards, usage = claude.extract_names_from_ocr(
                ocr_fragments, card_count,
                status_callback=lambda msg: send_event("status", {"message": msg}),
            )
            elapsed = time.time() - t0
            token_info = {}
            if usage:
                token_info = {"in": usage.input_tokens, "out": usage.output_tokens}
            _log_ingest(f"Claude names complete: {len(claude_cards)} unique names in {elapsed:.1f}s, tokens={token_info}")
            send_event("claude_complete", {
                "card_count": len(claude_cards),
                "cards": claude_cards,
                "tokens": token_info,
            })

            # Validate total quantity matches expected count
            total_qty = sum(c.get("quantity", 1) for c in claude_cards)
            if total_qty != card_count:
                msg = (f"Expected {card_count} card(s) but Claude found {total_qty}. "
                       f"Discard this image and take a better photo.")
                _log_ingest(f"Count mismatch (names): {msg}")
                send_event("count_mismatch", {"message": msg, "expected": card_count, "found": total_qty})
                conn.close()
                return

        # Save to cache
        conn.execute(
            """INSERT OR REPLACE INTO ingest_cache
               (image_md5, image_path, card_count, ocr_result, claude_result, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cache_key, image_path, card_count, json.dumps(ocr_fragments),
             json.dumps(claude_cards), now_iso()),
        )
        conn.commit()

        # Step 3: Scryfall search for each unique name
        send_event("status", {"message": "Querying Scryfall..."})
        scryfall = ScryfallAPI()

        names_data = []
        for ci, card_info in enumerate(claude_cards):
            name = card_info.get("name", "")
            quantity = card_info.get("quantity", 1)
            frag_indices = card_info.get("fragment_indices", [])

            # Search Scryfall by name
            candidates = scryfall.search_card(name)
            formatted = _format_candidates(candidates)
            _log_ingest(f"Scryfall name {ci}: {len(formatted)} candidates for '{name}' (qty={quantity})")

            # Compute per-occurrence bboxes
            occurrence_crops = []
            for occ_indices in frag_indices:
                crop = _compute_name_bbox(ocr_fragments, occ_indices)
                occurrence_crops.append(crop)

            names_data.append({
                "name": name,
                "quantity": quantity,
                "uncertain": card_info.get("uncertain", False),
                "candidates": formatted,
                "occurrence_crops": occurrence_crops,
                "occurrence_frag_indices": frag_indices,
            })

        # Check lineage for already-ingested
        lineage_rows = conn.execute(
            "SELECT card_index FROM ingest_lineage WHERE image_md5 = ?",
            (cache_key,),
        ).fetchall()
        already_ingested_indices = {row["card_index"] for row in lineage_rows}

        # Build names_disambiguated: per name_idx, list of length quantity
        names_disambiguated = []
        card_index_offset = 0
        for ni, nd in enumerate(names_data):
            qty = nd["quantity"]
            occ_list = []
            for oi in range(qty):
                global_idx = card_index_offset + oi
                if global_idx in already_ingested_indices:
                    occ_list.append("already_ingested")
                else:
                    occ_list.append(None)
            names_disambiguated.append(occ_list)
            card_index_offset += qty

        # Update session state
        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if session and img_idx < len(session["images"]):
                session["images"][img_idx]["ocr_result"] = ocr_fragments
                session["images"][img_idx]["claude_result"] = claude_cards
                session["images"][img_idx]["names_data"] = names_data
                session["images"][img_idx]["names_disambiguated"] = names_disambiguated

        send_event("names_ready", {"names": names_data})
        conn.close()

    def _process_image_click_names_sse(self, sid, img_idx, img, force, send_event):
        """Process a single image in click-names mode: OCR only, no Claude."""
        from mtg_collector.services.ocr import run_ocr_with_boxes
        from mtg_collector.db.schema import init_db
        from mtg_collector.utils import now_iso

        image_path = str(_get_ingest_images_dir() / img["stored_name"])
        md5 = img["md5"]
        cache_key = md5 + ":click_names"

        _log_ingest(f"Processing image {img_idx} (click_names mode): {img['filename']} (MD5={md5}, force={force})")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)

        ocr_fragments = None

        if not force:
            cache_row = conn.execute(
                "SELECT ocr_result FROM ingest_cache WHERE image_md5 = ?",
                (cache_key,),
            ).fetchone()
            if cache_row:
                _log_ingest(f"Cache hit for click_names MD5={cache_key}")
                ocr_fragments = json.loads(cache_row["ocr_result"])
                send_event("cached", {"step": "ocr"})
                send_event("ocr_complete", {"fragment_count": len(ocr_fragments), "fragments": ocr_fragments})

        # Step 1: OCR
        if ocr_fragments is None:
            send_event("status", {"message": "Running OCR..."})
            t0 = time.time()
            raw_fragments = run_ocr_with_boxes(image_path)
            elapsed = time.time() - t0
            _log_ingest(f"OCR complete: {len(raw_fragments)} fragments in {elapsed:.1f}s")
            send_event("ocr_complete", {"fragment_count": len(raw_fragments), "fragments": raw_fragments})

            # Merge nearby/overlapping fragments for cleaner click targets
            ocr_fragments = _merge_nearby_fragments(raw_fragments)
            _log_ingest(f"Merged {len(raw_fragments)} -> {len(ocr_fragments)} fragments")

        # Save to cache (merged fragments)
        conn.execute(
            """INSERT OR REPLACE INTO ingest_cache
               (image_md5, image_path, card_count, ocr_result, claude_result, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cache_key, image_path, 0, json.dumps(ocr_fragments), None, now_iso()),
        )
        conn.commit()

        # Update session state
        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if session and img_idx < len(session["images"]):
                session["images"][img_idx]["ocr_result"] = ocr_fragments

        # Emit fragments ready — client will show clickable overlay
        send_event("ocr_fragments_ready", {"fragments": ocr_fragments})
        conn.close()

    def _api_ingest_submit_clicked_names(self):
        """Accept user-clicked OCR fragments, search Scryfall for each name, set up names disambiguation."""
        from mtg_collector.services.scryfall import ScryfallAPI

        data = self._read_json_body()
        if data is None:
            return

        sid = data["session_id"]
        img_idx = data["image_idx"]
        clicked_fragments = data["fragments"]  # [{text, bbox, count}]

        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if not session or img_idx >= len(session["images"]):
                self._send_json({"error": "Invalid session or image"}, 400)
                return
            img = session["images"][img_idx]
            ocr_fragments = img.get("ocr_result", [])
            md5 = img["md5"]

        cache_key = md5 + ":click_names"
        scryfall = ScryfallAPI()

        names_data = []
        for ci, frag in enumerate(clicked_fragments):
            name = frag["text"]
            quantity = frag.get("count", 1)

            candidates = scryfall.search_card(name)
            formatted = _format_candidates(candidates)
            _log_ingest(f"Scryfall click-name {ci}: {len(formatted)} candidates for '{name}' (qty={quantity})")

            # Build occurrence crops from the bbox
            occurrence_crops = []
            for _ in range(quantity):
                occurrence_crops.append(frag.get("bbox"))

            names_data.append({
                "name": name,
                "quantity": quantity,
                "uncertain": False,
                "candidates": formatted,
                "occurrence_crops": occurrence_crops,
                "occurrence_frag_indices": [],
            })

        # Check lineage for already-ingested
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        from mtg_collector.db.schema import init_db
        init_db(conn)

        lineage_rows = conn.execute(
            "SELECT card_index FROM ingest_lineage WHERE image_md5 = ?",
            (cache_key,),
        ).fetchall()
        already_ingested_indices = {row["card_index"] for row in lineage_rows}
        conn.close()

        names_disambiguated = []
        card_index_offset = 0
        for ni, nd in enumerate(names_data):
            qty = nd["quantity"]
            occ_list = []
            for oi in range(qty):
                global_idx = card_index_offset + oi
                if global_idx in already_ingested_indices:
                    occ_list.append("already_ingested")
                else:
                    occ_list.append(None)
            names_disambiguated.append(occ_list)
            card_index_offset += qty

        # Update session state — reuse names mode structures
        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if session and img_idx < len(session["images"]):
                session["images"][img_idx]["mode"] = "names"
                session["images"][img_idx]["names_data"] = names_data
                session["images"][img_idx]["names_disambiguated"] = names_disambiguated

        self._send_json({"names": names_data})

    def _api_ingest_next_name(self):
        """Find the next name that needs disambiguation across all names-mode images."""
        data = self._read_json_body()
        if data is None:
            return
        sid = data.get("session_id", "")

        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if not session:
                self._send_json({"error": "Invalid session"}, 400)
                return

            total_names = 0
            total_done = 0

            for img_idx, img in enumerate(session["images"]):
                if img.get("mode") != "names":
                    continue
                names_disambiguated = img.get("names_disambiguated")
                if names_disambiguated is None:
                    continue
                names_data = img.get("names_data", [])

                for name_idx, occ_list in enumerate(names_disambiguated):
                    total_names += 1
                    # A name is "done" when all occurrences are assigned (non-None)
                    if all(o is not None for o in occ_list):
                        total_done += 1
                    else:
                        # Found next unfinished name
                        nd = names_data[name_idx] if name_idx < len(names_data) else {}
                        self._send_json({
                            "done": False,
                            "image_idx": img_idx,
                            "name_idx": name_idx,
                            "image_filename": img["stored_name"],
                            "name": nd.get("name", "???"),
                            "quantity": nd.get("quantity", 1),
                            "uncertain": nd.get("uncertain", False),
                            "candidates": nd.get("candidates", []),
                            "occurrence_crops": nd.get("occurrence_crops", []),
                            "assignments": occ_list,
                            "total_names": total_names + sum(
                                len(im.get("names_disambiguated", []))
                                for im in session["images"][img_idx + 1:]
                                if im.get("mode") == "names" and im.get("names_disambiguated") is not None
                            ),
                            "total_done": total_done,
                        })
                        return

        self._send_json({"done": True, "total_names": total_names, "total_done": total_done})

    def _api_ingest_confirm_name(self):
        """Batch-confirm assignments for a name: create one collection entry per occurrence."""
        from mtg_collector.services.scryfall import ScryfallAPI, cache_scryfall_data
        from mtg_collector.db.models import (
            CardRepository, SetRepository, PrintingRepository, CollectionRepository, CollectionEntry,
        )
        from mtg_collector.db.schema import init_db
        from mtg_collector.utils import now_iso

        data = self._read_json_body()
        if data is None:
            return

        sid = data["session_id"]
        img_idx = data["image_idx"]
        name_idx = data["name_idx"]
        assignments = data["assignments"]  # [{occurrence_idx, scryfall_id, finish}]

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)

        scryfall = ScryfallAPI()
        card_repo = CardRepository(conn)
        set_repo = SetRepository(conn)
        printing_repo = PrintingRepository(conn)
        collection_repo = CollectionRepository(conn)

        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if not session or img_idx >= len(session["images"]):
                conn.close()
                self._send_json({"error": "Invalid session or image"}, 400)
                return
            img = session["images"][img_idx]
            md5 = img["md5"]
            cache_key = md5 + ":names"
            names_data = img.get("names_data", [])

        # Compute card_index_offset for this name
        card_index_offset = 0
        for ni in range(name_idx):
            if ni < len(names_data):
                card_index_offset += names_data[ni].get("quantity", 1)

        entry_ids = []
        for a in assignments:
            occ_idx = a["occurrence_idx"]
            scryfall_id = a["scryfall_id"]
            finish = a.get("finish", "nonfoil")

            card_data = scryfall.get_card_by_id(scryfall_id)
            if not card_data:
                conn.close()
                self._send_json({"error": f"Card {scryfall_id} not found on Scryfall"}, 404)
                return

            cache_scryfall_data(scryfall, card_repo, set_repo, printing_repo, card_data)

            entry = CollectionEntry(
                id=None,
                scryfall_id=scryfall_id,
                finish=finish,
                condition="Near Mint",
                source="ocr_ingest_names",
            )
            entry_id = collection_repo.add(entry)
            entry_ids.append(entry_id)

            # Insert lineage
            global_card_idx = card_index_offset + occ_idx
            conn.execute(
                """INSERT INTO ingest_lineage (collection_id, image_md5, image_path, card_index, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (entry_id, cache_key, img.get("stored_name", ""), global_card_idx, now_iso()),
            )

        conn.commit()
        conn.close()

        # Mark disambiguated
        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if session and img_idx < len(session["images"]):
                nd = session["images"][img_idx].get("names_disambiguated")
                if nd and name_idx < len(nd):
                    for a in assignments:
                        nd[name_idx][a["occurrence_idx"]] = a["scryfall_id"]

        name = names_data[name_idx]["name"] if name_idx < len(names_data) else "???"
        _log_ingest(f"Confirmed name '{name}': {len(entry_ids)} occurrence(s) -> IDs {entry_ids}")

        self._send_json({"ok": True, "entry_ids": entry_ids, "name": name})

    def _api_ingest_skip_name(self):
        """Skip all occurrences of a name."""
        data = self._read_json_body()
        if data is None:
            return

        sid = data["session_id"]
        img_idx = data["image_idx"]
        name_idx = data["name_idx"]

        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if session and img_idx < len(session["images"]):
                nd = session["images"][img_idx].get("names_disambiguated")
                if nd and name_idx < len(nd):
                    for oi in range(len(nd[name_idx])):
                        if nd[name_idx][oi] is None:
                            nd[name_idx][oi] = "skipped"

        _log_ingest(f"Skipped name {name_idx} in image {img_idx}")
        self._send_json({"ok": True})

    def _api_ingest_next_card(self):
        """Find the next card that needs disambiguation across all images."""
        data = self._read_json_body()
        if data is None:
            # Try query string for GET-style POST
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            sid = params.get("session_id", [""])[0]
        else:
            sid = data.get("session_id", "")

        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if not session:
                self._send_json({"error": "Invalid session"}, 400)
                return

            total_cards = 0
            total_done = 0

            for img_idx, img in enumerate(session["images"]):
                if img["disambiguated"] is None:
                    continue
                for card_idx, status in enumerate(img["disambiguated"]):
                    total_cards += 1
                    if status is not None:
                        total_done += 1
                    else:
                        # Found next card
                        candidates = img["scryfall_matches"][card_idx] if img["scryfall_matches"] else []
                        crop = img["crops"][card_idx] if img["crops"] else None
                        card_info = img["claude_result"][card_idx] if img["claude_result"] else {}
                        self._send_json({
                            "done": False,
                            "image_idx": img_idx,
                            "card_idx": card_idx,
                            "image_filename": img["stored_name"],
                            "card": card_info,
                            "candidates": candidates,
                            "crop": crop,
                            "total_cards": total_cards + sum(
                                len(im["disambiguated"]) for im in session["images"][img_idx + 1:]
                                if im["disambiguated"] is not None
                            ),
                            "total_done": total_done,
                        })
                        return

        self._send_json({"done": True, "total_cards": total_cards, "total_done": total_done})

    def _api_ingest_confirm(self):
        """Confirm a card: add to collection + ingest_lineage."""
        from mtg_collector.services.scryfall import ScryfallAPI, cache_scryfall_data
        from mtg_collector.db.models import (
            CardRepository, SetRepository, PrintingRepository, CollectionRepository, CollectionEntry,
        )
        from mtg_collector.db.schema import init_db
        from mtg_collector.utils import now_iso

        data = self._read_json_body()
        if data is None:
            return

        sid = data["session_id"]
        img_idx = data["image_idx"]
        card_idx = data["card_idx"]
        scryfall_id = data["scryfall_id"]
        finish = data.get("finish", "nonfoil")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)

        scryfall = ScryfallAPI()
        card_repo = CardRepository(conn)
        set_repo = SetRepository(conn)
        printing_repo = PrintingRepository(conn)
        collection_repo = CollectionRepository(conn)

        # Look up the printing
        card_data = scryfall.get_card_by_id(scryfall_id)
        if not card_data:
            conn.close()
            self._send_json({"error": "Card not found on Scryfall"}, 404)
            return

        # Cache in DB
        cache_scryfall_data(scryfall, card_repo, set_repo, printing_repo, card_data)

        # Create collection entry
        entry = CollectionEntry(
            id=None,
            scryfall_id=scryfall_id,
            finish=finish,
            condition="Near Mint",
            source="ocr_ingest",
        )
        entry_id = collection_repo.add(entry)

        # Get image md5 from session
        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            md5 = ""
            image_path = ""
            if session and img_idx < len(session["images"]):
                md5 = session["images"][img_idx]["md5"]
                image_path = session["images"][img_idx].get("stored_name", "")

        # Insert lineage
        conn.execute(
            """INSERT INTO ingest_lineage (collection_id, image_md5, image_path, card_index, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entry_id, md5, image_path, card_idx, now_iso()),
        )
        conn.commit()
        conn.close()

        # Mark disambiguated
        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if session and img_idx < len(session["images"]):
                session["images"][img_idx]["disambiguated"][card_idx] = scryfall_id

        name = card_data.get("name", "???")
        set_code = card_data.get("set", "???")
        cn = card_data.get("collector_number", "???")
        _log_ingest(f"Confirmed: {name} ({set_code.upper()} #{cn}) -> collection ID {entry_id}")

        self._send_json({
            "ok": True,
            "entry_id": entry_id,
            "name": name,
            "set_code": set_code,
            "collector_number": cn,
        })

    def _api_ingest_skip(self):
        data = self._read_json_body()
        if data is None:
            return

        sid = data["session_id"]
        img_idx = data["image_idx"]
        card_idx = data["card_idx"]

        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if session and img_idx < len(session["images"]):
                if session["images"][img_idx]["disambiguated"] is not None:
                    session["images"][img_idx]["disambiguated"][card_idx] = "skipped"

        _log_ingest(f"Skipped card {card_idx} in image {img_idx}")
        self._send_json({"ok": True})

    def _api_ingest_search_card(self):
        """Manual card name search during disambiguation."""
        from mtg_collector.services.scryfall import ScryfallAPI

        data = self._read_json_body()
        if data is None:
            return

        sid = data.get("session_id", "")
        img_idx = data.get("image_idx")
        card_idx = data.get("card_idx")
        query = (data.get("query") or "").strip()

        if not query:
            self._send_json({"error": "Empty query"}, 400)
            return

        scryfall = ScryfallAPI()
        candidates = scryfall.search_card(query)
        formatted = _format_candidates(candidates)

        with _ingest_lock:
            session = _ingest_sessions.get(sid)
            if session and img_idx is not None and img_idx < len(session["images"]):
                img = session["images"][img_idx]
                if img["scryfall_matches"] and card_idx is not None and card_idx < len(img["scryfall_matches"]):
                    img["scryfall_matches"][card_idx] = formatted

        self._send_json({"candidates": formatted})

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return None
        body = self.rfile.read(content_length)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return None

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

    server = ThreadingHTTPServer(("", args.port), handler)
    print(f"Server running at http://localhost:{args.port}")
    print(f"Crack-a-Pack: http://localhost:{args.port}/crack")
    print(f"Explore Sheets: http://localhost:{args.port}/sheets")
    print(f"Collection: http://localhost:{args.port}/collection")
    print(f"Ingestor (OCR): http://localhost:{args.port}/ingestor-ocr")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
