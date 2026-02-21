"""
Integration tests for sealed product API endpoints.

Runs against a live container instance with demo data loaded.

    uv run pytest tests/integration/test_sealed_api.py -v --instance sealed-collection

To set up the instance:

    bash deploy/setup.sh sealed-collection --init
    systemctl --user start mtgc-sealed-collection
"""

import pytest


# =============================================================================
# Product reference data (read-only)
# =============================================================================


class TestSealedProductsSearch:
    def test_search_by_name(self, api):
        status, data = api.get("/api/sealed/products?q=play+booster+box&limit=5")
        assert status == 200
        assert isinstance(data, list)
        assert len(data) > 0
        assert len(data) <= 5
        for product in data:
            assert "play booster box" in product["name"].lower()

    def test_search_returns_expected_fields(self, api):
        status, data = api.get("/api/sealed/products?q=collector+booster&limit=1")
        assert status == 200
        assert len(data) == 1
        p = data[0]
        assert "uuid" in p
        assert "name" in p
        assert "set_code" in p
        assert "set_name" in p
        assert "category" in p
        assert "subtype" in p
        assert "tcgplayer_product_id" in p
        assert "image_url" in p
        assert "release_date" in p
        assert "purchase_url_tcgplayer" in p

    def test_search_image_url_format(self, api):
        status, data = api.get("/api/sealed/products?q=play+booster+box&limit=1")
        assert status == 200
        p = data[0]
        if p["tcgplayer_product_id"]:
            assert p["image_url"].startswith("https://tcgplayer-cdn.tcgplayer.com/product/")
            assert p["image_url"].endswith("_200w.jpg")

    def test_search_by_set_code(self, api):
        status, data = api.get("/api/sealed/products?set_code=mkm")
        assert status == 200
        assert len(data) > 0
        for product in data:
            assert product["set_code"] == "mkm"

    def test_search_by_set_with_category_filter(self, api):
        status, data = api.get("/api/sealed/products?set_code=mkm&category=booster_box")
        assert status == 200
        assert len(data) > 0
        for product in data:
            assert product["set_code"] == "mkm"
            assert product["category"] == "booster_box"

    def test_search_no_params_returns_empty(self, api):
        status, data = api.get("/api/sealed/products")
        assert status == 200
        assert data == []

    def test_search_no_results(self, api):
        status, data = api.get("/api/sealed/products?q=xyznonexistent99999")
        assert status == 200
        assert data == []

    def test_search_limit(self, api):
        status, data = api.get("/api/sealed/products?q=booster&limit=3")
        assert status == 200
        assert len(data) <= 3


class TestSealedProductsSets:
    def test_list_sets(self, api):
        status, data = api.get("/api/sealed/products/sets")
        assert status == 200
        assert isinstance(data, list)
        assert len(data) > 100  # demo data has 339 sets with sealed products

    def test_set_entry_fields(self, api):
        status, data = api.get("/api/sealed/products/sets")
        assert status == 200
        s = data[0]
        assert "set_code" in s
        assert "set_name" in s
        assert "product_count" in s
        assert isinstance(s["product_count"], int)
        assert s["product_count"] > 0


# =============================================================================
# Collection CRUD (mutating — tests clean up after themselves)
# =============================================================================


