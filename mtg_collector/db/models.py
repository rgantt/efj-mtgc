"""Database models and repositories."""

import sqlite3
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from mtg_collector.utils import now_iso, parse_json_array, to_json_array


@dataclass
class Card:
    """Abstract card (oracle-level)."""
    oracle_id: str
    name: str
    type_line: Optional[str] = None
    mana_cost: Optional[str] = None
    cmc: Optional[float] = None
    oracle_text: Optional[str] = None
    colors: List[str] = field(default_factory=list)
    color_identity: List[str] = field(default_factory=list)


@dataclass
class Set:
    """MTG set information."""
    set_code: str
    set_name: str
    set_type: Optional[str] = None
    released_at: Optional[str] = None
    cards_fetched_at: Optional[str] = None  # When full card list was cached


@dataclass
class Printing:
    """Specific card printing."""
    scryfall_id: str
    oracle_id: str
    set_code: str
    collector_number: str
    rarity: Optional[str] = None
    frame_effects: List[str] = field(default_factory=list)
    border_color: Optional[str] = None
    full_art: bool = False
    promo: bool = False
    promo_types: List[str] = field(default_factory=list)
    finishes: List[str] = field(default_factory=list)
    artist: Optional[str] = None
    image_uri: Optional[str] = None
    raw_json: Optional[str] = None  # Full Scryfall API response as JSON string

    def get_scryfall_data(self) -> Optional[Dict]:
        """Parse and return the full Scryfall API response as a dict."""
        if self.raw_json:
            import json
            return json.loads(self.raw_json)
        return None


@dataclass
class CollectionEntry:
    """A physical card in the user's collection."""
    id: Optional[int]
    scryfall_id: str
    finish: str
    condition: str = "Near Mint"
    language: str = "English"
    purchase_price: Optional[float] = None
    acquired_at: Optional[str] = None
    source: str = "manual"
    source_image: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[str] = None
    tradelist: bool = False
    alter: bool = False
    proxy: bool = False
    signed: bool = False
    misprint: bool = False
    status: str = "owned"
    sale_price: Optional[float] = None


@dataclass
class WishlistEntry:
    """A card the user wants to acquire."""
    id: Optional[int]
    oracle_id: str
    scryfall_id: Optional[str] = None
    max_price: Optional[float] = None
    priority: int = 0
    notes: Optional[str] = None
    added_at: Optional[str] = None
    source: str = "manual"
    fulfilled_at: Optional[str] = None


