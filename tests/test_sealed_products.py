"""
Tests for sealed product schema, import, and repositories.

To run: uv run pytest tests/test_sealed_products.py -v
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mtg_collector.db.connection import close_connection, get_connection
from mtg_collector.db.models import (
    SealedCollectionEntry,
    SealedCollectionRepository,
    SealedProductRepository,
)
from mtg_collector.db.schema import SCHEMA_VERSION, get_current_version, init_db

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
def mock_allprintings_with_sealed(tmp_path):
    """Create a minimal AllPrintings.json with cards AND sealed products."""
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
                        "identifiers": {"scryfallId": "scry-c01"},
                        "purchaseUrls": {},
                    },
                ],
                "sealedProduct": [
                    {
                        "uuid": "sealed-uuid-001",
                        "name": "Test Set Play Booster Box",
                        "category": "booster_box",
                        "subtype": "play",
                        "cardCount": 540,
                        "productSize": 36,
                        "releaseDate": "2025-01-01",
                        "identifiers": {
                            "tcgplayerProductId": "12345",
                        },
                        "purchaseUrls": {
                            "tcgplayer": "https://tcg.com/tst-play-box",
                            "cardKingdom": "https://ck.com/tst-play-box",
                        },
                        "contents": {
                            "pack": [
                                {"code": "tst", "count": 36, "name": "Play Booster"}
                            ]
                        },
                    },
                    {
                        "uuid": "sealed-uuid-002",
                        "name": "Test Set Collector Booster Box",
                        "category": "booster_box",
                        "subtype": "collector",
                        "identifiers": {
                            "tcgplayerProductId": "12346",
                        },
                        "purchaseUrls": {
                            "tcgplayer": "https://tcg.com/tst-collector-box",
                        },
                    },
                    {
                        "uuid": "sealed-uuid-003",
                        "name": "Test Set Bundle",
                        "category": "bundle",
                        "identifiers": {},
                        "purchaseUrls": {},
                    },
                    {
                        # Sealed product with no uuid should be skipped
                        "name": "Bad Product No UUID",
                        "category": "unknown",
                        "identifiers": {},
                        "purchaseUrls": {},
                    },
                ],
                "booster": {},
            },
            "TS2": {
                "name": "Test Set Two",
                "cards": [],
                "sealedProduct": [
                    {
                        "uuid": "sealed-uuid-004",
                        "name": "TS2 Commander Deck",
                        "category": "deck",
                        "subtype": "commander",
                        "identifiers": {
                            "tcgplayerProductId": "99999",
                        },
                        "purchaseUrls": {},
                    },
                ],
            },
        }
    }
    path = tmp_path / "AllPrintings.json"
    path.write_text(json.dumps(data))
    return path


def _run_import(db_path, mock_path):
    """Helper to run import_mtgjson with mocked AllPrintings path."""
    from mtg_collector.cli.data_cmd import import_mtgjson

    with patch(
        "mtg_collector.cli.data_cmd.get_allprintings_path", return_value=mock_path
    ):
        import_mtgjson(db_path)


# =============================================================================
# Schema Tests
# =============================================================================


class TestSealedSchema:
    def test_sealed_tables_created(self, test_db):
        """Verify sealed product tables exist after init_db."""
        _, conn = test_db
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "sealed_products" in table_names
        assert "sealed_collection" in table_names
        assert "sealed_prices" in table_names
        assert "tcgplayer_groups" in table_names

    def test_sealed_collection_view_created(self, test_db):
        """Verify sealed_collection_view exists."""
        _, conn = test_db
        views = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        ).fetchall()
        view_names = [r[0] for r in views]
        assert "sealed_collection_view" in view_names

    def test_sealed_collection_status_constraint(self, test_db):
        """Status CHECK constraint rejects invalid values."""
        _, conn = test_db
        # Insert a sealed product first
        conn.execute(
            "INSERT INTO sets (set_code, set_name) VALUES ('tst', 'Test Set')"
        )
        conn.execute(
            "INSERT INTO sealed_products (uuid, name, set_code, category, imported_at) "
            "VALUES ('sp-1', 'Test Box', 'tst', 'booster_box', '2025-01-01')"
        )
        # Valid status should work
        conn.execute(
            "INSERT INTO sealed_collection (sealed_product_uuid, status, added_at) "
            "VALUES ('sp-1', 'opened', '2025-01-01')"
        )
        # Invalid status should fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sealed_collection (sealed_product_uuid, status, added_at) "
                "VALUES ('sp-1', 'destroyed', '2025-01-01')"
            )


class TestMigrationV17ToV18:
    def test_migration(self):
        """Create a v17 DB, run init_db, verify v18 tables exist."""
        close_connection()
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")

        # Create minimal v15 schema (needed for migration chain through v16, v17)
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
                    CHECK(status IN ('owned', 'ordered', 'listed', 'sold', 'removed', 'traded', 'gifted', 'lost')),
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
            CREATE TABLE IF NOT EXISTS mtgjson_printings (
                uuid TEXT PRIMARY KEY,
                scryfall_id TEXT,
                name TEXT NOT NULL,
                set_code TEXT NOT NULL,
                number TEXT NOT NULL,
                rarity TEXT,
                border_color TEXT,
                is_full_art INTEGER DEFAULT 0,
                frame_effects TEXT,
                ck_url TEXT,
                ck_url_foil TEXT,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mtgjson_booster_sheets (
                id INTEGER PRIMARY KEY,
                set_code TEXT NOT NULL,
                product TEXT NOT NULL,
                sheet_name TEXT NOT NULL,
                is_foil INTEGER DEFAULT 0,
                uuid TEXT NOT NULL,
                weight INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mtgjson_booster_configs (
                id INTEGER PRIMARY KEY,
                set_code TEXT NOT NULL,
                product TEXT NOT NULL,
                variant_index INTEGER NOT NULL,
                variant_weight INTEGER NOT NULL,
                sheet_name TEXT NOT NULL,
                card_count INTEGER NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (17, '2025-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        # Now init_db should migrate v17 -> v18
        conn2 = get_connection(db_path)
        init_db(conn2)

        assert get_current_version(conn2) == SCHEMA_VERSION

        tables = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "sealed_products" in table_names
        assert "sealed_collection" in table_names
        assert "sealed_prices" in table_names
        assert "tcgplayer_groups" in table_names

        views = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        ).fetchall()
        view_names = [r[0] for r in views]
        assert "sealed_collection_view" in view_names

        close_connection()
        Path(db_path).unlink(missing_ok=True)


class TestMigrationV18ToV19:
    def test_migration(self):
        """Create a v18 DB (with sealed tables but no price view), run init_db, verify v19."""
        close_connection()
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")

        # Build a minimal v18 schema: core tables + sealed tables (no latest_sealed_prices view)
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
                status TEXT NOT NULL DEFAULT 'owned',
                sale_price REAL,
                order_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS sealed_products (
                uuid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                set_code TEXT NOT NULL,
                category TEXT,
                subtype TEXT,
                tcgplayer_product_id TEXT,
                card_count INTEGER,
                product_size INTEGER,
                release_date TEXT,
                purchase_url_tcgplayer TEXT,
                purchase_url_cardkingdom TEXT,
                contents_json TEXT,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sealed_collection (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sealed_product_uuid TEXT NOT NULL REFERENCES sealed_products(uuid),
                quantity INTEGER NOT NULL DEFAULT 1,
                condition TEXT NOT NULL DEFAULT 'Sealed',
                purchase_price REAL,
                purchase_date TEXT,
                source TEXT,
                seller_name TEXT,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'owned',
                sale_price REAL,
                added_at TEXT NOT NULL
            );
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
            CREATE TABLE IF NOT EXISTS tcgplayer_groups (
                group_id INTEGER PRIMARY KEY,
                set_code TEXT,
                name TEXT NOT NULL,
                abbreviation TEXT,
                published_on TEXT,
                fetched_at TEXT NOT NULL
            );
            CREATE VIEW IF NOT EXISTS sealed_collection_view AS
            SELECT sc.id, sp.name FROM sealed_collection sc
            JOIN sealed_products sp ON sc.sealed_product_uuid = sp.uuid;
        """)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (18, '2025-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        # Now init_db should migrate v18 -> v19
        conn2 = get_connection(db_path)
        init_db(conn2)

        assert get_current_version(conn2) == SCHEMA_VERSION

        views = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        ).fetchall()
        view_names = [r[0] for r in views]
        assert "latest_sealed_prices" in view_names

        # Verify the view is functional (should return empty result, not error)
        rows = conn2.execute("SELECT * FROM latest_sealed_prices").fetchall()
        assert rows == []

        close_connection()
        Path(db_path).unlink(missing_ok=True)