class TestSealedCollectionCRUD:
    """Full lifecycle: add → list → update → dispose → delete."""

    @pytest.fixture
    def product_uuid(self, api):
        """Find a real sealed product UUID to use in tests."""
        status, data = api.get("/api/sealed/products?q=play+booster+box&limit=1")
        assert status == 200
        assert len(data) > 0
        return data[0]["uuid"]

    def test_full_lifecycle(self, api, product_uuid):
        """End-to-end: add, read back, update, dispose, delete."""
        # Add
        status, added = api.post("/api/sealed/collection", {
            "sealed_product_uuid": product_uuid,
            "quantity": 2,
            "purchase_price": 149.99,
            "source": "integration_test",
            "seller_name": "Test Shop",
            "notes": "integration test entry",
        })
        assert status == 200
        assert added["id"] is not None
        entry_id = added["id"]
        assert added["quantity"] == 2
        assert added["purchase_price"] == 149.99
        assert added["status"] == "owned"
        assert added["product_name"] is not None
        assert added["image_url"] is not None or added.get("image_url") is None

        try:
            # List and verify it appears
            status, entries = api.get("/api/sealed/collection")
            assert status == 200
            ids = [e["id"] for e in entries]
            assert entry_id in ids

            # Stats should reflect the entry
            status, stats = api.get("/api/sealed/collection/stats")
            assert status == 200
            assert stats["total_entries"] >= 1
            assert stats["total_quantity"] >= 2

            # Update
            status, result = api.put(f"/api/sealed/collection/{entry_id}", {
                "quantity": 3,
                "notes": "updated by integration test",
            })
            assert status == 200
            assert result["ok"] is True

            # Verify update via list
            status, entries = api.get("/api/sealed/collection")
            assert status == 200
            entry = next(e for e in entries if e["id"] == entry_id)
            assert entry["quantity"] == 3
            assert entry["notes"] == "updated by integration test"

            # Dispose: owned -> sold
            status, result = api.post(f"/api/sealed/collection/{entry_id}/dispose", {
                "new_status": "sold",
                "sale_price": 200.00,
            })
            assert status == 200
            assert result["ok"] is True

        finally:
            # Cleanup: always delete even if assertions fail
            api.delete(f"/api/sealed/collection/{entry_id}?confirm=true")

    def test_add_missing_uuid(self, api):
        status, data = api.post("/api/sealed/collection", {
            "quantity": 1,
        })
        assert status == 400
        assert "sealed_product_uuid" in data["error"]

    def test_add_nonexistent_product(self, api):
        status, data = api.post("/api/sealed/collection", {
            "sealed_product_uuid": "nonexistent-uuid-00000",
        })
        assert status == 404
        assert "not found" in data["error"].lower()

    def test_dispose_invalid_transition(self, api, product_uuid):
        """Create an entry, open it, then try an invalid transition."""
        status, added = api.post("/api/sealed/collection", {
            "sealed_product_uuid": product_uuid,
            "source": "integration_test",
        })
        assert status == 200
        entry_id = added["id"]

        try:
            # Open it
            status, _ = api.post(f"/api/sealed/collection/{entry_id}/dispose", {
                "new_status": "opened",
            })
            assert status == 200

            # Try opened -> sold (invalid)
            status, data = api.post(f"/api/sealed/collection/{entry_id}/dispose", {
                "new_status": "sold",
            })
            assert status == 400
            assert "Cannot transition" in data["error"]
        finally:
            api.delete(f"/api/sealed/collection/{entry_id}?confirm=true")

    def test_dispose_missing_status(self, api, product_uuid):
        status, added = api.post("/api/sealed/collection", {
            "sealed_product_uuid": product_uuid,
            "source": "integration_test",
        })
        entry_id = added["id"]

        try:
            status, data = api.post(f"/api/sealed/collection/{entry_id}/dispose", {})
            assert status == 400
            assert "new_status" in data["error"]
        finally:
            api.delete(f"/api/sealed/collection/{entry_id}?confirm=true")

    def test_dispose_nonexistent(self, api):
        status, data = api.post("/api/sealed/collection/999999/dispose", {
            "new_status": "sold",
        })
        assert status == 400
        assert "not found" in data["error"].lower()

    def test_delete_nonexistent(self, api):
        status, data = api.delete("/api/sealed/collection/999999?confirm=true")
        assert status == 404

    def test_delete_requires_confirm(self, api, product_uuid):
        status, added = api.post("/api/sealed/collection", {
            "sealed_product_uuid": product_uuid,
            "source": "integration_test",
        })
        entry_id = added["id"]

        try:
            status, data = api.delete(f"/api/sealed/collection/{entry_id}")
            assert status == 400
            assert "confirm" in data["error"].lower()
        finally:
            api.delete(f"/api/sealed/collection/{entry_id}?confirm=true")

    def test_update_nonexistent(self, api):
        status, data = api.put("/api/sealed/collection/999999", {
            "notes": "nope",
        })
        assert status == 404


class TestSealedCollectionFilters:
    """Test list filtering by set_code, category, status."""

    @pytest.fixture
    def two_entries(self, api):
        """Create two entries in different sets for filter testing."""
        # Find products from two different sets
        _, mkm = api.get("/api/sealed/products?set_code=mkm&category=booster_box&limit=1")
        _, fdn = api.get("/api/sealed/products?set_code=fdn&category=booster_box&limit=1")

        ids = []
        if mkm:
            s, a = api.post("/api/sealed/collection", {
                "sealed_product_uuid": mkm[0]["uuid"],
                "source": "integration_test",
            })
            if s == 200:
                ids.append(a["id"])

        if fdn:
            s, a = api.post("/api/sealed/collection", {
                "sealed_product_uuid": fdn[0]["uuid"],
                "source": "integration_test",
            })
            if s == 200:
                ids.append(a["id"])
                # Transition second entry to "opened" for status filter testing
                api.post(f"/api/sealed/collection/{a['id']}/dispose", {
                    "new_status": "opened",
                })

        yield ids

        for eid in ids:
            api.delete(f"/api/sealed/collection/{eid}?confirm=true")

    def test_filter_by_set(self, api, two_entries):
        if len(two_entries) < 2:
            pytest.skip("Need products from both mkm and fdn sets")

        status, data = api.get("/api/sealed/collection?set_code=mkm")
        assert status == 200
        for entry in data:
            assert entry["set_code"] == "mkm"

    def test_filter_by_status(self, api, two_entries):
        if len(two_entries) < 2:
            pytest.skip("Need both entries")

        status, owned = api.get("/api/sealed/collection?status=owned")
        assert status == 200
        for entry in owned:
            assert entry["status"] == "owned"

        status, opened = api.get("/api/sealed/collection?status=opened")
        assert status == 200
        for entry in opened:
            assert entry["status"] == "opened"


