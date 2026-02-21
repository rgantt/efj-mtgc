"""Crack-a-pack web server: mtg crack-pack-server --port 8080"""

import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse

import requests

from mtg_collector.db.connection import get_db_path
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


def _get_sqlite_price(db_path: str, set_code: str, collector_number: str, source: str, price_type: str) -> str | None:
    """Look up a single price from the latest_prices view."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT price FROM latest_prices WHERE set_code = ? AND collector_number = ? AND source = ? AND price_type = ?",
        (set_code.lower(), collector_number, source, price_type),
    ).fetchone()
    conn.close()
    return str(row[0]) if row else None


_INGEST_IMAGES_DIR = None  # Set in _get_ingest_images_dir()

# ── Background ingest worker ──
_ingest_executor: ThreadPoolExecutor | None = None
_scryfall_rate_lock = threading.Lock()
_scryfall_last_request: float = 0.0
_background_db_path: str | None = None


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


def _merge_overlapping_cards(claude_cards, ocr_fragments):
    """Merge Claude-identified cards whose fragment bounding boxes heavily overlap.

    Sometimes Claude splits fragments from a single card into two objects (e.g. a
    full card + a ghost card with just the artist name from the bottom corner).
    This detects when one card's fragment bbox is mostly contained in another's
    and merges the smaller into the larger, combining fragment_indices and filling
    in any fields the larger card was missing.
    """
    if len(claude_cards) <= 1:
        return claude_cards

    def _fragment_bbox(card):
        """Compute raw union bbox of a card's fragment indices (no buffer)."""
        indices = card.get("fragment_indices", [])
        if not indices:
            return None
        xs, ys, xws, yhs = [], [], [], []
        for i in indices:
            if i < len(ocr_fragments):
                b = ocr_fragments[i]["bbox"]
                xs.append(b["x"])
                ys.append(b["y"])
                xws.append(b["x"] + b["w"])
                yhs.append(b["y"] + b["h"])
        if not xs:
            return None
        return (min(xs), min(ys), max(xws), max(yhs))

    def _overlap_fraction(inner, outer):
        """What fraction of inner's area is contained within outer?"""
        ix1, iy1, ix2, iy2 = inner
        ox1, oy1, ox2, oy2 = outer
        # Intersection
        xx1 = max(ix1, ox1)
        yy1 = max(iy1, oy1)
        xx2 = min(ix2, ox2)
        yy2 = min(iy2, oy2)
        if xx2 <= xx1 or yy2 <= yy1:
            return 0.0
        intersection = (xx2 - xx1) * (yy2 - yy1)
        inner_area = (ix2 - ix1) * (iy2 - iy1)
        return intersection / inner_area if inner_area > 0 else 0.0

    def _fields_conflict(a, b):
        """Check if two cards have conflicting non-null fields."""
        for key in ("name", "type", "subtype", "mana_cost", "collector_number", "set_code"):
            va = a.get(key)
            vb = b.get(key)
            if va and vb and str(va).lower() != str(vb).lower():
                return True
        return False

    # Compute bboxes
    bboxes = [_fragment_bbox(c) for c in claude_cards]

    # Find pairs to merge: smaller card absorbed into larger card
    absorbed = set()  # indices of cards absorbed into another
    merge_into = {}   # absorbed_idx -> target_idx

    for i in range(len(claude_cards)):
        if i in absorbed:
            continue
        for j in range(len(claude_cards)):
            if j == i or j in absorbed:
                continue
            if bboxes[i] is None or bboxes[j] is None:
                continue
            # Check if j is mostly inside i
            frac = _overlap_fraction(bboxes[j], bboxes[i])
            if frac >= 0.7 and not _fields_conflict(claude_cards[i], claude_cards[j]):
                absorbed.add(j)
                merge_into[j] = i

    if not absorbed:
        return claude_cards

    # Perform merges
    merged = [dict(c) for c in claude_cards]  # shallow copy each
    for src_idx, dst_idx in merge_into.items():
        src = claude_cards[src_idx]
        dst = merged[dst_idx]
        # Merge fragment_indices
        dst_frags = set(dst.get("fragment_indices", []))
        dst_frags.update(src.get("fragment_indices", []))
        dst["fragment_indices"] = sorted(dst_frags)
        # Fill in missing fields from src
        for key in ("name", "mana_cost", "mana_value", "type", "subtype",
                     "rules_text", "collector_number", "set_code", "artist",
                     "power", "toughness"):
            if not dst.get(key) and src.get(key):
                dst[key] = src[key]

    result = [merged[i] for i in range(len(merged)) if i not in absorbed]
    _log_ingest(f"Merged overlapping cards: {len(claude_cards)} -> {len(result)}")
    return result


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


def _extract_ocr_name(ocr_fragments, fragment_indices):
    """Extract the card name from OCR fragments by finding the topmost text.

    The card name sits at the top of the card. We take the fragments assigned
    to this card, find the topmost ones (within 3px of each other vertically,
    to handle overlapping/nearby bounding boxes), and merge their text
    left-to-right.
    """
    if not ocr_fragments or not fragment_indices:
        return ""
    # Gather the fragments for this card
    frags = []
    for i in fragment_indices:
        if i < len(ocr_fragments):
            frags.append(ocr_fragments[i])
    if not frags:
        return ""
    # Find the minimum y (topmost fragment)
    min_y = min(f["bbox"]["y"] for f in frags)
    # Collect all fragments within 3px of the topmost
    top_frags = [f for f in frags if f["bbox"]["y"] - min_y <= 3]
    # Sort left-to-right and join
    top_frags.sort(key=lambda f: f["bbox"]["x"])
    return " ".join(f["text"] for f in top_frags)


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
            "artist": c.get("artist", ""),
        })
    return formatted


def _local_name_search(conn, name, set_code=None, limit=20):
    """Search local DB for cards by name, return Scryfall-format dicts for _format_candidates."""
    from mtg_collector.db.models import CardRepository, PrintingRepository

    card_repo = CardRepository(conn)
    printing_repo = PrintingRepository(conn)

    cards = card_repo.search_cards_by_name(name, limit=limit)
    results = []
    for card in cards:
        printings = printing_repo.get_by_oracle_id(card.oracle_id)
        for p in printings:
            if set_code and p.set_code != set_code.lower():
                continue
            data = p.get_scryfall_data()
            if data:
                results.append(data)
    return results


def _log_ingest(msg):
    sys.stderr.write(f"[INGEST] {msg}\n")
    sys.stderr.flush()


def _scryfall_rate_limit():
    """Enforce 100ms spacing between Scryfall requests across all worker threads."""
    global _scryfall_last_request
    with _scryfall_rate_lock:
        now = time.time()
        elapsed = now - _scryfall_last_request
        if elapsed < 0.1:
            time.sleep(0.1 - elapsed)
        _scryfall_last_request = time.time()


def _process_image_core(conn, image_id, img, log_fn):
    """Process a single image: OCR -> Claude -> Scryfall. Returns final status string.

    Used by both the SSE endpoint and background workers.
    log_fn(event_type, data_dict) is called for progress events.
    """
    from mtg_collector.cli.ingest_ocr import _build_scryfall_query
    from mtg_collector.db.models import PrintingRepository
    from mtg_collector.services.agent import run_agent
    from mtg_collector.services.ocr import run_ocr_with_boxes
    from mtg_collector.utils import now_iso

    image_path = str(_get_ingest_images_dir() / img["stored_name"])
    md5 = img["md5"]

    _log_ingest(f"Processing image {image_id}: {img['filename']} (MD5={md5})")

    ocr_fragments = None
    claude_cards = None
    agent_trace = []

    api_usage = None

    # Check cache
    cache_row = conn.execute(
        "SELECT ocr_result, claude_result, agent_trace, api_usage FROM ingest_cache WHERE image_md5 = ?",
        (md5,),
    ).fetchone()
    if cache_row:
        _log_ingest(f"Cache hit for MD5={md5}")
        ocr_fragments = json.loads(cache_row["ocr_result"])
        log_fn("cached", {"step": "ocr"})
        log_fn("ocr_complete", {"fragment_count": len(ocr_fragments), "fragments": ocr_fragments})
        if cache_row["claude_result"]:
            claude_cards = json.loads(cache_row["claude_result"])
            agent_trace = json.loads(cache_row["agent_trace"]) if cache_row["agent_trace"] else []
            api_usage = json.loads(cache_row["api_usage"]) if cache_row["api_usage"] else None
            log_fn("cached", {"step": "claude"})
            log_fn("claude_complete", {"cards": claude_cards})

    # Step 1: OCR
    if ocr_fragments is None:
        log_fn("status", {"message": "Running OCR..."})
        t0 = time.time()
        raw_fragments = run_ocr_with_boxes(image_path)
        elapsed = time.time() - t0
        _log_ingest(f"OCR complete: {len(raw_fragments)} fragments in {elapsed:.1f}s")
        ocr_fragments = _merge_nearby_fragments(raw_fragments)
        _log_ingest(f"Merged {len(raw_fragments)} -> {len(ocr_fragments)} fragments")
        log_fn("ocr_complete", {"fragment_count": len(ocr_fragments), "fragments": ocr_fragments})

    # Step 2: Agent extraction
    if claude_cards is None:
        log_fn("status", {"message": "Calling agent..."})
        t0 = time.time()
        try:
            claude_cards, _, api_usage = run_agent(
                image_path,
                ocr_fragments=ocr_fragments,
                status_callback=lambda msg: log_fn("status", {"message": msg}),
                trace_out=agent_trace,
            )
        except Exception as e:
            e.agent_trace = agent_trace
            raise
        elapsed = time.time() - t0
        _log_ingest(f"Agent complete: {len(claude_cards)} cards in {elapsed:.1f}s")
        log_fn("claude_complete", {"cards": claude_cards})

    # Merge cards whose fragment bboxes heavily overlap (e.g. ghost artist-only card)
    claude_cards = _merge_overlapping_cards(claude_cards, ocr_fragments)

    # Group by fragment_indices: Claude returns multiple entries for one physical card
    # when uncertain about printing (per the DISAMBIGUATION RULE). Consolidate these
    # into one slot so we don't ingest the same card twice.
    _frag_groups: dict[tuple, list] = {}
    _frag_key_order: list[tuple] = []
    for _card in claude_cards:
        _key = tuple(sorted(_card.get("fragment_indices") or []))
        if _key not in _frag_groups:
            _frag_groups[_key] = []
            _frag_key_order.append(_key)
        _frag_groups[_key].append(_card)

    _CONF = {"high": 0, "medium": 1, "low": 2}
    # One representative per physical card (highest confidence); extras resolved separately
    claude_cards = [
        min(_frag_groups[k], key=lambda c: _CONF.get(c.get("confidence", "low"), 2))
        for k in _frag_key_order
    ]
    _group_extras = {
        ci: [c for c in _frag_groups[k] if c is not claude_cards[ci]]
        for ci, k in enumerate(_frag_key_order)
    }
    n_extras = sum(len(v) for v in _group_extras.values())
    if n_extras:
        _log_ingest(f"Grouped {len(claude_cards) + n_extras} agent entries -> {len(claude_cards)} physical card(s) ({n_extras} disambiguation candidate(s) merged)")

    # Save to cache
    conn.execute(
        """INSERT OR REPLACE INTO ingest_cache
           (image_md5, image_path, ocr_result, claude_result, agent_trace, api_usage, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (md5, image_path, json.dumps(ocr_fragments),
         json.dumps(claude_cards), json.dumps(agent_trace),
         json.dumps(api_usage) if api_usage else None, now_iso()),
    )
    conn.commit()

    # Step 3: Local DB resolution
    log_fn("status", {"message": "Resolving cards..."})
    printing_repo = PrintingRepository(conn)

    all_matches = []
    all_crops = []

    def _resolve_candidate(card_info) -> list:
        """Resolve one agent card_info to a list of raw Scryfall card dicts."""
        set_code, cn_or_query = _build_scryfall_query(card_info, {})
        candidates = []
        if set_code and cn_or_query:
            cn_raw = cn_or_query
            cn_stripped = cn_raw.lstrip("0") or "0"
            printing = printing_repo.get_by_set_cn(set_code, cn_stripped)
            if not printing:
                printing = printing_repo.get_by_set_cn(set_code, cn_raw)
            if printing:
                card_data = printing.get_scryfall_data()
                if card_data:
                    candidates = [card_data]
                    # If the set+CN lookup returned a different card name than
                    # Claude extracted, also do a name search so the correct card
                    # appears in disambiguation.
                    extracted_name = card_info.get("name", "")
                    returned_name = card_data.get("name", "")
                    if extracted_name and extracted_name.lower() != returned_name.lower():
                        _log_ingest(f"Name mismatch: Claude='{extracted_name}' vs DB='{returned_name}', adding name search")
                        name_results = _local_name_search(conn, extracted_name)
                        seen_ids = {c.get("id") for c in candidates}
                        for r in name_results:
                            if r.get("id") not in seen_ids:
                                candidates.append(r)
        if not candidates:
            name = card_info.get("name")
            search_set = card_info.get("set_code")
            if name:
                candidates = _local_name_search(conn, name, set_code=search_set)
        return candidates

    for ci, card_info in enumerate(claude_cards):
        # Skip cards with no identifying info (empty name + no fragments)
        if not card_info.get("name") and not card_info.get("fragment_indices"):
            all_matches.append([])
            all_crops.append(None)
            _log_ingest(f"Resolve card {ci}: skipped (no identifying info)")
            continue

        candidates = _resolve_candidate(card_info)

        if not candidates:
            set_code, cn_or_query = _build_scryfall_query(card_info, {})
            if cn_or_query and not set_code:
                candidates = _local_name_search(conn, cn_or_query)

        # Resolve extra candidates (same physical card, different printing guesses)
        # and merge into this slot's results, deduplicating by Scryfall ID.
        seen_ids = {c.get("id") for c in candidates}
        for extra_info in _group_extras.get(ci, []):
            for r in _resolve_candidate(extra_info):
                if r.get("id") not in seen_ids:
                    candidates.append(r)
                    seen_ids.add(r.get("id"))

        formatted = _format_candidates(candidates)
        all_matches.append(formatted)
        _log_ingest(f"Resolve card {ci}: {len(formatted)} candidates for '{card_info.get('name', '???')}'")

        frag_indices = card_info.get("fragment_indices", [])
        crop = _compute_card_crop(ocr_fragments, frag_indices)
        all_crops.append(crop)

    # Check lineage for already-ingested cards
    lineage_rows = conn.execute(
        "SELECT card_index FROM ingest_lineage WHERE image_md5 = ?",
        (md5,),
    ).fetchall()
    already_ingested = {row["card_index"] for row in lineage_rows}

    disambiguated = []
    for ci in range(len(claude_cards)):
        if ci in already_ingested:
            disambiguated.append("already_ingested")
        else:
            disambiguated.append(None)

    return ocr_fragments, claude_cards, all_matches, all_crops, disambiguated, agent_trace, api_usage


def _auto_ingest_single_candidates(conn, img, disambiguated, scryfall_matches):
    """Auto-ingest cards with exactly one candidate. Returns count of auto-ingested cards."""
    from mtg_collector.db.models import (
        CollectionEntry,
        CollectionRepository,
        PrintingRepository,
    )
    from mtg_collector.utils import now_iso

    auto_count = 0
    printing_repo = PrintingRepository(conn)
    collection_repo = CollectionRepository(conn)

    for card_idx, status in enumerate(disambiguated):
        if status is not None:
            continue
        candidates = scryfall_matches[card_idx] if card_idx < len(scryfall_matches) else []
        if len(candidates) != 1:
            continue

        c = candidates[0]
        scryfall_id = c["scryfall_id"]

        printing = printing_repo.get(scryfall_id)
        if not printing:
            continue

        entry = CollectionEntry(
            id=None,
            scryfall_id=scryfall_id,
            finish="nonfoil",
            condition="Near Mint",
            source="ocr_ingest",
        )
        entry_id = collection_repo.add(entry)

        md5 = img["md5"]
        conn.execute(
            """INSERT INTO ingest_lineage (collection_id, image_md5, image_path, card_index, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entry_id, md5, img["stored_name"], card_idx, now_iso()),
        )

        disambiguated[card_idx] = scryfall_id
        conn.commit()

        _log_ingest(f"Auto-confirmed: {printing.scryfall_id} ({c.get('set_code', '???').upper()} #{c.get('collector_number', '???')})")
        auto_count += 1

    return auto_count