class CardRepository:
    """CRUD operations for cards table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, card: Card) -> None:
        """Insert or update a card."""
        self.conn.execute(
            """
            INSERT INTO cards
            (oracle_id, name, type_line, mana_cost, cmc, oracle_text, colors, color_identity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(oracle_id) DO UPDATE SET
                name = excluded.name,
                type_line = excluded.type_line,
                mana_cost = excluded.mana_cost,
                cmc = excluded.cmc,
                oracle_text = excluded.oracle_text,
                colors = excluded.colors,
                color_identity = excluded.color_identity
            """,
            (
                card.oracle_id,
                card.name,
                card.type_line,
                card.mana_cost,
                card.cmc,
                card.oracle_text,
                to_json_array(card.colors),
                to_json_array(card.color_identity),
            ),
        )

    def get(self, oracle_id: str) -> Optional[Card]:
        """Get a card by oracle_id."""
        cursor = self.conn.execute(
            "SELECT * FROM cards WHERE oracle_id = ?", (oracle_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return Card(
            oracle_id=row["oracle_id"],
            name=row["name"],
            type_line=row["type_line"],
            mana_cost=row["mana_cost"],
            cmc=row["cmc"],
            oracle_text=row["oracle_text"],
            colors=parse_json_array(row["colors"]),
            color_identity=parse_json_array(row["color_identity"]),
        )

    def get_by_name(self, name: str) -> Optional[Card]:
        """Get a card by exact name."""
        cursor = self.conn.execute(
            "SELECT * FROM cards WHERE name = ?", (name,)
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return Card(
            oracle_id=row["oracle_id"],
            name=row["name"],
            type_line=row["type_line"],
            mana_cost=row["mana_cost"],
            cmc=row["cmc"],
            oracle_text=row["oracle_text"],
            colors=parse_json_array(row["colors"]),
            color_identity=parse_json_array(row["color_identity"]),
        )


class SetRepository:
    """CRUD operations for sets table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, s: Set) -> None:
        """Insert or update a set."""
        self.conn.execute(
            """
            INSERT INTO sets (set_code, set_name, set_type, released_at, cards_fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(set_code) DO UPDATE SET
                set_name = excluded.set_name,
                set_type = excluded.set_type,
                released_at = excluded.released_at,
                cards_fetched_at = COALESCE(excluded.cards_fetched_at, sets.cards_fetched_at)
            """,
            (s.set_code, s.set_name, s.set_type, s.released_at, s.cards_fetched_at),
        )

    def get(self, set_code: str) -> Optional[Set]:
        """Get a set by code."""
        cursor = self.conn.execute(
            "SELECT * FROM sets WHERE set_code = ?", (set_code,)
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return Set(
            set_code=row["set_code"],
            set_name=row["set_name"],
            set_type=row["set_type"],
            released_at=row["released_at"],
            cards_fetched_at=row["cards_fetched_at"],
        )

    def exists(self, set_code: str) -> bool:
        """Check if a set exists."""
        cursor = self.conn.execute(
            "SELECT 1 FROM sets WHERE set_code = ?", (set_code,)
        )
        return cursor.fetchone() is not None

    def is_cards_cached(self, set_code: str) -> bool:
        """Check if a set's card list has been fully cached."""
        cursor = self.conn.execute(
            "SELECT cards_fetched_at FROM sets WHERE set_code = ?", (set_code,)
        )
        row = cursor.fetchone()
        return row is not None and row["cards_fetched_at"] is not None

    def mark_cards_cached(self, set_code: str) -> None:
        """Mark a set's card list as fully cached."""
        from mtg_collector.utils import now_iso
        self.conn.execute(
            "UPDATE sets SET cards_fetched_at = ? WHERE set_code = ?",
            (now_iso(), set_code),
        )


class PrintingRepository:
    """CRUD operations for printings table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, p: Printing) -> None:
        """Insert or update a printing."""
        self.conn.execute(
            """
            INSERT INTO printings
            (scryfall_id, oracle_id, set_code, collector_number, rarity,
             frame_effects, border_color, full_art, promo, promo_types,
             finishes, artist, image_uri, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scryfall_id) DO UPDATE SET
                oracle_id = excluded.oracle_id,
                set_code = excluded.set_code,
                collector_number = excluded.collector_number,
                rarity = excluded.rarity,
                frame_effects = excluded.frame_effects,
                border_color = excluded.border_color,
                full_art = excluded.full_art,
                promo = excluded.promo,
                promo_types = excluded.promo_types,
                finishes = excluded.finishes,
                artist = excluded.artist,
                image_uri = excluded.image_uri,
                raw_json = excluded.raw_json
            """,
            (
                p.scryfall_id,
                p.oracle_id,
                p.set_code,
                p.collector_number,
                p.rarity,
                to_json_array(p.frame_effects),
                p.border_color,
                1 if p.full_art else 0,
                1 if p.promo else 0,
                to_json_array(p.promo_types),
                to_json_array(p.finishes),
                p.artist,
                p.image_uri,
                p.raw_json,
            ),
        )

    def get(self, scryfall_id: str) -> Optional[Printing]:
        """Get a printing by scryfall_id."""
        cursor = self.conn.execute(
            "SELECT * FROM printings WHERE scryfall_id = ?", (scryfall_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return self._row_to_printing(row)

    def get_by_set_cn(self, set_code: str, collector_number: str) -> Optional[Printing]:
        """Get a printing by set code and collector number."""
        cursor = self.conn.execute(
            "SELECT * FROM printings WHERE set_code = ? AND collector_number = ?",
            (set_code, collector_number),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return self._row_to_printing(row)

    def get_by_oracle_id(self, oracle_id: str) -> List[Printing]:
        """Get all printings for a card."""
        cursor = self.conn.execute(
            "SELECT * FROM printings WHERE oracle_id = ? ORDER BY set_code, collector_number",
            (oracle_id,),
        )
        return [self._row_to_printing(row) for row in cursor]

    def exists(self, scryfall_id: str) -> bool:
        """Check if a printing exists."""
        cursor = self.conn.execute(
            "SELECT 1 FROM printings WHERE scryfall_id = ?", (scryfall_id,)
        )
        return cursor.fetchone() is not None

    def _row_to_printing(self, row: sqlite3.Row) -> Printing:
        # Handle raw_json which might not exist in older databases
        raw_json = None
        try:
            raw_json = row["raw_json"]
        except (IndexError, KeyError):
            pass

        return Printing(
            scryfall_id=row["scryfall_id"],
            oracle_id=row["oracle_id"],
            set_code=row["set_code"],
            collector_number=row["collector_number"],
            rarity=row["rarity"],
            frame_effects=parse_json_array(row["frame_effects"]),
            border_color=row["border_color"],
            full_art=bool(row["full_art"]),
            promo=bool(row["promo"]),
            promo_types=parse_json_array(row["promo_types"]),
            finishes=parse_json_array(row["finishes"]),
            artist=row["artist"],
            image_uri=row["image_uri"],
            raw_json=raw_json,
        )


class CollectionRepository:
    """CRUD operations for collection table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, entry: CollectionEntry) -> int:
        """Add a new collection entry. Returns the new ID."""
        if entry.acquired_at is None:
            entry.acquired_at = now_iso()

        cursor = self.conn.execute(
            """
            INSERT INTO collection
            (scryfall_id, finish, condition, language, purchase_price,
             acquired_at, source, source_image, notes, tags, tradelist,
             is_alter, proxy, signed, misprint, status, sale_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.scryfall_id,
                entry.finish,
                entry.condition,
                entry.language,
                entry.purchase_price,
                entry.acquired_at,
                entry.source,
                entry.source_image,
                entry.notes,
                entry.tags,
                1 if entry.tradelist else 0,
                1 if entry.alter else 0,
                1 if entry.proxy else 0,
                1 if entry.signed else 0,
                1 if entry.misprint else 0,
                entry.status,
                entry.sale_price,
            ),
        )
        new_id = cursor.lastrowid
        # Log the initial status
        self.conn.execute(
            "INSERT INTO status_log (collection_id, from_status, to_status, changed_at) VALUES (?, NULL, ?, ?)",
            (new_id, entry.status, entry.acquired_at),
        )
        return new_id

    def get(self, entry_id: int) -> Optional[CollectionEntry]:
        """Get a collection entry by ID."""
        cursor = self.conn.execute(
            "SELECT * FROM collection WHERE id = ?", (entry_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return self._row_to_entry(row)

    def update(self, entry: CollectionEntry, status_note: Optional[str] = None) -> bool:
        """Update a collection entry. Returns True if updated."""
        if entry.id is None:
            return False

        # Check if status changed for logging
        old = self.get(entry.id)
        old_status = old.status if old else None

        cursor = self.conn.execute(
            """
            UPDATE collection SET
                scryfall_id = ?,
                finish = ?,
                condition = ?,
                language = ?,
                purchase_price = ?,
                acquired_at = ?,
                source = ?,
                source_image = ?,
                notes = ?,
                tags = ?,
                tradelist = ?,
                is_alter = ?,
                proxy = ?,
                signed = ?,
                misprint = ?,
                status = ?,
                sale_price = ?
            WHERE id = ?
            """,
            (
                entry.scryfall_id,
                entry.finish,
                entry.condition,
                entry.language,
                entry.purchase_price,
                entry.acquired_at,
                entry.source,
                entry.source_image,
                entry.notes,
                entry.tags,
                1 if entry.tradelist else 0,
                1 if entry.alter else 0,
                1 if entry.proxy else 0,
                1 if entry.signed else 0,
                1 if entry.misprint else 0,
                entry.status,
                entry.sale_price,
                entry.id,
            ),
        )
        updated = cursor.rowcount > 0

        # Log status change
        if updated and old_status and old_status != entry.status:
            self.conn.execute(
                "INSERT INTO status_log (collection_id, from_status, to_status, changed_at, note) VALUES (?, ?, ?, ?, ?)",
                (entry.id, old_status, entry.status, now_iso(), status_note),
            )

        return updated

    def delete(self, entry_id: int) -> bool:
        """Delete a collection entry. Returns True if deleted."""
        cursor = self.conn.execute(
            "DELETE FROM collection WHERE id = ?", (entry_id,)
        )
        return cursor.rowcount > 0

    def list_all(
        self,
        set_code: Optional[str] = None,
        name: Optional[str] = None,
        foil: Optional[bool] = None,
        condition: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        List collection entries with optional filters.
        Returns joined data with card/printing info.
        """
        query = """
            SELECT
                c.id, c.scryfall_id, c.finish, c.condition, c.language,
                c.purchase_price, c.acquired_at, c.source, c.notes, c.tags,
                c.tradelist, c.is_alter, c.proxy, c.signed, c.misprint,
                c.status, c.sale_price,
                p.set_code, p.collector_number, p.rarity, p.artist,
                card.name, card.type_line, card.mana_cost,
                s.set_name
            FROM collection c
            JOIN printings p ON c.scryfall_id = p.scryfall_id
            JOIN cards card ON p.oracle_id = card.oracle_id
            JOIN sets s ON p.set_code = s.set_code
            WHERE 1=1
        """
        params = []

        if set_code:
            query += " AND p.set_code = ?"
            params.append(set_code.lower())

        if name:
            query += " AND card.name LIKE ?"
            params.append(f"%{name}%")

        if foil is not None:
            if foil:
                query += " AND c.finish IN ('foil', 'etched')"
            else:
                query += " AND c.finish = 'nonfoil'"

        if condition:
            query += " AND c.condition = ?"
            params.append(condition)

        if source:
            query += " AND c.source = ?"
            params.append(source)

        if status:
            query += " AND c.status = ?"
            params.append(status)

        query += " ORDER BY c.acquired_at DESC"

        if limit:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor]

    def count(self, status: Optional[str] = None) -> int:
        """Get total number of entries in collection."""
        if status:
            cursor = self.conn.execute(
                "SELECT COUNT(*) FROM collection WHERE status = ?", (status,)
            )
        else:
            cursor = self.conn.execute("SELECT COUNT(*) FROM collection")
        return cursor.fetchone()[0]

    def stats(self) -> Dict[str, Any]:
        """Get collection statistics."""
        stats = {}

        # Total count
        stats["total_cards"] = self.count()

        # By status
        cursor = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM collection GROUP BY status"
        )
        stats["by_status"] = {row["status"]: row["cnt"] for row in cursor}

        # By finish
        cursor = self.conn.execute(
            "SELECT finish, COUNT(*) as cnt FROM collection GROUP BY finish"
        )
        stats["by_finish"] = {row["finish"]: row["cnt"] for row in cursor}

        # By condition
        cursor = self.conn.execute(
            "SELECT condition, COUNT(*) as cnt FROM collection GROUP BY condition"
        )
        stats["by_condition"] = {row["condition"]: row["cnt"] for row in cursor}

        # By source
        cursor = self.conn.execute(
            "SELECT source, COUNT(*) as cnt FROM collection GROUP BY source"
        )
        stats["by_source"] = {row["source"]: row["cnt"] for row in cursor}

        # Unique printings
        cursor = self.conn.execute(
            "SELECT COUNT(DISTINCT scryfall_id) FROM collection"
        )
        stats["unique_printings"] = cursor.fetchone()[0]

        # Unique cards (oracle_id)
        cursor = self.conn.execute(
            """
            SELECT COUNT(DISTINCT p.oracle_id)
            FROM collection c
            JOIN printings p ON c.scryfall_id = p.scryfall_id
            """
        )
        stats["unique_cards"] = cursor.fetchone()[0]

        # Total value
        cursor = self.conn.execute(
            "SELECT SUM(purchase_price) FROM collection WHERE purchase_price IS NOT NULL"
        )
        total = cursor.fetchone()[0]
        stats["total_value"] = total if total else 0.0

        return stats

    def get_status_history(self, collection_id: int) -> List[Dict[str, Any]]:
        """Get status transition history for a collection entry."""
        cursor = self.conn.execute(
            """
            SELECT from_status, to_status, changed_at, note
            FROM status_log
            WHERE collection_id = ?
            ORDER BY changed_at ASC
            """,
            (collection_id,),
        )
        return [dict(row) for row in cursor]

    def _row_to_entry(self, row: sqlite3.Row) -> CollectionEntry:
        # Handle optional columns that might not exist in older databases
        source_image = None
        try:
            source_image = row["source_image"]
        except (IndexError, KeyError):
            pass

        status = "owned"
        try:
            status = row["status"]
        except (IndexError, KeyError):
            pass

        sale_price = None
        try:
            sale_price = row["sale_price"]
        except (IndexError, KeyError):
            pass

        return CollectionEntry(
            id=row["id"],
            scryfall_id=row["scryfall_id"],
            finish=row["finish"],
            condition=row["condition"],
            language=row["language"],
            purchase_price=row["purchase_price"],
            acquired_at=row["acquired_at"],
            source=row["source"],
            source_image=source_image,
            notes=row["notes"],
            tags=row["tags"],
            tradelist=bool(row["tradelist"]),
            alter=bool(row["is_alter"]),
            proxy=bool(row["proxy"]),
            signed=bool(row["signed"]),
            misprint=bool(row["misprint"]),
            status=status,
            sale_price=sale_price,
        )


