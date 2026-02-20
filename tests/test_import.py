"""
Tests for CSV import (local DB only, no Scryfall API calls).

To run: pytest tests/test_import.py -v
"""

import csv
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import requests
import urllib3

from mtg_collector.db import (
    CardRepository,
    CollectionRepository,
    PrintingRepository,
    SetRepository,
    get_connection,
    init_db,
)
from mtg_collector.db.connection import close_connection
from mtg_collector.db.schema import init_db as schema_init_db
from mtg_collector.importers.moxfield import MoxfieldImporter

# ── Test data ────────────────────────────────────────────────────────

ORACLE_ALPHA = "aaaa-aaaa-aaaa-aaaa"
ORACLE_BETA = "bbbb-bbbb-bbbb-bbbb"
ORACLE_DFC = "cccc-cccc-cccc-cccc"

SCRYFALL_ALPHA_TST = "1111-1111-1111-1111"
SCRYFALL_BETA_TST = "2222-2222-2222-2222"
SCRYFALL_ALPHA_TS2 = "3333-3333-3333-3333"
SCRYFALL_DFC_TST = "4444-4444-4444-4444"


def _insert_test_data(conn):
    """Insert test cards, sets, and printings directly via SQL."""
    # Sets
    conn.execute(
        "INSERT INTO sets (set_code, set_name, set_type, released_at) VALUES (?, ?, ?, ?)",
        ("tst", "Test Set", "expansion", "2025-01-01"),
    )
    conn.execute(
        "INSERT INTO sets (set_code, set_name, set_type, released_at) VALUES (?, ?, ?, ?)",
        ("ts2", "Test Set Two", "expansion", "2025-06-01"),
    )

    # Cards
    conn.execute(
        "INSERT INTO cards (oracle_id, name, type_line, mana_cost, cmc, colors, color_identity) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ORACLE_ALPHA, "Test Card Alpha", "Creature", "{W}", 1.0, '["W"]', '["W"]'),
    )
    conn.execute(
        "INSERT INTO cards (oracle_id, name, type_line, mana_cost, cmc, colors, color_identity) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ORACLE_BETA, "Test Card Beta", "Instant", "{U}", 1.0, '["U"]', '["U"]'),
    )
    conn.execute(
        "INSERT INTO cards (oracle_id, name, type_line, mana_cost, cmc, colors, color_identity) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ORACLE_DFC, "Front Face // Back Face", "Creature // Land", "{B}", 1.0, '["B"]', '["B"]'),
    )

    # Printings — set tst: Alpha (001), Beta (002), DFC (003)
    conn.execute(
        "INSERT INTO printings (scryfall_id, oracle_id, set_code, collector_number, rarity, "
        "frame_effects, border_color, full_art, promo, promo_types, finishes, artist, image_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SCRYFALL_ALPHA_TST, ORACLE_ALPHA, "tst", "001", "rare",
         "[]", "black", 0, 0, "[]", '["nonfoil","foil"]', "Artist A", None),
    )
    conn.execute(
        "INSERT INTO printings (scryfall_id, oracle_id, set_code, collector_number, rarity, "
        "frame_effects, border_color, full_art, promo, promo_types, finishes, artist, image_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SCRYFALL_BETA_TST, ORACLE_BETA, "tst", "002", "common",
         "[]", "black", 0, 0, "[]", '["nonfoil"]', "Artist B", None),
    )
    conn.execute(
        "INSERT INTO printings (scryfall_id, oracle_id, set_code, collector_number, rarity, "
        "frame_effects, border_color, full_art, promo, promo_types, finishes, artist, image_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SCRYFALL_DFC_TST, ORACLE_DFC, "tst", "003", "mythic",
         "[]", "black", 0, 0, "[]", '["nonfoil"]', "Artist C", None),
    )

    # Printings — set ts2: Alpha (050) — same oracle_id, different set
    conn.execute(
        "INSERT INTO printings (scryfall_id, oracle_id, set_code, collector_number, rarity, "
        "frame_effects, border_color, full_art, promo, promo_types, finishes, artist, image_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SCRYFALL_ALPHA_TS2, ORACLE_ALPHA, "ts2", "050", "rare",
         "[]", "black", 0, 0, "[]", '["nonfoil"]', "Artist A", None),
    )

    conn.commit()


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def test_db():
    """Create a temporary database with test data."""
    close_connection()
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    conn = get_connection(db_path)
    init_db(conn)
    _insert_test_data(conn)

    yield db_path, conn

    close_connection()
    os.unlink(db_path)


