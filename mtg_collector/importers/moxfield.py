"""Moxfield CSV importer."""

import csv
from typing import List, Dict, Any, Tuple, Optional

from mtg_collector.importers.base import BaseImporter
from mtg_collector.db.models import CollectionEntry
from mtg_collector.utils import normalize_condition, normalize_finish, now_iso


class MoxfieldImporter(BaseImporter):
    """Import from Moxfield CSV format."""

    @property
    def format_name(self) -> str:
        return "Moxfield"

    @property
    def source_name(self) -> str:
        return "moxfield_import"

    def parse_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Parse Moxfield CSV file."""
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows

    def row_to_lookup(self, row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], int]:
        """Convert Moxfield row to lookup parameters."""
        name = row.get("Name", "").strip()
        set_code = row.get("Edition", "").strip() or None
        collector_number = row.get("Collector Number", "").strip() or None

        # Get quantity
        try:
            quantity = int(row.get("Count", 1))
        except (ValueError, TypeError):
            quantity = 1

        return name, set_code, collector_number, quantity

    def row_to_entry(self, row: Dict[str, Any], scryfall_id: str) -> CollectionEntry:
        """Convert Moxfield row to CollectionEntry."""
        # Determine finish
        foil_val = row.get("Foil", "").strip().lower()
        if foil_val in ("foil", "etched", "yes", "true", "1"):
            finish = "foil"
        else:
            finish = "nonfoil"

        # Normalize condition
        condition = normalize_condition(row.get("Condition", "Near Mint"))

        # Get language
        language = row.get("Language", "English").strip() or "English"

        # Get purchase price
        price_str = row.get("Purchase Price", "").strip()
        purchase_price = None
        if price_str:
            try:
                # Remove currency symbols
                price_str = price_str.replace("$", "").replace(",", "")
                purchase_price = float(price_str)
            except ValueError:
                pass

        # Get flags
        alter = row.get("Alter", "").strip().lower() in ("alter", "yes", "true", "1")
        proxy = row.get("Proxy", "").strip().lower() in ("proxy", "yes", "true", "1")

        # Check tradelist
        tradelist_str = row.get("Tradelist Count", "").strip()
        tradelist = False
        if tradelist_str:
            try:
                tradelist = int(tradelist_str) > 0
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
            tags=row.get("Tags", "").strip() or None,
            tradelist=tradelist,
            alter=alter,
            proxy=proxy,
            signed=False,
            misprint=False,
        )