def _reset_ingest_image(conn, image_id, md5, now):
    """Clear all artifacts for an ingest_images row and reset it to READY_FOR_OCR.

    Deletes the ingest_cache entry, removes any previously ingested collection
    entries and lineage records, and nulls all processing columns.

    Returns the number of collection entries removed.
    Does NOT commit — caller is responsible.
    """
    lineage_rows = conn.execute(
        "SELECT collection_id FROM ingest_lineage WHERE image_md5=?", (md5,)
    ).fetchall()
    removed = 0
    if lineage_rows:
        collection_ids = [r["collection_id"] for r in lineage_rows]
        placeholders = ",".join("?" * len(collection_ids))
        conn.execute("DELETE FROM ingest_lineage WHERE image_md5=?", (md5,))
        conn.execute(f"DELETE FROM collection WHERE id IN ({placeholders})", collection_ids)
        removed = len(collection_ids)

    conn.execute("DELETE FROM ingest_cache WHERE image_md5=?", (md5,))

    conn.execute(
        """UPDATE ingest_images SET
            status='READY_FOR_OCR',
            ocr_result=NULL,
            claude_result=NULL,
            agent_trace=NULL,
            api_usage=NULL,
            scryfall_matches=NULL,
            crops=NULL,
            disambiguated=NULL,
            names_data=NULL,
            names_disambiguated=NULL,
            user_card_edits=NULL,
            error_message=NULL,
            updated_at=?
           WHERE id=?""",
        (now, image_id),
    )
    return removed


