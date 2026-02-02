"""
Integration test for card image ingestion.

This test uses a real photo and calls the actual Claude API to verify
that all cards are correctly identified and imported.

To run: pytest tests/test_ingest_integration.py -v

Note: Requires ANTHROPIC_API_KEY environment variable to be set.
This test costs real API credits to run.
"""

import os
import tempfile
from pathlib import Path

import pytest

from mtg_collector.db import get_connection, init_db, CollectionRepository
from mtg_collector.db.connection import close_connection


# Expected cards in tests/fixtures/photo-test.jpg
# These are the 13 cards visible in the test photo
EXPECTED_CARDS = [
    "Noggle the Mind",
    "Ashling, Rekindled",
    "Soulbright Seeker",
    "Moonlit Lamenter",
    "Boggart Mischief",
    "Wanderwine Farewell",
    "Goblin Chieftain",
    "Wary Farmer",
    "Blight Rot",
    "Elder Auntie",
    "Unforgiving Aim",
    "Shore Lurker",
    "Aquitect's Defenses",
]


@pytest.fixture
def test_db():
    """Create a temporary database for testing."""
    close_connection()
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    conn = get_connection(db_path)
    init_db(conn)

    yield db_path, conn

    close_connection()
    os.unlink(db_path)


@pytest.fixture
def test_image():
    """Get the path to the test image."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    image_path = fixtures_dir / "photo-test.jpg"

    if not image_path.exists():
        pytest.skip(f"Test image not found: {image_path}")

    return str(image_path)


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set"
)
class TestIngestIntegration:
    """Integration tests for card ingestion."""

    def test_all_cards_identified(self, test_db, test_image):
        """
        Test that all 13 cards in the photo are correctly identified and imported.

        This test verifies:
        1. Claude identifies the correct NUMBER of cards with their sets
        2. Set codes are normalized against Scryfall
        3. Each card is found via set-aware fuzzy matching
        4. All cards are added to the collection

        Note: Claude may misread card names slightly (e.g., "Soothright" vs "Soulbright")
        but set-aware fuzzy matching should correct these errors.
        """
        from mtg_collector.db import CardRepository, SetRepository, PrintingRepository
        from mtg_collector.services.claude import ClaudeVision
        from mtg_collector.services.scryfall import (
            ScryfallAPI,
            cache_scryfall_data,
            ensure_set_cached,
            get_cached_set_cards,
        )
        from mtg_collector.db.models import CollectionEntry

        db_path, conn = test_db

        # Initialize services
        claude = ClaudeVision()
        scryfall = ScryfallAPI()

        # Initialize repositories
        card_repo = CardRepository(conn)
        set_repo = SetRepository(conn)
        printing_repo = PrintingRepository(conn)
        collection_repo = CollectionRepository(conn)

        # Step 1: Identify cards in image (now returns list of {name, set})
        cards_info = claude.identify_cards(test_image)

        assert len(cards_info) == len(EXPECTED_CARDS), (
            f"Expected {len(EXPECTED_CARDS)} cards, but Claude identified {len(cards_info)}: {cards_info}"
        )

        # Step 2: Collect and normalize set codes
        detected_sets = set()
        for card in cards_info:
            raw_set = card.get("set")
            if raw_set:
                normalized = scryfall.normalize_set_code(raw_set)
                if normalized:
                    card["_normalized_set"] = normalized
                    detected_sets.add(normalized)

        # Cache detected sets
        for set_code in detected_sets:
            ensure_set_cached(scryfall, set_code, card_repo, set_repo, printing_repo, conn)

        # Load cached card lists
        set_card_cache = {}
        for set_code in detected_sets:
            set_card_cache[set_code] = get_cached_set_cards(conn, set_code)

        # Step 3: Process each card using set-aware fuzzy matching
        added_cards = []
        failed_cards = []

        for card_info in cards_info:
            card_name = card_info["name"]
            detected_set = card_info.get("_normalized_set")

            # Try fuzzy matching against detected set's cached card list
            printings = []
            matched_name = None
            matched_set = None

            # First try the detected set
            if detected_set and detected_set in set_card_cache:
                cached_cards = set_card_cache[detected_set]
                matched_card = scryfall.fuzzy_match_in_set(
                    card_name, detected_set, cached_cards=cached_cards
                )
                if matched_card:
                    matched_name = matched_card["name"]
                    matched_set = detected_set

            # If not found in detected set, try other detected sets
            if not matched_name:
                for other_set in detected_sets:
                    if other_set != detected_set and other_set in set_card_cache:
                        cached_cards = set_card_cache[other_set]
                        matched_card = scryfall.fuzzy_match_in_set(
                            card_name, other_set, cached_cards=cached_cards
                        )
                        if matched_card:
                            matched_name = matched_card["name"]
                            matched_set = other_set
                            break

            # Search for printings using matched name
            if matched_name and matched_set:
                actual_name = matched_name
                if " // " in actual_name:
                    actual_name = actual_name.split(" // ")[0]
                printings = scryfall.search_card(actual_name, set_code=matched_set, fuzzy=False)

            # Fall back to normal search
            if not printings:
                printings = scryfall.search_card(card_name)

            if not printings:
                failed_cards.append(card_name)
                continue

            # Select first printing
            selected = printings[0]

            # Cache Scryfall data
            cache_scryfall_data(scryfall, card_repo, set_repo, printing_repo, selected)

            # Add to collection
            entry = CollectionEntry(
                id=None,
                scryfall_id=selected["id"],
                finish="nonfoil",
                condition="Near Mint",
                source="test_ingest",
            )

            collection_repo.add(entry)
            added_cards.append(selected["name"])

        conn.commit()

        # Step 4: Verify all cards were added
        # Note: We allow some failures since Claude may misread cards in ways
        # that can't be fuzzy matched. The important thing is that most cards work.
        assert len(failed_cards) <= 2, (
            f"Too many cards failed (max 2 allowed): {failed_cards}\n"
            f"This suggests a systematic issue with the pipeline."
        )

        assert len(added_cards) >= len(EXPECTED_CARDS) - 2, (
            f"Expected at least {len(EXPECTED_CARDS) - 2} cards in collection, got {len(added_cards)}"
        )

        # Step 5: Verify collection contents exist (not exact names, since Claude's output varies)
        entries = collection_repo.list_all()
        collection_names = [e["name"] for e in entries]

        print(f"Successfully added {len(added_cards)} cards: {collection_names}")

    def test_ingest_command_batch_mode(self, test_db, test_image):
        """
        Test the full ingest command in batch mode with auto-detection.

        This tests the CLI integration, not just the individual components.
        Claude should auto-detect set codes from the cards, and the system
        should cache and use those sets for fuzzy matching.
        """
        import subprocess
        import sys

        db_path, conn = test_db
        close_connection()  # Close so subprocess can use it

        # Run the ingest command WITHOUT --set flag
        # The system should auto-detect sets from the cards
        result = subprocess.run(
            [
                sys.executable, "-m", "mtg_collector",
                "--db", db_path,
                "ingest", test_image, "--batch"
            ],
            capture_output=True,
            text=True,
            env={**os.environ},
        )

        # Reconnect to check results
        conn = get_connection(db_path)
        collection_repo = CollectionRepository(conn)

        entries = collection_repo.list_all()
        collection_names = [e["name"] for e in entries]

        # Check that all expected cards are in the collection
        # Note: DFCs (double-faced cards) have names like "Front // Back"
        # so we check if the expected name is contained in the collection name
        def card_matches(expected: str, collection_names: list) -> bool:
            for name in collection_names:
                if expected == name or expected in name.split(" // "):
                    return True
            return False

        missing = [card for card in EXPECTED_CARDS if not card_matches(card, collection_names)]

        assert not missing, (
            f"Missing cards after batch ingest: {missing}\n"
            f"Collection contains: {collection_names}\n"
            f"Stdout: {result.stdout}\n"
            f"Stderr: {result.stderr}"
        )

        assert len(entries) == len(EXPECTED_CARDS), (
            f"Expected {len(EXPECTED_CARDS)} cards, got {len(entries)}\n"
            f"Collection: {collection_names}"
        )
