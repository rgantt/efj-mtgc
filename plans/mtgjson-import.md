# Plan: Import MTGJSON Card Data into SQLite

## Status: Deferred

## Context

AllPrintings.json (~1.5GB) is loaded entirely into memory by `PackGenerator` and scanned to build ad-hoc indexes on first use. Every new lookup pattern means another full scan of the dataset:

- `_card_indexes`: per-set `{uuid → card}` dict for booster generation
- `_scryfall_to_uuid`: global `{scryfall_id → uuid}` map (scans all sets)
- `_scryfall_to_card`: global `{scryfall_id → card}` map (for CK URLs via `purchaseUrls`)

This is slow at startup, wastes memory, and couples booster generation to having the entire JSON resident. Queries that should be indexed lookups are linear scans of nested dicts.

## Current Usage

### Pack generation (`PackGenerator.generate_pack`)

1. Reads `data["data"][set_code]["booster"]` for sheet definitions and variant weights
2. Picks a variant by weighted random selection
3. Builds card index from main set + source sets (`sourceSetCodes`)
4. Draws from sheets: each sheet is `{uuid: weight}`, sampled without replacement
5. Looks up card details (name, rarity, scryfall image URI, CK URL) from the index

### Sheets/explore (`get_sheet_data`)

Returns full booster structure with per-card pull rates, variant probabilities, prices, and CK URLs. Same in-memory indexes.

### CK URL lookup (`get_ck_url`)

Used by collection API and pack views. Builds `_scryfall_to_card` index, then reads `card["purchaseUrls"]["cardKingdom"]`.

### UUID mapping (`get_uuid_for_scryfall_id`)

Maps Scryfall IDs to MTGJSON UUIDs for price lookups in AllPricesToday.json.

## Proposed Architecture

### New tables

```sql
-- One row per MTGJSON card printing (immutable reference data)
CREATE TABLE mtgjson_printings (
    uuid            TEXT PRIMARY KEY,   -- MTGJSON UUID
    scryfall_id     TEXT,               -- links to printings.scryfall_id
    name            TEXT NOT NULL,
    set_code        TEXT NOT NULL,
    number          TEXT NOT NULL,      -- collector number
    rarity          TEXT,
    border_color    TEXT,
    is_full_art     INTEGER DEFAULT 0,
    frame_effects   TEXT,               -- JSON array
    ck_url          TEXT,               -- purchaseUrls.cardKingdom
    ck_url_foil     TEXT,               -- purchaseUrls.cardKingdomFoil
    tcg_url         TEXT,               -- purchaseUrls.tcgplayer
    imported_at     TEXT NOT NULL        -- ISO 8601
);
CREATE INDEX idx_mtgjson_scryfall ON mtgjson_printings(scryfall_id);
CREATE INDEX idx_mtgjson_set ON mtgjson_printings(set_code);

-- Booster sheet card pools with weights
CREATE TABLE mtgjson_booster_sheets (
    id          INTEGER PRIMARY KEY,
    set_code    TEXT NOT NULL,
    product     TEXT NOT NULL,       -- "play", "draft", "collector", etc.
    sheet_name  TEXT NOT NULL,       -- "common", "uncommon", "rareMythic", etc.
    is_foil     INTEGER DEFAULT 0,
    uuid        TEXT NOT NULL,       -- MTGJSON card UUID
    weight      INTEGER NOT NULL,    -- card weight within this sheet
    FOREIGN KEY (uuid) REFERENCES mtgjson_printings(uuid)
);
CREATE INDEX idx_booster_sheet_lookup ON mtgjson_booster_sheets(set_code, product, sheet_name);

-- Booster variant definitions (which sheets, how many cards per sheet)
CREATE TABLE mtgjson_booster_configs (
    id              INTEGER PRIMARY KEY,
    set_code        TEXT NOT NULL,
    product         TEXT NOT NULL,
    variant_weight  INTEGER NOT NULL,   -- weight for picking this variant
    sheet_name      TEXT NOT NULL,       -- which sheet to draw from
    card_count      INTEGER NOT NULL,   -- how many cards from this sheet
    source_set      TEXT                -- from sourceSetCodes (NULL = same set)
);
CREATE INDEX idx_config_set_product ON mtgjson_booster_configs(set_code, product);
```