# =============================================================================
# Import Tests
# =============================================================================


class TestSealedImport:
    def test_sealed_products_imported(self, test_db, mock_allprintings_with_sealed):
        """Import populates sealed_products with correct count."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings_with_sealed)

        conn = sqlite3.connect(db_path)
        # 3 products from TST + 1 from TS2 = 4 (the one without uuid is skipped)
        count = conn.execute("SELECT COUNT(*) FROM sealed_products").fetchone()[0]
        assert count == 4
        conn.close()

    def test_sealed_product_data(self, test_db, mock_allprintings_with_sealed):
        """Check sealed product fields are correctly stored."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings_with_sealed)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sealed_products WHERE uuid = 'sealed-uuid-001'"
        ).fetchone()

        assert row["name"] == "Test Set Play Booster Box"
        assert row["set_code"] == "tst"
        assert row["category"] == "booster_box"
        assert row["subtype"] == "play"
        assert row["tcgplayer_product_id"] == "12345"
        assert row["card_count"] == 540
        assert row["product_size"] == 36
        assert row["release_date"] == "2025-01-01"
        assert row["purchase_url_tcgplayer"] == "https://tcg.com/tst-play-box"
        assert row["purchase_url_cardkingdom"] == "https://ck.com/tst-play-box"
        assert row["contents_json"] is not None
        contents = json.loads(row["contents_json"])
        assert "pack" in contents
        conn.close()

    def test_sealed_product_minimal_fields(self, test_db, mock_allprintings_with_sealed):
        """Sealed product with minimal fields (no tcgplayer ID, no URLs) imports OK."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings_with_sealed)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sealed_products WHERE uuid = 'sealed-uuid-003'"
        ).fetchone()

        assert row["name"] == "Test Set Bundle"
        assert row["category"] == "bundle"
        assert row["subtype"] is None
        assert row["tcgplayer_product_id"] is None
        assert row["purchase_url_tcgplayer"] is None
        assert row["contents_json"] is None
        conn.close()

    def test_sealed_import_idempotent(self, test_db, mock_allprintings_with_sealed):
        """Re-import produces same counts (INSERT OR IGNORE)."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings_with_sealed)
        _run_import(db_path, mock_allprintings_with_sealed)

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM sealed_products").fetchone()[0]
        assert count == 4
        conn.close()

    def test_sealed_products_across_sets(self, test_db, mock_allprintings_with_sealed):
        """Sealed products from multiple sets are all imported."""
        db_path, _ = test_db
        _run_import(db_path, mock_allprintings_with_sealed)

        conn = sqlite3.connect(db_path)
        tst_count = conn.execute(
            "SELECT COUNT(*) FROM sealed_products WHERE set_code = 'tst'"
        ).fetchone()[0]
        ts2_count = conn.execute(
            "SELECT COUNT(*) FROM sealed_products WHERE set_code = 'ts2'"
        ).fetchone()[0]
        assert tst_count == 3
        assert ts2_count == 1
        conn.close()


