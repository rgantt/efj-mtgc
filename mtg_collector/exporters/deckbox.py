"""Deckbox CSV exporter."""

import csv
from typing import Dict, Any
import sqlite3

from mtg_collector.exporters.base import BaseExporter


class DeckboxExporter(BaseExporter):
    """Export to Deckbox CSV format."""

    HEADERS = [
        "Count",
        "Tradelist Count",
        "Name",
        "Edition",
        "Card Number",
        "Condition",
        "Language",
        "Foil",
        "Signed",
        "Artist Proof",
        "Altered Art",
        "Misprint",
        "Promo",
        "Textless",
        "My Price",
    ]

    @property
    def format_name(self) -> str:
        return "Deckbox"

    @property
    def file_extension(self) -> str:
        return ".csv"

    def export(self, conn: sqlite3.Connection, output_path: str, filters: Dict[str, Any] = None) -> int:
        """Export collection to Deckbox CSV format."""
        entries = self.get_collection_data(conn, filters)

        if not entries:
            return 0

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.HEADERS)
            writer.writeheader()

            # Deckbox expects individual rows (we could aggregate, but individual is safer for round-trip)
            for entry in entries:
                row = {
                    "Count": 1,
                    "Tradelist Count": 1 if entry["tradelist"] else "",
                    "Name": entry["name"],
                    "Edition": entry["set_name"],  # Deckbox uses full set name
                    "Card Number": entry["collector_number"],
                    "Condition": entry["condition"],  # Deckbox uses full names like "Near Mint"
                    "Language": entry["language"],
                    "Foil": "foil" if entry["finish"] in ("foil", "etched") else "",
                    "Signed": "signed" if entry["signed"] else "",
                    "Artist Proof": "",  # We don't track this
                    "Altered Art": "altered" if entry["is_alter"] else "",
                    "Misprint": "misprint" if entry["misprint"] else "",
                    "Promo": "",  # Could derive from printing.promo but Deckbox might handle differently
                    "Textless": "",  # We don't track this
                    "My Price": f"{entry['purchase_price']:.2f}" if entry["purchase_price"] else "",
                }
                writer.writerow(row)

        return len(entries)