### New command: `mtg data import`

Run after `mtg data fetch`. Reads AllPrintings.json and populates the three tables.

- Drops and recreates all three tables (idempotent full replace)
- Iterates `data["data"]` by set code, importing cards and booster specs
- Reports progress: set count, card count, time elapsed
- `mtg data fetch` auto-runs import after download

### PackGenerator migration

Replace in-memory indexes with SQL queries:

```python
# Current: load entire JSON, build dict, weighted sample
cards = self._data["data"][set_code]["booster"]["sheets"][sheet_name]
random.choices(list(cards.keys()), weights=list(cards.values()), k=count)

# After: query sheet weights, sample in Python, join for card details
SELECT uuid, weight FROM mtgjson_booster_sheets
WHERE set_code = ? AND product = ? AND sheet_name = ?

SELECT mp.*, p.image_uris FROM mtgjson_printings mp
LEFT JOIN printings p ON mp.scryfall_id = p.scryfall_id
WHERE mp.uuid IN (?)
```

Weighted random selection stays in Python (SQLite doesn't have `ORDER BY RANDOM() * weight` that's worth using). The key win is not loading 1.5GB to get a few hundred rows.

### CK URL migration

```python
# Current: build _scryfall_to_card scanning all sets, read purchaseUrls
card = self._scryfall_to_card[scryfall_id]
return card["purchaseUrls"]["cardKingdom"]

# After: indexed lookup
SELECT ck_url, ck_url_foil FROM mtgjson_printings WHERE scryfall_id = ?
```

### UUID mapping migration

```python
# Current: build _scryfall_to_uuid scanning all sets
uuid = self._scryfall_to_uuid[scryfall_id]

# After: indexed lookup
SELECT uuid FROM mtgjson_printings WHERE scryfall_id = ?
```

This also simplifies the price time series work (see `plans/price-time-series.md`) — prices keyed by UUID can JOIN directly to `mtgjson_printings` instead of building an in-memory mapping.

## Data separation

These are external reference tables — replaceable, re-importable at any time:

```
External reference data (drop-and-reimport safe):
├── mtgjson_printings      — card metadata from MTGJSON
├── mtgjson_booster_sheets — booster sheet card pools + weights
└── mtgjson_booster_configs — booster variant structure
```

Scryfall-sourced tables (`cards`, `sets`, `printings`) and user data (`collection`, `orders`, etc.) are untouched by import.

## Migration path

1. Add tables to schema.py (schema version bump)
2. Implement `mtg data import` command
3. Update `mtg data fetch` to auto-import after download
4. Update `mtg setup` to run import as part of setup flow
5. Migrate PackGenerator to SQL queries (generate_pack, get_sheet_data)
6. Migrate CK URL lookups to SQL
7. Migrate UUID mapping to SQL
8. Remove in-memory index code (`_card_indexes`, `_scryfall_to_uuid`, `_scryfall_to_card`, `_data`)
9. AllPrintings.json no longer needed at runtime — can keep on disk for re-import or delete

Steps 1–4 are additive (nothing breaks). Steps 5–8 can land together. Step 9 is cleanup.

## Open Questions

- **Stream the JSON import?** `json.load()` on 1.5GB will spike memory during import. Could use `ijson` for streaming, but adds a dependency. Acceptable since import is a one-time CLI operation, not a per-request cost.
- **Delete JSON after import?** Keeping it allows re-import without re-download (~1.5GB disk). Could offer `--keep-json` flag.
- **Merge `mtgjson_printings` into `printings`?** They share `scryfall_id` and overlap on some fields (name, set, rarity). Merging simplifies JOINs but mixes Scryfall-sourced and MTGJSON-sourced data. Keeping them separate lets either be rebuilt independently.