class TestSealedCollectionStats:
    def test_stats_shape(self, api):
        status, stats = api.get("/api/sealed/collection/stats")
        assert status == 200
        assert "total_entries" in stats
        assert "total_quantity" in stats
        assert "by_status" in stats
        assert "total_cost" in stats
        assert isinstance(stats["total_entries"], int)
        assert isinstance(stats["total_quantity"], int)

    def test_stats_reflect_adds(self, api):
        """Add an entry and verify stats update."""
        _, products = api.get("/api/sealed/products?q=booster+box&limit=1")
        if not products:
            pytest.skip("No sealed products available")

        # Get baseline
        _, before = api.get("/api/sealed/collection/stats")

        status, added = api.post("/api/sealed/collection", {
            "sealed_product_uuid": products[0]["uuid"],
            "quantity": 5,
            "purchase_price": 100.00,
            "source": "integration_test",
        })
        assert status == 200

        try:
            _, after = api.get("/api/sealed/collection/stats")
            assert after["total_entries"] == before["total_entries"] + 1
            assert after["total_quantity"] == before["total_quantity"] + 5
            assert after["total_cost"] == before["total_cost"] + 500.00
        finally:
            api.delete(f"/api/sealed/collection/{added['id']}?confirm=true")


# =============================================================================
# Price fetch endpoint
# =============================================================================


class TestSealedPriceFetch:
    def test_fetch_prices(self, api):
        """POST /api/sealed/fetch-prices triggers TCGCSV price fetch."""
        status, data = api.post("/api/sealed/fetch-prices", {}, timeout=120)
        assert status == 200
        assert "groups_fetched" in data
        assert "prices_for_today" in data
        assert isinstance(data["groups_fetched"], int)
        assert isinstance(data["prices_for_today"], int)

    def test_collection_list_has_price_fields(self, api):
        """After a price fetch, collection list entries should include price columns."""
        # Ensure prices are fetched first
        api.post("/api/sealed/fetch-prices", {}, timeout=120)

        # Add an entry for a product that likely has a TCGPlayer price
        _, products = api.get("/api/sealed/products?q=play+booster+box&limit=1")
        if not products:
            pytest.skip("No sealed products available")

        status, added = api.post("/api/sealed/collection", {
            "sealed_product_uuid": products[0]["uuid"],
            "source": "integration_test",
        })
        assert status == 200

        try:
            status, entries = api.get("/api/sealed/collection")
            assert status == 200
            entry = next(e for e in entries if e["id"] == added["id"])
            # Price columns should be present in the response (may be null if no TCGCSV match)
            assert "market_price" in entry
            assert "low_price" in entry
            assert "mid_price" in entry
            assert "high_price" in entry
            assert "contents_json" in entry
        finally:
            api.delete(f"/api/sealed/collection/{added['id']}?confirm=true")


# =============================================================================
# TCGPlayer URL lookup
# =============================================================================


class TestTcgPlayerLookup:
    def test_lookup_by_product_id(self, api):
        """POST /api/sealed/from-tcgplayer with a known product ID."""
        # First find a product that has a tcgplayer_product_id
        _, products = api.get("/api/sealed/products?q=play+booster+box&limit=1")
        if not products or not products[0].get("tcgplayer_product_id"):
            pytest.skip("No product with tcgplayer_product_id available")

        tcg_id = products[0]["tcgplayer_product_id"]
        status, data = api.post("/api/sealed/from-tcgplayer", {"product_id": str(tcg_id)})
        assert status == 200
        assert data["uuid"] == products[0]["uuid"]
        assert data["name"] == products[0]["name"]

    def test_lookup_by_url(self, api):
        """POST /api/sealed/from-tcgplayer with a TCGPlayer URL."""
        _, products = api.get("/api/sealed/products?q=play+booster+box&limit=1")
        if not products or not products[0].get("tcgplayer_product_id"):
            pytest.skip("No product with tcgplayer_product_id available")

        tcg_id = products[0]["tcgplayer_product_id"]
        url = f"https://www.tcgplayer.com/product/{tcg_id}/some-product-name"
        status, data = api.post("/api/sealed/from-tcgplayer", {"url": url})
        assert status == 200
        assert data["uuid"] == products[0]["uuid"]
        assert "image_url" in data

    def test_lookup_not_found(self, api):
        """POST /api/sealed/from-tcgplayer with a nonexistent product ID."""
        status, data = api.post("/api/sealed/from-tcgplayer", {"product_id": "999999999"})
        assert status == 404
        assert "error" in data

    def test_lookup_missing_input(self, api):
        """POST /api/sealed/from-tcgplayer with no url or product_id."""
        status, data = api.post("/api/sealed/from-tcgplayer", {})
        assert status == 400
        assert "error" in data