@pytest.fixture
def repos(test_db):
    _, conn = test_db
    return {
        "conn": conn,
        "card_repo": CardRepository(conn),
        "set_repo": SetRepository(conn),
        "printing_repo": PrintingRepository(conn),
        "collection_repo": CollectionRepository(conn),
    }


@pytest.fixture
def importer():
    return MoxfieldImporter()


def _write_csv(rows, fieldnames=None):
    """Write rows to a temp CSV and return the path."""
    if fieldnames is None:
        fieldnames = ["Count", "Name", "Edition", "Collector Number",
                      "Condition", "Foil", "Language", "Purchase Price"]
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


# ── TestResolveCard ──────────────────────────────────────────────────

class TestResolveCard:
    """Unit tests for BaseImporter._resolve_card (local DB only)."""

    def test_resolve_by_set_cn(self, repos, importer):
        """Correct name + set + cn → returns correct scryfall_id."""
        sid = importer._resolve_card(
            repos["card_repo"], repos["printing_repo"],
            "Test Card Alpha", "tst", "001",
        )
        assert sid == SCRYFALL_ALPHA_TST

    def test_resolve_by_name_only(self, repos, importer):
        """Name only (no set/cn) → finds card by name."""
        sid = importer._resolve_card(
            repos["card_repo"], repos["printing_repo"],
            "Test Card Beta", None, None,
        )
        assert sid == SCRYFALL_BETA_TST

    def test_resolve_by_name_prefers_matching_set(self, repos, importer):
        """Name + set_code (no cn) → prefers printing from that set."""
        sid = importer._resolve_card(
            repos["card_repo"], repos["printing_repo"],
            "Test Card Alpha", "ts2", None,
        )
        assert sid == SCRYFALL_ALPHA_TS2

    def test_wrong_set_cn_falls_back_to_name(self, repos, importer):
        """THE DOCTOR DOOM TEST: name="Test Card Alpha" but set/cn point to Beta.

        Name validation fails → falls back to name search → returns Alpha's id.
        """
        sid = importer._resolve_card(
            repos["card_repo"], repos["printing_repo"],
            "Test Card Alpha", "tst", "002",  # 002 is Beta, not Alpha
        )
        assert sid == SCRYFALL_ALPHA_TST

    def test_dfc_name_match(self, repos, importer):
        """Searching "Front Face" matches "Front Face // Back Face"."""
        sid = importer._resolve_card(
            repos["card_repo"], repos["printing_repo"],
            "Front Face", None, None,
        )
        assert sid == SCRYFALL_DFC_TST

    def test_not_found_returns_none(self, repos, importer):
        """Card not in DB → returns None."""
        sid = importer._resolve_card(
            repos["card_repo"], repos["printing_repo"],
            "Nonexistent Card", None, None,
        )
        assert sid is None


# ── TestImportFile ───────────────────────────────────────────────────

