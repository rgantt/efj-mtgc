"""Scryfall API interface."""

import json
import time
from typing import List, Dict, Optional

import requests

from mtg_collector.db.models import Card, Set, Printing


class ScryfallAPI:
    """Interface to Scryfall API."""

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

    def search_card(
        self,
        name: str,
        set_code: Optional[str] = None,
        collector_number: Optional[str] = None,
        fuzzy: bool = True,
    ) -> List[Dict]:
        """
        Search for card printings.
        If set_code and collector_number provided, tries exact match first.
        Otherwise returns all printings.

        If fuzzy=True (default), will try fuzzy matching if exact search fails.
        """
        # Try exact search first
        results = self._search_exact(name, set_code, collector_number)

        if results:
            return results

        # If exact search failed and fuzzy is enabled, try fuzzy matching
        if fuzzy:
            corrected_name = self._fuzzy_autocomplete(name)
            if corrected_name and corrected_name.lower() != name.lower():
                print(f"    Fuzzy match: '{name}' -> '{corrected_name}'")
                results = self._search_exact(corrected_name, set_code, collector_number)
                if results:
                    return results

            # Try fuzzy search endpoint as last resort
            results = self._search_fuzzy(name)
            if results:
                return results

        return []

    def _search_exact(
        self,
        name: str,
        set_code: Optional[str] = None,
        collector_number: Optional[str] = None,
    ) -> List[Dict]:
        """Search for exact card name match."""
        # Build search query with exact match operator
        if set_code and collector_number:
            query = f'!"{name}" set:{set_code} cn:{collector_number}'
        elif set_code:
            query = f'!"{name}" set:{set_code}'
        else:
            query = f'!"{name}"'

        url = f"{self.BASE_URL}/cards/search"
        params = {"q": query, "unique": "prints", "order": "released", "dir": "desc"}

        try:
            response = self._request_with_retry("GET", url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("object") == "list":
                return data.get("data", [])
            return []

        except requests.exceptions.RequestException:
            return []

    def _fuzzy_autocomplete(self, name: str) -> Optional[str]:
        """Use Scryfall autocomplete to find the correct card name."""
        url = f"{self.BASE_URL}/cards/autocomplete"
        params = {"q": name}

        try:
            response = self._request_with_retry("GET", url, params=params)
            response.raise_for_status()
            data = response.json()

            suggestions = data.get("data", [])
            if suggestions:
                # Return the first suggestion
                return suggestions[0]
            return None

        except requests.exceptions.RequestException:
            return None

    def _search_fuzzy(self, name: str) -> List[Dict]:
        """Use Scryfall's fuzzy named endpoint to find a card."""
        url = f"{self.BASE_URL}/cards/named"
        params = {"fuzzy": name}

        try:
            response = self._request_with_retry("GET", url, params=params)
            response.raise_for_status()
            card = response.json()

            if card.get("object") == "card":
                # Get all printings of this card
                return self._get_all_printings(card["name"])
            return []

        except requests.exceptions.RequestException:
            return []

    def _get_all_printings(self, name: str) -> List[Dict]:
        """Get all printings of a card by exact name."""
        url = f"{self.BASE_URL}/cards/search"
        params = {
            "q": f'!"{name}"',
            "unique": "prints",
            "order": "released",
            "dir": "desc",
        }

        try:
            response = self._request_with_retry("GET", url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("object") == "list":
                return data.get("data", [])
            return []

        except requests.exceptions.RequestException:
            return []

    def get_all_sets(self) -> List[Dict]:
        """
        Fetch all sets from Scryfall.

        Results are cached in memory for the session.
        """
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

    def normalize_set_code(self, set_input: str) -> Optional[str]:
        """
        Normalize a set code or name to a valid Scryfall set code.

        Args:
            set_input: Set code (e.g., "ECL", "ecl") or name (e.g., "Lorwyn Eclipsed")

        Returns:
            Normalized lowercase set code, or None if not found
        """
        set_input = set_input.strip()
        set_input_lower = set_input.lower()

        all_sets = self.get_all_sets()

        # Try exact code match first
        for s in all_sets:
            if s["code"].lower() == set_input_lower:
                return s["code"]

        # Try name match
        for s in all_sets:
            if s["name"].lower() == set_input_lower:
                return s["code"]

        # Try partial name match
        for s in all_sets:
            if set_input_lower in s["name"].lower():
                return s["code"]

        return None

    def get_set_cards(self, set_code: str) -> List[Dict]:
        """
        Fetch all cards in a set from Scryfall.

        Returns list of card dicts with name, id, collector_number, etc.
        Results are cached in memory for the session.

        Note: For persistent caching, use ensure_set_cached() which stores in SQLite.
        """
        if not hasattr(self, "_set_cache"):
            self._set_cache = {}

        set_code = set_code.lower()
        if set_code in self._set_cache:
            return self._set_cache[set_code]

        cards = []
        url = f"{self.BASE_URL}/cards/search"
        params = {"q": f"set:{set_code}", "unique": "prints", "order": "collector_number"}

        while url:
            try:
                response = self._request_with_retry("GET", url, params=params)
                response.raise_for_status()
                data = response.json()

                if data.get("object") == "list":
                    cards.extend(data.get("data", []))

                # Handle pagination
                if data.get("has_more"):
                    url = data.get("next_page")
                    params = {}  # next_page URL includes params
                else:
                    url = None

            except requests.exceptions.RequestException as e:
                print(f"    Error fetching set {set_code}: {e}")
                break

        self._set_cache[set_code] = cards
        return cards

    def get_card_by_id(self, scryfall_id: str) -> Optional[Dict]:
        """Get a specific card by Scryfall ID."""
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
        """Get a specific card by set code and collector number."""
        url = f"{self.BASE_URL}/cards/{set_code.lower()}/{collector_number}"

        try:
            response = self._request_with_retry("GET", url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException:
            return None

    def get_set(self, set_code: str) -> Optional[Dict]:
        """Get set information."""
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
        return Card(
            oracle_id=data["oracle_id"],
            name=data["name"],
            type_line=data.get("type_line"),
            mana_cost=data.get("mana_cost"),
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
        # Get image URI - prefer normal size
        image_uri = None
        if "image_uris" in data:
            image_uri = data["image_uris"].get("normal") or data["image_uris"].get(
                "small"
            )
        elif "card_faces" in data and data["card_faces"]:
            # Double-faced card
            face = data["card_faces"][0]
            if "image_uris" in face:
                image_uri = face["image_uris"].get("normal") or face["image_uris"].get(
                    "small"
                )

        # Store the complete JSON for future reference
        raw_json = json.dumps(data)

        return Printing(
            scryfall_id=data["id"],
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


def cache_scryfall_data(
    api: ScryfallAPI,
    card_repo,
    set_repo,
    printing_repo,
    scryfall_data: Dict,
) -> None:
    """
    Cache Scryfall card data in the database.
    Creates card, set, and printing records as needed.
    """
    # Cache card (oracle level)
    card = api.to_card_model(scryfall_data)
    card_repo.upsert(card)

    # Cache set if not already present
    set_code = scryfall_data["set"]
    if not set_repo.exists(set_code):
        set_data = api.get_set(set_code)
        if set_data:
            set_model = api.to_set_model(set_data)
            set_repo.upsert(set_model)

    # Cache printing
    printing = api.to_printing_model(scryfall_data)
    printing_repo.upsert(printing)


def ensure_set_cached(
    api: ScryfallAPI,
    set_code: str,
    card_repo,
    set_repo,
    printing_repo,
    conn,
) -> bool:
    """
    Ensure a set's full card list is cached in the local database.

    If the set is already cached, does nothing.
    Otherwise fetches all cards from Scryfall and stores them locally.

    Returns True if the set is cached (either already was or just cached).
    Returns False if the set couldn't be fetched.
    """
    set_code = set_code.lower()

    # Check if already cached
    if set_repo.is_cards_cached(set_code):
        return True

    print(f"  Caching card list for set: {set_code.upper()}")

    # Ensure set exists
    if not set_repo.exists(set_code):
        set_data = api.get_set(set_code)
        if not set_data:
            print(f"    Set not found: {set_code}")
            return False
        set_model = api.to_set_model(set_data)
        set_repo.upsert(set_model)

    # Fetch all cards in set
    cards = api.get_set_cards(set_code)
    if not cards:
        print(f"    No cards found in set: {set_code}")
        return False

    print(f"    Fetched {len(cards)} cards")

    # Cache each card and printing
    for card_data in cards:
        if "oracle_id" not in card_data:
            continue  # Skip tokens and other cards without oracle identity
        card = api.to_card_model(card_data)
        card_repo.upsert(card)

        # Don't overwrite printings that already exist from direct API lookups —
        # /cards/{set}/{cn} returns the canonical scryfall_id, search results may not.
        cn = card_data["collector_number"]
        if not printing_repo.get_by_set_cn(set_code, cn):
            printing = api.to_printing_model(card_data)
            printing_repo.upsert(printing)

    # Mark set as cached
    set_repo.mark_cards_cached(set_code)
    conn.commit()

    return True


