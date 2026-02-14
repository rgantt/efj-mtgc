"""Deckbox CSV importer."""

import csv
from typing import List, Dict, Any, Tuple, Optional

from mtg_collector.importers.base import BaseImporter
from mtg_collector.db.models import CollectionEntry
from mtg_collector.utils import normalize_condition, now_iso


class DeckboxImporter(BaseImporter):
    """Import from Deckbox CSV format."""

    @property
    def format_name(self) -> str:
        return "Deckbox"

    @property
    def source_name(self) -> str:
        return "deckbox_import"

    def parse_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Parse Deckbox CSV file."""
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows

    def row_to_lookup(self, row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], int]:
        """Convert Deckbox row to lookup parameters."""
        name = row.get("Name", "").strip()

        # Deckbox uses full set name in "Edition" column
        # We need to map it to set code - this is tricky
        # For now, we'll rely on the collector number search
        edition = row.get("Edition", "").strip()
        collector_number = row.get("Card Number", "").strip() or None

        # Store edition for potential future use
        row["_edition_name"] = edition

        # Get quantity
        try:
            quantity = int(row.get("Count", 1))
        except (ValueError, TypeError):
            quantity = 1

        # Deckbox doesn't provide set code directly
        # We'll search by name and collector number
        return name, None, collector_number, quantity

    def row_to_entry(self, row: Dict[str, Any], scryfall_id: str) -> CollectionEntry:
        """Convert Deckbox row to CollectionEntry."""
        # Determine finish
        foil_val = row.get("Foil", "").strip().lower()
        finish = "foil" if foil_val in ("foil", "yes", "true", "1") else "nonfoil"

        # Normalize condition - Deckbox uses full names
        condition = normalize_condition(row.get("Condition", "Near Mint"))

        # Get language
        language = row.get("Language", "English").strip() or "English"

        # Get purchase price
        price_str = row.get("My Price", "").strip()
        purchase_price = None
        if price_str:
            try:
                price_str = price_str.replace("$", "").replace(",", "")
                purchase_price = float(price_str)
            except ValueError:
                pass

        # Get Deckbox-specific flags
        signed = row.get("Signed", "").strip().lower() in ("signed", "yes", "true", "1")
        alter = row.get("Altered Art", "").strip().lower() in ("altered", "yes", "true", "1")
        misprint = row.get("Misprint", "").strip().lower() in ("misprint", "yes", "true", "1")

        # Check tradelist → status
        tradelist_str = row.get("Tradelist Count", "").strip()
        tradelist = False
        status = "owned"
        if tradelist_str:
            try:
                if int(tradelist_str) > 0:
                    tradelist = True
                    status = "listed"
            except ValueError:
                pass

        return CollectionEntry(
            id=None,
            scryfall_id=scryfall_id,
            finish=finish,
            condition=condition,
            language=language,
            purchase_price=purchase_price,
            acquired_at=now_iso(),
            source=self.source_name,
            notes=None,
            tags=None,
            tradelist=tradelist,
            alter=alter,
            proxy=False,  # Deckbox doesn't have proxy field in standard export
            signed=signed,
            misprint=misprint,
            status=status,
        )
