"""Moxfield CSV exporter."""

import csv
from collections import defaultdict
from typing import Dict, Any
import sqlite3

from mtg_collector.exporters.base import BaseExporter


class MoxfieldExporter(BaseExporter):
    """Export to Moxfield CSV format."""

    HEADERS = [
        "Count",
        "Tradelist Count",
        "Name",
        "Edition",
        "Condition",
        "Language",
        "Foil",
        "Tags",
        "Last Modified",
        "Collector Number",
        "Alter",
        "Proxy",
        "Purchase Price",
    ]

    @property
    def format_name(self) -> str:
        return "Moxfield"

    @property
    def file_extension(self) -> str:
        return ".csv"

    def export(self, conn: sqlite3.Connection, output_path: str, filters: Dict[str, Any] = None) -> int:
        """Export collection to Moxfield CSV format."""
        entries = self.get_collection_data(conn, filters)

        if not entries:
            return 0

        # Aggregate by (name, set, collector_number, condition, finish, language)
        # Moxfield expects aggregated counts
        aggregated = defaultdict(lambda: {
            "count": 0,
            "tradelist_count": 0,
            "entries": [],
        })

        for entry in entries:
            key = (
                entry["name"],
                entry["set_code"],
                entry["collector_number"],
                entry["condition"],
                entry["finish"],
                entry["language"],
            )
            aggregated[key]["count"] += 1
            if entry.get("status") == "listed":
                aggregated[key]["tradelist_count"] += 1
            aggregated[key]["entries"].append(entry)

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.HEADERS)
            writer.writeheader()

            for key, agg in aggregated.items():
                name, set_code, collector_number, condition, finish, language = key
                first_entry = agg["entries"][0]

                # Calculate average purchase price if any have prices
                prices = [e["purchase_price"] for e in agg["entries"] if e["purchase_price"] is not None]
                avg_price = sum(prices) / len(prices) if prices else None

                # Check if any entry has alter/proxy flags
                has_alter = any(e["is_alter"] for e in agg["entries"])
                has_proxy = any(e["proxy"] for e in agg["entries"])

                row = {
                    "Count": agg["count"],
                    "Tradelist Count": agg["tradelist_count"] if agg["tradelist_count"] > 0 else "",
                    "Name": name,
                    "Edition": set_code.lower(),
                    "Condition": condition,
                    "Language": language,
                    "Foil": "foil" if finish in ("foil", "etched") else "",
                    "Tags": first_entry["tags"] or "",
                    "Last Modified": "",
                    "Collector Number": collector_number,
                    "Alter": "alter" if has_alter else "",
                    "Proxy": "proxy" if has_proxy else "",
                    "Purchase Price": f"{avg_price:.2f}" if avg_price is not None else "",
                }
                writer.writerow(row)

        return len(entries)