class TestImportFile:
    """Integration tests for full import_file flow."""

    def test_basic_import(self, repos, importer):
        """CSV with 2 cards → creates 2 collection entries."""
        csv_path = _write_csv([
            {"Count": "1", "Name": "Test Card Alpha", "Edition": "tst",
             "Collector Number": "001", "Condition": "Near Mint",
             "Foil": "", "Language": "English", "Purchase Price": ""},
            {"Count": "1", "Name": "Test Card Beta", "Edition": "tst",
             "Collector Number": "002", "Condition": "Near Mint",
             "Foil": "", "Language": "English", "Purchase Price": ""},
        ])
        try:
            result = importer.import_file(
                csv_path, repos["conn"],
                repos["card_repo"], repos["set_repo"],
                repos["printing_repo"], repos["collection_repo"],
            )
            assert result.total_rows == 2
            assert result.cards_added == 2
            assert result.cards_skipped == 0
            assert repos["collection_repo"].count() == 2
        finally:
            os.unlink(csv_path)

    def test_quantity_handling(self, repos, importer):
        """CSV row with Count=3 → creates 3 entries."""
        csv_path = _write_csv([
            {"Count": "3", "Name": "Test Card Alpha", "Edition": "tst",
             "Collector Number": "001", "Condition": "Near Mint",
             "Foil": "", "Language": "English", "Purchase Price": ""},
        ])
        try:
            result = importer.import_file(
                csv_path, repos["conn"],
                repos["card_repo"], repos["set_repo"],
                repos["printing_repo"], repos["collection_repo"],
            )
            assert result.cards_added == 3
            assert repos["collection_repo"].count() == 3
        finally:
            os.unlink(csv_path)

    def test_missing_card_skipped(self, repos, importer):
        """CSV with 1 known + 1 unknown card → 1 added, 1 error."""
        csv_path = _write_csv([
            {"Count": "1", "Name": "Test Card Alpha", "Edition": "tst",
             "Collector Number": "001", "Condition": "Near Mint",
             "Foil": "", "Language": "English", "Purchase Price": ""},
            {"Count": "1", "Name": "Nonexistent Card", "Edition": "xxx",
             "Collector Number": "999", "Condition": "Near Mint",
             "Foil": "", "Language": "English", "Purchase Price": ""},
        ])
        try:
            result = importer.import_file(
                csv_path, repos["conn"],
                repos["card_repo"], repos["set_repo"],
                repos["printing_repo"], repos["collection_repo"],
            )
            assert result.cards_added == 1
            assert result.cards_skipped == 1
            assert len(result.errors) == 1
        finally:
            os.unlink(csv_path)

    def test_wrong_set_cn_resolves_correctly(self, repos, importer):
        """CSV where all rows share the commander's set/cn but have correct names.

        Each card resolves via name (not all to the same card).
        """
        csv_path = _write_csv([
            {"Count": "1", "Name": "Test Card Alpha", "Edition": "tst",
             "Collector Number": "002", "Condition": "Near Mint",
             "Foil": "", "Language": "English", "Purchase Price": ""},
            {"Count": "1", "Name": "Test Card Beta", "Edition": "tst",
             "Collector Number": "002", "Condition": "Near Mint",
             "Foil": "", "Language": "English", "Purchase Price": ""},
        ])
        try:
            result = importer.import_file(
                csv_path, repos["conn"],
                repos["card_repo"], repos["set_repo"],
                repos["printing_repo"], repos["collection_repo"],
            )
            assert result.cards_added == 2
            assert result.cards_skipped == 0

            # Verify they resolved to different scryfall_ids
            entries = repos["collection_repo"].list_all()
            scryfall_ids = {e["scryfall_id"] for e in entries}
            assert len(scryfall_ids) == 2
        finally:
            os.unlink(csv_path)

    def test_no_scryfall_api_calls(self, repos, importer, monkeypatch):
        """Prove no HTTP calls are made during import."""
        def _block_request(*args, **kwargs):
            raise RuntimeError("Unexpected HTTP request during import!")

        import requests
        monkeypatch.setattr(requests.Session, "request", _block_request)

        csv_path = _write_csv([
            {"Count": "1", "Name": "Test Card Alpha", "Edition": "tst",
             "Collector Number": "001", "Condition": "Near Mint",
             "Foil": "", "Language": "English", "Purchase Price": ""},
        ])
        try:
            result = importer.import_file(
                csv_path, repos["conn"],
                repos["card_repo"], repos["set_repo"],
                repos["printing_repo"], repos["collection_repo"],
            )
            assert result.cards_added == 1
        finally:
            os.unlink(csv_path)


# ── TestWebImportResolve ─────────────────────────────────────────────

