"""Database schema and migrations."""

import sqlite3

SCHEMA_VERSION = 18

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
        CHECK(status IN ('owned', 'ordered', 'listed', 'sold', 'removed', 'traded', 'gifted', 'lost')),
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
    agent_trace TEXT,               -- JSON array of trace strings from the agent run
    api_usage TEXT,                 -- JSON dict of token usage by model {haiku/sonnet/opus: {input, output}}
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
    agent_trace TEXT,               -- JSON array of trace strings from the agent run
    api_usage TEXT,                 -- JSON dict of token usage by model {haiku/sonnet/opus: {input, output}}
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

-- MTGJSON UUID → (set_code, collector_number) mapping
CREATE TABLE IF NOT EXISTS mtgjson_uuid_map (
    uuid TEXT PRIMARY KEY,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uuid_map_card ON mtgjson_uuid_map(set_code, collector_number);

-- Price time series (append-only)
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    source TEXT NOT NULL,        -- 'cardkingdom', 'tcgplayer'
    price_type TEXT NOT NULL,    -- 'normal', 'foil'
    price REAL NOT NULL,
    observed_at TEXT NOT NULL,   -- date string YYYY-MM-DD
    UNIQUE(set_code, collector_number, source, price_type, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(observed_at);
CREATE INDEX IF NOT EXISTS idx_prices_card ON prices(set_code, collector_number, source, price_type);

-- Price fetch audit log
CREATE TABLE IF NOT EXISTS price_fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    source_file TEXT,
    dates_imported TEXT,         -- JSON array of date strings
    uuid_total INTEGER,
    uuid_mapped INTEGER,
    uuid_unmapped INTEGER,
    rows_inserted INTEGER
);

-- Latest prices view (global max — correct because imports are atomic)
CREATE VIEW IF NOT EXISTS latest_prices AS
SELECT set_code, collector_number, source, price_type, price, observed_at
FROM prices
WHERE observed_at = (SELECT MAX(observed_at) FROM prices);

-- MTGJSON card printings (imported from AllPrintings.json)
CREATE TABLE IF NOT EXISTS mtgjson_printings (
    uuid            TEXT PRIMARY KEY,
    scryfall_id     TEXT,
    name            TEXT NOT NULL,
    set_code        TEXT NOT NULL,
    number          TEXT NOT NULL,
    rarity          TEXT,
    border_color    TEXT,
    is_full_art     INTEGER DEFAULT 0,
    frame_effects   TEXT,
    ck_url          TEXT,
    ck_url_foil     TEXT,
    imported_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mtgjson_scryfall ON mtgjson_printings(scryfall_id);
CREATE INDEX IF NOT EXISTS idx_mtgjson_set ON mtgjson_printings(set_code);

-- MTGJSON booster sheet entries (uuid/weight per sheet)
CREATE TABLE IF NOT EXISTS mtgjson_booster_sheets (
    id          INTEGER PRIMARY KEY,
    set_code    TEXT NOT NULL,
    product     TEXT NOT NULL,
    sheet_name  TEXT NOT NULL,
    is_foil     INTEGER DEFAULT 0,
    uuid        TEXT NOT NULL,
    weight      INTEGER NOT NULL,
    FOREIGN KEY (uuid) REFERENCES mtgjson_printings(uuid)
);
CREATE INDEX IF NOT EXISTS idx_booster_sheet_lookup ON mtgjson_booster_sheets(set_code, product, sheet_name);

-- MTGJSON booster variant configurations
CREATE TABLE IF NOT EXISTS mtgjson_booster_configs (
    id              INTEGER PRIMARY KEY,
    set_code        TEXT NOT NULL,
    product         TEXT NOT NULL,
    variant_index   INTEGER NOT NULL,
    variant_weight  INTEGER NOT NULL,
    sheet_name      TEXT NOT NULL,
    card_count      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_set_product ON mtgjson_booster_configs(set_code, product);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_collection_scryfall ON collection(scryfall_id);
CREATE INDEX IF NOT EXISTS idx_collection_source ON collection(source);
CREATE INDEX IF NOT EXISTS idx_collection_status ON collection(status);
CREATE INDEX IF NOT EXISTS idx_printings_oracle ON printings(oracle_id);
CREATE INDEX IF NOT EXISTS idx_printings_set ON printings(set_code);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);

-- Sealed product reference data (imported from MTGJSON AllPrintings.json)
CREATE TABLE IF NOT EXISTS sealed_products (
    uuid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    set_code TEXT NOT NULL,
    category TEXT NOT NULL,
    subtype TEXT,
    tcgplayer_product_id TEXT,
    card_count INTEGER,
    product_size INTEGER,
    release_date TEXT,
    purchase_url_tcgplayer TEXT,
    purchase_url_cardkingdom TEXT,
    contents_json TEXT,
    imported_at TEXT NOT NULL,
    FOREIGN KEY (set_code) REFERENCES sets(set_code)
);
CREATE INDEX IF NOT EXISTS idx_sealed_products_set ON sealed_products(set_code);
CREATE INDEX IF NOT EXISTS idx_sealed_products_tcg ON sealed_products(tcgplayer_product_id);
CREATE INDEX IF NOT EXISTS idx_sealed_products_category ON sealed_products(category);

-- User's sealed product collection (one row per acquisition)
CREATE TABLE IF NOT EXISTS sealed_collection (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sealed_product_uuid TEXT NOT NULL REFERENCES sealed_products(uuid),
    quantity INTEGER NOT NULL DEFAULT 1,
    condition TEXT DEFAULT 'Near Mint',
    purchase_price REAL,
    purchase_date TEXT,
    source TEXT,
    seller_name TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'owned'
        CHECK (status IN ('owned', 'listed', 'sold', 'traded', 'gifted', 'opened')),
    sale_price REAL,
    added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sealed_collection_product ON sealed_collection(sealed_product_uuid);
CREATE INDEX IF NOT EXISTS idx_sealed_collection_status ON sealed_collection(status);

-- Sealed product prices (time series from TCGCSV)
CREATE TABLE IF NOT EXISTS sealed_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tcgplayer_product_id TEXT NOT NULL,
    low_price REAL,
    mid_price REAL,
    high_price REAL,
    market_price REAL,
    direct_low_price REAL,
    observed_at TEXT NOT NULL,
    UNIQUE(tcgplayer_product_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_sealed_prices_product ON sealed_prices(tcgplayer_product_id);
CREATE INDEX IF NOT EXISTS idx_sealed_prices_date ON sealed_prices(observed_at);

-- TCGCSV group mapping (set_code -> tcgplayer groupId)
CREATE TABLE IF NOT EXISTS tcgplayer_groups (
    group_id INTEGER PRIMARY KEY,
    set_code TEXT,
    name TEXT NOT NULL,
    abbreviation TEXT,
    published_on TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tcgplayer_groups_abbr ON tcgplayer_groups(abbreviation);

-- Denormalized sealed collection view
CREATE VIEW IF NOT EXISTS sealed_collection_view AS
SELECT
    sc.id, sc.quantity, sc.condition, sc.purchase_price, sc.purchase_date,
    sc.source, sc.seller_name, sc.notes, sc.status, sc.sale_price, sc.added_at,
    sp.uuid, sp.name, sp.set_code, sp.category, sp.subtype,
    sp.tcgplayer_product_id, sp.card_count, sp.release_date,
    sp.purchase_url_tcgplayer, sp.purchase_url_cardkingdom,
    s.set_name, s.set_type, s.released_at AS set_released_at
FROM sealed_collection sc
JOIN sealed_products sp ON sc.sealed_product_uuid = sp.uuid
LEFT JOIN sets s ON sp.set_code = s.set_code;

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
        if current < 13:
            _migrate_v12_to_v13(conn)
        if current < 14:
            _migrate_v13_to_v14(conn)
        if current < 15:
            _migrate_v14_to_v15(conn)
        if current < 16:
            _migrate_v15_to_v16(conn)
        if current < 17:
            _migrate_v16_to_v17(conn)
        if current < 18:
            _migrate_v17_to_v18(conn)

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


def _migrate_v12_to_v13(conn: sqlite3.Connection):
    """Add agent_trace column to ingest_cache and ingest_images."""
    for table in ("ingest_cache", "ingest_images"):
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        if "agent_trace" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN agent_trace TEXT")


def _migrate_v13_to_v14(conn: sqlite3.Connection):
    """Add api_usage column to ingest_cache and ingest_images."""
    for table in ("ingest_cache", "ingest_images"):
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        if "api_usage" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN api_usage TEXT")


def _migrate_v14_to_v15(conn: sqlite3.Connection):
    """Add price tables, UUID map, and latest_prices view."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mtgjson_uuid_map (
            uuid TEXT PRIMARY KEY,
            set_code TEXT NOT NULL,
            collector_number TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_uuid_map_card ON mtgjson_uuid_map(set_code, collector_number);

        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            set_code TEXT NOT NULL,
            collector_number TEXT NOT NULL,
            source TEXT NOT NULL,
            price_type TEXT NOT NULL,
            price REAL NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(set_code, collector_number, source, price_type, observed_at)
        );
        CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(observed_at);
        CREATE INDEX IF NOT EXISTS idx_prices_card ON prices(set_code, collector_number, source, price_type);

        CREATE TABLE IF NOT EXISTS price_fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            source_file TEXT,
            dates_imported TEXT,
            uuid_total INTEGER,
            uuid_mapped INTEGER,
            uuid_unmapped INTEGER,
            rows_inserted INTEGER
        );
    """)

    conn.execute("DROP VIEW IF EXISTS latest_prices")
    conn.execute("""
        CREATE VIEW latest_prices AS
        SELECT set_code, collector_number, source, price_type, price, observed_at
        FROM prices
        WHERE observed_at = (SELECT MAX(observed_at) FROM prices)
    """)


def _migrate_v15_to_v16(conn: sqlite3.Connection):
    """Add MTGJSON printings, booster sheets, and booster configs tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mtgjson_printings (
            uuid            TEXT PRIMARY KEY,
            scryfall_id     TEXT,
            name            TEXT NOT NULL,
            set_code        TEXT NOT NULL,
            number          TEXT NOT NULL,
            rarity          TEXT,
            border_color    TEXT,
            is_full_art     INTEGER DEFAULT 0,
            frame_effects   TEXT,
            ck_url          TEXT,
            ck_url_foil     TEXT,
            imported_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mtgjson_scryfall ON mtgjson_printings(scryfall_id);
        CREATE INDEX IF NOT EXISTS idx_mtgjson_set ON mtgjson_printings(set_code);

        CREATE TABLE IF NOT EXISTS mtgjson_booster_sheets (
            id          INTEGER PRIMARY KEY,
            set_code    TEXT NOT NULL,
            product     TEXT NOT NULL,
            sheet_name  TEXT NOT NULL,
            is_foil     INTEGER DEFAULT 0,
            uuid        TEXT NOT NULL,
            weight      INTEGER NOT NULL,
            FOREIGN KEY (uuid) REFERENCES mtgjson_printings(uuid)
        );
        CREATE INDEX IF NOT EXISTS idx_booster_sheet_lookup ON mtgjson_booster_sheets(set_code, product, sheet_name);

        CREATE TABLE IF NOT EXISTS mtgjson_booster_configs (
            id              INTEGER PRIMARY KEY,
            set_code        TEXT NOT NULL,
            product         TEXT NOT NULL,
            variant_index   INTEGER NOT NULL,
            variant_weight  INTEGER NOT NULL,
            sheet_name      TEXT NOT NULL,
            card_count      INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_config_set_product ON mtgjson_booster_configs(set_code, product);
    """)


def _migrate_v16_to_v17(conn: sqlite3.Connection):
    """Expand status CHECK constraint to include traded, gifted, lost.

    SQLite can't ALTER CHECK constraints, so we rebuild the collection table.
    Must drop collection_view first — SQLite 3.25+ validates schema on
    ALTER TABLE RENAME and will error on a view referencing a dropped table.
    """
    # Drop view BEFORE the table rebuild to avoid schema validation errors
    conn.execute("DROP VIEW IF EXISTS collection_view")

    # Detect partially-migrated state from a previous failed run.
    # executescript auto-commits each DDL statement, so a failure mid-script
    # can leave collection_new present and collection already dropped.
    has_collection = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection'"
    ).fetchone() is not None
    has_collection_new = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_new'"
    ).fetchone() is not None

    if has_collection_new and not has_collection:
        # Recovery: previous run dropped collection but failed on rename
        conn.execute("ALTER TABLE collection_new RENAME TO collection")
    else:
        # Normal migration (clean up leftover collection_new if present)
        conn.executescript("""
            DROP TABLE IF EXISTS collection_new;

            CREATE TABLE collection_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT NOT NULL REFERENCES printings(scryfall_id),
                finish TEXT NOT NULL CHECK(finish IN ('nonfoil', 'foil', 'etched')),
                condition TEXT NOT NULL DEFAULT 'Near Mint'
                    CHECK(condition IN ('Near Mint', 'Lightly Played', 'Moderately Played', 'Heavily Played', 'Damaged')),
                language TEXT NOT NULL DEFAULT 'English',
                purchase_price REAL,
                acquired_at TEXT NOT NULL,
                source TEXT NOT NULL,
                source_image TEXT,
                notes TEXT,
                tags TEXT,
                tradelist INTEGER DEFAULT 0,
                is_alter INTEGER DEFAULT 0,
                proxy INTEGER DEFAULT 0,
                signed INTEGER DEFAULT 0,
                misprint INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'owned'
                    CHECK(status IN ('owned', 'ordered', 'listed', 'sold', 'removed', 'traded', 'gifted', 'lost')),
                sale_price REAL,
                order_id INTEGER REFERENCES orders(id)
            );

            INSERT INTO collection_new SELECT * FROM collection;

            DROP TABLE collection;

            ALTER TABLE collection_new RENAME TO collection;
        """)

    # Ensure indexes and view exist (idempotent for both normal and recovery paths)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_collection_scryfall ON collection(scryfall_id);
        CREATE INDEX IF NOT EXISTS idx_collection_source ON collection(source);
        CREATE INDEX IF NOT EXISTS idx_collection_status ON collection(status);
    """)
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


def _migrate_v17_to_v18(conn: sqlite3.Connection):
    """Add sealed product tables, sealed collection, sealed prices, and TCGCSV groups."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sealed_products (
            uuid TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            set_code TEXT NOT NULL,
            category TEXT NOT NULL,
            subtype TEXT,
            tcgplayer_product_id TEXT,
            card_count INTEGER,
            product_size INTEGER,
            release_date TEXT,
            purchase_url_tcgplayer TEXT,
            purchase_url_cardkingdom TEXT,
            contents_json TEXT,
            imported_at TEXT NOT NULL,
            FOREIGN KEY (set_code) REFERENCES sets(set_code)
        );
        CREATE INDEX IF NOT EXISTS idx_sealed_products_set ON sealed_products(set_code);
        CREATE INDEX IF NOT EXISTS idx_sealed_products_tcg ON sealed_products(tcgplayer_product_id);
        CREATE INDEX IF NOT EXISTS idx_sealed_products_category ON sealed_products(category);

        CREATE TABLE IF NOT EXISTS sealed_collection (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sealed_product_uuid TEXT NOT NULL REFERENCES sealed_products(uuid),
            quantity INTEGER NOT NULL DEFAULT 1,
            condition TEXT DEFAULT 'Near Mint',
            purchase_price REAL,
            purchase_date TEXT,
            source TEXT,
            seller_name TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'owned'
                CHECK (status IN ('owned', 'listed', 'sold', 'traded', 'gifted', 'opened')),
            sale_price REAL,
            added_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sealed_collection_product ON sealed_collection(sealed_product_uuid);
        CREATE INDEX IF NOT EXISTS idx_sealed_collection_status ON sealed_collection(status);

        CREATE TABLE IF NOT EXISTS sealed_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tcgplayer_product_id TEXT NOT NULL,
            low_price REAL,
            mid_price REAL,
            high_price REAL,
            market_price REAL,
            direct_low_price REAL,
            observed_at TEXT NOT NULL,
            UNIQUE(tcgplayer_product_id, observed_at)
        );
        CREATE INDEX IF NOT EXISTS idx_sealed_prices_product ON sealed_prices(tcgplayer_product_id);
        CREATE INDEX IF NOT EXISTS idx_sealed_prices_date ON sealed_prices(observed_at);

        CREATE TABLE IF NOT EXISTS tcgplayer_groups (
            group_id INTEGER PRIMARY KEY,
            set_code TEXT,
            name TEXT NOT NULL,
            abbreviation TEXT,
            published_on TEXT,
            fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tcgplayer_groups_abbr ON tcgplayer_groups(abbreviation);
    """)

    conn.execute("""
        CREATE VIEW IF NOT EXISTS sealed_collection_view AS
        SELECT
            sc.id, sc.quantity, sc.condition, sc.purchase_price, sc.purchase_date,
            sc.source, sc.seller_name, sc.notes, sc.status, sc.sale_price, sc.added_at,
            sp.uuid, sp.name, sp.set_code, sp.category, sp.subtype,
            sp.tcgplayer_product_id, sp.card_count, sp.release_date,
            sp.purchase_url_tcgplayer, sp.purchase_url_cardkingdom,
            s.set_name, s.set_type, s.released_at AS set_released_at
        FROM sealed_collection sc
        JOIN sealed_products sp ON sc.sealed_product_uuid = sp.uuid
        LEFT JOIN sets s ON sp.set_code = s.set_code
    """)


def drop_all_tables(conn: sqlite3.Connection):
    """Drop all tables (for testing/reset)."""
    conn.executescript("""
        DROP VIEW IF EXISTS sealed_collection_view;
        DROP VIEW IF EXISTS collection_view;
        DROP VIEW IF EXISTS latest_prices;
        DROP TABLE IF EXISTS tcgplayer_groups;
        DROP TABLE IF EXISTS sealed_prices;
        DROP TABLE IF EXISTS sealed_collection;
        DROP TABLE IF EXISTS sealed_products;
        DROP TABLE IF EXISTS price_fetch_log;
        DROP TABLE IF EXISTS prices;
        DROP TABLE IF EXISTS mtgjson_booster_configs;
        DROP TABLE IF EXISTS mtgjson_booster_sheets;
        DROP TABLE IF EXISTS mtgjson_printings;
        DROP TABLE IF EXISTS mtgjson_uuid_map;
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
