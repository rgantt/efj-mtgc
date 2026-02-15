# Plan: Price Data in SQLite (Time Series)

## Status: Deferred

## Context

Currently price data comes from MTGJSON's `AllPricesToday.json` flat file, loaded into memory with a 24-hour TTL. This works but has limitations:

- No historical price tracking — only "today's" prices are available
- Large memory footprint (~200MB JSON parsed into Python dicts)
- No way to query price trends, alert on drops, or show sparklines
- Cache invalidation is all-or-nothing (24h TTL on entire dataset)

## Proposed Architecture

### New tables

```sql
-- Price observations (append-only time series)
CREATE TABLE prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id TEXT NOT NULL REFERENCES printings(scryfall_id),
    source TEXT NOT NULL,        -- 'tcgplayer', 'cardkingdom'
    price_type TEXT NOT NULL,    -- 'normal', 'foil'
    price REAL NOT NULL,
    observed_at TEXT NOT NULL    -- ISO 8601 date (daily granularity)
);
CREATE INDEX idx_prices_card ON prices(scryfall_id, source, price_type);
CREATE INDEX idx_prices_date ON prices(observed_at);
CREATE UNIQUE INDEX idx_prices_unique ON prices(scryfall_id, source, price_type, observed_at);

-- Price fetch metadata
CREATE TABLE price_fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    cards_updated INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);
```

### Data flow

1. `mtg data fetch-prices` downloads MTGJSON AllPricesToday.json (same as today)
2. New step: parse JSON and INSERT INTO prices with today's date
3. UNIQUE constraint on (scryfall_id, source, price_type, observed_at) makes it idempotent
4. Old flat-file approach remains as fallback during migration

### API changes

- `/api/fetch-prices` POST → reads from SQLite instead of flat file
- New `/api/price-history/{scryfall_id}` → returns time series for sparklines
- Collection API response includes latest price from SQLite

### Migration path

1. Add tables in schema v10
2. Backfill from existing AllPricesToday.json if present
3. Update `fetch-prices` to write to SQLite
4. Update web UI price display to read from SQLite
5. Eventually remove flat-file dependency

## Open Questions

- Retention policy: keep all daily observations forever, or roll up to weekly after 90 days?
- Should price alerts be a settings-based feature, or just provide the data for manual checking?
- Should we continue using MTGJSON or switch to Scryfall prices directly (simpler, but less granular)?
