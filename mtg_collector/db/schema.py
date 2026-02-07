"""Database schema and migrations."""

import sqlite3
from typing import Optional

SCHEMA_VERSION = 4

SCHEMA_SQL = """
-- Abstract cards (oracle-level, cached from Scryfall)
CREATE TABLE IF NOT EXISTS cards (
    oracle_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type_line TEXT,
    mana_cost TEXT,
    cmc REAL,
    oracle_text TEXT,
    colors TEXT,           -- JSON array: ["R", "G"]
    color_identity TEXT    -- JSON array
);

-- Sets (cached from Scryfall)
CREATE TABLE IF NOT EXISTS sets (
    set_code TEXT PRIMARY KEY,
    set_name TEXT NOT NULL,
    set_type TEXT,
    released_at TEXT,
    cards_fetched_at TEXT  -- NULL = card list not cached, otherwise ISO timestamp
);

-- Specific printings (cached from Scryfall)
CREATE TABLE IF NOT EXISTS printings (
    scryfall_id TEXT PRIMARY KEY,
    oracle_id TEXT NOT NULL REFERENCES cards(oracle_id),
    set_code TEXT NOT NULL REFERENCES sets(set_code),
    collector_number TEXT NOT NULL,
    rarity TEXT,
    frame_effects TEXT,    -- JSON array
    border_color TEXT,
    full_art INTEGER,
    promo INTEGER,
    promo_types TEXT,      -- JSON array
    finishes TEXT,         -- JSON array (what finishes exist for this printing)
    artist TEXT,
    image_uri TEXT,
    raw_json TEXT,         -- Full Scryfall API response as JSON (for semantic search, etc.)
    UNIQUE(set_code, collector_number)
);

-- User's collection (one row per physical card owned)
CREATE TABLE IF NOT EXISTS collection (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id TEXT NOT NULL REFERENCES printings(scryfall_id),
    finish TEXT NOT NULL CHECK(finish IN ('nonfoil', 'foil', 'etched')),
    condition TEXT NOT NULL DEFAULT 'Near Mint'
        CHECK(condition IN ('Near Mint', 'Lightly Played', 'Moderately Played', 'Heavily Played', 'Damaged')),
    language TEXT NOT NULL DEFAULT 'English',
    purchase_price REAL,
    acquired_at TEXT NOT NULL,  -- ISO 8601 timestamp
    source TEXT NOT NULL,       -- 'photo_ingest', 'moxfield_import', 'archidekt_import', 'deckbox_import', 'manual'
    source_image TEXT,          -- file path of the source image (for photo-based ingestion)
    notes TEXT,
    tags TEXT,                  -- JSON array or comma-separated
    tradelist INTEGER DEFAULT 0,
    is_alter INTEGER DEFAULT 0,
    proxy INTEGER DEFAULT 0,
    signed INTEGER DEFAULT 0,
    misprint INTEGER DEFAULT 0
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_collection_scryfall ON collection(scryfall_id);
CREATE INDEX IF NOT EXISTS idx_collection_source ON collection(source);
CREATE INDEX IF NOT EXISTS idx_printings_oracle ON printings(oracle_id);
CREATE INDEX IF NOT EXISTS idx_printings_set ON printings(set_code);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);
"""


def get_current_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version, or 0 if not initialized."""
    try:
        cursor = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = cursor.fetchone()
        return row[0] if row[0] is not None else 0
    except sqlite3.OperationalError:
        # Table doesn't exist
        return 0


def init_db(conn: sqlite3.Connection, force: bool = False) -> bool:
    """
    Initialize or migrate the database schema.

    Args:
        conn: Database connection
        force: If True, recreate tables even if they exist

    Returns:
        True if schema was created/updated, False if already up to date
    """
    from mtg_collector.utils import now_iso

    current = get_current_version(conn)

    if current >= SCHEMA_VERSION and not force:
        return False

    if current == 0 or force:
        # Fresh install - create all tables
        conn.executescript(SCHEMA_SQL)
    else:
        # Run migrations
        if current < 2:
            _migrate_v1_to_v2(conn)
        if current < 3:
            _migrate_v2_to_v3(conn)
        if current < 4:
            _migrate_v3_to_v4(conn)

    # Record schema version
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, now_iso())
    )
    conn.commit()

    return True


def _migrate_v1_to_v2(conn: sqlite3.Connection):
    """Add cards_fetched_at column to sets table."""
    # Check if column already exists
    cursor = conn.execute("PRAGMA table_info(sets)")
    columns = [row[1] for row in cursor.fetchall()]

    if "cards_fetched_at" not in columns:
        conn.execute("ALTER TABLE sets ADD COLUMN cards_fetched_at TEXT")


def _migrate_v2_to_v3(conn: sqlite3.Connection):
    """Add raw_json column to printings table for full Scryfall metadata."""
    cursor = conn.execute("PRAGMA table_info(printings)")
    columns = [row[1] for row in cursor.fetchall()]

    if "raw_json" not in columns:
        conn.execute("ALTER TABLE printings ADD COLUMN raw_json TEXT")


def _migrate_v3_to_v4(conn: sqlite3.Connection):
    """Add source_image column to collection table."""
    cursor = conn.execute("PRAGMA table_info(collection)")
    columns = [row[1] for row in cursor.fetchall()]

    if "source_image" not in columns:
        conn.execute("ALTER TABLE collection ADD COLUMN source_image TEXT")


def drop_all_tables(conn: sqlite3.Connection):
    """Drop all tables (for testing/reset)."""
    conn.executescript("""
        DROP TABLE IF EXISTS collection;
        DROP TABLE IF EXISTS printings;
        DROP TABLE IF EXISTS cards;
        DROP TABLE IF EXISTS sets;
        DROP TABLE IF EXISTS schema_version;
    """)
    conn.commit()
