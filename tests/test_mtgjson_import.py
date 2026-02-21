"""
Tests for MTGJSON import pipeline and SQL-backed PackGenerator.

To run: uv run pytest tests/test_mtgjson_import.py -v
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mtg_collector.db.connection import close_connection, get_connection
from mtg_collector.db.schema import SCHEMA_VERSION, get_current_version, init_db
from mtg_collector.services.pack_generator import PackGenerator


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def test_db():
    """Create a fresh temporary database with current schema."""
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
    """Create a minimal AllPrintings.json with 1 set, 5 cards, 1 booster product."""
    data = {
        "data": {
            "TST": {
                "name": "Test Set",
                "cards": [
                    {
                        "uuid": "uuid-c01",
                        "name": "Plains",
                        "number": "1",
                        "setCode": "tst",
                        "rarity": "common",
                        "borderColor": "black",
                        "isFullArt": False,
                        "frameEffects": None,
                        "identifiers": {"scryfallId": "scry-c01"},
                        "purchaseUrls": {
                            "cardKingdom": "https://ck.com/plains",
                            "cardKingdomFoil": "https://ck.com/plains-foil",
                        },
                    },
                    {
                        "uuid": "uuid-c02",
                        "name": "Island",
                        "number": "2",
                        "setCode": "tst",
                        "rarity": "common",
                        "borderColor": "black",
                        "isFullArt": False,
                        "identifiers": {"scryfallId": "scry-c02"},
                        "purchaseUrls": {"cardKingdom": "https://ck.com/island"},
                    },
                    {
                        "uuid": "uuid-c03",
                        "name": "Swamp",
                        "number": "3",
                        "setCode": "tst",
                        "rarity": "common",
                        "borderColor": "black",
                        "isFullArt": False,
                        "identifiers": {"scryfallId": "scry-c03"},
                        "purchaseUrls": {},
                    },
                    {
                        "uuid": "uuid-u01",
                        "name": "Lightning Bolt",
                        "number": "4",
                        "setCode": "tst",
                        "rarity": "uncommon",
                        "borderColor": "black",
                        "isFullArt": False,
                        "frameEffects": ["showcase"],
                        "identifiers": {"scryfallId": "scry-u01"},
                        "purchaseUrls": {"cardKingdom": "https://ck.com/bolt"},
                    },
                    {
                        "uuid": "uuid-r01",
                        "name": "Black Lotus",
                        "number": "5",
                        "setCode": "tst",
                        "rarity": "rare",
                        "borderColor": "borderless",
                        "isFullArt": True,
                        "frameEffects": ["extendedart"],
                        "identifiers": {"scryfallId": "scry-r01"},
                        "purchaseUrls": {
                            "cardKingdom": "https://ck.com/lotus",
                            "cardKingdomFoil": "https://ck.com/lotus-foil",
                        },
                    },
                ],
                "booster": {
                    "play": {
                        "sheets": {
                            "common": {
                                "foil": False,
                                "cards": {
                                    "uuid-c01": 10,
                                    "uuid-c02": 10,
                                    "uuid-c03": 10,
                                },
                            },
                            "uncommonRare": {
                                "foil": False,
                                "cards": {
                                    "uuid-u01": 8,
                                    "uuid-r01": 2,
                                },
                            },
                            "foilAny": {
                                "foil": True,
                                "cards": {
                                    "uuid-c01": 5,
                                    "uuid-c02": 5,
                                    "uuid-u01": 3,
                                    "uuid-r01": 1,
                                },
                            },
                        },
                        "boosters": [
                            {
                                "weight": 7,
                                "contents": {
                                    "common": 3,
                                    "uncommonRare": 1,
                                },
                            },
                            {
                                "weight": 3,
                                "contents": {
                                    "common": 2,
                                    "uncommonRare": 1,
                                    "foilAny": 1,
                                },
                            },
                        ],
                    },
                },
            },
        }
    }
    path = tmp_path / "AllPrintings.json"
    path.write_text(json.dumps(data))
    return path


def _run_import(db_path, mock_path):
    """Helper to run import_mtgjson with mocked AllPrintings path."""
    from mtg_collector.cli.data_cmd import import_mtgjson
    with patch("mtg_collector.cli.data_cmd.get_allprintings_path", return_value=mock_path):
        import_mtgjson(db_path)


# =============================================================================
# Tests
# =============================================================================


class TestSchemaV16:
    def test_tables_created(self, test_db):
        """Verify all 3 new tables exist after init_db."""
        _, conn = test_db
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "mtgjson_printings" in table_names
        assert "mtgjson_booster_sheets" in table_names
        assert "mtgjson_booster_configs" in table_names

    def test_schema_version_is_current(self, test_db):
        _, conn = test_db
        assert get_current_version(conn) == SCHEMA_VERSION


class TestImportMtgjson:
    def test_populates_all_tables(self, test_db, mock_allprintings):
        """Import populates printings, sheets, configs, uuid_map with correct counts."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        conn = sqlite3.connect(db_path)

        # 5 cards
        assert conn.execute("SELECT COUNT(*) FROM mtgjson_printings").fetchone()[0] == 5

        # UUID map also has 5
        assert conn.execute("SELECT COUNT(*) FROM mtgjson_uuid_map").fetchone()[0] == 5

        # Booster sheets: common has 3 cards, uncommonRare has 2, foilAny has 4 = 9
        assert conn.execute("SELECT COUNT(*) FROM mtgjson_booster_sheets").fetchone()[0] == 9

        # Booster configs: variant 0 has 2 sheets, variant 1 has 3 sheets = 5
        assert conn.execute("SELECT COUNT(*) FROM mtgjson_booster_configs").fetchone()[0] == 5

        # sets table has TST
        row = conn.execute("SELECT set_name FROM sets WHERE set_code = 'tst'").fetchone()
        assert row[0] == "Test Set"

        conn.close()

    def test_printings_data(self, test_db, mock_allprintings):
        """Check that printing data is correctly stored."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM mtgjson_printings WHERE uuid = 'uuid-r01'"
        ).fetchone()

        assert row["name"] == "Black Lotus"
        assert row["set_code"] == "tst"
        assert row["number"] == "5"
        assert row["rarity"] == "rare"
        assert row["border_color"] == "borderless"
        assert row["is_full_art"] == 1
        assert json.loads(row["frame_effects"]) == ["extendedart"]
        assert row["scryfall_id"] == "scry-r01"
        assert row["ck_url"] == "https://ck.com/lotus"
        assert row["ck_url_foil"] == "https://ck.com/lotus-foil"
        conn.close()

    def test_idempotent(self, test_db, mock_allprintings):
        """Re-import produces same counts (delete+recreate)."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)
        _run_import(db_path, mock_allprintings)

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM mtgjson_printings").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM mtgjson_booster_sheets").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM mtgjson_booster_configs").fetchone()[0] == 5
        conn.close()