# =============================================================================
# SealedProductRepository Tests
# =============================================================================


class TestSealedProductRepository:
    @pytest.fixture
    def repo(self, test_db, mock_allprintings_with_sealed):
        """Set up a repo with imported sealed products."""
        db_path, conn = test_db
        _run_import(db_path, mock_allprintings_with_sealed)
        # Re-open with row_factory for repositories
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        return SealedProductRepository(conn2)

    def test_get(self, repo):
        product = repo.get("sealed-uuid-001")
        assert product is not None
        assert product.name == "Test Set Play Booster Box"
        assert product.category == "booster_box"
        assert product.subtype == "play"

    def test_get_not_found(self, repo):
        assert repo.get("nonexistent") is None

    def test_get_by_tcgplayer_id(self, repo):
        product = repo.get_by_tcgplayer_id("12345")
        assert product is not None
        assert product.uuid == "sealed-uuid-001"

    def test_get_by_tcgplayer_id_not_found(self, repo):
        assert repo.get_by_tcgplayer_id("00000") is None

    def test_search_by_name(self, repo):
        results = repo.search_by_name("Play Booster")
        assert len(results) == 1
        assert results[0].uuid == "sealed-uuid-001"

    def test_search_by_name_case_insensitive(self, repo):
        results = repo.search_by_name("play booster")
        assert len(results) == 1

    def test_search_by_name_partial(self, repo):
        results = repo.search_by_name("Bundle")
        assert len(results) == 1
        assert results[0].uuid == "sealed-uuid-003"

    def test_search_by_name_multiple_results(self, repo):
        results = repo.search_by_name("Booster Box")
        assert len(results) == 2

    def test_list_by_set(self, repo):
        products = repo.list_by_set("tst")
        assert len(products) == 3

    def test_list_by_set_empty(self, repo):
        products = repo.list_by_set("nonexistent")
        assert len(products) == 0

    def test_list_sets_with_products(self, repo):
        sets = repo.list_sets_with_products()
        codes = [s["set_code"] for s in sets]
        assert "tst" in codes
        assert "ts2" in codes

    def test_count(self, repo):
        assert repo.count() == 4


