"""Scryfall bulk import client — used only by cache/setup commands to populate the local DB."""

import json
import time
from typing import Dict, List, Optional

import requests

from mtg_collector.db.models import Card, Printing, Set


class ScryfallBulkClient:
    """Scryfall API client for bulk data import only.

    This class should only be used by CLI cache commands (mtg setup, mtg cache,
    mtg db recache/refresh) to populate the local SQLite database. All runtime
    card lookups must use the local database directly.
    """

    BASE_URL = "https://api.scryfall.com"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MTGCollectionTool/2.0"})
        self.last_request = 0

    def _rate_limit(self):
        """Respect Scryfall's rate limit (100ms between requests)."""
        elapsed = time.time() - self.last_request
        if elapsed < 0.1:
            time.sleep(0.1 - elapsed)
        self.last_request = time.time()

    def _request_with_retry(self, method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
        """Make an HTTP request with retry on 429 rate limit errors."""
        for attempt in range(max_retries + 1):
            self._rate_limit()
            response = self.session.request(method, url, **kwargs)
            if response.status_code == 429:
                wait = 0.5 * (2 ** attempt)
                print(f"    Scryfall rate limited, retrying in {wait:.1f}s...")
                time.sleep(wait)
                continue
            return response
        return response  # Return last response even if still 429

    def get_all_sets(self) -> List[Dict]:
        """Fetch all sets from Scryfall. Results cached in memory for the session."""
        if not hasattr(self, "_all_sets_cache"):
            try:
                response = self._request_with_retry("GET", f"{self.BASE_URL}/sets")
                response.raise_for_status()
                data = response.json()
                self._all_sets_cache = data.get("data", [])
            except requests.exceptions.RequestException as e:
                print(f"    Error fetching sets: {e}")
                self._all_sets_cache = []

        return self._all_sets_cache

    def get_set_cards(self, set_code: str) -> List[Dict]:
        """Fetch all cards in a set from Scryfall with pagination."""
        if not hasattr(self, "_set_cache"):
            self._set_cache = {}

        set_code = set_code.lower()
        if set_code in self._set_cache:
            return self._set_cache[set_code]

        cards = []
        url = f"{self.BASE_URL}/cards/search"
        params = {"q": f"set:{set_code} lang:en", "unique": "prints", "order": "collector_number"}

        while url:
            try:
                response = self._request_with_retry("GET", url, params=params)
                response.raise_for_status()
                data = response.json()

                if data.get("object") == "list":
                    cards.extend(data.get("data", []))

                if data.get("has_more"):
                    url = data.get("next_page")
                    params = {}
                else:
                    url = None

            except requests.exceptions.RequestException as e:
                print(f"    Error fetching set {set_code}: {e}")
                break

        self._set_cache[set_code] = cards
        return cards

    def get_card_by_id(self, scryfall_id: str) -> Optional[Dict]:
        """Get a specific card by Scryfall ID (for cache refresh)."""
        url = f"{self.BASE_URL}/cards/{scryfall_id}"

        try:
            response = self._request_with_retry("GET", url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException:
            return None

    def get_card_by_set_cn(
        self, set_code: str, collector_number: str
    ) -> Optional[Dict]:
        """Get a specific card by set code and collector number (for cache maintenance)."""
        url = f"{self.BASE_URL}/cards/{set_code.lower()}/{collector_number}"

        try:
            response = self._request_with_retry("GET", url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException:
            return None

    def get_set(self, set_code: str) -> Optional[Dict]:
        """Get set information from Scryfall."""
        url = f"{self.BASE_URL}/sets/{set_code.lower()}"

        try:
            response = self._request_with_retry("GET", url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException:
            return None

    # Conversion methods for database storage

    def to_card_model(self, data: Dict) -> Card:
        """Convert Scryfall API response to Card model."""
        mana_cost = data.get("mana_cost")
        if not mana_cost and "card_faces" in data:
            face_manas = [f.get("mana_cost", "") for f in data["card_faces"]]
            mana_cost = " // ".join(m for m in face_manas if m) or None
        return Card(
            oracle_id=data["oracle_id"],
            name=data["name"],
            type_line=data.get("type_line"),
            mana_cost=mana_cost,
            cmc=data.get("cmc"),
            oracle_text=data.get("oracle_text"),
            colors=data.get("colors", []),
            color_identity=data.get("color_identity", []),
        )

    def to_set_model(self, data: Dict) -> Set:
        """Convert Scryfall API set response to Set model."""
        return Set(
            set_code=data["code"],
            set_name=data["name"],
            set_type=data.get("set_type"),
            released_at=data.get("released_at"),
        )

    def to_printing_model(self, data: Dict) -> Printing:
        """Convert Scryfall API response to Printing model."""
        image_uri = None
        if "image_uris" in data:
            image_uri = data["image_uris"].get("normal") or data["image_uris"].get(
                "small"
            )
        elif "card_faces" in data and data["card_faces"]:
            face = data["card_faces"][0]
            if "image_uris" in face:
                image_uri = face["image_uris"].get("normal") or face["image_uris"].get(
                    "small"
                )

        raw_json = json.dumps(data)

        return Printing(
            printing_id=data["id"],
            oracle_id=data["oracle_id"],
            set_code=data["set"],
            collector_number=data["collector_number"],
            rarity=data.get("rarity"),
            frame_effects=data.get("frame_effects", []),
            border_color=data.get("border_color"),
            full_art=data.get("full_art", False),
            promo=data.get("promo", False),
            promo_types=data.get("promo_types", []),
            finishes=data.get("finishes", []),
            artist=data.get("artist"),
            image_uri=image_uri,
            raw_json=raw_json,
        )


def cache_card_data(
    api: ScryfallBulkClient,
    card_repo,
    set_repo,
    printing_repo,
    card_data: Dict,
) -> None:
    """Cache card data from a Scryfall API response in the database."""
    card = api.to_card_model(card_data)
    card_repo.upsert(card)

    set_code = card_data["set"]
    if not set_repo.exists(set_code):
        set_data = api.get_set(set_code)
        if set_data:
            set_model = api.to_set_model(set_data)
            set_repo.upsert(set_model)

    printing = api.to_printing_model(card_data)
    printing_repo.upsert(printing)


def ensure_set_populated(
    api: ScryfallBulkClient,
    set_code: str,
    card_repo,
    set_repo,
    printing_repo,
    conn,
) -> bool:
    """Ensure a set's full card list is cached in the local database.

    Returns True if the set is cached, False if fetch failed.
    """
    set_code = set_code.lower()

    if set_repo.is_cards_cached(set_code):
        return True

    print(f"  Caching card list for set: {set_code.upper()}")

    if not set_repo.exists(set_code):
        set_data = api.get_set(set_code)
        if not set_data:
            print(f"    Set not found: {set_code}")
            return False
        set_model = api.to_set_model(set_data)
        set_repo.upsert(set_model)

    cards = api.get_set_cards(set_code)
    if not cards:
        print(f"    No cards found in set: {set_code}")
        return False

    print(f"    Fetched {len(cards)} cards")

    for card_data in cards:
        if "oracle_id" not in card_data:
            continue
        card = api.to_card_model(card_data)
        card_repo.upsert(card)

        cn = card_data["collector_number"]
        if not printing_repo.get_by_set_cn(set_code, cn):
            printing = api.to_printing_model(card_data)
            printing_repo.upsert(printing)

    set_repo.mark_cards_cached(set_code)
    conn.commit()

    return True
