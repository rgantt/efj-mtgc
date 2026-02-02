"""Archidekt CSV exporter."""

import csv
from collections import defaultdict
from typing import Dict, Any
import sqlite3

from mtg_collector.exporters.base import BaseExporter


class ArchidektExporter(BaseExporter):
    """Export to Archidekt CSV format."""

    HEADERS = [
        "export_type",
        "scryfall_uuid",
        "set_code",
        "quantity",
        "foil_quantity",
        "card_name",
        "set_name",
        "cardMarketId",
        "english_card_name",
        "lang",
        "collector_number",
    ]

    @property
    def format_name(self) -> str:
        return "Archidekt"

    @property
    def file_extension(self) -> str:
        return ".csv"

    def export(self, conn: sqlite3.Connection, output_path: str, filters: Dict[str, Any] = None) -> int:
        """Export collection to Archidekt CSV format (semicolon-separated)."""
        entries = self.get_collection_data(conn, filters)

        if not entries:
            return 0

        # Aggregate by (scryfall_id, language) - Archidekt uses scryfall_uuid as unique identifier
        aggregated = defaultdict(lambda: {
            "quantity": 0,
            "foil_quantity": 0,
            "entry": None,
        })

        for entry in entries:
            key = (entry["scryfall_id"], entry["language"])
            aggregated[key]["entry"] = entry

            if entry["finish"] in ("foil", "etched"):
                aggregated[key]["foil_quantity"] += 1
            else:
                aggregated[key]["quantity"] += 1

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.HEADERS, delimiter=";")
            writer.writeheader()

            for key, agg in aggregated.items():
                scryfall_id, language = key
                entry = agg["entry"]

                # Map language to Archidekt format
                lang_code = self._language_to_code(language)

                row = {
                    "export_type": "collection",
                    "scryfall_uuid": scryfall_id,
                    "set_code": entry["set_code"].upper(),
                    "quantity": agg["quantity"],
                    "foil_quantity": agg["foil_quantity"],
                    "card_name": entry["name"],
                    "set_name": entry["set_name"],
                    "cardMarketId": "",  # We don't have this
                    "english_card_name": entry["name"],
                    "lang": lang_code,
                    "collector_number": entry["collector_number"],
                }
                writer.writerow(row)

        return len(entries)

    def _language_to_code(self, language: str) -> str:
        """Convert language name to Scryfall/Archidekt language code."""
        mapping = {
            "English": "en",
            "Spanish": "es",
            "French": "fr",
            "German": "de",
            "Italian": "it",
            "Portuguese": "pt",
            "Japanese": "ja",
            "Korean": "ko",
            "Russian": "ru",
            "Chinese Simplified": "zhs",
            "Chinese Traditional": "zht",
            "Phyrexian": "ph",
        }
        return mapping.get(language, "en")
