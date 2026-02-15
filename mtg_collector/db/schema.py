"""Database schema and migrations."""

import sqlite3

SCHEMA_VERSION = 12

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

-- Orders (TCGPlayer, Card Kingdom, etc.)
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT,
    source TEXT,            -- 'tcgplayer', 'cardkingdom', 'other'
    seller_name TEXT,
    order_date TEXT,
    subtotal REAL,
    shipping REAL,
    tax REAL,
    total REAL,
    shipping_status TEXT,
    estimated_delivery TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(order_number);

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
    misprint INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'owned'
        CHECK(status IN ('owned', 'ordered', 'listed', 'sold', 'removed')),
    sale_price REAL,
    order_id INTEGER REFERENCES orders(id)
);

-- Status audit log (append-only)
CREATE TABLE IF NOT EXISTS status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_log_collection ON status_log(collection_id);

-- Wishlist (separate entity — can be oracle-level or printing-specific)
CREATE TABLE IF NOT EXISTS wishlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    oracle_id TEXT NOT NULL REFERENCES cards(oracle_id),
    scryfall_id TEXT REFERENCES printings(scryfall_id),  -- NULL = "any printing"
    max_price REAL,
    priority INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    added_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    fulfilled_at TEXT  -- set when the want is satisfied
);
CREATE INDEX IF NOT EXISTS idx_wishlist_oracle ON wishlist(oracle_id);
CREATE INDEX IF NOT EXISTS idx_wishlist_scryfall ON wishlist(scryfall_id);

-- Ingest cache: OCR + Claude results by image MD5
CREATE TABLE IF NOT EXISTS ingest_cache (
    image_md5 TEXT PRIMARY KEY,
    image_path TEXT NOT NULL,
    ocr_result TEXT NOT NULL,       -- JSON array of {text, bbox, confidence}
    claude_result TEXT,             -- JSON array of card dicts from Claude
    created_at TEXT NOT NULL
);

-- Ingest lineage: track which card came from which image
CREATE TABLE IF NOT EXISTS ingest_lineage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES collection(id),
    image_md5 TEXT NOT NULL,
    image_path TEXT NOT NULL,
    card_index INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lineage_md5 ON ingest_lineage(image_md5);
CREATE INDEX IF NOT EXISTS idx_lineage_collection ON ingest_lineage(collection_id);

-- Ingest images: persistent ingest pipeline state
CREATE TABLE IF NOT EXISTS ingest_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    md5 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'READY_FOR_OCR'
        CHECK(status IN ('READY_FOR_OCR','PROCESSING','READY_FOR_DISAMBIGUATION','DONE','ERROR')),
    mode TEXT,
    ocr_result TEXT,
    claude_result TEXT,
    scryfall_matches TEXT,
    crops TEXT,
    disambiguated TEXT,
    names_data TEXT,
    names_disambiguated TEXT,
    user_card_edits TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingest_images_status ON ingest_images(status);

-- Global settings (key-value pairs)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_collection_scryfall ON collection(scryfall_id);
CREATE INDEX IF NOT EXISTS idx_collection_source ON collection(source);
CREATE INDEX IF NOT EXISTS idx_collection_status ON collection(status);
CREATE INDEX IF NOT EXISTS idx_printings_oracle ON printings(oracle_id);
CREATE INDEX IF NOT EXISTS idx_printings_set ON printings(set_code);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);

-- Denormalized collection view
CREATE VIEW IF NOT EXISTS collection_view AS
SELECT
    c.id,
    card.name,
    s.set_name,
    p.set_code,
    p.collector_number,
    p.rarity,
    p.promo,
    c.finish,
    c.condition,
    c.language,
    card.type_line,
    card.mana_cost,
    card.cmc,
    card.colors,
    card.color_identity,
    p.artist,
    c.purchase_price,
    c.acquired_at,
    c.source,
    c.source_image,
    c.notes,
    c.tags,
    c.tradelist,
    c.status,
    c.sale_price,
    c.scryfall_id,
    p.oracle_id,
    c.order_id
