"""
Performance regression tests for /api/collection.

Measures API response time for the full unfiltered collection and asserts it
stays below a budget.  Run against a container with the demo dataset (~50 cards)
or a real collection for more realistic numbers.

Usage:
    uv run pytest tests/integration/test_collection_perf.py -v --instance <instance>
"""

import gzip
import json
import ssl
import time
import urllib.request

import pytest

# Budget: API must respond within this many milliseconds.
# The demo dataset is tiny (~50 cards), so this is generous.  A real collection
# with 1800+ cards should still be well under this after the batched-price fix.
API_RESPONSE_BUDGET_MS = 500


@pytest.fixture(scope="module")
def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _timed_get(url: str, ctx, headers: dict | None = None, accept_gzip: bool = False):
    """GET *url*, return (elapsed_ms, status, body_bytes, response_headers)."""
    req = urllib.request.Request(url, headers=headers or {})
    if accept_gzip:
        req.add_header("Accept-Encoding", "gzip")
    t0 = time.perf_counter()
    resp = urllib.request.urlopen(req, context=ctx, timeout=30)
    raw = resp.read()
    elapsed = (time.perf_counter() - t0) * 1000
    return elapsed, resp.status, raw, resp.headers


class TestCollectionApiPerformance:
    """API-level performance checks for /api/collection."""

    def test_response_time_unfiltered(self, base_url, _ssl_ctx):
        """Full collection (no filters) responds within budget."""
        url = f"{base_url}/api/collection"

        # Warm-up request (first hit may open DB connections, JIT views, etc.)
        _timed_get(url, _ssl_ctx)

        # Measured request
        elapsed_ms, status, body, _ = _timed_get(url, _ssl_ctx)
        cards = json.loads(body)

        assert status == 200
        assert isinstance(cards, list)
        print(f"\n  /api/collection: {len(cards)} cards in {elapsed_ms:.0f} ms")
        assert elapsed_ms < API_RESPONSE_BUDGET_MS, (
            f"Collection API took {elapsed_ms:.0f} ms (budget: {API_RESPONSE_BUDGET_MS} ms)"
        )

    def test_gzip_compression(self, base_url, _ssl_ctx):
        """Server compresses JSON when client accepts gzip."""
        url = f"{base_url}/api/collection"
        _, status, raw, headers = _timed_get(url, _ssl_ctx, accept_gzip=True)

        assert status == 200
        encoding = headers.get("Content-Encoding", "")
        # Only assert compression if there's enough data to compress
        if len(raw) > 1024 or encoding == "gzip":
            assert encoding == "gzip", "Expected gzip Content-Encoding"
            body = gzip.decompress(raw)
            cards = json.loads(body)
            assert isinstance(cards, list)
            ratio = len(raw) / len(body) if body else 1
            print(f"\n  gzip: {len(body)} -> {len(raw)} bytes ({ratio:.1%})")

    def test_prices_present(self, base_url, _ssl_ctx):
        """Cards include price fields (verifies bulk lookup works)."""
        url = f"{base_url}/api/collection"
        _, status, body, _ = _timed_get(url, _ssl_ctx)
        cards = json.loads(body)

        assert status == 200
        if not cards:
            pytest.skip("No cards in collection")

        # Every card should have price keys (even if null)
        for card in cards:
            assert "tcg_price" in card, f"Missing tcg_price on {card.get('name')}"
            assert "ck_price" in card, f"Missing ck_price on {card.get('name')}"
            assert "ck_url" in card, f"Missing ck_url on {card.get('name')}"

    def test_response_time_with_search(self, base_url, _ssl_ctx):
        """Search-filtered collection also responds quickly."""
        url = f"{base_url}/api/collection?search=mountain"
        _timed_get(url, _ssl_ctx)  # warm-up
        elapsed_ms, status, body, _ = _timed_get(url, _ssl_ctx)
        cards = json.loads(body)

        assert status == 200
        print(f"\n  /api/collection?search=mountain: {len(cards)} cards in {elapsed_ms:.0f} ms")
        assert elapsed_ms < API_RESPONSE_BUDGET_MS

    def test_multiple_requests_consistent(self, base_url, _ssl_ctx):
        """Five sequential requests to check for consistency and no degradation."""
        url = f"{base_url}/api/collection"
        _timed_get(url, _ssl_ctx)  # warm-up

        times = []
        for _ in range(5):
            elapsed_ms, status, _, _ = _timed_get(url, _ssl_ctx)
            assert status == 200
            times.append(elapsed_ms)

        avg = sum(times) / len(times)
        worst = max(times)
        print(f"\n  5 requests: avg={avg:.0f} ms, worst={worst:.0f} ms, all={[f'{t:.0f}' for t in times]}")
        assert worst < API_RESPONSE_BUDGET_MS, (
            f"Worst request took {worst:.0f} ms (budget: {API_RESPONSE_BUDGET_MS} ms)"
        )