CONTAINER_NAME = "mtgc-test-import"
IMAGE_NAME = "mtgc:test-import"


def _podman_available():
    try:
        return subprocess.run(
            ["podman", "--version"], capture_output=True,
        ).returncode == 0
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not _podman_available(), reason="podman not available")
class TestWebImportResolve:
    """Integration test: builds the container image, starts the server via
    the same entrypoint as production, and hits /api/import/resolve."""

    @pytest.fixture(scope="class")
    def container_url(self, tmp_path_factory):
        """Build image, seed DB, start container, yield base URL."""
        repo_dir = Path(__file__).resolve().parent.parent

        # 1. Create and seed a temp DB on the host
        db_dir = tmp_path_factory.mktemp("import-test-db")
        db_path = db_dir / "collection.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        schema_init_db(conn)
        _insert_test_data(conn)
        conn.close()

        # 2. Build the container image from the same Containerfile as prod
        subprocess.run(
            ["podman", "build", "-t", IMAGE_NAME, "-f", "Containerfile", "."],
            cwd=str(repo_dir), check=True,
            capture_output=True,
        )

        # 3. Clean up any stale container from a previous run
        subprocess.run(
            ["podman", "rm", "-f", CONTAINER_NAME],
            capture_output=True,
        )

        # 4. Start container — same entrypoint as production, seeded DB mounted in
        subprocess.run(
            [
                "podman", "run", "-d",
                "--name", CONTAINER_NAME,
                "-p", ":8081",
                "-e", "ANTHROPIC_API_KEY=test-dummy",
                "-v", f"{db_path}:/data/collection.sqlite:Z",
                IMAGE_NAME,
            ],
            check=True, capture_output=True,
        )

        # 5. Discover the auto-assigned host port
        port_output = subprocess.check_output(
            ["podman", "port", CONTAINER_NAME, "8081/tcp"], text=True,
        ).strip()
        port = port_output.split(":")[-1]

        # 6. Wait for the server to accept connections
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        base_url = f"https://127.0.0.1:{port}"
        for attempt in range(30):
            try:
                requests.get(f"{base_url}/", verify=False, timeout=2)
                break
            except Exception:
                time.sleep(1)
        else:
            logs = subprocess.check_output(
                ["podman", "logs", CONTAINER_NAME], text=True,
                stderr=subprocess.STDOUT,
            )
            subprocess.run(["podman", "rm", "-f", CONTAINER_NAME], capture_output=True)
            pytest.fail(f"Container failed to start within 30s.\nLogs:\n{logs}")

        yield base_url

        subprocess.run(["podman", "rm", "-f", CONTAINER_NAME], capture_output=True)

    def test_resolve_via_web_api(self, container_url):
        """/api/import/resolve resolves cards using local DB, not Scryfall."""
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.post(
            f"{container_url}/api/import/resolve",
            json={
                "format": "moxfield",
                "rows": [
                    {"name": "Test Card Alpha", "set_code": "tst",
                     "collector_number": "001", "quantity": 1, "raw": {}},
                    {"name": "Test Card Beta", "set_code": "tst",
                     "collector_number": "002", "quantity": 1, "raw": {}},
                ],
            },
            verify=False,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["resolved"] == 2
        assert data["summary"]["failed"] == 0
        assert data["resolved"][0]["scryfall_id"] == SCRYFALL_ALPHA_TST
        assert data["resolved"][1]["scryfall_id"] == SCRYFALL_BETA_TST

    def test_resolve_wrong_set_cn_via_web_api(self, container_url):
        """Web UI Doctor Doom test: wrong set/cn falls back to name."""
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.post(
            f"{container_url}/api/import/resolve",
            json={
                "format": "moxfield",
                "rows": [
                    {"name": "Test Card Alpha", "set_code": "tst",
                     "collector_number": "002", "quantity": 1, "raw": {}},
                ],
            },
            verify=False,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["resolved"] == 1
        # Must resolve to Alpha, not Beta (whose set/cn was provided)
        assert data["resolved"][0]["scryfall_id"] == SCRYFALL_ALPHA_TST