FROM collection c
JOIN printings p ON c.scryfall_id = p.scryfall_id
JOIN cards card ON p.oracle_id = card.oracle_id
JOIN sets s ON p.set_code = s.set_code;
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
        # Seed default settings
        _seed_default_settings(conn)
    else:
        # Run migrations
        if current < 2:
            _migrate_v1_to_v2(conn)
        if current < 3:
            _migrate_v2_to_v3(conn)
        if current < 4:
            _migrate_v3_to_v4(conn)
        if current < 5:
            _migrate_v4_to_v5(conn)
        if current < 6:
            _migrate_v5_to_v6(conn)
        if current < 7:
            _migrate_v6_to_v7(conn)
        if current < 8:
            _migrate_v7_to_v8(conn)
        if current < 9:
            _migrate_v8_to_v9(conn)
        if current < 10:
            _migrate_v9_to_v10(conn)
        if current < 11:
            _migrate_v10_to_v11(conn)
        if current < 12:
            _migrate_v11_to_v12(conn)

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


def _migrate_v4_to_v5(conn: sqlite3.Connection):
    """Add denormalized collection_view."""
    conn.execute("DROP VIEW IF EXISTS collection_view")
    conn.execute("""
        CREATE VIEW collection_view AS
        SELECT
            c.id,
            card.name,
            s.set_name,
            p.set_code,
            p.collector_number,
            p.rarity,
            p.promo,
            c.finish,
            c.condition,
            c.language,
            card.type_line,
            card.mana_cost,
            card.cmc,
            card.colors,
            card.color_identity,
            p.artist,
            c.purchase_price,
            c.acquired_at,
            c.source,
            c.source_image,
            c.notes,
            c.tags,
            c.tradelist,
            c.scryfall_id,
            p.oracle_id
        FROM collection c
        JOIN printings p ON c.scryfall_id = p.scryfall_id
        JOIN cards card ON p.oracle_id = card.oracle_id
        JOIN sets s ON p.set_code = s.set_code
    """)


def _migrate_v5_to_v6(conn: sqlite3.Connection):
    """Add ingest_cache and ingest_lineage tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ingest_cache (
            image_md5 TEXT PRIMARY KEY,
            image_path TEXT NOT NULL,
            ocr_result TEXT NOT NULL,
            claude_result TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ingest_lineage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id INTEGER NOT NULL REFERENCES collection(id),
            image_md5 TEXT NOT NULL,
            image_path TEXT NOT NULL,
            card_index INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_lineage_md5 ON ingest_lineage(image_md5);
        CREATE INDEX IF NOT EXISTS idx_lineage_collection ON ingest_lineage(collection_id);
    """)


