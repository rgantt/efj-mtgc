"""Database models and repositories."""

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
class Order:
    """An order from a card vendor."""
    id: Optional[int]
    order_number: Optional[str] = None
    source: Optional[str] = None  # 'tcgplayer', 'cardkingdom', 'other'
    seller_name: Optional[str] = None
    order_date: Optional[str] = None
    subtotal: Optional[float] = None
    shipping: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    shipping_status: Optional[str] = None
    estimated_delivery: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None


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
    order_id: Optional[int] = None


@dataclass
class SealedProduct:
    """A sealed MTG product (booster box, bundle, deck, etc.)."""
    uuid: str
    name: str
    set_code: str
    category: str
    subtype: Optional[str] = None
    tcgplayer_product_id: Optional[str] = None
    card_count: Optional[int] = None
    product_size: Optional[int] = None
    release_date: Optional[str] = None
    purchase_url_tcgplayer: Optional[str] = None
    purchase_url_cardkingdom: Optional[str] = None
    contents_json: Optional[str] = None
    imported_at: Optional[str] = None


@dataclass
class SealedCollectionEntry:
    """A sealed product in the user's collection."""
    id: Optional[int]
    sealed_product_uuid: str
    quantity: int = 1
    condition: str = "Near Mint"
    purchase_price: Optional[float] = None
    purchase_date: Optional[str] = None
    source: Optional[str] = None
    seller_name: Optional[str] = None
    notes: Optional[str] = None
    status: str = "owned"
    sale_price: Optional[float] = None
    added_at: Optional[str] = None


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

    def search_by_name(self, name: str) -> Optional[Card]:
        """Search for a card by name (case-insensitive, handles DFCs).

        Handles double-faced cards where the DB stores "Front // Back"
        but the search term is just "Front".
        """
        # Try case-insensitive exact match first
        cursor = self.conn.execute(
            "SELECT * FROM cards WHERE name COLLATE NOCASE = ?", (name,)
        )
        row = cursor.fetchone()
        if row:
            return self._row_to_card(row)

        # Try matching front face of double-faced cards ("Name // ...")
        cursor = self.conn.execute(
            "SELECT * FROM cards WHERE name LIKE ? LIMIT 1",
            (name + " // %",),
        )
        row = cursor.fetchone()
        if row:
            return self._row_to_card(row)

        return None

    def search_cards_by_name(self, name: str, limit: int = 20) -> List[Card]:
        """Search for cards by name, returning multiple results.

        Case-insensitive exact match first, then DFC front-face match,
        then partial LIKE match.
        """
        results = []
        seen = set()

        # Exact case-insensitive match
        for row in self.conn.execute(
            "SELECT * FROM cards WHERE name COLLATE NOCASE = ?", (name,)
        ):
            results.append(self._row_to_card(row))
            seen.add(row["oracle_id"])

        # DFC front-face match
        if seen:
            placeholders = ",".join("?" * len(seen))
            query = f"SELECT * FROM cards WHERE name LIKE ? COLLATE NOCASE AND oracle_id NOT IN ({placeholders})"
            params = (name + " // %", *seen)
        else:
            query = "SELECT * FROM cards WHERE name LIKE ? COLLATE NOCASE"
            params = (name + " // %",)
        for row in self.conn.execute(query, params):
            results.append(self._row_to_card(row))
            seen.add(row["oracle_id"])

        if len(results) >= limit:
            return results[:limit]

        # Partial match (substring)
        exclude = f"AND oracle_id NOT IN ({','.join('?' * len(seen))})" if seen else ""
        for row in self.conn.execute(
            f"SELECT * FROM cards WHERE name LIKE ? COLLATE NOCASE {exclude} LIMIT ?",
            (f"%{name}%", *seen, limit - len(results)),
        ):
            results.append(self._row_to_card(row))

        return results

    def _row_to_card(self, row) -> Card:
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

    def get_by_name(self, name: str) -> Optional[Set]:
        """Find a set by name (case-insensitive, with partial match fallback)."""
        name_lower = name.lower()

        # Try exact case-insensitive match
        cursor = self.conn.execute(
            "SELECT * FROM sets WHERE set_name COLLATE NOCASE = ?", (name,)
        )
        row = cursor.fetchone()
        if row:
            return self._row_to_set(row)

        # Try partial match (set name contains the search term)
        cursor = self.conn.execute(
            "SELECT * FROM sets WHERE LOWER(set_name) LIKE ? LIMIT 1",
            (f"%{name_lower}%",),
        )
        row = cursor.fetchone()
        if row:
            return self._row_to_set(row)

        return None

    def _row_to_set(self, row) -> Set:
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
             is_alter, proxy, signed, misprint, status, sale_price, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                entry.order_id,
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
                sale_price = ?,
                order_id = ?
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
                entry.order_id,
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

    def get_copies(
        self,
        scryfall_id: str,
        finish: Optional[str] = None,
        condition: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return individual (unaggregated) collection rows for a printing, joined with order data."""
        query = """
            SELECT c.id, c.scryfall_id, c.finish, c.condition, c.language,
                   c.purchase_price, c.acquired_at, c.source, c.status, c.order_id,
                   o.seller_name as order_seller, o.order_number as order_number,
                   o.order_date as order_date
            FROM collection c
            LEFT JOIN orders o ON c.order_id = o.id
            WHERE c.scryfall_id = ?
        """
        params: list = [scryfall_id]
        if finish:
            query += " AND c.finish = ?"
            params.append(finish)
        if condition:
            query += " AND c.condition = ?"
            params.append(condition)
        if status:
            query += " AND c.status = ?"
            params.append(status)
        query += " ORDER BY c.acquired_at DESC"
        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor]

    def receive_card(self, collection_id: int) -> bool:
        """Receive a single ordered card (flip ordered -> owned). Returns True if updated."""
        entry = self.get(collection_id)
        if entry is None or entry.status != "ordered":
            return False
        entry.status = "owned"
        return self.update(entry, status_note="card received")

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

    # Valid status transitions for dispose()
    VALID_TRANSITIONS = {
        'owned': {'sold', 'traded', 'gifted', 'lost', 'listed'},
        'listed': {'sold', 'owned'},
    }

    def get_copies(
        self,
        scryfall_id: str,
        finish: Optional[str] = None,
        condition: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return individual collection rows for a card, joined with order and lineage info."""
        query = """
            SELECT
                c.id, c.scryfall_id, c.finish, c.condition, c.language,
                c.purchase_price, c.acquired_at, c.source, c.source_image,
                c.notes, c.tags, c.status, c.sale_price, c.order_id,
                o.order_number, o.source AS order_source, o.seller_name,
                o.order_date,
                il.image_md5, il.image_path, il.card_index,
                ii.id AS image_id
            FROM collection c
            LEFT JOIN orders o ON c.order_id = o.id
            LEFT JOIN ingest_lineage il ON il.collection_id = c.id
            LEFT JOIN ingest_images ii ON il.image_md5 = ii.md5
            WHERE c.scryfall_id = ?
        """
        params: List[Any] = [scryfall_id]

        if finish:
            query += " AND c.finish = ?"
            params.append(finish)
        if condition:
            query += " AND c.condition = ?"
            params.append(condition)
        if status:
            query += " AND c.status = ?"
            params.append(status)

        query += " ORDER BY c.acquired_at DESC"
        cursor = self.conn.execute(query, params)
        copies = []
        for row in cursor:
            copy = dict(row)
            # Attach status history
            copy['status_history'] = self.get_status_history(copy['id'])
            copies.append(copy)
        return copies

    def dispose(
        self,
        entry_id: int,
        new_status: str,
        sale_price: Optional[float] = None,
        note: Optional[str] = None,
    ) -> bool:
        """Transition a card to a disposition status (sold, traded, gifted, lost, listed).

        Validates that the transition is allowed, then updates via the existing
        update() method which handles status_log.
        """
        entry = self.get(entry_id)
        if not entry:
            raise ValueError(f"Collection entry {entry_id} not found")

        allowed = self.VALID_TRANSITIONS.get(entry.status)
        if not allowed or new_status not in allowed:
            raise ValueError(
                f"Cannot transition from '{entry.status}' to '{new_status}'"
            )

        entry.status = new_status
        if sale_price is not None:
            entry.sale_price = sale_price
        return self.update(entry, status_note=note)

    def delete_with_lineage(self, entry_id: int) -> bool:
        """Delete a collection entry and its ingest_lineage rows.

        Only allows deletion of entries with status 'owned' or 'ordered'.
        """
        entry = self.get(entry_id)
        if not entry:
            raise ValueError(f"Collection entry {entry_id} not found")
        if entry.status not in ('owned', 'ordered'):
            raise ValueError(
                f"Cannot delete entry with status '{entry.status}' — "
                "only 'owned' or 'ordered' entries can be deleted"
            )

        self.conn.execute(
            "DELETE FROM ingest_lineage WHERE collection_id = ?", (entry_id,)
        )
        return self.delete(entry_id)

    def bulk_delete(self, ids: List[int]) -> Dict[str, List[int]]:
        """Delete multiple collection entries with lineage cleanup.

        Returns dict with 'deleted' and 'skipped' ID lists.
        """
        deleted = []
        skipped = []
        for entry_id in ids:
            entry = self.get(entry_id)
            if not entry or entry.status not in ('owned', 'ordered'):
                skipped.append(entry_id)
                continue
            self.conn.execute(
                "DELETE FROM ingest_lineage WHERE collection_id = ?",
                (entry_id,),
            )
            self.delete(entry_id)
            deleted.append(entry_id)
        return {"deleted": deleted, "skipped": skipped}

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

        order_id = None
        try:
            order_id = row["order_id"]
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
            order_id=order_id,
        )


