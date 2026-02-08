"""Pack generation engine using MTGJSON AllPrintings.json booster data."""

import json
import random
from pathlib import Path
from typing import Optional


class PackGenerator:
    """Generate virtual booster packs from MTGJSON data."""

    def __init__(self, mtgjson_path: Optional[Path] = None):
        if mtgjson_path is None:
            mtgjson_path = Path.home() / ".mtgc" / "AllPrintings.json"
        self.mtgjson_path = mtgjson_path
        self._data = None
        self._card_indexes: dict[str, dict[str, dict]] = {}  # set_code -> {uuid -> card}

    @property
    def data(self) -> dict:
        if self._data is None:
            with open(self.mtgjson_path) as f:
                self._data = json.load(f)
        return self._data

    def _get_set(self, set_code: str) -> dict:
        set_code = set_code.upper()
        if set_code not in self.data["data"]:
            raise ValueError(f"Set '{set_code}' not found")
        return self.data["data"][set_code]

    def _build_card_index(self, set_code: str, booster: dict) -> dict[str, dict]:
        """Build UUID→card lookup for a set and its source sets."""
        set_code = set_code.upper()
        if set_code in self._card_indexes:
            return self._card_indexes[set_code]

        index = {}
        # Collect cards from source sets (includes the set itself typically)
        source_codes = booster.get("sourceSetCodes", [set_code])
        for code in source_codes:
            code_upper = code.upper()
            if code_upper in self.data["data"]:
                for card in self.data["data"][code_upper].get("cards", []):
                    index[card["uuid"]] = card

        # Also index the main set's cards in case sourceSetCodes doesn't include it
        for card in self._get_set(set_code).get("cards", []):
            index[card["uuid"]] = card

        self._card_indexes[set_code] = index
        return index

    def list_sets(self) -> list[tuple[str, str]]:
        """Return (code, name) for sets that have booster data, sorted by name."""
        results = []
        for code, set_data in self.data["data"].items():
            if "booster" in set_data and set_data["booster"]:
                results.append((code, set_data.get("name", code)))
        return sorted(results, key=lambda x: x[1])

    def list_products(self, set_code: str) -> list[str]:
        """Return available booster product types for a set."""
        set_data = self._get_set(set_code)
        if "booster" not in set_data:
            return []
        return list(set_data["booster"].keys())

    def generate_pack(
        self, set_code: str, product: str, seed: int | None = None
    ) -> dict:
        """
        Generate a virtual booster pack.

        Returns a dict with:
            seed: the seed used for generation
            variant_index: which variant was selected (0-based)
            variant_weight: weight of the selected variant
            total_weight: sum of all variant weights
            cards: list of card dicts
        """
        set_code = set_code.upper()
        set_data = self._get_set(set_code)

        if "booster" not in set_data:
            raise ValueError(f"Set '{set_code}' has no booster data")

        product_key = product.lower()
        if product_key not in set_data["booster"]:
            available = list(set_data["booster"].keys())
            raise ValueError(
                f"Product '{product}' not found for '{set_code}'. Available: {available}"
            )

        booster = set_data["booster"][product_key]
        sheets = booster.get("sheets", {})
        variants = booster.get("boosters", [])

        if not variants:
            raise ValueError(f"No booster variants defined for {set_code} {product}")

        if seed is None:
            seed = random.randint(0, 2**31 - 1)
        rng = random.Random(seed)

        # Pick a variant by weight
        weights = [v.get("weight", 1) for v in variants]
        variant_index = rng.choices(range(len(variants)), weights=weights, k=1)[0]
        variant = variants[variant_index]

        # Build card index for UUID lookups
        card_index = self._build_card_index(set_code, booster)

        # Draw cards from each slot
        pack = []
        for sheet_name, count in variant.get("contents", {}).items():
            if sheet_name not in sheets:
                continue

            sheet = sheets[sheet_name]
            card_uuids = list(sheet.get("cards", {}).keys())
            card_weights = list(sheet.get("cards", {}).values())

            if not card_uuids:
                continue

            drawn_uuids = rng.choices(card_uuids, weights=card_weights, k=count)

            is_foil = sheet.get("foil", False)

            for uuid in drawn_uuids:
                card = card_index.get(uuid)
                if card is None:
                    continue

                scryfall_id = card.get("identifiers", {}).get("scryfallId", "")
                image_uri = ""
                if scryfall_id:
                    image_uri = (
                        f"https://cards.scryfall.io/normal/front/"
                        f"{scryfall_id[0]}/{scryfall_id[1]}/{scryfall_id}.jpg"
                    )

                purchase_urls = card.get("purchaseUrls", {})
                ck_url = purchase_urls.get("cardKingdomFoil" if is_foil else "cardKingdom", "")
                if not ck_url:
                    ck_url = purchase_urls.get("cardKingdom", "")

                pack.append({
                    "name": card.get("name", "Unknown"),
                    "set_code": card.get("setCode", set_code),
                    "collector_number": card.get("number", ""),
                    "rarity": card.get("rarity", ""),
                    "scryfall_id": scryfall_id,
                    "image_uri": image_uri,
                    "sheet_name": sheet_name,
                    "foil": is_foil,
                    "border_color": card.get("borderColor", "black"),
                    "frame_effects": card.get("frameEffects") or [],
                    "is_full_art": card.get("isFullArt", False),
                    "ck_url": ck_url,
                })

        return {
            "seed": seed,
            "variant_index": variant_index,
            "variant_weight": variant.get("weight", 1),
            "total_weight": sum(weights),
            "cards": pack,
        }
