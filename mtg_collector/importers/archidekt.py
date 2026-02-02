"""Archidekt CSV importer."""

import csv
from typing import List, Dict, Any, Tuple, Optional

from mtg_collector.importers.base import BaseImporter
from mtg_collector.db.models import CollectionEntry
from mtg_collector.utils import now_iso


class ArchidektImporter(BaseImporter):
    """Import from Archidekt CSV format."""

    @property
    def format_name(self) -> str:
        return "Archidekt"

    @property
    def source_name(self) -> str:
        return "archidekt_import"

    def parse_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Parse Archidekt CSV file (semicolon-separated)."""
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            # Archidekt uses semicolon separator
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                rows.append(row)
        return rows

    def row_to_lookup(self, row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], int]:
        """Convert Archidekt row to lookup parameters."""
        # Archidekt provides scryfall_uuid which is the best identifier
        # But we also need name for error messages
        name = row.get("card_name", "").strip() or row.get("english_card_name", "").strip()
        set_code = row.get("set_code", "").strip() or None
        collector_number = row.get("collector_number", "").strip() or None

        # Archidekt has separate quantity and foil_quantity
        try:
            quantity = int(row.get("quantity", 0))
        except (ValueError, TypeError):
            quantity = 0

        try:
            foil_quantity = int(row.get("foil_quantity", 0))
        except (ValueError, TypeError):
            foil_quantity = 0

        # Store foil_quantity in row for later use
        row["_parsed_foil_quantity"] = foil_quantity

        return name, set_code, collector_number, quantity + foil_quantity

    def row_to_entry(self, row: Dict[str, Any], scryfall_id: str) -> CollectionEntry:
        """Convert Archidekt row to CollectionEntry."""
        # Archidekt tracks foil separately - we need to handle this
        # For simplicity, we'll create entries based on which quantity pool we're drawing from
        foil_qty = row.get("_parsed_foil_quantity", 0)
        regular_qty = int(row.get("quantity", 0))

        # Determine finish - if we still have foil quantity to consume, use foil
        # This is a simplification; the actual tracking happens in the importer
        if foil_qty > 0:
            finish = "foil"
            row["_parsed_foil_quantity"] = foil_qty - 1
        else:
            finish = "nonfoil"

        # Map language code to full name
        lang_code = row.get("lang", "en").strip().lower()
        language = self._code_to_language(lang_code)

        return CollectionEntry(
            id=None,
            scryfall_id=scryfall_id,
            finish=finish,
            condition="Near Mint",  # Archidekt doesn't track condition
            language=language,
            purchase_price=None,
            acquired_at=now_iso(),
            source=self.source_name,
            notes=None,
            tags=None,
            tradelist=False,
            alter=False,
            proxy=False,
            signed=False,
            misprint=False,
        )

    def _resolve_card(self, api, name, set_code, collector_number):
        """Override to use scryfall_uuid if available."""
        # Check if we have the scryfall_uuid from the current row context
        # This is a bit of a hack since we don't have direct access to the row here
        # The base class will handle this via set_code/collector_number
        return super()._resolve_card(api, name, set_code, collector_number)

    def _code_to_language(self, code: str) -> str:
        """Convert language code to full name."""
        mapping = {
            "en": "English",
            "es": "Spanish",
            "fr": "French",
            "de": "German",
            "it": "Italian",
            "pt": "Portuguese",
            "ja": "Japanese",
            "ko": "Korean",
            "ru": "Russian",
            "zhs": "Chinese Simplified",
            "zht": "Chinese Traditional",
            "ph": "Phyrexian",
        }
        return mapping.get(code, "English")