class TestPackGeneratorSQL:
    def test_list_sets(self, test_db, mock_allprintings):
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        gen = PackGenerator(db_path)
        sets = gen.list_sets()
        assert len(sets) == 1
        assert sets[0] == ("tst", "Test Set")

    def test_list_products(self, test_db, mock_allprintings):
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        gen = PackGenerator(db_path)
        products = gen.list_products("tst")
        assert "play" in products

    def test_generate_pack(self, test_db, mock_allprintings):
        """Generate a pack and verify all expected card fields are present."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        gen = PackGenerator(db_path)
        result = gen.generate_pack("tst", "play", seed=42)

        assert result["set_code"] == "tst"
        assert result["seed"] == 42
        assert "variant_index" in result
        assert "variant_weight" in result
        assert "total_weight" in result
        assert result["total_weight"] == 10  # 7 + 3

        cards = result["cards"]
        assert len(cards) > 0

        # Check card dict fields
        for card in cards:
            assert "uuid" in card
            assert "name" in card
            assert "set_code" in card
            assert "collector_number" in card
            assert "rarity" in card
            assert "scryfall_id" in card
            assert "image_uri" in card
            assert "sheet_name" in card
            assert "foil" in card
            assert "border_color" in card
            assert "frame_effects" in card
            assert "is_full_art" in card
            assert "ck_url" in card

    def test_generate_pack_deterministic(self, test_db, mock_allprintings):
        """Same seed produces same pack."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        gen = PackGenerator(db_path)
        r1 = gen.generate_pack("tst", "play", seed=123)
        r2 = gen.generate_pack("tst", "play", seed=123)

        assert r1["variant_index"] == r2["variant_index"]
        assert [c["uuid"] for c in r1["cards"]] == [c["uuid"] for c in r2["cards"]]

    def test_generate_pack_case_insensitive(self, test_db, mock_allprintings):
        """Set code is case-insensitive."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        gen = PackGenerator(db_path)
        r1 = gen.generate_pack("TST", "play", seed=42)
        r2 = gen.generate_pack("tst", "play", seed=42)
        assert [c["uuid"] for c in r1["cards"]] == [c["uuid"] for c in r2["cards"]]

    def test_get_ck_url(self, test_db, mock_allprintings):
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        gen = PackGenerator(db_path)
        assert gen.get_ck_url("scry-c01") == "https://ck.com/plains"
        assert gen.get_ck_url("scry-c01", foil=True) == "https://ck.com/plains-foil"
        assert gen.get_ck_url("scry-r01") == "https://ck.com/lotus"
        assert gen.get_ck_url("scry-r01", foil=True) == "https://ck.com/lotus-foil"
        assert gen.get_ck_url("nonexistent") == ""

    def test_get_uuid_for_scryfall_id(self, test_db, mock_allprintings):
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        gen = PackGenerator(db_path)
        assert gen.get_uuid_for_scryfall_id("scry-c01") == "uuid-c01"
        assert gen.get_uuid_for_scryfall_id("scry-r01") == "uuid-r01"
        assert gen.get_uuid_for_scryfall_id("nonexistent") is None

    def test_get_sheet_data(self, test_db, mock_allprintings):
        """get_sheet_data returns correct structure."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        gen = PackGenerator(db_path)
        data = gen.get_sheet_data("tst", "play")

        assert data["set_code"] == "tst"
        assert data["product"] == "play"
        assert data["total_weight"] == 10
        assert len(data["variants"]) == 2
        assert data["variants"][0]["weight"] == 7
        assert data["variants"][1]["weight"] == 3

        assert "common" in data["sheets"]
        assert "uncommonRare" in data["sheets"]
        assert "foilAny" in data["sheets"]

        common_sheet = data["sheets"]["common"]
        assert common_sheet["foil"] is False
        assert common_sheet["card_count"] == 3
        assert common_sheet["total_weight"] == 30  # 10+10+10

        foil_sheet = data["sheets"]["foilAny"]
        assert foil_sheet["foil"] is True

    def test_invalid_set_raises(self, test_db, mock_allprintings):
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        gen = PackGenerator(db_path)
        with pytest.raises(ValueError):
            gen.generate_pack("NONEXISTENT", "play")

    def test_invalid_product_raises(self, test_db, mock_allprintings):
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings)

        gen = PackGenerator(db_path)
        with pytest.raises(ValueError):
            gen.generate_pack("tst", "nonexistent")


