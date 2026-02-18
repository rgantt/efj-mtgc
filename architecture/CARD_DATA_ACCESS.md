# Card Data Access Policy

## Rule: No runtime Scryfall API calls for card data

All card data at runtime MUST come from the local SQLite database. The Scryfall
API is only accessed during explicit cache-population operations:

- `mtg cache all` -- bulk download of entire Scryfall dataset
- `ensure_set_cached()` -- on-demand caching of a single set's card list

**Every other code path** (ingestors, collection browsing, pack generation,
exports, etc.) reads exclusively from the local DB via repository methods:

- `PrintingRepository.get(scryfall_id)` -- lookup by Scryfall ID
- `PrintingRepository.get_by_set_cn(set_code, cn)` -- lookup by set + collector number
- `CardRepository.search_by_name(name)` / `search_cards_by_name(name)` -- name search
- `PrintingRepository.get_by_oracle_id(oracle_id)` -- all printings of a card
- `Printing.get_scryfall_data()` -- full cached Scryfall response from `raw_json`

## Why

Scryfall's different API endpoints (search, direct lookup, bulk data) can return
**different UUIDs** for the same physical card. Mixing data from different endpoints
at runtime causes `UNIQUE constraint` violations on `printings(set_code, collector_number)`
because `ON CONFLICT(scryfall_id)` only handles the primary key.

Using a single data source (bulk cache) guarantees consistent IDs throughout the system.

## How to add a new feature that needs card data

1. Assume the local DB is already populated (user ran `mtg setup` or `mtg cache all`)
2. Use repository methods to query -- never instantiate `ScryfallAPI` for lookups
3. If the data isn't cached, return empty results -- don't fall back to the API
4. If you need data that isn't in the current schema, add it to the bulk cache pipeline
