"""Text deck list importer (Moxfield/MTGO/generic format).

Handles lines like:
    1 Auntie Ool, Cursewretch (ECC) 2 *F*
    6 Forest (ECL) 283

Format: <quantity> <card name> (<set_code>) <collector_number> [*F*]
"""

from typing import Any, Dict, List, Optional, Tuple

from mtg_collector.db.models import CollectionEntry
from mtg_collector.importers.base import BaseImporter
from mtg_collector.utils import now_iso


class ParseError(ValueError):
    """A line in the deck list could not be parsed."""

    def __init__(self, line_number: int, line_text: str, reason: str):
        self.line_number = line_number
        self.line_text = line_text
        self.reason = reason
        super().__init__(f"Line {line_number}: {reason}: {line_text!r}")


def parse_line(line: str, line_number: int) -> Dict[str, Any]:
    """Parse a single deck list line into a structured dict.

    Structure: <quantity> <card name> (<set_code>) <collector_number> [*F*]
    The parenthesized set code is the structural anchor.
    """
    # Step 1: Find the quantity (leading integer separated by space)
    space_idx = line.find(" ")
    if space_idx == -1:
        raise ParseError(line_number, line, "expected '<quantity> <card name> (<set>) <number>'")

    qty_str = line[:space_idx]
    if not qty_str.isdigit():
        raise ParseError(line_number, line, f"expected quantity as first token, got {qty_str!r}")
    quantity = int(qty_str)

    rest = line[space_idx + 1:]

    # Step 2: Find the set code — last parenthesized group "(XXX)"
    open_paren = rest.rfind("(")
    close_paren = rest.rfind(")")

    if open_paren == -1 or close_paren == -1 or close_paren < open_paren:
        raise ParseError(line_number, line, "missing set code — expected '(SET)' somewhere in the line")

    set_code = rest[open_paren + 1:close_paren].strip()
    if not set_code:
        raise ParseError(line_number, line, "empty set code in parentheses")

    # Step 3: Card name is everything before the opening paren, stripped
    card_name = rest[:open_paren].strip()
    if not card_name:
        raise ParseError(line_number, line, "missing card name before set code")

    # Step 4: After the closing paren — collector number and optional flags
    after_set = rest[close_paren + 1:].strip()
    if not after_set:
        raise ParseError(line_number, line, "missing collector number after set code")

    # Split the remainder into tokens: first token is collector number, rest are flags
    tokens = after_set.split()
    collector_number = tokens[0]
    flags = tokens[1:]

    foil = "*F*" in flags

    return {
        "Count": str(quantity),
        "Name": card_name,
        "Edition": set_code,
        "Collector Number": collector_number,
        "Foil": "foil" if foil else "",
    }


class DecklistImporter(BaseImporter):
    """Import from text deck list format (Moxfield export, MTGO, etc.)."""

    @property
    def format_name(self) -> str:
        return "Decklist"

    @property
    def source_name(self) -> str:
        return "decklist_import"

    def parse_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Parse text deck list file, one card per line.

        Raises ParseError with line number and reason on malformed lines.
        """
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                rows.append(parse_line(line, line_number))
        return rows

    def row_to_lookup(self, row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], int]:
        """Convert deck list row to lookup parameters."""
        name = row.get("Name", "").strip()
        set_code = row.get("Edition", "").strip() or None
        collector_number = row.get("Collector Number", "").strip() or None

        try:
            quantity = int(row.get("Count", 1))
        except (ValueError, TypeError):
            quantity = 1

        return name, set_code, collector_number, quantity

    def row_to_entry(self, row: Dict[str, Any], scryfall_id: str) -> CollectionEntry:
        """Convert deck list row to CollectionEntry."""
        foil_val = row.get("Foil", "").strip().lower()
        finish = "foil" if foil_val in ("foil", "yes", "true", "1") else "nonfoil"

        return CollectionEntry(
            id=None,
            scryfall_id=scryfall_id,
            finish=finish,
            condition="Near Mint",
            language="English",
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
