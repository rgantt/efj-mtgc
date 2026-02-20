"""
Tests for price import pipeline (schema v15, UUID map, import, latest_prices view).

To run: uv run pytest tests/test_price_import.py -v
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mtg_collector.db.connection import close_connection, get_connection
from mtg_collector.db.schema import (
    SCHEMA_VERSION,
    get_current_version,
    init_db,
)


@pytest.fixture
def test_db():
    """Create a fresh temporary database with v15 schema."""
    close_connection()
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    conn = get_connection(db_path)
    init_db(conn)

    yield db_path, conn

    close_connection()
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def mock_allprintings(tmp_path):
    """Create a mock AllPrintings.json."""
    data = {
        "data": {
            "NEO": {
                "cards": [
                    {"uuid": "uuid-001", "number": "1"},
                    {"uuid": "uuid-002", "number": "2"},
                    {"uuid": "uuid-003", "number": "3"},
                ]
            },
            "MOM": {
                "cards": [
                    {"uuid": "uuid-010", "number": "10"},
                    {"uuid": "uuid-011", "number": "11"},
                ]
            },
        }
    }
    path = tmp_path / "AllPrintings.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def mock_allpricestoday(tmp_path):
    """Create a mock AllPricesToday.json with 5 cards."""
    data = {
        "data": {
            "uuid-001": {
                "paper": {
                    "cardkingdom": {
                        "retail": {
                            "normal": {"2024-01-15": 1.50, "2024-01-14": 1.45},
                            "foil": {"2024-01-15": 3.00},
                        }
                    },
                    "tcgplayer": {
                        "retail": {
                            "normal": {"2024-01-15": 1.25},
                        }
                    },
                }
            },
            "uuid-002": {
                "paper": {
                    "cardkingdom": {
                        "retail": {
                            "normal": {"2024-01-15": 0.50},
                        }
                    },
                }
            },
            "uuid-003": {
                "paper": {
                    "tcgplayer": {
                        "retail": {
                            "normal": {"2024-01-15": 2.00},
                            "foil": {"2024-01-15": 5.00},
                        }
                    },
                }
            },
            "uuid-010": {
                "paper": {
                    "cardkingdom": {
                        "retail": {
                            "normal": {"2024-01-15": 10.00},
                        }
                    },
                }
            },
            # uuid-999 won't be in the uuid map
            "uuid-999": {
                "paper": {
                    "cardkingdom": {
                        "retail": {
                            "normal": {"2024-01-15": 99.99},
                        }
                    },
                }
            },
        }
    }
    path = tmp_path / "AllPricesToday.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def mock_allpricestoday_backfill(tmp_path):
    """Create a mock AllPricesToday.json with 3 dates per card."""
    data = {
        "data": {
            "uuid-001": {
                "paper": {
                    "cardkingdom": {
                        "retail": {
                            "normal": {
                                "2024-01-13": 1.40,
                                "2024-01-14": 1.45,
                                "2024-01-15": 1.50,
                            },
                        }
                    },
                }
            },
        }
    }
    path = tmp_path / "AllPricesToday.json"
    path.write_text(json.dumps(data))
    return path


# =============================================================================
# Tests
# =============================================================================


class TestSchemaV15:
    def test_tables_created(self, test_db):
        """Verify all 3 tables + view exist after init_db."""
        _, conn = test_db
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "mtgjson_uuid_map" in table_names
        assert "prices" in table_names
        assert "price_fetch_log" in table_names

        views = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        ).fetchall()
        view_names = [r[0] for r in views]
        assert "latest_prices" in view_names

    def test_schema_version_is_current(self, test_db):
        _, conn = test_db
        assert get_current_version(conn) == SCHEMA_VERSION


class TestUuidMap:
    def test_population(self, test_db, mock_allprintings):
        """Verify _ensure_uuid_map populates correctly with lowercase set codes."""
        from mtg_collector.cli.data_cmd import _ensure_uuid_map

        db_path, conn = test_db
        with patch("mtg_collector.cli.data_cmd.get_allprintings_path", return_value=mock_allprintings):
            _ensure_uuid_map(conn)

        rows = conn.execute("SELECT * FROM mtgjson_uuid_map ORDER BY uuid").fetchall()
        assert len(rows) == 5

        # Check lowercase set codes
        row = conn.execute("SELECT set_code FROM mtgjson_uuid_map WHERE uuid = 'uuid-001'").fetchone()
        assert row[0] == "neo"  # lowercase

        row = conn.execute("SELECT set_code FROM mtgjson_uuid_map WHERE uuid = 'uuid-010'").fetchone()
        assert row[0] == "mom"  # lowercase

    def test_idempotent(self, test_db, mock_allprintings):
        """Call _ensure_uuid_map twice, no duplicates."""
        from mtg_collector.cli.data_cmd import _ensure_uuid_map

        _, conn = test_db
        with patch("mtg_collector.cli.data_cmd.get_allprintings_path", return_value=mock_allprintings):
            _ensure_uuid_map(conn)
            _ensure_uuid_map(conn)

        count = conn.execute("SELECT COUNT(*) FROM mtgjson_uuid_map").fetchone()[0]
        assert count == 5


class TestImportPrices:
    def _setup_uuid_map(self, conn):
        """Pre-populate uuid_map for testing."""
        rows = [
            ("uuid-001", "neo", "1"),
            ("uuid-002", "neo", "2"),
            ("uuid-003", "neo", "3"),
            ("uuid-010", "mom", "10"),
            ("uuid-011", "mom", "11"),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO mtgjson_uuid_map (uuid, set_code, collector_number) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()

    def test_basic(self, test_db, mock_allprintings, mock_allpricestoday):
        """Import prices, verify correct rows in prices table."""
        from mtg_collector.cli.data_cmd import import_prices

        db_path, conn = test_db
        self._setup_uuid_map(conn)

        with patch("mtg_collector.cli.data_cmd.get_allpricestoday_path", return_value=mock_allpricestoday):
            with patch("mtg_collector.cli.data_cmd.get_allprintings_path", return_value=mock_allprintings):
                import_prices(db_path)

        # Reconnect since import_prices closes conn
        conn2 = sqlite3.connect(db_path)
        rows = conn2.execute("SELECT * FROM prices").fetchall()
        # uuid-001: ck normal x2 dates + ck foil x1 + tcg normal x1 = 4
        # uuid-002: ck normal x1 = 1
        # uuid-003: tcg normal x1 + tcg foil x1 = 2
        # uuid-010: ck normal x1 = 1
        # uuid-999: unmapped, skipped = 0
        assert len(rows) == 8
        conn2.close()

    def test_idempotent(self, test_db, mock_allprintings, mock_allpricestoday):
        """Import twice, same row count due to UNIQUE constraint + INSERT OR IGNORE."""
        from mtg_collector.cli.data_cmd import import_prices

        db_path, conn = test_db
        self._setup_uuid_map(conn)

        with patch("mtg_collector.cli.data_cmd.get_allpricestoday_path", return_value=mock_allpricestoday):
            with patch("mtg_collector.cli.data_cmd.get_allprintings_path", return_value=mock_allprintings):
                import_prices(db_path)
                import_prices(db_path)

        conn2 = sqlite3.connect(db_path)
        count = conn2.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        assert count == 8
        conn2.close()

    def test_backfill(self, test_db, mock_allprintings, mock_allpricestoday_backfill):
        """Fixture with 3 dates per card, all 3 inserted."""
        from mtg_collector.cli.data_cmd import import_prices

        db_path, conn = test_db
        self._setup_uuid_map(conn)

        with patch("mtg_collector.cli.data_cmd.get_allpricestoday_path", return_value=mock_allpricestoday_backfill):
            with patch("mtg_collector.cli.data_cmd.get_allprintings_path", return_value=mock_allprintings):
                import_prices(db_path)

        conn2 = sqlite3.connect(db_path)
        rows = conn2.execute(
            "SELECT observed_at FROM prices WHERE set_code = 'neo' AND collector_number = '1' ORDER BY observed_at"
        ).fetchall()
        assert len(rows) == 3
        assert rows[0][0] == "2024-01-13"
        assert rows[1][0] == "2024-01-14"
        assert rows[2][0] == "2024-01-15"
        conn2.close()

    def test_unmapped_uuids_skipped(self, test_db, mock_allprintings, mock_allpricestoday):
        """UUID not in map is skipped and counted in log."""
        from mtg_collector.cli.data_cmd import import_prices

        db_path, conn = test_db
        self._setup_uuid_map(conn)

        with patch("mtg_collector.cli.data_cmd.get_allpricestoday_path", return_value=mock_allpricestoday):
            with patch("mtg_collector.cli.data_cmd.get_allprintings_path", return_value=mock_allprintings):
                import_prices(db_path)

        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        log = conn2.execute("SELECT * FROM price_fetch_log ORDER BY id DESC LIMIT 1").fetchone()
        assert log["uuid_unmapped"] == 1  # uuid-999
        assert log["uuid_mapped"] == 4    # uuid-001, 002, 003, 010
        assert log["uuid_total"] == 5

        # No prices for uuid-999's card
        no_rows = conn2.execute(
            "SELECT * FROM prices WHERE set_code = 'UNMAPPED'"
        ).fetchall()
        assert len(no_rows) == 0
        conn2.close()


class TestLatestPricesView:
    def test_returns_only_latest(self, test_db):
        """Insert 2 dates, view returns only latest."""
        _, conn = test_db
        conn.executemany(
            "INSERT INTO prices (set_code, collector_number, source, price_type, price, observed_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("neo", "1", "cardkingdom", "normal", 1.00, "2024-01-14"),
                ("neo", "1", "cardkingdom", "normal", 1.50, "2024-01-15"),
                ("neo", "2", "tcgplayer", "normal", 2.00, "2024-01-15"),
            ],
        )
        conn.commit()

        rows = conn.execute("SELECT * FROM latest_prices ORDER BY set_code, collector_number").fetchall()
        assert len(rows) == 2  # Only the 2024-01-15 rows

        prices = {(r[0], r[1], r[2]): r[4] for r in rows}
        assert prices[("neo", "1", "cardkingdom")] == 1.50
        assert prices[("neo", "2", "tcgplayer")] == 2.00


class TestPriceFetchLog:
    def test_log_entry(self, test_db, mock_allprintings, mock_allpricestoday):
        """Verify log entry after import with correct counts."""
        from mtg_collector.cli.data_cmd import import_prices

        db_path, conn = test_db
        # Pre-populate uuid_map
        rows = [
            ("uuid-001", "neo", "1"),
            ("uuid-002", "neo", "2"),
            ("uuid-003", "neo", "3"),
            ("uuid-010", "mom", "10"),
            ("uuid-011", "mom", "11"),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO mtgjson_uuid_map (uuid, set_code, collector_number) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()

        with patch("mtg_collector.cli.data_cmd.get_allpricestoday_path", return_value=mock_allpricestoday):
            with patch("mtg_collector.cli.data_cmd.get_allprintings_path", return_value=mock_allprintings):
                import_prices(db_path)

        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        log = conn2.execute("SELECT * FROM price_fetch_log ORDER BY id DESC LIMIT 1").fetchone()
        assert log is not None
        assert log["uuid_total"] == 5
        assert log["uuid_mapped"] == 4
        assert log["uuid_unmapped"] == 1
        assert log["rows_inserted"] == 8
        dates = json.loads(log["dates_imported"])
        assert "2024-01-15" in dates
        conn2.close()


class TestMigrationV14ToV15:
    def test_migration(self):
        """Create a v14 DB, run init_db, verify v15 price tables exist."""
        close_connection()
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
            db_path = f.name

        # Create a v14 database (has agent_trace + api_usage columns but no price tables)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cards (
                oracle_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type_line TEXT,
                mana_cost TEXT,
                cmc REAL,
                oracle_text TEXT,
                colors TEXT,
                color_identity TEXT
            );
            CREATE TABLE IF NOT EXISTS sets (
                set_code TEXT PRIMARY KEY,
                set_name TEXT NOT NULL,
                set_type TEXT,
                released_at TEXT,
                cards_fetched_at TEXT
            );
            CREATE TABLE IF NOT EXISTS printings (
                scryfall_id TEXT PRIMARY KEY,
                oracle_id TEXT NOT NULL REFERENCES cards(oracle_id),
                set_code TEXT NOT NULL REFERENCES sets(set_code),
                collector_number TEXT NOT NULL,
                rarity TEXT,
                frame_effects TEXT,
                border_color TEXT,
                full_art INTEGER,
                promo INTEGER,
                promo_types TEXT,
                finishes TEXT,
                artist TEXT,
                image_uri TEXT,
                raw_json TEXT,
                UNIQUE(set_code, collector_number)
            );
            CREATE TABLE IF NOT EXISTS collection (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT NOT NULL REFERENCES printings(scryfall_id),
                finish TEXT NOT NULL,
                condition TEXT NOT NULL DEFAULT 'Near Mint',
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
                status TEXT NOT NULL DEFAULT 'owned',
                sale_price REAL,
                order_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS ingest_cache (
                image_md5 TEXT PRIMARY KEY,
                image_path TEXT NOT NULL,
                ocr_result TEXT NOT NULL,
                claude_result TEXT,
                agent_trace TEXT,
                api_usage TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingest_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                md5 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'READY_FOR_OCR',
                mode TEXT,
                ocr_result TEXT,
                claude_result TEXT,
                agent_trace TEXT,
                api_usage TEXT,
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
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (14, '2024-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        # Now init_db should migrate to v15
        conn2 = get_connection(db_path)
        init_db(conn2)

        assert get_current_version(conn2) == SCHEMA_VERSION

        tables = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "mtgjson_uuid_map" in table_names
        assert "prices" in table_names
        assert "price_fetch_log" in table_names

        views = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        ).fetchall()
        view_names = [r[0] for r in views]
        assert "latest_prices" in view_names

        close_connection()
        Path(db_path).unlink(missing_ok=True)