# =============================================================================
# SealedCollectionRepository Tests
# =============================================================================


class TestSealedCollectionRepository:
    @pytest.fixture
    def repos(self, test_db, mock_allprintings_with_sealed):
        """Set up repos with imported sealed products."""
        db_path, conn = test_db
        _run_import(db_path, mock_allprintings_with_sealed)
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        product_repo = SealedProductRepository(conn2)
        collection_repo = SealedCollectionRepository(conn2)
        return product_repo, collection_repo, conn2

    def test_add_and_get(self, repos):
        _, repo, conn = repos
        entry = SealedCollectionEntry(
            id=None,
            sealed_product_uuid="sealed-uuid-001",
            quantity=2,
            purchase_price=149.99,
            purchase_date="2025-01-15",
            source="tcgplayer",
            seller_name="Card Shop",
            notes="Great deal",
        )
        new_id = repo.add(entry)
        assert new_id is not None

        fetched = repo.get(new_id)
        assert fetched is not None
        assert fetched.sealed_product_uuid == "sealed-uuid-001"
        assert fetched.quantity == 2
        assert fetched.purchase_price == 149.99
        assert fetched.status == "owned"
        assert fetched.added_at is not None
        conn.commit()

    def test_update(self, repos):
        _, repo, conn = repos
        entry = SealedCollectionEntry(
            id=None,
            sealed_product_uuid="sealed-uuid-001",
        )
        new_id = repo.add(entry)
        conn.commit()

        fetched = repo.get(new_id)
        fetched.notes = "Updated note"
        fetched.quantity = 5
        assert repo.update(fetched) is True

        updated = repo.get(new_id)
        assert updated.notes == "Updated note"
        assert updated.quantity == 5
        conn.commit()

    def test_delete(self, repos):
        _, repo, conn = repos
        entry = SealedCollectionEntry(
            id=None,
            sealed_product_uuid="sealed-uuid-001",
        )
        new_id = repo.add(entry)
        conn.commit()

        assert repo.delete(new_id) is True
        assert repo.get(new_id) is None

    def test_delete_nonexistent(self, repos):
        _, repo, _ = repos
        assert repo.delete(99999) is False

    def test_list_all(self, repos):
        _, repo, conn = repos
        repo.add(SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-001"))
        repo.add(SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-002"))
        repo.add(SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-004"))
        conn.commit()

        results = repo.list_all()
        assert len(results) == 3

    def test_list_all_filter_by_set(self, repos):
        _, repo, conn = repos
        repo.add(SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-001"))
        repo.add(SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-004"))
        conn.commit()

        tst = repo.list_all(set_code="tst")
        assert len(tst) == 1
        ts2 = repo.list_all(set_code="ts2")
        assert len(ts2) == 1

    def test_list_all_filter_by_status(self, repos):
        _, repo, conn = repos
        repo.add(SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-001"))
        entry2 = SealedCollectionEntry(
            id=None, sealed_product_uuid="sealed-uuid-002", status="opened"
        )
        repo.add(entry2)
        conn.commit()

        owned = repo.list_all(status="owned")
        assert len(owned) == 1
        opened = repo.list_all(status="opened")
        assert len(opened) == 1

    def test_dispose_owned_to_sold(self, repos):
        _, repo, conn = repos
        new_id = repo.add(
            SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-001")
        )
        conn.commit()

        assert repo.dispose(new_id, "sold", sale_price=200.00) is True
        entry = repo.get(new_id)
        assert entry.status == "sold"
        assert entry.sale_price == 200.00

    def test_dispose_owned_to_opened(self, repos):
        _, repo, conn = repos
        new_id = repo.add(
            SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-001")
        )
        conn.commit()

        assert repo.dispose(new_id, "opened") is True
        assert repo.get(new_id).status == "opened"

    def test_dispose_invalid_transition(self, repos):
        _, repo, conn = repos
        new_id = repo.add(
            SealedCollectionEntry(
                id=None, sealed_product_uuid="sealed-uuid-001", status="opened"
            )
        )
        conn.commit()

        with pytest.raises(ValueError, match="Cannot transition"):
            repo.dispose(new_id, "sold")

    def test_dispose_nonexistent(self, repos):
        _, repo, _ = repos
        with pytest.raises(ValueError, match="not found"):
            repo.dispose(99999, "sold")

    def test_stats_empty(self, repos):
        _, repo, _ = repos
        stats = repo.stats()
        assert stats["total_entries"] == 0
        assert stats["total_quantity"] == 0
        assert stats["total_cost"] == 0

    def test_stats_with_entries(self, repos):
        _, repo, conn = repos
        repo.add(
            SealedCollectionEntry(
                id=None,
                sealed_product_uuid="sealed-uuid-001",
                quantity=2,
                purchase_price=150.00,
            )
        )
        repo.add(
            SealedCollectionEntry(
                id=None,
                sealed_product_uuid="sealed-uuid-002",
                quantity=1,
                purchase_price=250.00,
            )
        )
        conn.commit()

        stats = repo.stats()
        assert stats["total_entries"] == 2
        assert stats["total_quantity"] == 3
        assert stats["total_cost"] == 550.00  # (150*2) + (250*1)
        assert "owned" in stats["by_status"]
        assert stats["by_status"]["owned"]["count"] == 2
        assert stats["by_status"]["owned"]["quantity"] == 3

    def test_stats_market_value_no_prices(self, repos):
        """Stats include market_value and gain_loss fields (zero when no prices)."""
        _, repo, conn = repos
        repo.add(SealedCollectionEntry(
            id=None, sealed_product_uuid="sealed-uuid-001",
            quantity=2, purchase_price=150.00,
        ))
        conn.commit()

        stats = repo.stats()
        assert "market_value" in stats
        assert "gain_loss" in stats
        assert stats["market_value"] == 0
        assert stats["gain_loss"] == -300.00  # 0 - (150*2)

    def test_stats_market_value_with_prices(self, repos):
        """Stats compute market_value from latest_sealed_prices."""
        _, repo, conn = repos
        repo.add(SealedCollectionEntry(
            id=None, sealed_product_uuid="sealed-uuid-001",
            quantity=2, purchase_price=100.00,
        ))
        conn.commit()

        # Insert price for tcgplayer_product_id "12345" (matches sealed-uuid-001)
        conn.execute("""
            INSERT INTO sealed_prices (tcgplayer_product_id, low_price, mid_price,
                high_price, market_price, direct_low_price, observed_at)
            VALUES ('12345', 100.0, 150.0, 200.0, 125.0, NULL, '2025-06-01')
        """)
        conn.commit()

        stats = repo.stats()
        assert stats["market_value"] == 250.0  # 125 * 2
        assert stats["gain_loss"] == 50.0  # 250 - (100*2)

    def test_list_all_includes_price_columns(self, repos):
        """list_all() includes market/low/mid/high price columns (NULL when no prices)."""
        _, repo, conn = repos
        repo.add(SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-001"))
        conn.commit()

        results = repo.list_all()
        assert len(results) == 1
        row = results[0]
        # Price columns present but NULL (no price data loaded)
        assert "market_price" in row
        assert "low_price" in row
        assert "mid_price" in row
        assert "high_price" in row
        assert row["market_price"] is None

    def test_list_all_includes_contents_json(self, repos):
        """list_all() includes contents_json from sealed_products."""
        _, repo, conn = repos
        repo.add(SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-001"))
        conn.commit()

        results = repo.list_all()
        assert len(results) == 1
        row = results[0]
        assert "contents_json" in row
        # sealed-uuid-001 has contents_json with a "pack" key (from fixture)
        if row["contents_json"]:
            contents = json.loads(row["contents_json"])
            assert "pack" in contents

    def test_list_all_with_prices(self, repos):
        """list_all() returns price data when sealed_prices has data."""
        _, repo, conn = repos
        repo.add(SealedCollectionEntry(id=None, sealed_product_uuid="sealed-uuid-001"))
        conn.commit()

        # Insert a price row for tcgplayer_product_id "12345" (matches sealed-uuid-001)
        conn.execute("""
            INSERT INTO sealed_prices (tcgplayer_product_id, low_price, mid_price,
                high_price, market_price, direct_low_price, observed_at)
            VALUES ('12345', 100.0, 150.0, 200.0, 125.0, NULL, '2025-06-01')
        """)
        conn.commit()

        results = repo.list_all()
        assert len(results) == 1
        row = results[0]
        assert row["market_price"] == 125.0
        assert row["low_price"] == 100.0
        assert row["mid_price"] == 150.0
        assert row["high_price"] == 200.0