class TestMigrationV15ToV16:
    def test_migration(self):
        """Create a v15 DB, run init_db, verify v16 tables exist."""
        close_connection()
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")

        # Create minimal v15 schema (includes tables needed by v16→v17 migration)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cards (
                oracle_id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sets (
                set_code TEXT PRIMARY KEY,
                set_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS printings (
                scryfall_id TEXT PRIMARY KEY,
                oracle_id TEXT NOT NULL REFERENCES cards(oracle_id),
                set_code TEXT NOT NULL REFERENCES sets(set_code),
                collector_number TEXT,
                rarity TEXT,
                promo INTEGER DEFAULT 0,
                artist TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number TEXT,
                seller_name TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collection (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT NOT NULL REFERENCES printings(scryfall_id),
                finish TEXT NOT NULL CHECK(finish IN ('nonfoil', 'foil', 'etched')),
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
                status TEXT NOT NULL DEFAULT 'owned'
                    CHECK(status IN ('owned', 'ordered', 'listed', 'sold', 'removed')),
                sale_price REAL,
                order_id INTEGER REFERENCES orders(id)
            );
            CREATE VIEW IF NOT EXISTS collection_view AS
            SELECT c.id, card.name, s.set_name, p.set_code,
                   p.collector_number, p.rarity, p.promo,
                   c.finish, c.condition, c.language,
                   c.purchase_price, c.acquired_at, c.source,
                   c.source_image, c.notes, c.tags, c.tradelist,
                   c.status, c.sale_price, c.scryfall_id, p.oracle_id,
                   c.order_id
            FROM collection c
            JOIN printings p ON c.scryfall_id = p.scryfall_id
            JOIN cards card ON p.oracle_id = card.oracle_id
            JOIN sets s ON p.set_code = s.set_code;
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mtgjson_uuid_map (
                uuid TEXT PRIMARY KEY,
                set_code TEXT NOT NULL,
                collector_number TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                set_code TEXT NOT NULL,
                collector_number TEXT NOT NULL,
                source TEXT NOT NULL,
                price_type TEXT NOT NULL,
                price REAL NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS price_fetch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (15, '2024-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        # Now init_db should migrate through v16 and v17
        conn2 = get_connection(db_path)
        init_db(conn2)

        assert get_current_version(conn2) == SCHEMA_VERSION

        tables = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "mtgjson_printings" in table_names
        assert "mtgjson_booster_sheets" in table_names
        assert "mtgjson_booster_configs" in table_names

        close_connection()
        Path(db_path).unlink(missing_ok=True)