def _process_image_background(db_path, image_id):
    """Background worker: process one image end-to-end in its own thread."""
    from mtg_collector.db.schema import init_db
    from mtg_collector.utils import now_iso

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)

    # Atomic claim
    cursor = conn.execute(
        "UPDATE ingest_images SET status='PROCESSING', updated_at=? WHERE id=? AND status='READY_FOR_OCR'",
        (now_iso(), image_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        conn.close()
        return

    row = conn.execute("SELECT * FROM ingest_images WHERE id = ?", (image_id,)).fetchone()
    img = dict(row)

    def log_fn(event_type, data_obj):
        # Background worker just logs, no SSE
        if event_type == "status":
            _log_ingest(f"[bg:{image_id}] {data_obj.get('message', '')}")

    try:
        ocr_fragments, claude_cards, all_matches, all_crops, disambiguated, agent_trace, api_usage = _process_image_core(
            conn, image_id, img, log_fn,
        )

        # Auto-ingest single-candidate cards
        auto_count = _auto_ingest_single_candidates(conn, img, disambiguated, all_matches)
        if auto_count:
            _log_ingest(f"[bg:{image_id}] Auto-ingested {auto_count} single-candidate card(s)")

        # Determine final status
        if all(d is not None for d in disambiguated):
            final_status = "DONE"
        else:
            final_status = "READY_FOR_DISAMBIGUATION"

        # Save state
        conn.execute(
            """UPDATE ingest_images SET
                status=?, ocr_result=?, claude_result=?, agent_trace=?, api_usage=?,
                scryfall_matches=?, crops=?, disambiguated=?, updated_at=?
               WHERE id=?""",
            (final_status, json.dumps(ocr_fragments), json.dumps(claude_cards),
             json.dumps(agent_trace), json.dumps(api_usage) if api_usage else None,
             json.dumps(all_matches), json.dumps(all_crops),
             json.dumps(disambiguated), now_iso(), image_id),
        )
        conn.commit()
        _log_ingest(f"[bg:{image_id}] Finished -> {final_status}")

    except Exception as e:
        tb = traceback.format_exc()
        _log_ingest(f"[bg:{image_id}] ERROR: {e}\n{tb}")
        partial_trace = getattr(e, "agent_trace", [])
        conn.execute(
            "UPDATE ingest_images SET status='ERROR', agent_trace=?, error_message=?, updated_at=? WHERE id=?",
            (json.dumps(partial_trace) if partial_trace else None, f"{e}\n{tb}", now_iso(), image_id),
        )
        conn.commit()
    finally:
        conn.close()


def _recover_pending_images(db_path):
    """On startup, re-queue any READY_FOR_OCR or stale PROCESSING images."""
    from mtg_collector.db.schema import init_db
    from mtg_collector.utils import now_iso

    print("[startup] Running database migrations ...", flush=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    print("[startup] Database ready", flush=True)

    # Reset stale PROCESSING (>10 min old) back to READY_FOR_OCR
    cutoff = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE ingest_images SET status='READY_FOR_OCR', updated_at=?
           WHERE status='PROCESSING'
           AND updated_at < datetime(?, '-600 seconds')""",
        (now_iso(), cutoff),
    )
    conn.commit()

    # Re-queue all READY_FOR_OCR
    rows = conn.execute("SELECT id FROM ingest_images WHERE status='READY_FOR_OCR'").fetchall()
    conn.close()

    if rows:
        print(f"[startup] Re-queuing {len(rows)} pending image(s) for OCR", flush=True)
    for row in rows:
        _log_ingest(f"Recovering image {row['id']} for background processing")
        _ingest_executor.submit(_process_image_background, db_path, row["id"])


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
        elif path == "/upload":
            self._serve_static("upload.html")
        elif path == "/recent":
            self._serve_static("recent.html")
        elif path == "/process":
            self._serve_static("recent.html")
        elif path == "/disambiguate":
            self._serve_static("disambiguate.html")
        elif path == "/correct":
            self._serve_static("correct.html")
        elif path == "/api/sets":
            self._api_sets()
        elif path == "/api/cached-sets":
            self._api_cached_sets()
        elif path == "/api/products":
            set_code = params.get("set", [""])[0]
            self._api_products(set_code)
        elif path == "/api/sheets":
            set_code = params.get("set", [""])[0]
            product = params.get("product", [""])[0]
            self._api_sheets(set_code, product)
        elif path == "/api/collection/copies":
            self._api_collection_copies(params)
        elif path == "/api/collection":
            self._api_collection(params)
        elif path == "/api/wishlist":
            self._api_wishlist_list(params)
        elif path.startswith("/api/card/"):
            scryfall_id = path[len("/api/card/"):]
            self._api_card(scryfall_id)
        elif path.startswith("/api/set-browse/"):
            set_code = path[len("/api/set-browse/"):]
            self._api_set_browse(set_code, params)
        elif path == "/ingest-corners":
            self._serve_static("ingest_corners.html")
        elif path == "/ingestor-ids":
            self._serve_static("ingest_ids.html")
        elif path == "/ingestor-order":
            self._serve_static("ingest_order.html")
        elif path == "/import-csv":
            self._serve_static("import_csv.html")
        elif path == "/api/orders":
            self._api_orders_list()
        elif path.startswith("/api/orders/") and path.endswith("/cards"):
            oid = path[len("/api/orders/"):-len("/cards")]
            self._api_order_cards(int(oid))
        elif path == "/api/settings":
            self._api_get_settings()
        elif path == "/api/prices-status":
            self._api_prices_status()
        elif path.startswith("/api/price-history/"):
            parts = path[len("/api/price-history/"):].split("/", 1)
            if len(parts) == 2:
                self._api_price_history(parts[0], parts[1])
            else:
                self._send_json({"error": "Expected /api/price-history/{set_code}/{collector_number}"}, 400)
        elif path == "/api/shorten":
            self._api_shorten(params)
        # Ingest2 API routes
        elif path == "/api/ingest2/images":
            self._api_ingest2_images(params)
        elif path == "/api/ingest2/counts":
            self._api_ingest2_counts()
        elif path == "/api/ingest2/usage-stats":
            self._api_ingest2_usage_stats(params)
        elif path == "/api/ingest2/recent":
            self._api_ingest2_recent(params)
        elif path == "/api/ingest2/pending-disambiguation":
            self._api_ingest2_pending_disambiguation()
        elif path.startswith("/api/ingest2/images/"):
            image_id = path[len("/api/ingest2/images/"):]
            self._api_ingest2_image_detail(int(image_id))
        elif path.startswith("/api/ingest2/process/"):
            image_id = path[len("/api/ingest2/process/"):]
            self._api_ingest2_process_sse(int(image_id))
        elif path == "/api/ingest2/next-card":
            image_id = params.get("image_id", [""])[0]
            self._api_ingest2_next_card(int(image_id) if image_id else None)
        elif path.startswith("/api/ingest/image/"):
            filename = unquote(path[len("/api/ingest/image/"):])
            self._api_ingest_serve_image(filename)
        # Sealed product API routes
        elif path == "/api/sealed/products/sets":
            self._api_sealed_products_sets()
        elif path == "/api/sealed/products":
            self._api_sealed_products(params)
        elif path == "/api/sealed/collection/stats":
            self._api_sealed_collection_stats()
        elif path == "/api/sealed/collection":
            self._api_sealed_collection_list(params)
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
        elif path == "/api/ingest2/upload":
            self._api_ingest2_upload()
        elif path == "/api/ingest2/set-params":
            self._api_ingest2_set_params()
        elif path == "/api/ingest2/confirm":
            self._api_ingest2_confirm()
        elif path == "/api/ingest2/skip":
            self._api_ingest2_skip()
        elif path == "/api/ingest2/correct":
            self._api_ingest2_correct()
        elif path == "/api/ingest2/search-card":
            self._api_ingest2_search_card()
        elif path == "/api/ingest2/update-cards":
            self._api_ingest2_update_cards()
        elif path == "/api/ingest2/add-card":
            self._api_ingest2_add_card()
        elif path == "/api/ingest2/remove-card":
            self._api_ingest2_remove_card()
        elif path == "/api/ingest2/delete":
            self._api_ingest2_delete()
        elif path == "/api/ingest2/reset":
            self._api_ingest2_reset()
        elif path == "/api/wishlist":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            self._api_wishlist_add(data)
        elif path == "/api/wishlist/bulk":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            self._api_wishlist_bulk_add(data)
        elif path == "/api/corners/detect":
            self._api_corners_detect()
        elif path == "/api/corners/commit":
            self._api_corners_commit()
        elif path == "/api/ingest-ids/resolve":
            self._api_ingest_ids_resolve()
        elif path == "/api/ingest-ids/commit":
            self._api_ingest_ids_commit()
        elif path == "/api/order/parse":
            self._api_order_parse()
        elif path == "/api/order/resolve":
            self._api_order_resolve()
        elif path == "/api/order/commit":
            self._api_order_commit()
        elif path.startswith("/api/collection/") and path.endswith("/receive"):
            cid = path[len("/api/collection/"):-len("/receive")]
            self._api_collection_receive(int(cid))
        elif path.startswith("/api/orders/") and path.endswith("/receive"):
            oid = path[len("/api/orders/"):-len("/receive")]
            self._api_order_receive(int(oid))
        elif path.startswith("/api/wishlist/") and path.endswith("/fulfill"):
            wid = path[len("/api/wishlist/"):-len("/fulfill")]
            self._api_wishlist_fulfill(int(wid))
        elif path == "/api/collection/bulk-delete":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            self._api_collection_bulk_delete(data)
        elif path.startswith("/api/collection/") and path.endswith("/dispose"):
            entry_id = path[len("/api/collection/"):-len("/dispose")]
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            self._api_collection_dispose(int(entry_id), data)
        elif path == "/api/import/parse":
            self._api_import_parse()
        elif path == "/api/import/resolve":
            self._api_import_resolve()
        elif path == "/api/import/commit":
            self._api_import_commit()
        # Sealed collection POST routes
        elif path == "/api/sealed/collection":
            data = self._read_json_body()
            if data is None:
                return
            self._api_sealed_collection_add(data)
        elif path.startswith("/api/sealed/collection/") and path.endswith("/dispose"):
            entry_id = path[len("/api/sealed/collection/"):-len("/dispose")]
            data = self._read_json_body()
            if data is None:
                return
            self._api_sealed_collection_dispose(int(entry_id), data)
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/settings":
            self._api_put_settings()
        elif path.startswith("/api/sealed/collection/"):
            entry_id = path[len("/api/sealed/collection/"):]
            data = self._read_json_body()
            if data is None:
                return
            self._api_sealed_collection_update(int(entry_id), data)
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path.startswith("/api/sealed/collection/"):
            entry_id = path[len("/api/sealed/collection/"):]
            confirm = params.get("confirm", [""])[0]
            if confirm != "true":
                self._send_json({"error": "Must pass ?confirm=true"}, 400)
                return
            self._api_sealed_collection_delete(int(entry_id))
        elif path.startswith("/api/collection/") and not path.startswith("/api/collection/bulk"):
            entry_id = path[len("/api/collection/"):]
            confirm = params.get("confirm", [""])[0]
            if confirm != "true":
                self._send_json({"error": "Must pass ?confirm=true"}, 400)
                return
            self._api_collection_delete(int(entry_id))
        elif path.startswith("/api/wishlist/"):
            wid = path[len("/api/wishlist/"):]
            self._api_wishlist_delete(int(wid))
        else:
            self._send_json({"error": "Not found"}, 404)

    _CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".ico": "image/x-icon",
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
        if not self.generator:
            self._send_json({"error": "AllPrintings.json not loaded — run: mtg data fetch"}, 503)
            return
        sets = self.generator.list_sets()
        self._send_json([{"code": code, "name": name} for code, name in sets])

    def _api_cached_sets(self):
        """Return all sets whose card list has been fully cached."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT set_code, set_name FROM sets WHERE cards_fetched_at IS NOT NULL ORDER BY set_name"
        )
        result = [{"code": row["set_code"], "name": row["set_name"]} for row in cursor]
        conn.close()
        self._send_json(result)

    def _api_products(self, set_code: str):
        if not self.generator:
            self._send_json({"error": "AllPrintings.json not loaded — run: mtg data fetch"}, 503)
            return
        if not set_code:
            self._send_json({"error": "Missing 'set' parameter"}, 400)
            return
        products = self.generator.list_products(set_code)
        self._send_json(products)

    def _api_sheets(self, set_code: str, product: str):
        if not self.generator:
            self._send_json({"error": "AllPrintings.json not loaded — run: mtg data fetch"}, 503)
            return
        if not set_code or not product:
            self._send_json({"error": "Missing 'set' or 'product' parameter"}, 400)
            return
        result = self.generator.get_sheet_data(set_code, product)

        # Attach local prices from SQLite
        for sheet in result["sheets"].values():
            for card in sheet["cards"]:
                foil = card.get("foil", False)
                price_type = "foil" if foil else "normal"
                sc = card.get("set_code", "").lower()
                cn = card.get("collector_number", "")
                card["ck_price"] = _get_sqlite_price(self.db_path, sc, cn, "cardkingdom", price_type)
                card["tcg_price"] = _get_sqlite_price(self.db_path, sc, cn, "tcgplayer", price_type)

        self._send_json(result)

    def _api_generate(self, data: dict):
        if not self.generator:
            self._send_json({"error": "AllPrintings.json not loaded — run: mtg data fetch"}, 503)
            return
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

            # Attach CK price from SQLite
            foil = card.get("foil", False)
            price_type = "foil" if foil else "normal"
            sc = card.get("set_code", "").lower()
            cn = card.get("collector_number", "")
            card["ck_price"] = _get_sqlite_price(self.db_path, sc, cn, "cardkingdom", price_type)

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
        filter_date_min = params.get("filter_date_min", [""])[0]
        filter_date_max = params.get("filter_date_max", [""])[0]
        filter_status = params.get("status", ["owned"])[0]
        filter_wanted = params.get("filter_wanted", [""])[0] == "true"
        _unowned_raw = params.get("include_unowned", [""])[0]
        include_unowned = _unowned_raw if _unowned_raw in ("base", "full") else ""

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # When including unowned cards, ensure each selected set is fully cached
        if include_unowned and filter_sets:
            from mtg_collector.db.models import CardRepository, PrintingRepository, SetRepository
            from mtg_collector.services.scryfall import ScryfallAPI, ensure_set_cached
            api = ScryfallAPI()
            card_repo = CardRepository(conn)
            set_repo = SetRepository(conn)
            printing_repo = PrintingRepository(conn)
            for sc in filter_sets:
                ensure_set_cached(api, sc, card_repo, set_repo, printing_repo, conn)

        where_clauses = []
        sql_params = []

        # Status filter (default: owned)
        # "owned" includes both owned and ordered cards so ordered items are visible
        # For include_unowned: applied in the LEFT JOIN ON clause (see query below)
        # so unowned cards (c.* IS NULL) aren't filtered out
        if not include_unowned and filter_status != "all":
            if filter_status == "owned":
                where_clauses.append("c.status IN ('owned', 'ordered')")
            else:
                where_clauses.append("c.status = ?")
                sql_params.append(filter_status)

        # Exclude non-collectible cards (digital-only, meld backs)
        if include_unowned:
            where_clauses.append("json_extract(p.raw_json, '$.digital') = 0")
            where_clauses.append(
                "NOT (json_extract(p.raw_json, '$.layout') = 'meld'"
                " AND p.collector_number LIKE '%b')"
            )

        if q:
            where_clauses.append("(card.name LIKE ? OR card.type_line LIKE ? OR json_extract(p.raw_json, '$.flavor_name') LIKE ?)")
            sql_params.append(f"%{q}%")
            sql_params.append(f"%{q}%")
            sql_params.append(f"%{q}%")

        if filter_colors:
            color_conditions = []
            for color in filter_colors:
                if color == "C":
                    color_conditions.append("(card.colors IS NULL OR card.colors = '[]')")
                else:
                    color_conditions.append("card.colors LIKE ?")
                    sql_params.append(f'%"{color}"%')
            where_clauses.append(f"({' AND '.join(color_conditions)})")

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
            if include_unowned == "full":
                # Full mode: filter on the expanded finish value
                where_clauses.append(f"f.value IN ({placeholders})")
                sql_params.extend(filter_finish)
            elif not include_unowned:
                # Normal mode: filter on collection finish
                where_clauses.append(f"c.finish IN ({placeholders})")
                sql_params.extend(filter_finish)
            # Base mode: skip finish filter (rows span all finishes)

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

        if filter_date_min and not include_unowned:
            where_clauses.append("c.acquired_at >= ?")
            sql_params.append(filter_date_min)
        if filter_date_max and not include_unowned:
            where_clauses.append("c.acquired_at < date(?, '+1 day')")
            sql_params.append(filter_date_max)

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
            "date_added": "c.acquired_at",
        }
        sort_col = sort_map.get(sort, "card.name")
        order_dir = "DESC" if order == "desc" else "ASC"

        # Wishlist filter: INNER JOIN to restrict results to wishlisted cards
        wanted_join = ""
        if filter_wanted:
            wanted_join = """
                    JOIN wishlist w ON (
                        w.scryfall_id = p.scryfall_id
                        OR (w.scryfall_id IS NULL AND w.oracle_id = card.oracle_id)
                    ) AND w.fulfilled_at IS NULL"""

        if include_unowned:
            if filter_status == "owned":
                join_status_sql = " AND c.status IN ('owned', 'ordered')"
                join_params = []
            elif filter_status != "all":
                join_status_sql = " AND c.status = ?"
                join_params = [filter_status]
            else:
                join_status_sql = ""
                join_params = []
            if include_unowned == "full":
                # Full set: one row per (scryfall_id, finish) via json_each
                query = f"""
                    SELECT
                        card.oracle_id, card.name, card.type_line, card.mana_cost, card.cmc,
                        card.colors, card.color_identity,
                        p.set_code, s.set_name, p.collector_number, p.rarity,
                        p.scryfall_id, p.image_uri, p.artist,
                        p.frame_effects, p.border_color, p.full_art, p.promo,
                        p.promo_types, p.finishes,
                        COALESCE(json_extract(p.raw_json, '$.flavor_name'), json_extract(p.raw_json, '$.card_faces[0].flavor_name')) as flavor_name,
                        json_extract(p.raw_json, '$.layout') as layout,
                        json_extract(p.raw_json, '$.card_faces[0].mana_cost') as face0_mana,
                        json_extract(p.raw_json, '$.card_faces[1].mana_cost') as face1_mana,
                        COALESCE(c.finish, f.value) as finish,
                        c.condition, c.status,
                        COALESCE(COUNT(DISTINCT c.id), 0) as qty,
                        MAX(c.acquired_at) as acquired_at,
                        CASE WHEN c.id IS NOT NULL THEN 1 ELSE 0 END as owned,
                        c.order_id,
                        o.seller_name as order_seller,
                        o.order_number as order_number,
                        o.order_date as order_date,
                        c.purchase_price,
                        GROUP_CONCAT(DISTINCT ii.id || '|' || il.card_index || '|' || ii.filename || '|' || ii.created_at) as ingest_lineage_raw
                    FROM printings p
                    JOIN cards card ON p.oracle_id = card.oracle_id
                    JOIN sets s ON p.set_code = s.set_code
                    CROSS JOIN json_each(p.finishes) AS f
                    LEFT JOIN collection c ON p.scryfall_id = c.scryfall_id AND c.finish = f.value{join_status_sql}
                    LEFT JOIN orders o ON c.order_id = o.id
                    LEFT JOIN ingest_lineage il ON il.collection_id = c.id
                    LEFT JOIN ingest_images ii ON il.image_md5 = ii.md5{wanted_join}
                    WHERE {where_sql}
                    GROUP BY p.scryfall_id, f.value
                    ORDER BY {sort_col} {order_dir}, card.name ASC
                """
            else:
                # Base set: one row per scryfall_id
                query = f"""
                    SELECT
                        card.oracle_id, card.name, card.type_line, card.mana_cost, card.cmc,
                        card.colors, card.color_identity,
                        p.set_code, s.set_name, p.collector_number, p.rarity,
                        p.scryfall_id, p.image_uri, p.artist,
                        p.frame_effects, p.border_color, p.full_art, p.promo,
                        p.promo_types, p.finishes,
                        COALESCE(json_extract(p.raw_json, '$.flavor_name'), json_extract(p.raw_json, '$.card_faces[0].flavor_name')) as flavor_name,
                        json_extract(p.raw_json, '$.layout') as layout,
                        json_extract(p.raw_json, '$.card_faces[0].mana_cost') as face0_mana,
                        json_extract(p.raw_json, '$.card_faces[1].mana_cost') as face1_mana,
                        c.finish, c.condition, c.status,
                        COALESCE(COUNT(DISTINCT c.id), 0) as qty,
                        MAX(c.acquired_at) as acquired_at,
                        CASE WHEN c.id IS NOT NULL THEN 1 ELSE 0 END as owned,
                        c.order_id,
                        o.seller_name as order_seller,
                        o.order_number as order_number,
                        o.order_date as order_date,
                        c.purchase_price,
                        GROUP_CONCAT(DISTINCT ii.id || '|' || il.card_index || '|' || ii.filename || '|' || ii.created_at) as ingest_lineage_raw
                    FROM printings p
                    JOIN cards card ON p.oracle_id = card.oracle_id
                    JOIN sets s ON p.set_code = s.set_code
                    LEFT JOIN collection c ON p.scryfall_id = c.scryfall_id{join_status_sql}
                    LEFT JOIN orders o ON c.order_id = o.id
                    LEFT JOIN ingest_lineage il ON il.collection_id = c.id
                    LEFT JOIN ingest_images ii ON il.image_md5 = ii.md5{wanted_join}
                    WHERE {where_sql}
                    GROUP BY p.scryfall_id
                    ORDER BY {sort_col} {order_dir}, card.name ASC
                """
            sql_params = join_params + sql_params
        else:
            query = f"""
                SELECT
                    card.oracle_id, card.name, card.type_line, card.mana_cost, card.cmc,
                    card.colors, card.color_identity,
                    p.set_code, s.set_name, p.collector_number, p.rarity,
                    p.scryfall_id, p.image_uri, p.artist,
                    p.frame_effects, p.border_color, p.full_art, p.promo,
                    p.promo_types, p.finishes,
                    COALESCE(json_extract(p.raw_json, '$.flavor_name'), json_extract(p.raw_json, '$.card_faces[0].flavor_name')) as flavor_name,
                    json_extract(p.raw_json, '$.layout') as layout,
                    json_extract(p.raw_json, '$.card_faces[0].mana_cost') as face0_mana,
                    json_extract(p.raw_json, '$.card_faces[1].mana_cost') as face1_mana,
                    c.finish, c.condition, c.status,
                    COUNT(DISTINCT c.id) as qty,
                    MAX(c.acquired_at) as acquired_at,
                    c.order_id,
                    o.seller_name as order_seller,
                    o.order_number as order_number,
                    o.order_date as order_date,
                    c.purchase_price,
                    GROUP_CONCAT(DISTINCT ii.id || '|' || il.card_index || '|' || ii.filename || '|' || ii.created_at) as ingest_lineage_raw
                FROM collection c
                JOIN printings p ON c.scryfall_id = p.scryfall_id
                JOIN cards card ON p.oracle_id = card.oracle_id
                JOIN sets s ON p.set_code = s.set_code
                LEFT JOIN orders o ON c.order_id = o.id
                LEFT JOIN ingest_lineage il ON il.collection_id = c.id
                LEFT JOIN ingest_images ii ON il.image_md5 = ii.md5{wanted_join}
                WHERE {where_sql}
                GROUP BY p.scryfall_id, c.finish, c.condition, c.status, c.order_id
                ORDER BY {sort_col} {order_dir}, card.name ASC
            """

        cursor = conn.execute(query, sql_params)
        rows = cursor.fetchall()

        results = []
        for row in rows:
            mana_cost = row["mana_cost"]
            if not mana_cost:
                face0 = row["face0_mana"] or ""
                face1 = row["face1_mana"] or ""
                if face0 or face1:
                    mana_cost = " // ".join(p for p in [face0, face1] if p)
            card = {
                "oracle_id": row["oracle_id"],
                "name": row["flavor_name"] or row["name"],
                "oracle_name": row["name"] if row["flavor_name"] and row["flavor_name"] != row["name"] else None,
                "type_line": row["type_line"],
                "mana_cost": mana_cost,
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
                "layout": row["layout"] or "normal",
                "finish": row["finish"],
                "condition": row["condition"],
                "status": row["status"],
                "qty": row["qty"],
                "acquired_at": row["acquired_at"],
                "owned": bool(row["owned"]) if include_unowned else True,
            }
            # Order info
            order_id = row["order_id"] if "order_id" in row.keys() else None
            if order_id:
                card["order_id"] = order_id
                card["order_seller"] = row["order_seller"]
                card["order_number"] = row["order_number"]
                card["order_date"] = row["order_date"]
                card["purchase_price"] = row["purchase_price"]
            # Ingest lineage (for "Correct" link)
            raw = row["ingest_lineage_raw"] if "ingest_lineage_raw" in row.keys() else None
            if raw:
                lineage = []
                for entry in raw.split(","):
                    parts = entry.split("|", 3)
                    lineage.append({
                        "image_id": int(parts[0]),
                        "card_idx": int(parts[1]),
                        "filename": parts[2],
                        "created_at": parts[3],
                    })
                card["ingest_lineage"] = lineage
                # Keep first entry for backwards compat
                card["ingest_image_id"] = lineage[0]["image_id"]
                card["ingest_card_idx"] = lineage[0]["card_idx"]
            card["tcg_price"] = None
            card["ck_price"] = None
            card["ck_url"] = ""
            results.append(card)

        # Prices via SQLite latest_prices
        for card in results:
            foil = card["finish"] in ("foil", "etched")
            price_type = "foil" if foil else "normal"
            sc = card["set_code"].lower()
            cn = card["collector_number"]
            card["ck_price"] = _get_sqlite_price(self.db_path, sc, cn, "cardkingdom", price_type)
            card["tcg_price"] = _get_sqlite_price(self.db_path, sc, cn, "tcgplayer", price_type)
            card["ck_url"] = self.generator.get_ck_url(card["scryfall_id"], foil) if self.generator else ""

        conn.close()
        self._send_json(results)

    def _api_card(self, scryfall_id: str):
        """Return full card data for a single printing by scryfall_id."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        row = conn.execute(
            """
            SELECT
                card.oracle_id, card.name, card.type_line, card.mana_cost, card.cmc,
                card.colors, card.color_identity,
                p.set_code, s.set_name, p.collector_number, p.rarity,
                p.scryfall_id, p.image_uri, p.artist,
                p.frame_effects, p.border_color, p.full_art, p.promo,
                p.promo_types, p.finishes,
                COALESCE(json_extract(p.raw_json, '$.flavor_name'), json_extract(p.raw_json, '$.card_faces[0].flavor_name')) as flavor_name,
                json_extract(p.raw_json, '$.layout') as layout,
                json_extract(p.raw_json, '$.card_faces[0].mana_cost') as face0_mana,
                json_extract(p.raw_json, '$.card_faces[1].mana_cost') as face1_mana
            FROM printings p
            JOIN cards card ON p.oracle_id = card.oracle_id
            JOIN sets s ON p.set_code = s.set_code
            WHERE p.scryfall_id = ?
            """,
            (scryfall_id,),
        ).fetchone()

        if not row:
            conn.close()
            self._send_json({"error": "Card not found"}, 404)
            return

        mana_cost = row["mana_cost"]
        if not mana_cost:
            face0 = row["face0_mana"] or ""
            face1 = row["face1_mana"] or ""
            if face0 or face1:
                mana_cost = " // ".join(p for p in [face0, face1] if p)

        result = {
            "oracle_id": row["oracle_id"],
            "name": row["flavor_name"] or row["name"],
            "oracle_name": row["name"] if row["flavor_name"] and row["flavor_name"] != row["name"] else None,
            "type_line": row["type_line"],
            "mana_cost": mana_cost,
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
            "layout": row["layout"] or "normal",
        }

        # Prices from SQLite
        sc = row["set_code"].lower()
        cn = row["collector_number"]
        result["ck_price"] = _get_sqlite_price(self.db_path, sc, cn, "cardkingdom", "normal")
        result["tcg_price"] = _get_sqlite_price(self.db_path, sc, cn, "tcgplayer", "normal")
        result["ck_url"] = self.generator.get_ck_url(scryfall_id, False) if self.generator else ""

        conn.close()
        self._send_json(result)

    def _api_prices_status(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        log = conn.execute(
            "SELECT fetched_at FROM price_fetch_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        conn.close()
        if log and count > 0:
            self._send_json({"available": True, "last_modified": log["fetched_at"]})
        else:
            self._send_json({"available": False, "last_modified": None})

    def _api_price_history(self, set_code: str, collector_number: str):
        """Return full price time series for a card."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT source, price_type, price, observed_at FROM prices "
            "WHERE set_code = ? AND collector_number = ? ORDER BY observed_at",
            (set_code.lower(), collector_number),
        ).fetchall()
        conn.close()

        result: dict[str, list] = {}
        for row in rows:
            key = f"{row['source']}_{row['price_type']}"
            result.setdefault(key, []).append({
                "date": row["observed_at"],
                "price": row["price"],
            })
        self._send_json(result)

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
            from mtg_collector.cli.data_cmd import _fetch_prices as fetch_prices_cmd
            fetch_prices_cmd(force=True)
            # Return updated status
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            log = conn.execute(
                "SELECT fetched_at FROM price_fetch_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
            last_modified = log["fetched_at"] if log else None
            self._send_json({"available": bool(log), "last_modified": last_modified})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # ── Ingest2 API endpoints (DB-backed) ──

    def _ingest2_db(self):
        """Get a DB connection with schema init."""
        from mtg_collector.db.schema import init_db
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)
        return conn

    def _ingest2_load_image(self, conn, image_id):
        """Load an ingest_images row as dict."""
        row = conn.execute("SELECT * FROM ingest_images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    def _ingest2_update_image(self, conn, image_id, **updates):
        """Update columns on an ingest_images row."""
        from mtg_collector.utils import now_iso
        updates["updated_at"] = now_iso()
        set_clauses = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [image_id]
        conn.execute(f"UPDATE ingest_images SET {set_clauses} WHERE id = ?", values)
        conn.commit()

    def _api_ingest2_counts(self):
        """Return counts per status for badge display."""
        conn = self._ingest2_db()
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM ingest_images GROUP BY status"
        ).fetchall()
        conn.close()
        counts = {row["status"]: row["cnt"] for row in rows}
        self._send_json(counts)

    def _api_ingest2_recent(self, params):
        """Return images from last N hours with computed status info.

        Optional ?id=X filters to a single image (for per-image polling).
        """
        hours = float(params.get("hours", ["2"])[0])
        image_id = params.get("id", [None])[0]
        conn = self._ingest2_db()
        where = "WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)"
        args = [f"-{int(hours * 3600)} seconds"]
        if image_id is not None:
            where += " AND id = ?"
            args.append(int(image_id))
        rows = conn.execute(
            f"""SELECT id, filename, stored_name, status, error_message,
                      ocr_result, claude_result, scryfall_matches, disambiguated,
                      created_at, updated_at
               FROM ingest_images
               {where}
               ORDER BY id DESC""",
            args,
        ).fetchall()
        conn.close()

        result = []
        for r in rows:
            d = dict(r)
            # Compute card counts
            claude_result = json.loads(d["claude_result"]) if d.get("claude_result") else []
            disambiguated = json.loads(d["disambiguated"]) if d.get("disambiguated") else []
            total_cards = len(disambiguated) if disambiguated else len(claude_result)
            done_count = sum(1 for s in disambiguated if s is not None) if disambiguated else 0
            pending_count = total_cards - done_count

            # Compute border_status
            status = d["status"]
            if status in ("READY_FOR_OCR", "PROCESSING"):
                border_status = "processing"
            elif status == "ERROR":
                border_status = "error"
            elif status == "DONE":
                border_status = "done"
            elif status == "READY_FOR_DISAMBIGUATION":
                border_status = "needs_disambiguation"
            else:
                border_status = "processing"

            # Extract card summaries — use confirmed scryfall name when
            # available so corrections are reflected on the recent page.
            scryfall_matches = json.loads(d["scryfall_matches"]) if d.get("scryfall_matches") else []
            ocr_fragments = json.loads(d["ocr_result"]) if d.get("ocr_result") else []
            cards_summary = []
            for idx, card in enumerate(claude_result):
                sid = disambiguated[idx] if idx < len(disambiguated) else None
                resolved = None
                if sid and sid != "skipped" and idx < len(scryfall_matches):
                    resolved = next((c for c in scryfall_matches[idx] if c.get("scryfall_id") == sid), None)
                if resolved:
                    entry = {
                        "name": resolved.get("name", card.get("name", "")),
                        "set_code": (resolved.get("set_code") or card.get("set_code") or "").upper(),
                    }
                else:
                    entry = {
                        "name": card.get("name", ""),
                        "set_code": (card.get("set_code") or "").upper(),
                    }
                # OCR name: topmost fragments for this card, merging nearby bboxes
                entry["ocr_name"] = _extract_ocr_name(ocr_fragments, card.get("fragment_indices", []))
                entry["claude_name"] = card.get("name", "")
                cards_summary.append(entry)

            result.append({
                "id": d["id"],
                "filename": d["filename"],
                "stored_name": d["stored_name"],
                "status": status,
                "border_status": border_status,
                "error_message": d["error_message"],
                "total_cards": total_cards,
                "done_count": done_count,
                "pending_count": pending_count,
                "cards": cards_summary,
                "created_at": d["created_at"],
                "updated_at": d["updated_at"],
            })
        self._send_json(result)

    def _api_ingest2_usage_stats(self, params):
        """Aggregate API token usage and estimated cost over a time window."""
        hours = float(params.get("hours", ["24"])[0])
        conn = self._ingest2_db()
        rows = conn.execute(
            """SELECT api_usage FROM ingest_images
               WHERE api_usage IS NOT NULL
               AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)""",
            (f"-{int(hours * 3600)} seconds",),
        ).fetchall()
        conn.close()

        totals = {
            "haiku": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
            "sonnet": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
            "opus": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        }
        images_with_usage = 0
        for row in rows:
            u = json.loads(row["api_usage"])
            for model in ("haiku", "sonnet", "opus"):
                if model in u:
                    totals[model]["input"] += u[model].get("input", 0)
                    totals[model]["output"] += u[model].get("output", 0)
                    totals[model]["cache_read"] += u[model].get("cache_read", 0)
                    totals[model]["cache_creation"] += u[model].get("cache_creation", 0)
            images_with_usage += 1

        # Per-million-token pricing (cache_read = 10% of input, cache_creation = 125% of input)
        PRICES = {
            "haiku":  {"input": 0.80,  "output": 4.00},
            "sonnet": {"input": 3.00,  "output": 15.00},
            "opus":   {"input": 15.00, "output": 75.00},
        }
        estimated_cost = sum(
            totals[m]["input"]  * PRICES[m]["input"]  / 1_000_000 +
            totals[m]["output"] * PRICES[m]["output"] / 1_000_000 +
            totals[m]["cache_read"] * PRICES[m]["input"] * 0.1 / 1_000_000 +
            totals[m]["cache_creation"] * PRICES[m]["input"] * 1.25 / 1_000_000
            for m in PRICES
        )
        self._send_json({
            "images_with_usage": images_with_usage,
            "usage": totals,
            "estimated_cost_usd": round(estimated_cost, 6),
        })

    def _api_ingest2_pending_disambiguation(self):
        """Return flat list of all cards needing disambiguation across all images."""
        conn = self._ingest2_db()
        rows = conn.execute(
            """SELECT id, filename, stored_name, claude_result, scryfall_matches,
                      crops, disambiguated
               FROM ingest_images
               WHERE status = 'READY_FOR_DISAMBIGUATION'
               ORDER BY id""",
        ).fetchall()
        conn.close()

        pending = []
        for r in rows:
            d = dict(r)
            claude_result = json.loads(d["claude_result"]) if d.get("claude_result") else []
            scryfall_matches = json.loads(d["scryfall_matches"]) if d.get("scryfall_matches") else []
            crops = json.loads(d["crops"]) if d.get("crops") else []
            disambiguated = json.loads(d["disambiguated"]) if d.get("disambiguated") else []

            for card_idx, status in enumerate(disambiguated):
                if status is not None:
                    continue
                pending.append({
                    "image_id": d["id"],
                    "card_idx": card_idx,
                    "image_filename": d["stored_name"],
                    "card_info": claude_result[card_idx] if card_idx < len(claude_result) else {},
                    "candidates": scryfall_matches[card_idx] if card_idx < len(scryfall_matches) else [],
                    "crop": crops[card_idx] if card_idx < len(crops) else None,
                })
        self._send_json(pending)

    def _api_ingest2_images(self, params):
        """List images filtered by status."""
        status = params.get("status", [""])[0]
        conn = self._ingest2_db()
        if status:
            rows = conn.execute(
                "SELECT id, filename, stored_name, md5, status, mode, claude_result, error_message, created_at, updated_at FROM ingest_images WHERE status = ? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, filename, stored_name, md5, status, mode, claude_result, error_message, created_at, updated_at FROM ingest_images ORDER BY id"
            ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            # Add claude_count without sending full claude_result
            cr = d.pop("claude_result", None)
            if cr:
                cards = json.loads(cr)
                d["claude_count"] = len(cards)
            else:
                d["claude_count"] = None
            result.append(d)
        self._send_json(result)

    def _api_ingest2_image_detail(self, image_id):
        """Get full state for one image."""
        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        conn.close()
        if not img:
            self._send_json({"error": "Not found"}, 404)
            return
        # Parse JSON fields
        for field in ("ocr_result", "claude_result", "agent_trace", "scryfall_matches", "crops",
                      "disambiguated", "names_data", "names_disambiguated", "user_card_edits"):
            if img.get(field):
                img[field] = json.loads(img[field])
        # Pre-compute ocr_name and claude_name per card
        ocr_fragments = img.get("ocr_result") or []
        claude_cards = img.get("claude_result") or []
        card_names = []
        for card in claude_cards:
            card_names.append({
                "ocr_name": _extract_ocr_name(ocr_fragments, card.get("fragment_indices", [])),
                "claude_name": card.get("name", ""),
            })
        img["card_names"] = card_names
        self._send_json(img)

    def _api_ingest2_upload(self):
        """Upload files and create DB rows."""
        from mtg_collector.utils import now_iso

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        boundary = content_type.split("boundary=")[1].strip()
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        uploaded = []
        collisions = []
        parts = body.split(f"--{boundary}".encode())

        conn = self._ingest2_db()
        ts = now_iso()

        for part in parts:
            if not part or part.strip() == b"--" or part.strip() == b"":
                continue

            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            header_bytes = part[:header_end]
            file_content = part[header_end + 4:]
            if file_content.endswith(b"\r\n"):
                file_content = file_content[:-2]

            header_str = header_bytes.decode("utf-8", errors="replace")

            filename_match = re.search(r'filename="([^"]+)"', header_str)
            if not filename_match:
                continue

            original_name = filename_match.group(1)
            ext = Path(original_name).suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                continue

            stored_name = original_name
            dest = _get_ingest_images_dir() / stored_name

            if dest.exists():
                collisions.append(original_name)
                continue

            dest.write_bytes(file_content)

            md5 = _md5_file(str(dest))

            cursor = conn.execute(
                """INSERT INTO ingest_images (filename, stored_name, md5, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'READY_FOR_OCR', ?, ?)""",
                (original_name, stored_name, md5, ts, ts),
            )
            image_id = cursor.lastrowid
            conn.commit()

            _log_ingest(f"Upload2: {original_name} -> {stored_name} (ID={image_id}, MD5={md5})")
            uploaded.append({
                "id": image_id,
                "filename": original_name,
                "stored_name": stored_name,
                "md5": md5,
            })

            # Submit for background processing
            if _ingest_executor is not None:
                _ingest_executor.submit(_process_image_background, self.db_path, image_id)

        conn.close()
        self._send_json({"uploaded": uploaded, "collisions": collisions})

    def _api_ingest2_set_params(self):
        """Set mode for an image."""
        data = self._read_json_body()
        if data is None:
            return
        image_id = data.get("image_id")
        conn = self._ingest2_db()
        updates = {}
        if "mode" in data:
            updates["mode"] = data["mode"]
        if updates:
            self._ingest2_update_image(conn, image_id, **updates)
        conn.close()
        self._send_json({"ok": True})

    def _api_ingest2_process_sse(self, image_id):
        """SSE endpoint: process one image through OCR -> Claude -> Scryfall, DB-backed."""
        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        if not img:
            conn.close()
            self._send_json({"error": "Image not found"}, 404)
            return

        # Stale processing recovery
        if img["status"] == "PROCESSING":
            from datetime import datetime, timezone

            updated = datetime.fromisoformat(img["updated_at"].replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - updated).total_seconds() > 600:
                self._ingest2_update_image(conn, image_id, status="READY_FOR_OCR")
                img["status"] = "READY_FOR_OCR"

        if img["status"] not in ("READY_FOR_OCR",):
            conn.close()
            self._send_json({"error": f"Image status is {img['status']}, not READY_FOR_OCR"}, 400)
            return

        # Mark as processing
        self._ingest2_update_image(conn, image_id, status="PROCESSING")

        # Set up SSE response
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send_event(event_type, data_obj):
            payload = f"event: {event_type}\ndata: {json.dumps(data_obj)}\n\n"
            try:
                self.wfile.write(payload.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        try:
            self._process_image2_sse(conn, image_id, img, send_event)
            send_event("done", {})
        except Exception as e:
            _log_ingest(f"Error processing image {image_id}: {e}")
            send_event("error", {"message": str(e)})
            partial_trace = getattr(e, "agent_trace", [])
            self._ingest2_update_image(
                conn, image_id,
                status="READY_FOR_OCR",
                agent_trace=json.dumps(partial_trace) if partial_trace else None,
                error_message=str(e),
            )
            send_event("done", {"error": True})
        conn.close()

    def _process_image2_sse(self, conn, image_id, img, send_event):
        """Process a single image: OCR -> Claude -> Scryfall, streaming SSE events. DB-backed."""
        ocr_fragments, claude_cards, all_matches, all_crops, disambiguated, agent_trace, api_usage = _process_image_core(
            conn, image_id, img, send_event,
        )

        # Save all state to DB
        self._ingest2_update_image(conn, image_id,
            status="READY_FOR_DISAMBIGUATION",
            ocr_result=json.dumps(ocr_fragments),
            claude_result=json.dumps(claude_cards),
            agent_trace=json.dumps(agent_trace),
            api_usage=json.dumps(api_usage) if api_usage else None,
            scryfall_matches=json.dumps(all_matches),
            crops=json.dumps(all_crops),
            disambiguated=json.dumps(disambiguated),
        )

        # Build matches_ready payload
        already_ingested = {ci for ci, d in enumerate(disambiguated) if d == "already_ingested"}
        cards_payload = []
        for ci, card_info in enumerate(claude_cards):
            cards_payload.append({
                "card_info": card_info,
                "candidates": all_matches[ci] if ci < len(all_matches) else [],
                "crop": all_crops[ci] if ci < len(all_crops) else None,
                "already_ingested": ci in already_ingested,
            })

        send_event("matches_ready", {"cards": cards_payload})

    def _api_ingest2_next_card(self, image_id):
        """Find the next undisambiguated card for an image. Auto-confirms single-candidate cards."""
        from mtg_collector.db.models import (
            CollectionEntry,
            CollectionRepository,
            PrintingRepository,
        )
        from mtg_collector.utils import now_iso

        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        if not img:
            conn.close()
            self._send_json({"error": "Image not found"}, 404)
            return

        disambiguated = json.loads(img["disambiguated"]) if img.get("disambiguated") else []
        scryfall_matches = json.loads(img["scryfall_matches"]) if img.get("scryfall_matches") else []
        crops = json.loads(img["crops"]) if img.get("crops") else []
        claude_result = json.loads(img["claude_result"]) if img.get("claude_result") else []

        auto_confirmed = 0

        for card_idx, status in enumerate(disambiguated):
            if status is not None:
                continue

            candidates = scryfall_matches[card_idx] if card_idx < len(scryfall_matches) else []

            # Auto-confirm single-candidate cards
            if len(candidates) == 1:
                c = candidates[0]
                scryfall_id = c["scryfall_id"]

                printing_repo = PrintingRepository(conn)
                collection_repo = CollectionRepository(conn)

                printing = printing_repo.get(scryfall_id)
                if printing:
                    entry = CollectionEntry(
                        id=None,
                        scryfall_id=scryfall_id,
                        finish="nonfoil",
                        condition="Near Mint",
                        source="ocr_ingest",
                    )
                    entry_id = collection_repo.add(entry)

                    md5 = img["md5"]
                    conn.execute(
                        """INSERT INTO ingest_lineage (collection_id, image_md5, image_path, card_index, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (entry_id, md5, img["stored_name"], card_idx, now_iso()),
                    )

                    disambiguated[card_idx] = scryfall_id
                    self._ingest2_update_image(conn, image_id, disambiguated=json.dumps(disambiguated))
                    conn.commit()

                    _log_ingest(f"Auto-confirmed: {scryfall_id} ({c.get('set_code', '???').upper()} #{c.get('collector_number', '???')})")
                    auto_confirmed += 1
                    continue

            # Card needs human input
            total_cards = len(disambiguated)
            total_done = sum(1 for s in disambiguated if s is not None)

            # Check if all done after auto-confirms
            if all(d is not None for d in disambiguated):
                self._ingest2_update_image(conn, image_id, status="DONE")

            conn.close()
            self._send_json({
                "done": False,
                "image_id": image_id,
                "card_idx": card_idx,
                "image_filename": img["stored_name"],
                "card": claude_result[card_idx] if card_idx < len(claude_result) else {},
                "candidates": candidates,
                "crop": crops[card_idx] if card_idx < len(crops) else None,
                "total_cards": total_cards,
                "total_done": total_done,
                "auto_confirmed": auto_confirmed,
            })
            return

        # All cards done (possibly all auto-confirmed)
        if all(d is not None for d in disambiguated):
            self._ingest2_update_image(conn, image_id, status="DONE")
            conn.commit()

        total_cards = len(disambiguated)
        total_done = sum(1 for s in disambiguated if s is not None)
        conn.close()
        self._send_json({"done": True, "total_cards": total_cards, "total_done": total_done, "auto_confirmed": auto_confirmed})

    def _api_ingest2_confirm(self):
        """Confirm a card: add to collection + ingest_lineage."""
        from mtg_collector.db.models import (
            CollectionEntry,
            CollectionRepository,
            PrintingRepository,
        )
        from mtg_collector.utils import now_iso

        data = self._read_json_body()
        if data is None:
            return

        image_id = data["image_id"]
        card_idx = data["card_idx"]
        scryfall_id = data["scryfall_id"]
        finish = data.get("finish", "nonfoil")

        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        if not img:
            conn.close()
            self._send_json({"error": "Image not found"}, 404)
            return

        printing_repo = PrintingRepository(conn)
        collection_repo = CollectionRepository(conn)

        printing = printing_repo.get(scryfall_id)
        if not printing:
            conn.close()
            self._send_json({"error": f"Printing {scryfall_id} not in local cache"}, 404)
            return

        entry = CollectionEntry(
            id=None,
            scryfall_id=scryfall_id,
            finish=finish,
            condition="Near Mint",
            source="ocr_ingest",
        )
        entry_id = collection_repo.add(entry)

        md5 = img["md5"]
        conn.execute(
            """INSERT INTO ingest_lineage (collection_id, image_md5, image_path, card_index, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entry_id, md5, img["stored_name"], card_idx, now_iso()),
        )

        # Update disambiguated
        disambiguated = json.loads(img["disambiguated"]) if img.get("disambiguated") else []
        if card_idx < len(disambiguated):
            disambiguated[card_idx] = scryfall_id
        self._ingest2_update_image(conn, image_id, disambiguated=json.dumps(disambiguated))

        # Check if all cards done
        if all(d is not None for d in disambiguated):
            self._ingest2_update_image(conn, image_id, status="DONE")

        conn.commit()
        conn.close()

        name = printing.raw_json and json.loads(printing.raw_json).get("name", "???") or "???"
        set_code = printing.set_code
        cn = printing.collector_number
        _log_ingest(f"Confirmed2: {name} ({set_code.upper()} #{cn}) -> collection ID {entry_id}")

        self._send_json({"ok": True, "entry_id": entry_id, "name": name, "set_code": set_code, "collector_number": cn})

    def _api_ingest2_add_card(self):
        """Add a new card slot to an existing image and confirm it."""
        from mtg_collector.db.models import (
            CollectionEntry,
            CollectionRepository,
            PrintingRepository,
        )
        from mtg_collector.utils import now_iso

        data = self._read_json_body()
        if data is None:
            return

        image_id = data["image_id"]
        scryfall_id = data["scryfall_id"]
        finish = data.get("finish", "nonfoil")

        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        if not img:
            conn.close()
            self._send_json({"error": "Image not found"}, 404)
            return

        # Append to all parallel arrays
        disambiguated = json.loads(img["disambiguated"]) if img.get("disambiguated") else []
        scryfall_matches = json.loads(img["scryfall_matches"]) if img.get("scryfall_matches") else []
        claude_result = json.loads(img["claude_result"]) if img.get("claude_result") else []
        crops = json.loads(img["crops"]) if img.get("crops") else []

        disambiguated.append(None)
        scryfall_matches.append([])
        claude_result.append({})
        crops.append(None)

        card_idx = len(disambiguated) - 1

        # Look up in local DB
        printing_repo = PrintingRepository(conn)
        collection_repo = CollectionRepository(conn)

        printing = printing_repo.get(scryfall_id)
        if not printing:
            conn.close()
            self._send_json({"error": f"Printing {scryfall_id} not in local cache"}, 404)
            return

        card_data = printing.get_scryfall_data()

        # Create collection entry
        entry = CollectionEntry(
            id=None,
            scryfall_id=scryfall_id,
            finish=finish,
            condition="Near Mint",
            source="ocr_ingest",
        )
        entry_id = collection_repo.add(entry)

        # Insert ingest_lineage
        md5 = img["md5"]
        conn.execute(
            """INSERT INTO ingest_lineage (collection_id, image_md5, image_path, card_index, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entry_id, md5, img["stored_name"], card_idx, now_iso()),
        )

        # Update disambiguated and prepend to scryfall_matches
        disambiguated[card_idx] = scryfall_id
        scryfall_matches[card_idx] = _format_candidates([card_data]) if card_data else []

        # Check if all done
        status_update = {}
        if all(d is not None for d in disambiguated):
            status_update["status"] = "DONE"

        self._ingest2_update_image(
            conn, image_id,
            disambiguated=json.dumps(disambiguated),
            scryfall_matches=json.dumps(scryfall_matches),
            claude_result=json.dumps(claude_result),
            crops=json.dumps(crops),
            **status_update,
        )

        conn.commit()
        conn.close()

        name = card_data.get("name", "???") if card_data else "???"
        set_code = printing.set_code
        _log_ingest(f"AddCard: {name} ({set_code.upper()}) -> collection ID {entry_id}, image {image_id} slot {card_idx}")

        self._send_json({"ok": True, "entry_id": entry_id, "name": name, "set_code": set_code, "card_idx": card_idx})

    def _api_ingest2_remove_card(self):
        """Remove a card slot from an image. If confirmed, also remove from collection."""
        data = self._read_json_body()
        if data is None:
            return

        image_id = data["image_id"]
        card_idx = data["card_idx"]

        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        if not img:
            conn.close()
            self._send_json({"error": "Image not found"}, 404)
            return

        disambiguated = json.loads(img["disambiguated"]) if img.get("disambiguated") else []
        scryfall_matches = json.loads(img["scryfall_matches"]) if img.get("scryfall_matches") else []
        claude_result = json.loads(img["claude_result"]) if img.get("claude_result") else []
        crops = json.loads(img["crops"]) if img.get("crops") else []

        if card_idx < 0 or card_idx >= len(disambiguated):
            conn.close()
            self._send_json({"error": "Invalid card index"}, 400)
            return

        # If this card was confirmed, remove collection entry + lineage
        sid = disambiguated[card_idx]
        removed_collection = False
        if sid and sid != "skipped":
            md5 = img["md5"]
            lineage = conn.execute(
                "SELECT collection_id FROM ingest_lineage WHERE image_md5 = ? AND card_index = ?",
                (md5, card_idx),
            ).fetchone()
            if lineage:
                conn.execute("DELETE FROM ingest_lineage WHERE image_md5 = ? AND card_index = ?", (md5, card_idx))
                conn.execute("DELETE FROM collection WHERE id = ?", (lineage["collection_id"],))
                removed_collection = True

        # Remove from all parallel arrays
        disambiguated.pop(card_idx)
        if card_idx < len(scryfall_matches):
            scryfall_matches.pop(card_idx)
        if card_idx < len(claude_result):
            claude_result.pop(card_idx)
        if card_idx < len(crops):
            crops.pop(card_idx)

        # Fix card_index values in ingest_lineage for shifted slots
        conn.execute(
            "UPDATE ingest_lineage SET card_index = card_index - 1 WHERE image_md5 = ? AND card_index > ?",
            (img["md5"], card_idx),
        )

        # Determine status
        status_update = {}
        if len(disambiguated) == 0:
            status_update["status"] = "DONE"
        elif all(d is not None for d in disambiguated):
            status_update["status"] = "DONE"
        else:
            status_update["status"] = "READY_FOR_DISAMBIGUATION"

        self._ingest2_update_image(
            conn, image_id,
            disambiguated=json.dumps(disambiguated),
            scryfall_matches=json.dumps(scryfall_matches),
            claude_result=json.dumps(claude_result),
            crops=json.dumps(crops),
            **status_update,
        )

        conn.commit()
        conn.close()

        _log_ingest(f"RemoveCard: image {image_id} slot {card_idx}, collection_removed={removed_collection}")
        self._send_json({"ok": True, "removed_collection": removed_collection})

    def _api_ingest2_skip(self):
        """Skip a card."""
        data = self._read_json_body()
        if data is None:
            return
        image_id = data["image_id"]
        card_idx = data["card_idx"]

        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        if not img:
            conn.close()
            self._send_json({"error": "Image not found"}, 404)
            return

        disambiguated = json.loads(img["disambiguated"]) if img.get("disambiguated") else []
        if card_idx < len(disambiguated):
            disambiguated[card_idx] = "skipped"
        self._ingest2_update_image(conn, image_id, disambiguated=json.dumps(disambiguated))

        if all(d is not None for d in disambiguated):
            self._ingest2_update_image(conn, image_id, status="DONE")

        conn.close()
        self._send_json({"ok": True})

    def _api_ingest2_correct(self):
        """Correct a mis-identified card: swap collection entry."""
        from mtg_collector.db.models import (
            CollectionEntry,
            CollectionRepository,
            PrintingRepository,
        )

        data = self._read_json_body()
        if data is None:
            return

        image_id = data["image_id"]
        card_idx = data["card_idx"]
        scryfall_id = data["scryfall_id"]
        finish = data.get("finish", "nonfoil")

        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        if not img:
            conn.close()
            self._send_json({"error": "Image not found"}, 404)
            return

        md5 = img["md5"]

        # Find existing ingest_lineage entry for this image+card_idx
        lineage = conn.execute(
            "SELECT collection_id FROM ingest_lineage WHERE image_md5 = ? AND card_index = ?",
            (md5, card_idx),
        ).fetchone()
        if not lineage:
            conn.close()
            self._send_json({"error": "No existing collection entry found for this card"}, 404)
            return

        old_collection_id = lineage["collection_id"]

        # Look up in local DB
        printing_repo = PrintingRepository(conn)
        collection_repo = CollectionRepository(conn)

        printing = printing_repo.get(scryfall_id)
        if not printing:
            conn.close()
            self._send_json({"error": f"Printing {scryfall_id} not in local cache"}, 404)
            return

        card_data = printing.get_scryfall_data()

        # Create new collection entry
        entry = CollectionEntry(
            id=None,
            scryfall_id=scryfall_id,
            finish=finish,
            condition="Near Mint",
            source="ocr_ingest",
        )
        entry_id = collection_repo.add(entry)

        # Update ingest_lineage to point to new entry BEFORE deleting old (FK constraint)
        conn.execute(
            "UPDATE ingest_lineage SET collection_id = ? WHERE image_md5 = ? AND card_index = ?",
            (entry_id, md5, card_idx),
        )

        # Now safe to delete old collection entry
        collection_repo.delete(old_collection_id)

        # Update disambiguated
        disambiguated = json.loads(img["disambiguated"]) if img.get("disambiguated") else []
        if card_idx < len(disambiguated):
            disambiguated[card_idx] = scryfall_id

        # Ensure corrected card is in scryfall_matches so recent detail can display it
        scryfall_matches = json.loads(img["scryfall_matches"]) if img.get("scryfall_matches") else []
        if card_idx < len(scryfall_matches):
            existing_ids = {c["scryfall_id"] for c in scryfall_matches[card_idx]}
            if scryfall_id not in existing_ids:
                formatted = _format_candidates([card_data]) if card_data else []
                scryfall_matches[card_idx] = formatted + scryfall_matches[card_idx]

        self._ingest2_update_image(
            conn, image_id,
            disambiguated=json.dumps(disambiguated),
            scryfall_matches=json.dumps(scryfall_matches),
        )

        conn.commit()
        conn.close()

        name = card_data.get("name", "???") if card_data else "???"
        set_code = printing.set_code
        _log_ingest(f"Corrected: {name} ({set_code.upper()}) -> collection ID {entry_id} (replaced {old_collection_id})")

        self._send_json({"ok": True, "entry_id": entry_id, "name": name, "set_code": set_code})

    def _api_ingest2_search_card(self):
        """Manual card search during disambiguation."""
        data = self._read_json_body()
        if data is None:
            return

        image_id = data.get("image_id")
        card_idx = data.get("card_idx")
        query = (data.get("query") or "").strip()

        if not query:
            self._send_json({"error": "Empty query"}, 400)
            return

        conn = self._ingest2_db()
        results = _local_name_search(conn, query)
        formatted = _format_candidates(results)

        # Update scryfall_matches in DB if image_id provided
        if image_id is not None and card_idx is not None:
            img = self._ingest2_load_image(conn, image_id)
            if img and img.get("scryfall_matches"):
                matches = json.loads(img["scryfall_matches"])
                if card_idx < len(matches):
                    matches[card_idx] = formatted
                    self._ingest2_update_image(conn, image_id, scryfall_matches=json.dumps(matches))
        conn.close()

        self._send_json({"candidates": formatted})

    def _api_ingest2_update_cards(self):
        """Stage 3.1: save corrected card list after count mismatch resolution."""
        data = self._read_json_body()
        if data is None:
            return

        image_id = data["image_id"]
        corrected_cards = data["cards"]  # [{name, set_code, collector_number, ...}]

        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        if not img:
            conn.close()
            self._send_json({"error": "Image not found"}, 404)
            return

        ocr_fragments = json.loads(img["ocr_result"]) if img.get("ocr_result") else []

        # Resolve corrected card list against local DB
        from mtg_collector.cli.ingest_ocr import _build_scryfall_query
        from mtg_collector.db.models import PrintingRepository
        printing_repo = PrintingRepository(conn)

        all_matches = []
        all_crops = []
        for ci, card_info in enumerate(corrected_cards):
            set_code, cn_or_query = _build_scryfall_query(card_info, {})
            candidates = []

            if set_code and cn_or_query:
                cn_raw = cn_or_query
                cn_stripped = cn_raw.lstrip("0") or "0"
                printing = printing_repo.get_by_set_cn(set_code, cn_stripped)
                if not printing:
                    printing = printing_repo.get_by_set_cn(set_code, cn_raw)
                if printing:
                    card_data = printing.get_scryfall_data()
                    if card_data:
                        candidates = [card_data]

            if not candidates:
                name = card_info.get("name")
                if name:
                    candidates = _local_name_search(conn, name, set_code=card_info.get("set_code"))

            formatted = _format_candidates(candidates)
            all_matches.append(formatted)

            frag_indices = card_info.get("fragment_indices", [])
            crop = _compute_card_crop(ocr_fragments, frag_indices)
            all_crops.append(crop)

        disambiguated = [None] * len(corrected_cards)

        self._ingest2_update_image(conn, image_id,
            claude_result=json.dumps(corrected_cards),
            scryfall_matches=json.dumps(all_matches),
            crops=json.dumps(all_crops),
            disambiguated=json.dumps(disambiguated),
            user_card_edits=json.dumps(corrected_cards),
            status="READY_FOR_DISAMBIGUATION",
        )
        conn.close()

        self._send_json({"ok": True, "card_count": len(corrected_cards)})

    def _api_ingest2_reset(self):
        """Reset an image: clear all artifacts + ingest_cache, remove ingested collection entries, requeue for processing."""
        from mtg_collector.utils import now_iso

        data = self._read_json_body()
        if data is None:
            return

        image_id = data["image_id"]
        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        if not img:
            conn.close()
            self._send_json({"error": "Image not found"}, 404)
            return

        _reset_ingest_image(conn, image_id, img["md5"], now_iso())
        conn.commit()
        conn.close()

        _log_ingest(f"Reset image {image_id}: {img['filename']} — requeued for processing")

        # Submit for background processing
        if _ingest_executor is not None:
            _ingest_executor.submit(_process_image_background, self.db_path, image_id)

        self._send_json({"ok": True})

    def _api_ingest2_delete(self):
        """Delete an image and its file."""
        data = self._read_json_body()
        if data is None:
            return

        image_id = data["image_id"]
        conn = self._ingest2_db()
        img = self._ingest2_load_image(conn, image_id)
        if not img:
            conn.close()
            self._send_json({"error": "Image not found"}, 404)
            return

        # Delete file
        filepath = _get_ingest_images_dir() / img["stored_name"]
        if filepath.is_file():
            filepath.unlink()

        conn.execute("DELETE FROM ingest_images WHERE id = ?", (image_id,))
        conn.commit()
        conn.close()

        _log_ingest(f"Deleted image {image_id}: {img['filename']}")
        self._send_json({"ok": True})

    # ── Ingest image serving (shared by ingest2 frontend) ──

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
        self.send_header("Cache-Control", "public, max-age=86400, immutable")
        self.end_headers()
        self.wfile.write(content)

    # (Legacy session-based ingest pipeline removed — use ingest2 endpoints)

    # ── Order API endpoints ──

    def _api_orders_list(self):
        """List all orders with card counts."""
        from mtg_collector.db.models import OrderRepository
        from mtg_collector.db.schema import init_db
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = OrderRepository(conn)
        orders = repo.list_all()
        conn.close()
        self._send_json(orders)

    def _api_order_cards(self, order_id: int):
        """Get cards in an order."""
        from mtg_collector.db.models import OrderRepository
        from mtg_collector.db.schema import init_db
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = OrderRepository(conn)
        cards = repo.get_order_cards(order_id)
        conn.close()
        self._send_json(cards)

    def _api_order_parse(self):
        """Parse order text into structured data."""
        from mtg_collector.services.order_parser import parse_order
        data = self._read_json_body()
        if data is None:
            return
        text = data.get("text", "")
        fmt = data.get("format")
        if fmt == "auto":
            fmt = None
        orders = parse_order(text, fmt)
        # Serialize to JSON-safe dicts
        result = []
        for o in orders:
            result.append({
                "order_number": o.order_number,
                "source": o.source,
                "seller_name": o.seller_name,
                "order_date": o.order_date,
                "subtotal": o.subtotal,
                "shipping": o.shipping,
                "tax": o.tax,
                "total": o.total,
                "shipping_status": o.shipping_status,
                "estimated_delivery": o.estimated_delivery,
                "items": [
                    {
                        "card_name": item.card_name,
                        "set_hint": item.set_hint,
                        "condition": item.condition,
                        "foil": item.foil,
                        "quantity": item.quantity,
                        "price": item.price,
                        "treatment": item.treatment,
                        "rarity_hint": item.rarity_hint,
                    }
                    for item in o.items
                ],
            })
        self._send_json(result)

    def _api_order_resolve(self):
        """Resolve parsed orders against Scryfall."""
        from mtg_collector.db.models import CardRepository, PrintingRepository, SetRepository
        from mtg_collector.db.schema import init_db
        from mtg_collector.services.order_parser import ParsedOrder, ParsedOrderItem
        from mtg_collector.services.order_resolver import resolve_orders
        from mtg_collector.services.scryfall import ScryfallAPI

        data = self._read_json_body()
        if data is None:
            return

        # Reconstruct ParsedOrder objects from JSON
        orders = []
        for od in data.get("orders", []):
            order = ParsedOrder(
                order_number=od.get("order_number"),
                source=od.get("source", "tcgplayer"),
                seller_name=od.get("seller_name"),
                order_date=od.get("order_date"),
                subtotal=od.get("subtotal"),
                shipping=od.get("shipping"),
                tax=od.get("tax"),
                total=od.get("total"),
                shipping_status=od.get("shipping_status"),
                estimated_delivery=od.get("estimated_delivery"),
            )
            for item_d in od.get("items", []):
                order.items.append(ParsedOrderItem(
                    card_name=item_d["card_name"],
                    set_hint=item_d.get("set_hint"),
                    condition=item_d.get("condition", "Near Mint"),
                    foil=item_d.get("foil", False),
                    quantity=item_d.get("quantity", 1),
                    price=item_d.get("price"),
                    treatment=item_d.get("treatment"),
                    rarity_hint=item_d.get("rarity_hint"),
                ))
            orders.append(order)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        scryfall = ScryfallAPI()
        card_repo = CardRepository(conn)
        set_repo = SetRepository(conn)
        printing_repo = PrintingRepository(conn)

        resolved = resolve_orders(orders, scryfall, card_repo, set_repo, printing_repo, conn)

        # Serialize
        result = []
        for ro in resolved:
            items = []
            for item in ro.items:
                items.append({
                    "card_name": item.card_name or item.parsed.card_name,
                    "parsed_name": item.parsed.card_name,
                    "set_hint": item.parsed.set_hint,
                    "set_code": item.set_code,
                    "collector_number": item.collector_number,
                    "scryfall_id": item.scryfall_id,
                    "image_uri": item.image_uri,
                    "condition": item.parsed.condition,
                    "foil": item.parsed.foil,
                    "quantity": item.parsed.quantity,
                    "price": item.parsed.price,
                    "treatment": item.parsed.treatment,
                    "rarity_hint": item.parsed.rarity_hint,
                    "error": item.error,
                    "resolved": item.scryfall_id is not None,
                })
            result.append({
                "order_number": ro.parsed.order_number,
                "source": ro.parsed.source,
                "seller_name": ro.parsed.seller_name,
                "order_date": ro.parsed.order_date,
                "subtotal": ro.parsed.subtotal,
                "shipping": ro.parsed.shipping,
                "tax": ro.parsed.tax,
                "total": ro.parsed.total,
                "shipping_status": ro.parsed.shipping_status,
                "estimated_delivery": ro.parsed.estimated_delivery,
                "items": items,
            })

        conn.close()
        self._send_json(result)

    def _api_order_commit(self):
        """Commit resolved orders to the database."""
        from mtg_collector.db.models import (
            CollectionRepository,
            OrderRepository,
        )
        from mtg_collector.db.schema import init_db
        from mtg_collector.services.order_parser import ParsedOrder, ParsedOrderItem
        from mtg_collector.services.order_resolver import (
            ResolvedItem,
            ResolvedOrder,
            commit_orders,
        )

        data = self._read_json_body()
        if data is None:
            return

        status = data.get("status", "ordered")
        source = data.get("source", "order_import")

        # Reconstruct ResolvedOrder objects
        resolved_orders = []
        for od in data.get("orders", []):
            parsed = ParsedOrder(
                order_number=od.get("order_number"),
                source=od.get("source", "tcgplayer"),
                seller_name=od.get("seller_name"),
                order_date=od.get("order_date"),
                subtotal=od.get("subtotal"),
                shipping=od.get("shipping"),
                tax=od.get("tax"),
                total=od.get("total"),
                shipping_status=od.get("shipping_status"),
                estimated_delivery=od.get("estimated_delivery"),
            )
            ro = ResolvedOrder(parsed=parsed)
            for item_d in od.get("items", []):
                parsed_item = ParsedOrderItem(
                    card_name=item_d.get("parsed_name", item_d["card_name"]),
                    set_hint=item_d.get("set_hint"),
                    condition=item_d.get("condition", "Near Mint"),
                    foil=item_d.get("foil", False),
                    quantity=item_d.get("quantity", 1),
                    price=item_d.get("price"),
                    treatment=item_d.get("treatment"),
                    rarity_hint=item_d.get("rarity_hint"),
                )
                ri = ResolvedItem(
                    parsed=parsed_item,
                    scryfall_id=item_d.get("scryfall_id"),
                    card_name=item_d.get("card_name"),
                    set_code=item_d.get("set_code"),
                    collector_number=item_d.get("collector_number"),
                    image_uri=item_d.get("image_uri"),
                    error=item_d.get("error"),
                )
                ro.items.append(ri)
            resolved_orders.append(ro)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        collection_repo = CollectionRepository(conn)
        order_repo = OrderRepository(conn)

        summary = commit_orders(
            resolved_orders, order_repo, collection_repo, conn,
            status=status, source=source,
        )

        conn.close()
        self._send_json(summary)

    def _api_collection_copies(self, params: dict):
        """Return individual collection rows for a printing, with order data."""
        from mtg_collector.db.models import CollectionRepository
        from mtg_collector.db.schema import init_db
        scryfall_id = params.get("scryfall_id", [""])[0]
        if not scryfall_id:
            self._send_json({"error": "scryfall_id required"}, 400)
            return
        finish = params.get("finish", [""])[0] or None
        condition = params.get("condition", [""])[0] or None
        status = params.get("status", [""])[0] or None
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = CollectionRepository(conn)
        copies = repo.get_copies(scryfall_id, finish=finish, condition=condition, status=status)
        conn.close()
        self._send_json(copies)

    def _api_collection_receive(self, collection_id: int):
        """Receive a single ordered card (flip ordered -> owned)."""
        from mtg_collector.db.models import CollectionRepository
        from mtg_collector.db.schema import init_db
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = CollectionRepository(conn)
        ok = repo.receive_card(collection_id)
        conn.commit()
        conn.close()
        self._send_json({"received": 1 if ok else 0})

    def _api_order_receive(self, order_id: int):
        """Mark ordered cards in an order as owned. Accepts optional card_ids in JSON body."""
        from mtg_collector.db.models import OrderRepository
        from mtg_collector.db.schema import init_db
        data = self._read_json_body()  # None when no body — backward-compatible
        card_ids = data.get("card_ids") if data else None
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = OrderRepository(conn)
        count = repo.receive_order(order_id, card_ids=card_ids)
        conn.commit()
        conn.close()
        self._send_json({"received": count})

    # ── Corner Ingest API endpoints ──

    def _api_corners_detect(self):
        """Upload a photo, run Claude Vision corner detection, resolve cards."""
        from mtg_collector.cli.ingest_ids import RARITY_MAP, lookup_card
        from mtg_collector.db.models import CardRepository, PrintingRepository, SetRepository
        from mtg_collector.db.schema import init_db
        from mtg_collector.services.claude import ClaudeVision
        from mtg_collector.services.scryfall import (
            ScryfallAPI,
            cache_scryfall_data,
            ensure_set_cached,
        )

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        boundary = content_type.split("boundary=")[1].strip()
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Extract the file from multipart
        file_content = None
        original_name = None
        parts = body.split(f"--{boundary}".encode())
        for part in parts:
            if not part or part.strip() == b"--" or part.strip() == b"":
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            header_bytes = part[:header_end]
            data = part[header_end + 4:]
            if data.endswith(b"\r\n"):
                data = data[:-2]
            header_str = header_bytes.decode("utf-8", errors="replace")
            filename_match = re.search(r'filename="([^"]+)"', header_str)
            if not filename_match:
                continue
            original_name = filename_match.group(1)
            ext = Path(original_name).suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            file_content = data
            break

        if file_content is None:
            self._send_json({"error": "No image file found in upload"}, 400)
            return

        # Save to ingest images dir with timestamped name
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        ext = Path(original_name).suffix.lower()
        stored_name = f"corners_{ts}{ext}"
        dest = _get_ingest_images_dir() / stored_name
        dest.write_bytes(file_content)
        image_key = stored_name

        _log_ingest(f"Corner detect: saved {original_name} as {stored_name}")

        # Run Claude Vision corner detection
        try:
            claude = ClaudeVision()
            detections, skipped = claude.read_card_corners(str(dest))
        except Exception as e:
            self._send_json({"error": f"Claude Vision error: {e}"}, 500)
            return

        if not detections and not skipped:
            self._send_json({"cards": [], "skipped": [], "errors": ["No card corners detected"], "image_key": image_key})
            return

        # Resolve each detection to a Scryfall card
        scryfall = ScryfallAPI()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        card_repo = CardRepository(conn)
        set_repo = SetRepository(conn)
        printing_repo = PrintingRepository(conn)

        # Normalize set codes
        unique_sets = {}
        errors = []
        for d in detections:
            raw = d["set"]
            if raw.lower() not in unique_sets:
                normalized = scryfall.normalize_set_code(raw)
                if not normalized:
                    errors.append(f"Unknown set code: {raw}")
                    continue
                unique_sets[raw.lower()] = normalized

        # Cache each unique set
        for set_code in set(unique_sets.values()):
            ensure_set_cached(scryfall, set_code, card_repo, set_repo, printing_repo, conn)

        # Resolve cards
        resolved_cards = []
        for d in detections:
            raw_set = d["set"]
            if raw_set.lower() not in unique_sets:
                continue
            set_code = unique_sets[raw_set.lower()]

            cn_raw = d["collector_number"]
            cn_stripped = cn_raw.lstrip("0") or "0"

            rarity_code = d.get("rarity", "C")
            if rarity_code not in RARITY_MAP:
                rarity_code = "C"
            rarity_expected = RARITY_MAP[rarity_code]

            card_data = lookup_card(set_code, cn_raw, cn_stripped, rarity_expected, printing_repo, scryfall)
            if not card_data:
                errors.append(f"Card not found: {rarity_code} {cn_raw} {set_code.upper()}")
                continue

            cache_scryfall_data(scryfall, card_repo, set_repo, printing_repo, card_data)

            # Extract image URI
            image_uri = None
            if "image_uris" in card_data:
                image_uri = card_data["image_uris"].get("small") or card_data["image_uris"].get("normal")
            elif "card_faces" in card_data and card_data["card_faces"]:
                face = card_data["card_faces"][0]
                if "image_uris" in face:
                    image_uri = face["image_uris"].get("small") or face["image_uris"].get("normal")

            resolved_cards.append({
                "scryfall_id": card_data["id"],
                "name": card_data.get("name", "Unknown"),
                "image_uri": image_uri,
                "set_code": set_code,
                "collector_number": card_data.get("collector_number", cn_raw),
                "rarity": card_data.get("rarity", rarity_expected),
                "foil": d.get("foil", False),
                "condition": "Near Mint",
            })

        conn.close()

        _log_ingest(f"Corner detect: {len(resolved_cards)} resolved, {len(skipped)} skipped, {len(errors)} errors")

        self._send_json({
            "cards": resolved_cards,
            "skipped": skipped,
            "errors": errors,
            "image_key": image_key,
        })

    def _api_corners_commit(self):
        """Commit reviewed corner-detected cards to collection."""
        from mtg_collector.db.models import (
            CollectionEntry,
            CollectionRepository,
            PrintingRepository,
        )
        from mtg_collector.db.schema import init_db
        from mtg_collector.utils import (
            normalize_condition,
            normalize_finish,
            now_iso,
            store_source_image,
        )

        data = self._read_json_body()
        if data is None:
            return

        image_key = data.get("image_key")
        cards = data.get("cards", [])

        if not cards:
            self._send_json({"error": "No cards to commit"}, 400)
            return

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        collection_repo = CollectionRepository(conn)
        printing_repo = PrintingRepository(conn)

        # Store source image permanently if image_key provided
        source_image = None
        if image_key:
            src_path = _get_ingest_images_dir() / image_key
            if src_path.exists():
                source_image = store_source_image(str(src_path))

        added = []
        for i, card in enumerate(cards):
            scryfall_id = card.get("scryfall_id")
            if not scryfall_id:
                continue

            printing = printing_repo.get(scryfall_id)
            if not printing:
                continue

            foil = card.get("foil", False)
            finish = normalize_finish("foil" if foil else "nonfoil")
            condition = normalize_condition(card.get("condition", "Near Mint"))

            entry = CollectionEntry(
                id=None,
                scryfall_id=scryfall_id,
                finish=finish,
                condition=condition,
                source="corner_ingest",
                source_image=source_image,
            )
            entry_id = collection_repo.add(entry)

            # Insert lineage record
            md5 = _md5_file(str(_get_ingest_images_dir() / image_key)) if image_key else ""
            conn.execute(
                """INSERT INTO ingest_lineage (collection_id, image_md5, image_path, card_index, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (entry_id, md5, image_key or "", i, now_iso()),
            )

            name = "???"
            if printing.raw_json:
                name = json.loads(printing.raw_json).get("name", "???")

            added.append({
                "entry_id": entry_id,
                "name": name,
                "scryfall_id": scryfall_id,
            })

            _log_ingest(f"Corner commit: {name} ({printing.set_code.upper()} #{printing.collector_number}) -> collection ID {entry_id}")

        conn.commit()
        conn.close()

        self._send_json({"added": added})

    # ── Manual ID Ingest API endpoints ──

    def _api_ingest_ids_resolve(self):
        from mtg_collector.cli.ingest_ids import RARITY_MAP, lookup_card
        from mtg_collector.db.models import CardRepository, PrintingRepository, SetRepository
        from mtg_collector.db.schema import init_db
        from mtg_collector.services.scryfall import (
            ScryfallAPI,
            cache_scryfall_data,
            ensure_set_cached,
        )

        data = self._read_json_body()
        if data is None:
            return
        entries = data.get("entries", [])
        if not entries:
            self._send_json({"error": "No entries provided"}, 400)
            return

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        scryfall = ScryfallAPI()
        card_repo = CardRepository(conn)
        set_repo = SetRepository(conn)
        printing_repo = PrintingRepository(conn)

        # Normalize set codes
        set_map = {}
        set_errors = []
        for e in entries:
            raw = e.get("set_code", "").strip()
            if raw.lower() not in set_map:
                normalized = scryfall.normalize_set_code(raw)
                if normalized:
                    set_map[raw.lower()] = normalized
                else:
                    set_errors.append({"set_code": raw, "error": f"Unknown set code '{raw}'"})
                    set_map[raw.lower()] = None

        # Pre-cache valid sets
        for sc in set(v for v in set_map.values() if v):
            ensure_set_cached(scryfall, sc, card_repo, set_repo, printing_repo, conn)

        resolved = []
        failed = []
        for idx, e in enumerate(entries):
            rarity_code = e.get("rarity", "").upper()
            if rarity_code not in RARITY_MAP:
                failed.append({"index": idx, **e, "error": f"Invalid rarity code '{rarity_code}'"})
                continue

            raw_set = e.get("set_code", "").strip()
            set_code = set_map.get(raw_set.lower())
            if not set_code:
                failed.append({"index": idx, **e, "error": f"Unknown set code '{raw_set}'"})
                continue

            cn_raw = e.get("collector_number", "").strip()
            cn_stripped = cn_raw.lstrip("0") or "0"
            rarity = RARITY_MAP[rarity_code]

            card_data = lookup_card(set_code, cn_raw, cn_stripped, rarity, printing_repo, scryfall)
            if not card_data:
                failed.append({"index": idx, "rarity_code": rarity_code, "collector_number": cn_raw,
                              "set_code": raw_set, "foil": e.get("foil", False), "error": "Card not found"})
                continue

            cache_scryfall_data(scryfall, card_repo, set_repo, printing_repo, card_data)

            actual_rarity = card_data.get("rarity", "")
            image_uris = card_data.get("image_uris") or {}
            if not image_uris and card_data.get("card_faces"):
                image_uris = card_data["card_faces"][0].get("image_uris", {})
            image_uri = image_uris.get("small", image_uris.get("normal", ""))

            resolved.append({
                "index": idx,
                "rarity_code": rarity_code,
                "rarity": rarity,
                "collector_number": card_data.get("collector_number", cn_raw),
                "set_code": set_code,
                "set_name": card_data.get("set_name", ""),
                "foil": e.get("foil", False),
                "scryfall_id": card_data["id"],
                "card_name": card_data.get("name", "Unknown"),
                "image_uri": image_uri,
                "actual_rarity": actual_rarity,
                "rarity_mismatch": rarity != "promo" and rarity != "token" and actual_rarity != rarity,
            })

        conn.close()
        self._send_json({"resolved": resolved, "failed": failed, "set_errors": set_errors})

    def _api_ingest_ids_commit(self):
        from mtg_collector.db.models import (
            CollectionEntry,
            CollectionRepository,
            PrintingRepository,
        )
        from mtg_collector.db.schema import init_db
        from mtg_collector.utils import normalize_condition, normalize_finish

        data = self._read_json_body()
        if data is None:
            return
        cards = data.get("cards", [])
        condition = normalize_condition(data.get("condition", "Near Mint"))
        source = data.get("source", "manual_id")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        collection_repo = CollectionRepository(conn)
        printing_repo = PrintingRepository(conn)

        added = 0
        for card in cards:
            scryfall_id = card.get("scryfall_id")
            if not scryfall_id:
                continue
            printing = printing_repo.get(scryfall_id)
            if not printing:
                continue
            finish = normalize_finish("foil" if card.get("foil") else "nonfoil")
            entry = CollectionEntry(id=None, scryfall_id=scryfall_id, finish=finish,
                                   condition=condition, source=source)
            collection_repo.add(entry)
            added += 1

        conn.commit()
        conn.close()
        self._send_json({"added": added, "failed": len(cards) - added})

    # ── CSV Import API endpoints ──

    def _api_import_parse(self):
        """Parse CSV text into structured rows."""
        import tempfile

        from mtg_collector.importers import detect_format, get_importer

        data = self._read_json_body()
        if data is None:
            return
        text = data.get("text", "").strip()
        fmt = data.get("format", "auto")

        if not text:
            self._send_json({"error": "No CSV text provided"}, 400)
            return

        # Write to temp file for existing importer to parse
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name

        try:
            # Detect format
            if fmt == "auto":
                try:
                    fmt = detect_format(tmp_path)
                except ValueError as e:
                    self._send_json({"error": str(e)}, 400)
                    return

            importer = get_importer(fmt)
            rows = importer.parse_file(tmp_path)

            # Extract lookup fields for each row
            parsed_rows = []
            for row in rows:
                name, set_code, cn, qty = importer.row_to_lookup(row)
                parsed_rows.append({
                    "name": name,
                    "set_code": set_code,
                    "collector_number": cn,
                    "quantity": qty,
                    "raw": row,
                })

            self._send_json({"format": fmt, "rows": parsed_rows, "total_rows": len(parsed_rows)})
        except Exception as e:
            self._send_json({"error": f"Failed to parse CSV: {e}"}, 400)
        finally:
            os.unlink(tmp_path)

    def _api_import_resolve(self):
        """Resolve parsed CSV rows using local DB."""
        from mtg_collector.db.models import CardRepository, PrintingRepository, SetRepository
        from mtg_collector.db.schema import init_db
        from mtg_collector.importers import get_importer

        data = self._read_json_body()
        if data is None:
            return
        fmt = data.get("format")
        rows = data.get("rows", [])

        if not fmt or not rows:
            self._send_json({"error": "Missing format or rows"}, 400)
            return

        importer = get_importer(fmt)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        card_repo = CardRepository(conn)
        set_repo = SetRepository(conn)
        printing_repo = PrintingRepository(conn)

        resolved = []
        total = 0
        resolved_count = 0
        failed_count = 0

        for idx, row in enumerate(rows):
            name = row.get("name")
            set_code = row.get("set_code")
            cn = row.get("collector_number")
            qty = row.get("quantity", 1)
            total += 1

            scryfall_id = importer._resolve_card(card_repo, printing_repo, name, set_code, cn)
            if scryfall_id:
                printing = printing_repo.get(scryfall_id)
                card = card_repo.get(printing.oracle_id) if printing else None
                s = set_repo.get(printing.set_code) if printing else None
                image_uri = printing.image_uri or "" if printing else ""

                resolved.append({
                    "index": idx,
                    "name": card.name if card else name,
                    "set_code": printing.set_code if printing else set_code,
                    "set_name": s.set_name if s else "",
                    "collector_number": printing.collector_number if printing else cn,
                    "quantity": qty,
                    "scryfall_id": scryfall_id,
                    "image_uri": image_uri,
                    "resolved": True,
                    "error": None,
                    "raw": row.get("raw", {}),
                })
                resolved_count += 1
            else:
                resolved.append({
                    "index": idx,
                    "name": name,
                    "set_code": set_code,
                    "collector_number": cn,
                    "quantity": qty,
                    "scryfall_id": None,
                    "image_uri": "",
                    "resolved": False,
                    "error": f"Could not find: {name} ({set_code or 'any set'})",
                    "raw": row.get("raw", {}),
                })
                failed_count += 1

        conn.close()
        self._send_json({
            "resolved": resolved,
            "summary": {"total": total, "resolved": resolved_count, "failed": failed_count},
        })

    def _api_import_commit(self):
        """Commit resolved CSV import cards to the collection."""
        from mtg_collector.db.models import CollectionRepository
        from mtg_collector.db.schema import init_db
        from mtg_collector.importers import get_importer

        data = self._read_json_body()
        if data is None:
            return
        fmt = data.get("format")
        cards = data.get("cards", [])

        if not fmt or not cards:
            self._send_json({"error": "Missing format or cards"}, 400)
            return

        importer = get_importer(fmt)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        collection_repo = CollectionRepository(conn)

        added = 0
        errors = []
        for card in cards:
            scryfall_id = card.get("scryfall_id")
            raw = card.get("raw", {})
            qty = card.get("quantity", 1)
            if not scryfall_id:
                continue
            try:
                entry = importer.row_to_entry(raw, scryfall_id)
                for _ in range(qty):
                    collection_repo.add(entry)
                    added += 1
            except Exception as e:
                errors.append(f"Error adding {card.get('name', '?')}: {e}")

        conn.commit()
        conn.close()
        self._send_json({"cards_added": added, "cards_skipped": len(cards) - added, "errors": errors})

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

    def _api_shorten(self, params):
        url = params.get("url", [""])[0]
        shorteners = [
            ("https://da.gd/s", {"url": url}),
            ("https://is.gd/create.php", {"format": "simple", "url": url}),
        ]
        for base, qs in shorteners:
            try:
                resp = requests.get(base, params=qs, timeout=5)
                short = resp.text.strip()
                if resp.ok and short.startswith("http"):
                    self._send_json({"short_url": short})
                    return
            except Exception:
                continue
        self._send_json({"error": "Shortening failed"}, 502)

    def _api_wishlist_list(self, params: dict):
        """List wishlist entries."""
        from mtg_collector.db.models import WishlistRepository
        from mtg_collector.db.schema import init_db

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        repo = WishlistRepository(conn)
        fulfilled_param = params.get("fulfilled", [""])[0]
        fulfilled = None
        if fulfilled_param == "true":
            fulfilled = True
        elif fulfilled_param == "false":
            fulfilled = False

        name = params.get("name", [""])[0] or None
        limit_str = params.get("limit", [""])[0]
        limit = int(limit_str) if limit_str else None

        entries = repo.list_all(fulfilled=fulfilled, name=name, limit=limit)
        conn.close()
        self._send_json(entries)

    def _api_wishlist_add(self, data: dict):
        """Add a wishlist entry."""
        from mtg_collector.db.models import (
            CardRepository,
            PrintingRepository,
            SetRepository,
            WishlistEntry,
            WishlistRepository,
        )
        from mtg_collector.db.schema import init_db
        from mtg_collector.services.scryfall import ScryfallAPI, cache_scryfall_data
        from mtg_collector.utils import now_iso

        name = data.get("name", "").strip()
        if not name:
            self._send_json({"error": "name is required"}, 400)
            return

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        card_repo = CardRepository(conn)
        set_repo = SetRepository(conn)
        printing_repo = PrintingRepository(conn)
        scryfall = ScryfallAPI()

        set_code = data.get("set_code")
        cn = data.get("collector_number")
        results = scryfall.search_card(name, set_code=set_code, collector_number=cn)
        if not results:
            conn.close()
            self._send_json({"error": f"No card found matching '{name}'"}, 404)
            return

        card_data = results[0]
        cache_scryfall_data(scryfall, card_repo, set_repo, printing_repo, card_data)

        oracle_id = card_data["oracle_id"]
        scryfall_id = card_data["id"] if set_code else None

        repo = WishlistRepository(conn)
        entry = WishlistEntry(
            id=None,
            oracle_id=oracle_id,
            scryfall_id=scryfall_id,
            max_price=data.get("max_price"),
            priority=data.get("priority", 0),
            notes=data.get("notes"),
            added_at=now_iso(),
            source="server",
        )
        new_id = repo.add(entry)
        conn.commit()
        conn.close()

        self._send_json({"id": new_id, "name": card_data["name"], "oracle_id": oracle_id, "scryfall_id": scryfall_id})

    def _api_wishlist_bulk_add(self, data: dict):
        """Bulk-add cards to the wishlist."""
        from mtg_collector.db.models import (
            CardRepository,
            PrintingRepository,
            SetRepository,
            WishlistEntry,
            WishlistRepository,
        )
        from mtg_collector.db.schema import init_db
        from mtg_collector.services.scryfall import ScryfallAPI, cache_scryfall_data
        from mtg_collector.utils import now_iso

        cards = data.get("cards", [])
        if not cards:
            self._send_json({"added": [], "errors": []})
            return

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        card_repo = CardRepository(conn)
        set_repo = SetRepository(conn)
        printing_repo = PrintingRepository(conn)
        wishlist_repo = WishlistRepository(conn)
        scryfall = ScryfallAPI()

        added = []
        errors = []

        for item in cards:
            name = (item.get("name") or "").strip()
            if not name:
                errors.append({"name": name, "error": "name is required"})
                continue
            set_code = item.get("set_code")
            cn = item.get("collector_number")
            try:
                results = scryfall.search_card(name, set_code=set_code, collector_number=cn)
                if not results:
                    errors.append({"name": name, "error": f"No card found matching '{name}'"})
                    continue
                card_data = results[0]
                cache_scryfall_data(scryfall, card_repo, set_repo, printing_repo, card_data)
                oracle_id = card_data["oracle_id"]
                scryfall_id = card_data["id"] if set_code else None
                entry = WishlistEntry(
                    id=None,
                    oracle_id=oracle_id,
                    scryfall_id=scryfall_id,
                    priority=item.get("priority", 0),
                    added_at=now_iso(),
                    source="server",
                )
                new_id = wishlist_repo.add(entry)
                added.append({"id": new_id, "name": card_data["name"], "oracle_id": oracle_id, "scryfall_id": scryfall_id})
            except Exception as exc:
                errors.append({"name": name, "error": str(exc)})

        conn.commit()
        conn.close()
        self._send_json({"added": added, "errors": errors})

    def _api_wishlist_delete(self, wid: int):
        """Delete a wishlist entry."""
        from mtg_collector.db.models import WishlistRepository
        from mtg_collector.db.schema import init_db

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        repo = WishlistRepository(conn)
        deleted = repo.delete(wid)
        conn.commit()
        conn.close()

        if deleted:
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "Not found"}, 404)

    def _api_wishlist_fulfill(self, wid: int):
        """Mark a wishlist entry as fulfilled."""
        from mtg_collector.db.models import WishlistRepository
        from mtg_collector.db.schema import init_db

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        repo = WishlistRepository(conn)
        fulfilled = repo.fulfill(wid)
        conn.commit()
        conn.close()

        if fulfilled:
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "Not found"}, 404)

    def _api_set_browse(self, set_code: str, params: dict):
        """Browse all printings in a set with owned/wanted annotations."""
        from mtg_collector.db.models import CardRepository, PrintingRepository, SetRepository
        from mtg_collector.db.schema import init_db
        from mtg_collector.services.scryfall import ScryfallAPI, ensure_set_cached

        set_code = set_code.lower()

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        card_repo = CardRepository(conn)
        set_repo = SetRepository(conn)
        printing_repo = PrintingRepository(conn)

        # Ensure the set is cached
        if not set_repo.is_cards_cached(set_code):
            scryfall = ScryfallAPI()
            cached = ensure_set_cached(scryfall, set_code, card_repo, set_repo, printing_repo, conn)
            if not cached:
                conn.close()
                self._send_json({"error": f"Could not fetch set '{set_code}'"}, 404)
                return
            conn.commit()

        query = """
            SELECT p.scryfall_id, p.collector_number, p.rarity, p.image_uri, p.artist,
                   p.frame_effects, p.border_color, p.full_art, p.promo, p.promo_types, p.finishes,
                   card.name, card.type_line, card.mana_cost, card.colors, card.color_identity,
                   c.id as collection_id, c.status, c.finish as owned_finish, c.condition,
                   w.id as wishlist_id, w.priority
            FROM printings p
            JOIN cards card ON p.oracle_id = card.oracle_id
            LEFT JOIN collection c ON p.scryfall_id = c.scryfall_id AND c.status = 'owned'
            LEFT JOIN wishlist w ON (
                w.scryfall_id = p.scryfall_id
                OR (w.scryfall_id IS NULL AND w.oracle_id = p.oracle_id)
            ) AND w.fulfilled_at IS NULL
            WHERE p.set_code = ?
            ORDER BY CAST(p.collector_number AS INTEGER), p.collector_number
        """
        cursor = conn.execute(query, (set_code,))
        rows = cursor.fetchall()

        results = []
        for row in rows:
            results.append({
                "scryfall_id": row["scryfall_id"],
                "collector_number": row["collector_number"],
                "rarity": row["rarity"],
                "image_uri": row["image_uri"],
                "artist": row["artist"],
                "name": row["name"],
                "type_line": row["type_line"],
                "mana_cost": row["mana_cost"],
                "colors": row["colors"],
                "color_identity": row["color_identity"],
                "frame_effects": row["frame_effects"],
                "border_color": row["border_color"],
                "full_art": bool(row["full_art"]),
                "promo": bool(row["promo"]),
                "promo_types": row["promo_types"],
                "finishes": row["finishes"],
                "collection_id": row["collection_id"],
                "status": row["status"],
                "owned_finish": row["owned_finish"],
                "condition": row["condition"],
                "wishlist_id": row["wishlist_id"],
                "wishlist_priority": row["priority"],
            })

        conn.close()
        self._send_json(results)

    def _api_collection_copies(self, params: dict):
        """Return per-copy data for a card (by scryfall_id + optional filters)."""
        from mtg_collector.db.models import CollectionRepository
        from mtg_collector.db.schema import init_db

        scryfall_id = params.get("scryfall_id", [""])[0]
        if not scryfall_id:
            self._send_json({"error": "scryfall_id required"}, 400)
            return

        finish = params.get("finish", [""])[0] or None
        condition = params.get("condition", [""])[0] or None
        status = params.get("status", [""])[0] or None

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        repo = CollectionRepository(conn)
        copies = repo.get_copies(scryfall_id, finish=finish, condition=condition, status=status)
        conn.close()
        self._send_json(copies)

    def _api_collection_dispose(self, entry_id: int, data: dict):
        """Transition a collection entry to a disposition status."""
        from mtg_collector.db.models import CollectionRepository
        from mtg_collector.db.schema import init_db

        new_status = data.get("new_status")
        if not new_status:
            self._send_json({"error": "new_status required"}, 400)
            return

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        repo = CollectionRepository(conn)
        try:
            repo.dispose(
                entry_id,
                new_status,
                sale_price=data.get("sale_price"),
                note=data.get("note"),
            )
            conn.commit()
            conn.close()
            self._send_json({"ok": True})
        except ValueError as e:
            conn.close()
            self._send_json({"error": str(e)}, 400)

    def _api_collection_delete(self, entry_id: int):
        """Hard-delete a collection entry with lineage cleanup."""
        from mtg_collector.db.models import CollectionRepository
        from mtg_collector.db.schema import init_db

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        repo = CollectionRepository(conn)
        try:
            deleted = repo.delete_with_lineage(entry_id)
            conn.commit()
            conn.close()
            if deleted:
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)
        except ValueError as e:
            conn.close()
            self._send_json({"error": str(e)}, 400)

    def _api_collection_bulk_delete(self, data: dict):
        """Bulk-delete collection entries with lineage cleanup."""
        from mtg_collector.db.models import CollectionRepository
        from mtg_collector.db.schema import init_db

        ids = data.get("ids", [])
        if not ids:
            self._send_json({"error": "ids array required"}, 400)
            return

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        repo = CollectionRepository(conn)
        result = repo.bulk_delete(ids)
        conn.commit()
        conn.close()
        self._send_json({
            "deleted": len(result["deleted"]),
            "skipped": len(result["skipped"]),
            "deleted_ids": result["deleted"],
            "skipped_ids": result["skipped"],
        })

    # ── Sealed product API handlers ─────────────────────────────────

    def _api_sealed_products(self, params: dict):
        """Search/list sealed products (reference data)."""
        from mtg_collector.db.models import SealedProductRepository
        from mtg_collector.db.schema import init_db

        q = params.get("q", [""])[0]
        set_code = params.get("set_code", [""])[0]
        category = params.get("category", [""])[0]
        limit = int(params.get("limit", ["50"])[0])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = SealedProductRepository(conn)

        if q:
            products = repo.search_by_name(q, limit=limit)
        elif set_code:
            products = repo.list_by_set(set_code.lower())
        else:
            # Default: return nothing without a search term or set filter
            conn.close()
            self._send_json([])
            return

        if category:
            products = [p for p in products if p.category == category]

        result = []
        for p in products[:limit]:
            image_url = None
            if p.tcgplayer_product_id:
                image_url = f"https://tcgplayer-cdn.tcgplayer.com/product/{p.tcgplayer_product_id}_200w.jpg"
            # Look up set name
            set_name = None
            row = conn.execute("SELECT set_name FROM sets WHERE set_code = ?", (p.set_code,)).fetchone()
            if row:
                set_name = row["set_name"]
            result.append({
                "uuid": p.uuid,
                "name": p.name,
                "set_code": p.set_code,
                "set_name": set_name,
                "category": p.category,
                "subtype": p.subtype,
                "tcgplayer_product_id": p.tcgplayer_product_id,
                "card_count": p.card_count,
                "product_size": p.product_size,
                "release_date": p.release_date,
                "image_url": image_url,
                "purchase_url_tcgplayer": p.purchase_url_tcgplayer,
                "purchase_url_cardkingdom": p.purchase_url_cardkingdom,
            })

        conn.close()
        self._send_json(result)

    def _api_sealed_products_sets(self):
        """List sets that have sealed products."""
        from mtg_collector.db.models import SealedProductRepository
        from mtg_collector.db.schema import init_db

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = SealedProductRepository(conn)
        sets = repo.list_sets_with_products()
        conn.close()
        self._send_json(sets)

    def _api_sealed_collection_list(self, params: dict):
        """List user's sealed collection with filters."""
        from mtg_collector.db.models import SealedCollectionRepository
        from mtg_collector.db.schema import init_db

        set_code = params.get("set_code", [""])[0] or None
        category = params.get("category", [""])[0] or None
        subtype = params.get("subtype", [""])[0] or None
        status = params.get("status", [""])[0] or None

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = SealedCollectionRepository(conn)
        entries = repo.list_all(set_code=set_code, category=category, subtype=subtype, status=status)

        # Attach image URLs
        for entry in entries:
            tcg_id = entry.get("tcgplayer_product_id")
            if tcg_id:
                entry["image_url"] = f"https://tcgplayer-cdn.tcgplayer.com/product/{tcg_id}_200w.jpg"
            else:
                entry["image_url"] = None

        conn.close()
        self._send_json(entries)

    def _api_sealed_collection_stats(self):
        """Get sealed collection statistics."""
        from mtg_collector.db.models import SealedCollectionRepository
        from mtg_collector.db.schema import init_db

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = SealedCollectionRepository(conn)
        stats = repo.stats()
        conn.close()
        self._send_json(stats)

    def _api_sealed_collection_add(self, data: dict):
        """Add a sealed product to the collection."""
        from mtg_collector.db.models import SealedCollectionEntry, SealedCollectionRepository, SealedProductRepository
        from mtg_collector.db.schema import init_db

        uuid = data.get("sealed_product_uuid")
        if not uuid:
            self._send_json({"error": "sealed_product_uuid required"}, 400)
            return

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        # Verify the product exists
        product_repo = SealedProductRepository(conn)
        product = product_repo.get(uuid)
        if not product:
            conn.close()
            self._send_json({"error": f"Sealed product '{uuid}' not found"}, 404)
            return

        entry = SealedCollectionEntry(
            id=None,
            sealed_product_uuid=uuid,
            quantity=data.get("quantity", 1),
            condition=data.get("condition", "Near Mint"),
            purchase_price=data.get("purchase_price"),
            purchase_date=data.get("purchase_date"),
            source=data.get("source"),
            seller_name=data.get("seller_name"),
            notes=data.get("notes"),
            status=data.get("status", "owned"),
        )

        repo = SealedCollectionRepository(conn)
        new_id = repo.add(entry)
        conn.commit()

        # Fetch back for response
        created = repo.get(new_id)
        conn.close()

        image_url = None
        if product.tcgplayer_product_id:
            image_url = f"https://tcgplayer-cdn.tcgplayer.com/product/{product.tcgplayer_product_id}_200w.jpg"

        self._send_json({
            "id": created.id,
            "sealed_product_uuid": created.sealed_product_uuid,
            "product_name": product.name,
            "set_code": product.set_code,
            "category": product.category,
            "quantity": created.quantity,
            "condition": created.condition,
            "purchase_price": created.purchase_price,
            "purchase_date": created.purchase_date,
            "source": created.source,
            "seller_name": created.seller_name,
            "notes": created.notes,
            "status": created.status,
            "added_at": created.added_at,
            "image_url": image_url,
        })

    def _api_sealed_collection_update(self, entry_id: int, data: dict):
        """Update a sealed collection entry."""
        from mtg_collector.db.models import SealedCollectionRepository
        from mtg_collector.db.schema import init_db

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = SealedCollectionRepository(conn)

        entry = repo.get(entry_id)
        if not entry:
            conn.close()
            self._send_json({"error": "Not found"}, 404)
            return

        # Apply updates from request body
        if "quantity" in data:
            entry.quantity = data["quantity"]
        if "condition" in data:
            entry.condition = data["condition"]
        if "purchase_price" in data:
            entry.purchase_price = data["purchase_price"]
        if "purchase_date" in data:
            entry.purchase_date = data["purchase_date"]
        if "source" in data:
            entry.source = data["source"]
        if "seller_name" in data:
            entry.seller_name = data["seller_name"]
        if "notes" in data:
            entry.notes = data["notes"]

        repo.update(entry)
        conn.commit()
        conn.close()
        self._send_json({"ok": True})

    def _api_sealed_collection_dispose(self, entry_id: int, data: dict):
        """Transition a sealed collection entry's status."""
        from mtg_collector.db.models import SealedCollectionRepository
        from mtg_collector.db.schema import init_db

        new_status = data.get("new_status")
        if not new_status:
            self._send_json({"error": "new_status required"}, 400)
            return

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = SealedCollectionRepository(conn)

        try:
            repo.dispose(entry_id, new_status, sale_price=data.get("sale_price"))
            conn.commit()
            conn.close()
            self._send_json({"ok": True})
        except ValueError as e:
            conn.close()
            self._send_json({"error": str(e)}, 400)

    def _api_sealed_collection_delete(self, entry_id: int):
        """Delete a sealed collection entry."""
        from mtg_collector.db.models import SealedCollectionRepository
        from mtg_collector.db.schema import init_db

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = SealedCollectionRepository(conn)

        deleted = repo.delete(entry_id)
        conn.commit()
        conn.close()
        if deleted:
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "Not found"}, 404)

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
        "--db",
        default=None,
        help="Path to collection SQLite database (default: ~/.mtgc/collection.sqlite)",
    )
    parser.add_argument(
        "--https",
        action="store_true",
        help="Serve over HTTPS with a self-signed certificate (enables camera on mobile)",
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the crack-pack-server command."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable is not set", file=sys.stderr)
        print("Card ingestion (OCR + corner reading) requires an Anthropic API key.", file=sys.stderr)
        sys.exit(1)

    db_path = get_db_path(getattr(args, "db", None))

    gen = PackGenerator(db_path)

    # Start background ingest worker pool
    global _ingest_executor, _background_db_path
    _background_db_path = db_path
    _ingest_executor = ThreadPoolExecutor(max_workers=4)
    _recover_pending_images(db_path)

    static_dir = Path(__file__).resolve().parent.parent / "static"
    handler = partial(CrackPackHandler, gen, static_dir, db_path)

    server = ThreadingHTTPServer(("", args.port), handler)

    if args.https:
        import socket
        import ssl
        import subprocess

        cert_dir = Path(os.environ.get("MTGC_HOME", Path.home() / ".mtgc"))
        cert_file = cert_dir / "server.pem"
        key_file = cert_dir / "server-key.pem"

        if not cert_file.exists() or not key_file.exists():
            print("Generating self-signed certificate...")
            san = "DNS:localhost,IP:127.0.0.1"
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
                san += f",IP:{local_ip}"
            except Exception:
                pass
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048",
                    "-keyout", str(key_file), "-out", str(cert_file),
                    "-days", "3650", "-nodes",
                    "-subj", "/CN=mtgc-local",
                    "-addext", f"subjectAltName={san}",
                ],
                check=True,
            )

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_file), str(key_file))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    else:
        scheme = "http"

    print(f"Server running at {scheme}://localhost:{args.port}")
    print(f"Crack-a-Pack: {scheme}://localhost:{args.port}/crack")
    print(f"Explore Sheets: {scheme}://localhost:{args.port}/sheets")
    print(f"Collection: {scheme}://localhost:{args.port}/collection")
    print(f"Upload: {scheme}://localhost:{args.port}/upload")
    print(f"Recent: {scheme}://localhost:{args.port}/recent")
    print(f"Disambiguate: {scheme}://localhost:{args.port}/disambiguate")
    print(f"Ingestor (Manual ID): {scheme}://localhost:{args.port}/ingestor-ids")
    print(f"Ingestor (Orders): {scheme}://localhost:{args.port}/ingestor-order")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        _ingest_executor.shutdown(wait=False)
        server.shutdown()
