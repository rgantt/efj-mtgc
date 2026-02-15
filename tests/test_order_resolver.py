"""
Tests for order resolution and commit (uses fixture DB, no live Scryfall).

To run: pytest tests/test_order_resolver.py -v
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from mtg_collector.db import (
    get_connection,
    init_db,
    CardRepository,
    SetRepository,
    PrintingRepository,
    CollectionRepository,
    OrderRepository,
)
from mtg_collector.db.connection import close_connection
from mtg_collector.db.models import CollectionEntry, Order
from mtg_collector.services.order_parser import ParsedOrder, ParsedOrderItem
from mtg_collector.services.order_resolver import (
    resolve_orders,
    commit_orders,
    ResolvedOrder,
    ResolvedItem,
)
from mtg_collector.services.scryfall import ScryfallAPI

CACHE_DB = Path(__file__).parent / "fixtures" / "scryfall-cache.sqlite"


@pytest.fixture
def test_db():
    """Create a temporary database pre-loaded with Scryfall cache."""
    close_connection()
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    if CACHE_DB.exists():
        shutil.copy2(str(CACHE_DB), db_path)
        conn = get_connection(db_path)
        init_db(conn)
    else:
        conn = get_connection(db_path)
        init_db(conn)

    yield db_path, conn

    close_connection()
    os.unlink(db_path)


@pytest.fixture
def repos(test_db):
    db_path, conn = test_db
    return {
        "conn": conn,
        "card_repo": CardRepository(conn),
        "set_repo": SetRepository(conn),
        "printing_repo": PrintingRepository(conn),
        "collection_repo": CollectionRepository(conn),
        "order_repo": OrderRepository(conn),
    }


class TestOrderRepository:
    def test_add_and_get(self, repos):
        order_repo = repos["order_repo"]
        conn = repos["conn"]

        order = Order(
            id=None,
            order_number="TEST-123",
            source="tcgplayer",
            seller_name="Test Seller",
            order_date="January 1, 2026",
            total=10.00,
        )
        oid = order_repo.add(order)
        conn.commit()

        fetched = order_repo.get(oid)
        assert fetched is not None
        assert fetched.order_number == "TEST-123"
        assert fetched.seller_name == "Test Seller"
        assert fetched.total == 10.00

    def test_get_by_number(self, repos):
        order_repo = repos["order_repo"]
        conn = repos["conn"]

        for seller in ["Seller A", "Seller B"]:
            order_repo.add(Order(
                id=None,
                order_number="SHARED-NUM",
                source="tcgplayer",
                seller_name=seller,
            ))
        conn.commit()

        orders = order_repo.get_by_number("SHARED-NUM")
        assert len(orders) == 2

    def test_list_all_with_card_count(self, repos):
        order_repo = repos["order_repo"]
        collection_repo = repos["collection_repo"]
        conn = repos["conn"]

        oid = order_repo.add(Order(id=None, order_number="CNT-1", source="tcgplayer"))

        # Need a real scryfall_id from the cache DB
        cursor = conn.execute("SELECT scryfall_id FROM printings LIMIT 1")
        row = cursor.fetchone()
        if not row:
            pytest.skip("No printings in cache DB")
        sid = row["scryfall_id"]

        collection_repo.add(CollectionEntry(
            id=None, scryfall_id=sid, finish="nonfoil",
            status="ordered", order_id=oid,
        ))
        conn.commit()

        orders = order_repo.list_all()
        assert len(orders) >= 1
        found = [o for o in orders if o["order_number"] == "CNT-1"]
        assert found[0]["card_count"] == 1

    def test_receive_order(self, repos):
        order_repo = repos["order_repo"]
        collection_repo = repos["collection_repo"]
        conn = repos["conn"]

        oid = order_repo.add(Order(id=None, order_number="RCV-1", source="tcgplayer"))

        cursor = conn.execute("SELECT scryfall_id FROM printings LIMIT 1")
        row = cursor.fetchone()
        if not row:
            pytest.skip("No printings in cache DB")
        sid = row["scryfall_id"]

        cid = collection_repo.add(CollectionEntry(
            id=None, scryfall_id=sid, finish="nonfoil",
            status="ordered", order_id=oid,
        ))
        conn.commit()

        count = order_repo.receive_order(oid)
        conn.commit()
        assert count == 1

        entry = collection_repo.get(cid)
        assert entry.status == "owned"


class TestCommitOrders:
    def test_commit_creates_order_and_cards(self, repos):
        order_repo = repos["order_repo"]
        collection_repo = repos["collection_repo"]
        conn = repos["conn"]

        # Get a real scryfall_id
        cursor = conn.execute("SELECT scryfall_id FROM printings LIMIT 1")
        row = cursor.fetchone()
        if not row:
            pytest.skip("No printings in cache DB")
        sid = row["scryfall_id"]

        parsed = ParsedOrder(
            order_number="COMMIT-1",
            source="tcgplayer",
            seller_name="Test Seller",
            total=5.00,
        )
        item = ParsedOrderItem(
            card_name="Test Card",
            condition="Near Mint",
            quantity=1,
            price=5.00,
        )
        resolved_item = ResolvedItem(
            parsed=item,
            scryfall_id=sid,
            card_name="Test Card",
        )
        resolved_order = ResolvedOrder(parsed=parsed, items=[resolved_item])

        summary = commit_orders(
            [resolved_order], order_repo, collection_repo, conn,
            status="ordered", source="order_import",
        )

        assert summary["orders_created"] == 1
        assert summary["cards_added"] == 1
        assert summary["cards_linked"] == 0

    def test_commit_links_existing_unlinked(self, repos):
        order_repo = repos["order_repo"]
        collection_repo = repos["collection_repo"]
        conn = repos["conn"]

        cursor = conn.execute("SELECT scryfall_id FROM printings LIMIT 1")
        row = cursor.fetchone()
        if not row:
            pytest.skip("No printings in cache DB")
        sid = row["scryfall_id"]

        # Create an existing unlinked ordered card
        collection_repo.add(CollectionEntry(
            id=None, scryfall_id=sid, finish="nonfoil",
            status="ordered", order_id=None,
        ))
        conn.commit()

        # Now commit an order with the same card
        parsed = ParsedOrder(order_number="LINK-1", source="tcgplayer", seller_name="Seller")
        item = ParsedOrderItem(card_name="Card", condition="Near Mint", quantity=1)
        resolved_item = ResolvedItem(parsed=item, scryfall_id=sid, card_name="Card")
        resolved_order = ResolvedOrder(parsed=parsed, items=[resolved_item])

        summary = commit_orders(
            [resolved_order], order_repo, collection_repo, conn,
            status="ordered", source="order_import",
        )

        assert summary["cards_linked"] == 1
        assert summary["cards_added"] == 0

    def test_commit_skips_unresolved(self, repos):
        order_repo = repos["order_repo"]
        collection_repo = repos["collection_repo"]
        conn = repos["conn"]

        parsed = ParsedOrder(order_number="SKIP-1", source="tcgplayer")
        item = ParsedOrderItem(card_name="Nonexistent Card", condition="Near Mint")
        resolved_item = ResolvedItem(
            parsed=item,
            scryfall_id=None,
            error="Card not found",
        )
        resolved_order = ResolvedOrder(parsed=parsed, items=[resolved_item])

        summary = commit_orders(
            [resolved_order], order_repo, collection_repo, conn,
        )

        assert summary["cards_added"] == 0
        assert len(summary["errors"]) == 1


class TestCollectionEntryOrderId:
    def test_add_with_order_id(self, repos):
        order_repo = repos["order_repo"]
        collection_repo = repos["collection_repo"]
        conn = repos["conn"]

        oid = order_repo.add(Order(id=None, order_number="OID-1", source="tcgplayer"))

        cursor = conn.execute("SELECT scryfall_id FROM printings LIMIT 1")
        row = cursor.fetchone()
        if not row:
            pytest.skip("No printings in cache DB")

        cid = collection_repo.add(CollectionEntry(
            id=None, scryfall_id=row["scryfall_id"], finish="nonfoil",
            order_id=oid,
        ))
        conn.commit()

        entry = collection_repo.get(cid)
        assert entry.order_id == oid

    def test_update_order_id(self, repos):
        order_repo = repos["order_repo"]
        collection_repo = repos["collection_repo"]
        conn = repos["conn"]

        cursor = conn.execute("SELECT scryfall_id FROM printings LIMIT 1")
        row = cursor.fetchone()
        if not row:
            pytest.skip("No printings in cache DB")

        cid = collection_repo.add(CollectionEntry(
            id=None, scryfall_id=row["scryfall_id"], finish="nonfoil",
        ))
        conn.commit()

        entry = collection_repo.get(cid)
        assert entry.order_id is None

        oid = order_repo.add(Order(id=None, order_number="UPD-1", source="tcgplayer"))
        entry.order_id = oid
        collection_repo.update(entry)
        conn.commit()

        entry = collection_repo.get(cid)
        assert entry.order_id == oid