def _seed_default_settings(conn: sqlite3.Connection):
    """Insert default settings values (idempotent)."""
    for key, value in [
        ("image_display", "crop"),
        ("price_sources", "tcg,ck"),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def _migrate_v6_to_v7(conn: sqlite3.Connection):
    """Add settings table with default values."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    _seed_default_settings(conn)


def _migrate_v7_to_v8(conn: sqlite3.Connection):
    """Add status/sale_price to collection, status_log table, wishlist table."""
    from mtg_collector.utils import now_iso

    cursor = conn.execute("PRAGMA table_info(collection)")
    columns = [row[1] for row in cursor.fetchall()]

    if "status" not in columns:
        conn.execute(
            "ALTER TABLE collection ADD COLUMN status TEXT NOT NULL DEFAULT 'owned'"
        )
    if "sale_price" not in columns:
        conn.execute("ALTER TABLE collection ADD COLUMN sale_price REAL")

    # Migrate tradelist=1 → status='listed'
    conn.execute("UPDATE collection SET status = 'listed' WHERE tradelist = 1")

    # Status audit log
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id INTEGER NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
            from_status TEXT,
            to_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_status_log_collection ON status_log(collection_id);
    """)

    # Wishlist
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS wishlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            oracle_id TEXT NOT NULL REFERENCES cards(oracle_id),
            scryfall_id TEXT REFERENCES printings(scryfall_id),
            max_price REAL,
            priority INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            added_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            fulfilled_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wishlist_oracle ON wishlist(oracle_id);
        CREATE INDEX IF NOT EXISTS idx_wishlist_scryfall ON wishlist(scryfall_id);
    """)

    # Status index
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collection_status ON collection(status)"
    )

    # Rebuild collection_view to include status and sale_price
    conn.execute("DROP VIEW IF EXISTS collection_view")
    conn.execute("""
        CREATE VIEW collection_view AS
        SELECT
            c.id,
            card.name,
            s.set_name,
            p.set_code,
            p.collector_number,
            p.rarity,
            p.promo,
            c.finish,
            c.condition,
            c.language,
            card.type_line,
            card.mana_cost,
            card.cmc,
            card.colors,
            card.color_identity,
            p.artist,
            c.purchase_price,
            c.acquired_at,
            c.source,
            c.source_image,
            c.notes,
            c.tags,
            c.tradelist,
            c.status,
            c.sale_price,
            c.scryfall_id,
            p.oracle_id
        FROM collection c
        JOIN printings p ON c.scryfall_id = p.scryfall_id
        JOIN cards card ON p.oracle_id = card.oracle_id
        JOIN sets s ON p.set_code = s.set_code
    """)

    # Seed status_log with initial entries for existing collection rows
    ts = now_iso()
    conn.execute("""
        INSERT INTO status_log (collection_id, from_status, to_status, changed_at, note)
        SELECT id, NULL, status, COALESCE(acquired_at, ?), 'migration seed'
        FROM collection
        WHERE id NOT IN (SELECT collection_id FROM status_log)
    """, (ts,))


def _migrate_v8_to_v9(conn: sqlite3.Connection):
    """Add orders table and order_id FK on collection."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT,
            source TEXT,
            seller_name TEXT,
            order_date TEXT,
            subtotal REAL,
            shipping REAL,
            tax REAL,
            total REAL,
            shipping_status TEXT,
            estimated_delivery TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(order_number);
    """)

    cursor = conn.execute("PRAGMA table_info(collection)")
    columns = [row[1] for row in cursor.fetchall()]

    if "order_id" not in columns:
        conn.execute("ALTER TABLE collection ADD COLUMN order_id INTEGER REFERENCES orders(id)")

    # Rebuild collection_view to include order_id
    conn.execute("DROP VIEW IF EXISTS collection_view")
    conn.execute("""
        CREATE VIEW collection_view AS
        SELECT
            c.id,
            card.name,
            s.set_name,
            p.set_code,
            p.collector_number,
            p.rarity,
            p.promo,
            c.finish,
            c.condition,
            c.language,
            card.type_line,
            card.mana_cost,
            card.cmc,
            card.colors,
            card.color_identity,
            p.artist,
            c.purchase_price,
            c.acquired_at,
            c.source,
            c.source_image,
            c.notes,
            c.tags,
            c.tradelist,
            c.status,
            c.sale_price,
            c.scryfall_id,
            p.oracle_id,
            c.order_id
        FROM collection c
        JOIN printings p ON c.scryfall_id = p.scryfall_id
        JOIN cards card ON p.oracle_id = card.oracle_id
        JOIN sets s ON p.set_code = s.set_code
    """)


def _migrate_v9_to_v10(conn: sqlite3.Connection):
    """Add ingest_images table for persistent ingest pipeline state."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ingest_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            md5 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'READY_FOR_OCR'
                CHECK(status IN ('READY_FOR_OCR','PROCESSING','READY_FOR_DISAMBIGUATION','DONE','ERROR')),
            mode TEXT,
            ocr_result TEXT,
            claude_result TEXT,
            scryfall_matches TEXT,
            crops TEXT,
            disambiguated TEXT,
            names_data TEXT,
            names_disambiguated TEXT,
            user_card_edits TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ingest_images_status ON ingest_images(status);
    """)


def _migrate_v10_to_v11(conn: sqlite3.Connection):
    """Remove card_count columns; ensure orders table exists (catch-up from rebase)."""
    # Ensure orders table + order_id column exist (may have been skipped if DB
    # was already at v10 under the old numbering before the merge renumbered v8→v9).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT,
            source TEXT,
            seller_name TEXT,
            order_date TEXT,
            subtotal REAL,
            shipping REAL,
            tax REAL,
            total REAL,
            shipping_status TEXT,
            estimated_delivery TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(order_number);
    """)

    cursor = conn.execute("PRAGMA table_info(collection)")
    columns = [row[1] for row in cursor.fetchall()]
    if "order_id" not in columns:
        conn.execute("ALTER TABLE collection ADD COLUMN order_id INTEGER REFERENCES orders(id)")

    # Rebuild collection_view to include order_id
    conn.execute("DROP VIEW IF EXISTS collection_view")
    conn.execute("""
        CREATE VIEW collection_view AS
        SELECT
            c.id,
            card.name,
            s.set_name,
            p.set_code,
            p.collector_number,
            p.rarity,
            p.promo,
            c.finish,
            c.condition,
            c.language,
            card.type_line,
            card.mana_cost,
            card.cmc,
            card.colors,
            card.color_identity,
            p.artist,
            c.purchase_price,
            c.acquired_at,
            c.source,
            c.source_image,
            c.notes,
            c.tags,
            c.tradelist,
            c.status,
            c.sale_price,
            c.scryfall_id,
            p.oracle_id,
            c.order_id
        FROM collection c
        JOIN printings p ON c.scryfall_id = p.scryfall_id
        JOIN cards card ON p.oracle_id = card.oracle_id
        JOIN sets s ON p.set_code = s.set_code
    """)

    # ingest_cache: drop card_count
    cursor = conn.execute("PRAGMA table_info(ingest_cache)")
    columns = [row[1] for row in cursor.fetchall()]
    if "card_count" in columns:
        conn.execute("ALTER TABLE ingest_cache DROP COLUMN card_count")

    # ingest_images: drop card_count
    cursor = conn.execute("PRAGMA table_info(ingest_images)")
    columns = [row[1] for row in cursor.fetchall()]
    if "card_count" in columns:
        conn.execute("ALTER TABLE ingest_images DROP COLUMN card_count")


