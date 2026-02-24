"""Scryfall API — compatibility aliases for bulk import client.

All runtime card lookups must use the local database directly.
This module re-exports the bulk import client under the legacy name
for use by cache/setup commands (cache_cmd.py, db_cmd.py).
"""

from mtg_collector.services.bulk_import import (  # noqa: F401
    ScryfallBulkClient as ScryfallAPI,
)
from mtg_collector.services.bulk_import import (
    cache_card_data as cache_scryfall_data,
)
from mtg_collector.services.bulk_import import (
    ensure_set_populated as ensure_set_cached,
)

__all__ = ["ScryfallAPI", "cache_scryfall_data", "ensure_set_cached"]
