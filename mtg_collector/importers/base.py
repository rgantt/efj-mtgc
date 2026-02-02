"""Base importer interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
import sqlite3

from mtg_collector.db.models import CollectionEntry
from mtg_collector.services.scryfall import ScryfallAPI


@dataclass
class ImportResult:
    """Result of an import operation."""

    total_rows: int = 0
    cards_added: int = 0
    cards_skipped: int = 0
    errors: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class BaseImporter(ABC):
    """Abstract base class for collection importers."""

    @property
    @abstractmethod
    def format_name(self) -> str:
        """Human-readable format name."""
        pass

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Source identifier for imported cards."""
        pass

    @abstractmethod
    def parse_file(self, file_path: str) -> List[Dict[str, Any]]:
        """
        Parse the import file and return raw row data.

        Args:
            file_path: Path to import file

        Returns:
            List of dicts, one per row
        """
        pass

    @abstractmethod
    def row_to_lookup(self, row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], int]:
        """
        Convert a row to Scryfall lookup parameters.

        Args:
            row: Parsed row dict

        Returns:
            Tuple of (card_name, set_code, collector_number, quantity)
        """
        pass

    @abstractmethod
    def row_to_entry(self, row: Dict[str, Any], scryfall_id: str) -> CollectionEntry:
        """
        Convert a row to a CollectionEntry.

        Args:
            row: Parsed row dict
            scryfall_id: Resolved Scryfall ID

        Returns:
            CollectionEntry ready to insert
        """
        pass

    def import_file(
        self,
        file_path: str,
        conn: sqlite3.Connection,
        card_repo,
        set_repo,
        printing_repo,
        collection_repo,
        api: ScryfallAPI,
        dry_run: bool = False,
    ) -> ImportResult:
        """
        Import a file into the collection.

        Args:
            file_path: Path to import file
            conn: Database connection
            card_repo, set_repo, printing_repo, collection_repo: Repositories
            api: Scryfall API instance
            dry_run: If True, don't actually insert

        Returns:
            ImportResult with statistics
        """
        from mtg_collector.services.scryfall import cache_scryfall_data

        result = ImportResult()

        rows = self.parse_file(file_path)
        result.total_rows = len(rows)

        for row in rows:
            try:
                name, set_code, collector_number, quantity = self.row_to_lookup(row)

                if not name:
                    result.cards_skipped += 1
                    continue

                # Try to resolve the card
                scryfall_data = self._resolve_card(api, name, set_code, collector_number)

                if not scryfall_data:
                    result.errors.append(f"Could not find: {name} ({set_code or 'any set'})")
                    result.cards_skipped += 1
                    continue

                if not dry_run:
                    # Cache Scryfall data
                    cache_scryfall_data(api, card_repo, set_repo, printing_repo, scryfall_data)

                    # Add to collection (one entry per quantity)
                    for _ in range(quantity):
                        entry = self.row_to_entry(row, scryfall_data["id"])
                        collection_repo.add(entry)

                result.cards_added += quantity

            except Exception as e:
                result.errors.append(f"Error processing row: {e}")
                result.cards_skipped += 1

        if not dry_run:
            conn.commit()

        return result

    def _resolve_card(
        self,
        api: ScryfallAPI,
        name: str,
        set_code: Optional[str],
        collector_number: Optional[str],
    ) -> Optional[Dict]:
        """Resolve a card using Scryfall API."""
        # Try exact match first if we have set/cn
        if set_code and collector_number:
            data = api.get_card_by_set_cn(set_code, collector_number)
            if data:
                return data

        # Fall back to search
        printings = api.search_card(name, set_code, collector_number)
        if printings:
            # If we have a set hint, try to match it
            if set_code:
                for p in printings:
                    if p.get("set", "").lower() == set_code.lower():
                        return p
            # Return first match
            return printings[0]

        return None