def _migrate_v11_to_v12(conn: sqlite3.Connection):
    """Catch-up: ensure orders table + order_id exist (fixes v11 DBs that missed v8→v9)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT,
            source TEXT,
            seller_name TEXT,
            order_date TEXT,
            subtotal REAL,
            shipping REAL,
            tax REAL,
            total REAL,
            shipping_status TEXT,
            estimated_delivery TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(order_number);
    """)

    cursor = conn.execute("PRAGMA table_info(collection)")
    columns = [row[1] for row in cursor.fetchall()]
    if "order_id" not in columns:
        conn.execute("ALTER TABLE collection ADD COLUMN order_id INTEGER REFERENCES orders(id)")

    # Rebuild collection_view to include order_id
    conn.execute("DROP VIEW IF EXISTS collection_view")
    conn.execute("""
        CREATE VIEW collection_view AS
        SELECT
            c.id,
            card.name,
            s.set_name,
            p.set_code,
            p.collector_number,
            p.rarity,
            p.promo,
            c.finish,
            c.condition,
            c.language,
            card.type_line,
            card.mana_cost,
            card.cmc,
            card.colors,
            card.color_identity,
            p.artist,
            c.purchase_price,
            c.acquired_at,
            c.source,
            c.source_image,
            c.notes,
            c.tags,
            c.tradelist,
            c.status,
            c.sale_price,
            c.scryfall_id,
            p.oracle_id,
            c.order_id
        FROM collection c
        JOIN printings p ON c.scryfall_id = p.scryfall_id
        JOIN cards card ON p.oracle_id = card.oracle_id
        JOIN sets s ON p.set_code = s.set_code
    """)


def drop_all_tables(conn: sqlite3.Connection):
    """Drop all tables (for testing/reset)."""
    conn.executescript("""
        DROP VIEW IF EXISTS collection_view;
        DROP TABLE IF EXISTS status_log;
        DROP TABLE IF EXISTS wishlist;
        DROP TABLE IF EXISTS settings;
        DROP TABLE IF EXISTS ingest_images;
        DROP TABLE IF EXISTS ingest_lineage;
        DROP TABLE IF EXISTS ingest_cache;
        DROP TABLE IF EXISTS collection;
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS printings;
        DROP TABLE IF EXISTS cards;
        DROP TABLE IF EXISTS sets;
        DROP TABLE IF EXISTS schema_version;
    """)
    conn.commit()