class OrderRepository:
    """CRUD operations for orders table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, order: Order) -> int:
        """Add a new order. Returns the new ID."""
        if order.created_at is None:
            order.created_at = now_iso()

        cursor = self.conn.execute(
            """
            INSERT INTO orders
            (order_number, source, seller_name, order_date, subtotal,
             shipping, tax, total, shipping_status, estimated_delivery,
             notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.order_number,
                order.source,
                order.seller_name,
                order.order_date,
                order.subtotal,
                order.shipping,
                order.tax,
                order.total,
                order.shipping_status,
                order.estimated_delivery,
                order.notes,
                order.created_at,
            ),
        )
        return cursor.lastrowid

    def get(self, order_id: int) -> Optional[Order]:
        """Get an order by ID."""
        cursor = self.conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_order(row)

    def get_by_number(self, order_number: str) -> List[Order]:
        """Get orders by order number (may be multiple sellers per order)."""
        cursor = self.conn.execute(
            "SELECT * FROM orders WHERE order_number = ? ORDER BY seller_name",
            (order_number,),
        )
        return [self._row_to_order(row) for row in cursor]

    def list_all(self, source: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all orders with card counts and ordered counts."""
        query = """
            SELECT o.*, COUNT(c.id) as card_count,
                   SUM(CASE WHEN c.status = 'ordered' THEN 1 ELSE 0 END) as ordered_count
            FROM orders o
            LEFT JOIN collection c ON c.order_id = o.id
            WHERE 1=1
        """
        params = []
        if source:
            query += " AND o.source = ?"
            params.append(source)
        query += " GROUP BY o.id ORDER BY o.created_at DESC"

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor]

    def get_order_cards(self, order_id: int) -> List[Dict[str, Any]]:
        """Get all collection entries for an order."""
        cursor = self.conn.execute(
            """
            SELECT c.*, card.name, p.set_code, p.collector_number, p.rarity,
                   p.image_uri, s.set_name
            FROM collection c
            JOIN printings p ON c.scryfall_id = p.scryfall_id
            JOIN cards card ON p.oracle_id = card.oracle_id
            JOIN sets s ON p.set_code = s.set_code
            WHERE c.order_id = ?
            ORDER BY card.name
            """,
            (order_id,),
        )
        return [dict(row) for row in cursor]

    def receive_order(self, order_id: int, card_ids: Optional[List[int]] = None) -> int:
        """Batch flip ordered cards in this order to owned. Returns count.

        If card_ids is provided, only those specific collection entries are received.
        """
        ts = now_iso()
        # Get IDs of cards to update
        if card_ids:
            placeholders = ",".join("?" * len(card_ids))
            cursor = self.conn.execute(
                f"SELECT id FROM collection WHERE order_id = ? AND status = 'ordered' AND id IN ({placeholders})",
                [order_id] + card_ids,
            )
        else:
            cursor = self.conn.execute(
                "SELECT id FROM collection WHERE order_id = ? AND status = 'ordered'",
                (order_id,),
            )
        ids = [row["id"] for row in cursor]
        if not ids:
            return 0

        # Update status
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(
            f"UPDATE collection SET status = 'owned' WHERE id IN ({placeholders})",
            ids,
        )

        # Log status changes
        for cid in ids:
            self.conn.execute(
                "INSERT INTO status_log (collection_id, from_status, to_status, changed_at, note) "
                "VALUES (?, 'ordered', 'owned', ?, 'order received')",
                (cid, ts),
            )

        return len(ids)

    def _row_to_order(self, row: sqlite3.Row) -> Order:
        return Order(
            id=row["id"],
            order_number=row["order_number"],
            source=row["source"],
            seller_name=row["seller_name"],
            order_date=row["order_date"],
            subtotal=row["subtotal"],
            shipping=row["shipping"],
            tax=row["tax"],
            total=row["total"],
            shipping_status=row["shipping_status"],
            estimated_delivery=row["estimated_delivery"],
            notes=row["notes"],
            created_at=row["created_at"],
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


class SealedProductRepository:
    """CRUD operations for sealed_products table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, uuid: str) -> Optional[SealedProduct]:
        """Get a sealed product by UUID."""
        cursor = self.conn.execute(
            "SELECT * FROM sealed_products WHERE uuid = ?", (uuid,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_product(row)

    def get_by_tcgplayer_id(self, tcgplayer_product_id: str) -> Optional[SealedProduct]:
        """Get a sealed product by TCGPlayer product ID."""
        cursor = self.conn.execute(
            "SELECT * FROM sealed_products WHERE tcgplayer_product_id = ?",
            (tcgplayer_product_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_product(row)

    def search_by_name(self, name: str, limit: int = 20) -> List[SealedProduct]:
        """Search sealed products by name (case-insensitive partial match)."""
        cursor = self.conn.execute(
            "SELECT * FROM sealed_products WHERE name LIKE ? COLLATE NOCASE ORDER BY name LIMIT ?",
            (f"%{name}%", limit),
        )
        return [self._row_to_product(row) for row in cursor]

    def list_by_set(self, set_code: str) -> List[SealedProduct]:
        """List all sealed products for a set."""
        cursor = self.conn.execute(
            "SELECT * FROM sealed_products WHERE set_code = ? ORDER BY category, name",
            (set_code,),
        )
        return [self._row_to_product(row) for row in cursor]

    def list_sets_with_products(self) -> List[Dict[str, Any]]:
        """List sets that have sealed products, with counts."""
        cursor = self.conn.execute("""
            SELECT sp.set_code, s.set_name, COUNT(*) as product_count
            FROM sealed_products sp
            LEFT JOIN sets s ON sp.set_code = s.set_code
            GROUP BY sp.set_code
            ORDER BY s.released_at DESC
        """)
        return [dict(row) for row in cursor]

    def count(self) -> int:
        """Get total number of sealed products."""
        return self.conn.execute("SELECT COUNT(*) FROM sealed_products").fetchone()[0]

    def _row_to_product(self, row: sqlite3.Row) -> SealedProduct:
        return SealedProduct(
            uuid=row["uuid"],
            name=row["name"],
            set_code=row["set_code"],
            category=row["category"],
            subtype=row["subtype"],
            tcgplayer_product_id=row["tcgplayer_product_id"],
            card_count=row["card_count"],
            product_size=row["product_size"],
            release_date=row["release_date"],
            purchase_url_tcgplayer=row["purchase_url_tcgplayer"],
            purchase_url_cardkingdom=row["purchase_url_cardkingdom"],
            contents_json=row["contents_json"],
            imported_at=row["imported_at"],
        )


class SealedCollectionRepository:
    """CRUD operations for sealed_collection table."""

    VALID_TRANSITIONS = {
        'owned': {'sold', 'traded', 'gifted', 'listed', 'opened'},
        'listed': {'sold', 'owned'},
    }

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, entry: SealedCollectionEntry) -> int:
        """Add a sealed product to the collection. Returns the new ID."""
        if entry.added_at is None:
            entry.added_at = now_iso()
        cursor = self.conn.execute(
            """
            INSERT INTO sealed_collection
            (sealed_product_uuid, quantity, condition, purchase_price, purchase_date,
             source, seller_name, notes, status, sale_price, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.sealed_product_uuid,
                entry.quantity,
                entry.condition,
                entry.purchase_price,
                entry.purchase_date,
                entry.source,
                entry.seller_name,
                entry.notes,
                entry.status,
                entry.sale_price,
                entry.added_at,
            ),
        )
        return cursor.lastrowid

    def get(self, entry_id: int) -> Optional[SealedCollectionEntry]:
        """Get a sealed collection entry by ID."""
        cursor = self.conn.execute(
            "SELECT * FROM sealed_collection WHERE id = ?", (entry_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def update(self, entry: SealedCollectionEntry) -> bool:
        """Update a sealed collection entry. Returns True if updated."""
        if entry.id is None:
            return False
        cursor = self.conn.execute(
            """
            UPDATE sealed_collection SET
                sealed_product_uuid = ?, quantity = ?, condition = ?,
                purchase_price = ?, purchase_date = ?, source = ?,
                seller_name = ?, notes = ?, status = ?, sale_price = ?
            WHERE id = ?
            """,
            (
                entry.sealed_product_uuid,
                entry.quantity,
                entry.condition,
                entry.purchase_price,
                entry.purchase_date,
                entry.source,
                entry.seller_name,
                entry.notes,
                entry.status,
                entry.sale_price,
                entry.id,
            ),
        )
        return cursor.rowcount > 0

    def delete(self, entry_id: int) -> bool:
        """Delete a sealed collection entry. Returns True if deleted."""
        cursor = self.conn.execute(
            "DELETE FROM sealed_collection WHERE id = ?", (entry_id,)
        )
        return cursor.rowcount > 0

    def list_all(
        self,
        set_code: Optional[str] = None,
        category: Optional[str] = None,
        subtype: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List sealed collection entries with product info and latest prices."""
        query = """
            SELECT
                sc.id, sc.sealed_product_uuid, sc.quantity, sc.condition,
                sc.purchase_price, sc.purchase_date, sc.source, sc.seller_name,
                sc.notes, sc.status, sc.sale_price, sc.added_at,
                sp.name, sp.set_code, sp.category, sp.subtype,
                sp.tcgplayer_product_id, sp.card_count, sp.release_date,
                sp.purchase_url_tcgplayer, sp.purchase_url_cardkingdom,
                sp.contents_json,
                s.set_name,
                lsp.market_price, lsp.low_price, lsp.mid_price, lsp.high_price
            FROM sealed_collection sc
            JOIN sealed_products sp ON sc.sealed_product_uuid = sp.uuid
            LEFT JOIN sets s ON sp.set_code = s.set_code
            LEFT JOIN latest_sealed_prices lsp ON sp.tcgplayer_product_id = lsp.tcgplayer_product_id
            WHERE 1=1
        """
        params: List[Any] = []

        if set_code:
            query += " AND sp.set_code = ?"
            params.append(set_code.lower())
        if category:
            query += " AND sp.category = ?"
            params.append(category)
        if subtype:
            query += " AND sp.subtype = ?"
            params.append(subtype)
        if status:
            query += " AND sc.status = ?"
            params.append(status)

        query += " ORDER BY sc.added_at DESC"
        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor]

    def dispose(
        self,
        entry_id: int,
        new_status: str,
        sale_price: Optional[float] = None,
    ) -> bool:
        """Transition a sealed product to a disposition status."""
        entry = self.get(entry_id)
        if not entry:
            raise ValueError(f"Sealed collection entry {entry_id} not found")

        allowed = self.VALID_TRANSITIONS.get(entry.status)
        if not allowed or new_status not in allowed:
            raise ValueError(
                f"Cannot transition from '{entry.status}' to '{new_status}'"
            )

        entry.status = new_status
        if sale_price is not None:
            entry.sale_price = sale_price
        return self.update(entry)

    def stats(self) -> Dict[str, Any]:
        """Get sealed collection statistics."""
        stats: Dict[str, Any] = {}
        stats["total_entries"] = self.conn.execute(
            "SELECT COUNT(*) FROM sealed_collection"
        ).fetchone()[0]
        stats["total_quantity"] = self.conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM sealed_collection"
        ).fetchone()[0]

        cursor = self.conn.execute(
            "SELECT status, COUNT(*) as cnt, SUM(quantity) as qty FROM sealed_collection GROUP BY status"
        )
        stats["by_status"] = {row["status"]: {"count": row["cnt"], "quantity": row["qty"]} for row in cursor}

        total_cost = self.conn.execute(
            "SELECT COALESCE(SUM(purchase_price * quantity), 0) FROM sealed_collection WHERE purchase_price IS NOT NULL"
        ).fetchone()[0]
        stats["total_cost"] = total_cost

        market_value = self.conn.execute(
            """SELECT COALESCE(SUM(lsp.market_price * sc.quantity), 0)
            FROM sealed_collection sc
            JOIN sealed_products sp ON sc.sealed_product_uuid = sp.uuid
            LEFT JOIN latest_sealed_prices lsp ON sp.tcgplayer_product_id = lsp.tcgplayer_product_id
            WHERE sc.status = 'owned'"""
        ).fetchone()[0]
        stats["market_value"] = market_value
        stats["gain_loss"] = market_value - total_cost

        return stats

    def _row_to_entry(self, row: sqlite3.Row) -> SealedCollectionEntry:
        return SealedCollectionEntry(
            id=row["id"],
            sealed_product_uuid=row["sealed_product_uuid"],
            quantity=row["quantity"],
            condition=row["condition"],
            purchase_price=row["purchase_price"],
            purchase_date=row["purchase_date"],
            source=row["source"],
            seller_name=row["seller_name"],
            notes=row["notes"],
            status=row["status"],
            sale_price=row["sale_price"],
            added_at=row["added_at"],
        )