class WishlistRepository:
    """CRUD operations for wishlist table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, entry: WishlistEntry) -> int:
        """Add a wishlist entry. Returns the new ID."""
        if entry.added_at is None:
            entry.added_at = now_iso()

        cursor = self.conn.execute(
            """
            INSERT INTO wishlist
            (oracle_id, scryfall_id, max_price, priority, notes, added_at, source, fulfilled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.oracle_id,
                entry.scryfall_id,
                entry.max_price,
                entry.priority,
                entry.notes,
                entry.added_at,
                entry.source,
                entry.fulfilled_at,
            ),
        )
        return cursor.lastrowid

    def get(self, entry_id: int) -> Optional[WishlistEntry]:
        """Get a wishlist entry by ID."""
        cursor = self.conn.execute(
            "SELECT * FROM wishlist WHERE id = ?", (entry_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def update(self, entry: WishlistEntry) -> bool:
        """Update a wishlist entry. Returns True if updated."""
        if entry.id is None:
            return False
        cursor = self.conn.execute(
            """
            UPDATE wishlist SET
                oracle_id = ?, scryfall_id = ?, max_price = ?,
                priority = ?, notes = ?, source = ?, fulfilled_at = ?
            WHERE id = ?
            """,
            (
                entry.oracle_id,
                entry.scryfall_id,
                entry.max_price,
                entry.priority,
                entry.notes,
                entry.source,
                entry.fulfilled_at,
                entry.id,
            ),
        )
        return cursor.rowcount > 0

    def delete(self, entry_id: int) -> bool:
        """Delete a wishlist entry. Returns True if deleted."""
        cursor = self.conn.execute(
            "DELETE FROM wishlist WHERE id = ?", (entry_id,)
        )
        return cursor.rowcount > 0

    def fulfill(self, entry_id: int) -> bool:
        """Mark a wishlist entry as fulfilled."""
        cursor = self.conn.execute(
            "UPDATE wishlist SET fulfilled_at = ? WHERE id = ?",
            (now_iso(), entry_id),
        )
        return cursor.rowcount > 0

    def list_all(
        self,
        fulfilled: Optional[bool] = None,
        oracle_id: Optional[str] = None,
        name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """List wishlist entries with card info joined."""
        query = """
            SELECT
                w.id, w.oracle_id, w.scryfall_id, w.max_price,
                w.priority, w.notes, w.added_at, w.source, w.fulfilled_at,
                card.name, card.type_line, card.mana_cost,
                p.set_code, p.collector_number, p.image_uri
            FROM wishlist w
            JOIN cards card ON w.oracle_id = card.oracle_id
            LEFT JOIN printings p ON w.scryfall_id = p.scryfall_id
            WHERE 1=1
        """
        params = []

        if fulfilled is not None:
            if fulfilled:
                query += " AND w.fulfilled_at IS NOT NULL"
            else:
                query += " AND w.fulfilled_at IS NULL"

        if oracle_id:
            query += " AND w.oracle_id = ?"
            params.append(oracle_id)

        if name:
            query += " AND card.name LIKE ?"
            params.append(f"%{name}%")

        query += " ORDER BY w.priority DESC, w.added_at DESC"

        if limit:
            query += " LIMIT ?"
            params.append(limit)

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor]

    def count(self, fulfilled: Optional[bool] = None) -> int:
        """Count wishlist entries."""
        if fulfilled is not None:
            if fulfilled:
                cursor = self.conn.execute(
                    "SELECT COUNT(*) FROM wishlist WHERE fulfilled_at IS NOT NULL"
                )
            else:
                cursor = self.conn.execute(
                    "SELECT COUNT(*) FROM wishlist WHERE fulfilled_at IS NULL"
                )
        else:
            cursor = self.conn.execute("SELECT COUNT(*) FROM wishlist")
        return cursor.fetchone()[0]

    def _row_to_entry(self, row: sqlite3.Row) -> WishlistEntry:
        return WishlistEntry(
            id=row["id"],
            oracle_id=row["oracle_id"],
            scryfall_id=row["scryfall_id"],
            max_price=row["max_price"],
            priority=row["priority"],
            notes=row["notes"],
            added_at=row["added_at"],
            source=row["source"],
            fulfilled_at=row["fulfilled_at"],
        )
