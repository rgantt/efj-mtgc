"""Pack generation engine using SQLite-backed MTGJSON booster data."""

import json
import random
import sqlite3

from mtg_collector.db.connection import get_db_path


class PackGenerator:
    """Generate virtual booster packs from MTGJSON data stored in SQLite."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or get_db_path()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def list_sets(self) -> list[tuple[str, str]]:
        """Return (code, name) for sets that have booster data, sorted by name."""
        conn = self._connect()
        rows = conn.execute("""
            SELECT DISTINCT bc.set_code, s.set_name
            FROM mtgjson_booster_configs bc
            JOIN sets s ON bc.set_code = s.set_code
            ORDER BY s.set_name
        """).fetchall()
        conn.close()
        return [(r["set_code"], r["set_name"]) for r in rows]

    def list_products(self, set_code: str) -> list[str]:
        """Return available booster product types for a set."""
        set_code = set_code.lower()
        conn = self._connect()
        rows = conn.execute(
            "SELECT DISTINCT product FROM mtgjson_booster_configs WHERE set_code = ?",
            (set_code,),
        ).fetchall()
        conn.close()
        if not rows:
            raise ValueError(f"Set '{set_code}' not found or has no booster data")
        return [r["product"] for r in rows]

    def generate_pack(
        self, set_code: str, product: str, seed: int | None = None
    ) -> dict:
        """Generate a virtual booster pack from SQLite data."""
        set_code = set_code.lower()
        product_key = product.lower()

        conn = self._connect()

        # 1. Query variants
        variants = conn.execute(
            "SELECT DISTINCT variant_index, variant_weight "
            "FROM mtgjson_booster_configs "
            "WHERE set_code = ? AND product = ? "
            "ORDER BY variant_index",
            (set_code, product_key),
        ).fetchall()

        if not variants:
            conn.close()
            raise ValueError(
                f"No booster data for set '{set_code}' product '{product_key}'"
            )

        if seed is None:
            seed = random.randint(0, 2**31 - 1)
        rng = random.Random(seed)

        # 2. Pick variant by weighted random
        variant_indices = [v["variant_index"] for v in variants]
        weights = [v["variant_weight"] for v in variants]
        total_weight = sum(weights)
        chosen_idx = rng.choices(range(len(variants)), weights=weights, k=1)[0]
        variant_index = variant_indices[chosen_idx]
        variant_weight = weights[chosen_idx]

        # 3. Query contents for chosen variant
        contents = conn.execute(
            "SELECT sheet_name, card_count "
            "FROM mtgjson_booster_configs "
            "WHERE set_code = ? AND product = ? AND variant_index = ?",
            (set_code, product_key, variant_index),
        ).fetchall()

        # 4. Draw cards from each sheet
        pack = []
        all_drawn_uuids = []

        for row in contents:
            sheet_name = row["sheet_name"]
            count = row["card_count"]

            # Get sheet cards with weights
            sheet_cards = conn.execute(
                "SELECT uuid, weight, is_foil "
                "FROM mtgjson_booster_sheets "
                "WHERE set_code = ? AND product = ? AND sheet_name = ?",
                (set_code, product_key, sheet_name),
            ).fetchall()

            if not sheet_cards:
                continue

            is_foil = bool(sheet_cards[0]["is_foil"])
            pool_uuids = [c["uuid"] for c in sheet_cards]
            pool_weights = [c["weight"] for c in sheet_cards]

            # Draw without replacement
            drawn_uuids = []
            for _ in range(min(count, len(pool_uuids))):
                idx = rng.choices(range(len(pool_uuids)), weights=pool_weights, k=1)[0]
                drawn_uuids.append((pool_uuids.pop(idx), sheet_name, is_foil))
                pool_weights.pop(idx)

            all_drawn_uuids.extend(drawn_uuids)

        # 5. Batch lookup card details
        uuids_only = [u[0] for u in all_drawn_uuids]
        if uuids_only:
            placeholders = ",".join("?" * len(uuids_only))
            card_rows = conn.execute(
                f"SELECT * FROM mtgjson_printings WHERE uuid IN ({placeholders})",
                uuids_only,
            ).fetchall()
            card_map = {r["uuid"]: r for r in card_rows}
        else:
            card_map = {}

        conn.close()

        # 6. Build card dicts
        for uuid, sheet_name, is_foil in all_drawn_uuids:
            card = card_map.get(uuid)
            if card is None:
                continue

            scryfall_id = card["scryfall_id"] or ""
            image_uri = ""
            if scryfall_id:
                image_uri = (
                    f"https://cards.scryfall.io/normal/front/"
                    f"{scryfall_id[0]}/{scryfall_id[1]}/{scryfall_id}.jpg"
                )

            ck_url = (card["ck_url_foil"] if is_foil else card["ck_url"]) or ""
            if not ck_url:
                ck_url = card["ck_url"] or ""

            frame_effects = json.loads(card["frame_effects"]) if card["frame_effects"] else []

            pack.append({
                "uuid": uuid,
                "name": card["name"],
                "set_code": card["set_code"],
                "collector_number": card["number"],
                "rarity": card["rarity"] or "",
                "scryfall_id": scryfall_id,
                "image_uri": image_uri,
                "sheet_name": sheet_name,
                "foil": is_foil,
                "border_color": card["border_color"] or "black",
                "frame_effects": frame_effects,
                "is_full_art": bool(card["is_full_art"]),
                "ck_url": ck_url,
            })

        _RARITY_ORDER = {"common": 0, "uncommon": 1, "rare": 2, "mythic": 3}
        pack.sort(key=lambda c: _RARITY_ORDER.get(c["rarity"], 1))

        return {
            "set_code": set_code,
            "seed": seed,
            "variant_index": variant_index,
            "variant_weight": variant_weight,
            "total_weight": total_weight,
            "cards": pack,
        }

    def get_sheet_data(self, set_code: str, product: str) -> dict:
        """Return full booster structure: variants, sheets, and per-card pull rates."""
        set_code = set_code.lower()
        product_key = product.lower()

        conn = self._connect()

        # Query variants
        variants = conn.execute(
            "SELECT DISTINCT variant_index, variant_weight "
            "FROM mtgjson_booster_configs "
            "WHERE set_code = ? AND product = ? "
            "ORDER BY variant_index",
            (set_code, product_key),
        ).fetchall()

        if not variants:
            conn.close()
            raise ValueError(
                f"No booster data for set '{set_code}' product '{product_key}'"
            )

        weights = [v["variant_weight"] for v in variants]
        total_weight = sum(weights)

        # Build variant list with contents
        variant_list = []
        all_sheet_names = set()
        for v in variants:
            contents_rows = conn.execute(
                "SELECT sheet_name, card_count "
                "FROM mtgjson_booster_configs "
                "WHERE set_code = ? AND product = ? AND variant_index = ?",
                (set_code, product_key, v["variant_index"]),
            ).fetchall()
            contents = {r["sheet_name"]: r["card_count"] for r in contents_rows}
            all_sheet_names.update(contents.keys())
            variant_list.append({
                "index": v["variant_index"],
                "weight": v["variant_weight"],
                "probability": v["variant_weight"] / total_weight if total_weight else 0,
                "contents": contents,
            })

        # Build sheet data
        sheet_data = {}
        for sheet_name in all_sheet_names:
            sheet_cards = conn.execute(
                "SELECT bs.uuid, bs.weight, bs.is_foil, "
                "p.scryfall_id, p.name, p.set_code, p.number, p.rarity, "
                "p.border_color, p.is_full_art, p.frame_effects, p.ck_url, p.ck_url_foil "
                "FROM mtgjson_booster_sheets bs "
                "JOIN mtgjson_printings p ON bs.uuid = p.uuid "
                "WHERE bs.set_code = ? AND bs.product = ? AND bs.sheet_name = ?",
                (set_code, product_key, sheet_name),
            ).fetchall()

            if not sheet_cards:
                continue

            is_foil = bool(sheet_cards[0]["is_foil"])
            sheet_total_weight = sum(c["weight"] for c in sheet_cards)

            cards = []
            for c in sheet_cards:
                scryfall_id = c["scryfall_id"] or ""
                image_uri = ""
                if scryfall_id:
                    image_uri = (
                        f"https://cards.scryfall.io/normal/front/"
                        f"{scryfall_id[0]}/{scryfall_id[1]}/{scryfall_id}.jpg"
                    )

                ck_url = (c["ck_url_foil"] if is_foil else c["ck_url"]) or ""
                if not ck_url:
                    ck_url = c["ck_url"] or ""

                frame_effects = json.loads(c["frame_effects"]) if c["frame_effects"] else []

                cards.append({
                    "uuid": c["uuid"],
                    "name": c["name"],
                    "set_code": c["set_code"],
                    "collector_number": c["number"],
                    "rarity": c["rarity"] or "",
                    "scryfall_id": scryfall_id,
                    "image_uri": image_uri,
                    "weight": c["weight"],
                    "pull_rate": c["weight"] / sheet_total_weight if sheet_total_weight else 0,
                    "foil": is_foil,
                    "border_color": c["border_color"] or "black",
                    "frame_effects": frame_effects,
                    "is_full_art": bool(c["is_full_art"]),
                    "ck_url": ck_url,
                })

            def sort_key(c):
                num = c["collector_number"]
                try:
                    return (0, int(num), "")
                except ValueError:
                    return (1, 0, num)

            cards.sort(key=sort_key)

            sheet_data[sheet_name] = {
                "foil": is_foil,
                "total_weight": sheet_total_weight,
                "card_count": len(cards),
                "cards": cards,
            }

        conn.close()

        return {
            "set_code": set_code,
            "product": product_key,
            "total_weight": total_weight,
            "variants": variant_list,
            "sheets": sheet_data,
        }

    def get_ck_url(self, scryfall_id: str, foil: bool = False) -> str:
        """Get Card Kingdom product URL for a card by Scryfall ID."""
        conn = self._connect()
        row = conn.execute(
            "SELECT ck_url, ck_url_foil FROM mtgjson_printings WHERE scryfall_id = ?",
            (scryfall_id,),
        ).fetchone()
        conn.close()
        if not row:
            return ""
        ck_url = (row["ck_url_foil"] if foil else row["ck_url"]) or ""
        if not ck_url:
            ck_url = row["ck_url"] or ""
        return ck_url

    def get_uuid_for_scryfall_id(self, scryfall_id: str) -> str | None:
        """Look up MTGJSON uuid for a Scryfall ID."""
        conn = self._connect()
        row = conn.execute(
            "SELECT uuid FROM mtgjson_printings WHERE scryfall_id = ?",
            (scryfall_id,),
        ).fetchone()
        conn.close()
        return row["uuid"] if row else None
