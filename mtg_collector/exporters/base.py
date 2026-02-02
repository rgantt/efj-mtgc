"""Base exporter interface."""

from abc import ABC, abstractmethod
from typing import List, Dict, Any
import sqlite3


class BaseExporter(ABC):
    """Abstract base class for collection exporters."""

    @property
    @abstractmethod
    def format_name(self) -> str:
        """Human-readable format name."""
        pass

    @property
    @abstractmethod
    def file_extension(self) -> str:
        """Default file extension for this format."""
        pass

    @abstractmethod
    def export(self, conn: sqlite3.Connection, output_path: str, filters: Dict[str, Any] = None) -> int:
        """
        Export collection to file.

        Args:
            conn: Database connection
            output_path: Path to output file
            filters: Optional filters (set_code, name, etc.)

        Returns:
            Number of cards exported
        """
        pass

    def get_collection_data(self, conn: sqlite3.Connection, filters: Dict[str, Any] = None) -> List[Dict]:
        """
        Get collection data with card/printing info joined.

        Args:
            conn: Database connection
            filters: Optional filters

        Returns:
            List of dicts with full card info
        """
        query = """
            SELECT
                c.id, c.scryfall_id, c.finish, c.condition, c.language,
                c.purchase_price, c.acquired_at, c.source, c.notes, c.tags,
                c.tradelist, c.is_alter, c.proxy, c.signed, c.misprint,
                p.set_code, p.collector_number, p.rarity, p.artist, p.finishes,
                card.oracle_id, card.name, card.type_line, card.mana_cost,
                s.set_name
            FROM collection c
            JOIN printings p ON c.scryfall_id = p.scryfall_id
            JOIN cards card ON p.oracle_id = card.oracle_id
            JOIN sets s ON p.set_code = s.set_code
            WHERE 1=1
        """
        params = []

        if filters:
            if filters.get("set_code"):
                query += " AND p.set_code = ?"
                params.append(filters["set_code"].lower())

            if filters.get("name"):
                query += " AND card.name LIKE ?"
                params.append(f"%{filters['name']}%")

        query += " ORDER BY card.name, p.set_code, c.id"

        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor]
