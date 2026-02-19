# Plan: Price Data in SQLite (Time Series)

## Status: Deferred

## Context

Currently price data comes from MTGJSON's `AllPricesToday.json` flat file, loaded into memory at server startup (~200MB parsed into Python dicts). This has several problems:

- No historical price tracking — only "today's" prices are available
- Large memory footprint; loaded once, never refreshable without server restart
- No way to query price trends, alert on drops, or show sparklines
- Collection API does SQL for card data, then Python dict lookups for prices — queries that should be JOINs are two-phase operations
- Server fails to start if AllPricesToday.json is missing on disk
- No automated refresh — prices go stale unless someone manually clicks the UI button or runs the CLI

## Data Source

**MTGJSON AllPricesToday.json** — the same source used today.

- URL: `https://mtgjson.com/api/v5/AllPricesToday.json.gz` (~60MB gzip → ~500MB JSON)
- Keyed by **MTGJSON UUID** (not Scryfall ID, not set+number)
- Structure: `data[uuid]["paper"][provider]["retail"][price_type][date] → price_string`
- Providers: `"cardkingdom"`, `"tcgplayer"`
- Price types: `"normal"`, `"foil"`
- Contains trailing history: ~7 days of daily prices per card (useful for backfill)
- Updated daily by MTGJSON

### UUID → natural key mapping

AllPricesToday.json is keyed by MTGJSON UUID, but our `printings` table uses `(set_code, collector_number)` as its natural key (with a `UNIQUE` constraint). Both MTGJSON and Scryfall expose set code and collector number for every card, so this is a reliable join key.

The mapping comes from **AllPrintings.json**, which has `setCode` and `number` for every UUID. During price import:

1. Parse AllPrintings.json to build `{uuid → (set_code, number)}` in memory
2. Persist this mapping in a lightweight `mtgjson_uuid_map` table
3. On subsequent imports, if `mtgjson_uuid_map` is populated, skip AllPrintings.json parsing entirely — only AllPricesToday.json is needed

Set code normalization: MTGJSON uses uppercase (`MKM`), our DB uses lowercase (`mkm`). Lowercased during import.

When the mtgjson-import plan (`plans/mtgjson-import.md`) lands, `mtgjson_printings` subsumes `mtgjson_uuid_map` and the mapping table can be dropped.

## Schema

```sql
-- UUID → natural key mapping (built from AllPrintings.json)
CREATE TABLE mtgjson_uuid_map (
    uuid              TEXT PRIMARY KEY,
    set_code          TEXT NOT NULL,       -- lowercase
    collector_number  TEXT NOT NULL
);
CREATE INDEX idx_uuid_map_natural ON mtgjson_uuid_map(set_code, collector_number);

-- Price observations (append-only time series)
CREATE TABLE prices (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    set_code          TEXT NOT NULL,
    collector_number  TEXT NOT NULL,
    source            TEXT NOT NULL,       -- 'tcgplayer', 'cardkingdom'
    price_type        TEXT NOT NULL,       -- 'normal', 'foil'
    price             REAL NOT NULL,
    observed_at       TEXT NOT NULL        -- ISO 8601 date (daily granularity)
);
CREATE INDEX idx_prices_card ON prices(set_code, collector_number, source, price_type);
CREATE INDEX idx_prices_date ON prices(observed_at);
CREATE UNIQUE INDEX idx_prices_unique
    ON prices(set_code, collector_number, source, price_type, observed_at);

-- Materialized latest-date view for fast collection JOINs
CREATE VIEW latest_prices AS
SELECT set_code, collector_number, source, price_type, price, observed_at
FROM prices
WHERE observed_at = (SELECT MAX(observed_at) FROM prices);

-- Import audit log
CREATE TABLE price_fetch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at      TEXT NOT NULL,        -- ISO 8601 timestamp
    source_file     TEXT NOT NULL,        -- 'AllPricesToday.json'
    dates_imported  TEXT NOT NULL,        -- JSON array of date strings found in file
    uuid_total      INTEGER NOT NULL,     -- UUIDs in source file
    uuid_mapped     INTEGER NOT NULL,     -- UUIDs with set+CN mapping
    uuid_unmapped   INTEGER NOT NULL,     -- UUIDs with no mapping (digital-only, etc.)
    rows_inserted   INTEGER NOT NULL      -- price rows added
);
```

### Why natural keys

- `(set_code, collector_number)` is the natural key both MTGJSON and Scryfall share
- Our `printings` table already enforces `UNIQUE(set_code, collector_number)`
- JOINs to `printings` are straightforward — no intermediate mapping at query time
- Prices can exist for cards not yet in our Scryfall cache (decoupled)
- When a Scryfall cache rebuild changes a `scryfall_id` (known to happen across API endpoints), prices are unaffected

### latest_prices view

The `latest_prices` view avoids per-row `MAX(observed_at)` subqueries in collection JOINs. The inner `SELECT MAX(observed_at) FROM prices` is fast — `idx_prices_date` makes it an index-only scan returning a single value, and all prices for a given date are imported atomically so filtering by that date is correct.

## Import Pipeline

### `mtg data import-prices`

New CLI command. Reads AllPricesToday.json (and AllPrintings.json on first run) and writes to SQLite.

```
Step 1: Ensure UUID mapping exists
  - If mtgjson_uuid_map is empty:
      - Require AllPrintings.json on disk
      - Parse it, extract (uuid, setCode, number) for each card
      - INSERT into mtgjson_uuid_map in a single transaction
      - Log: "{N} UUIDs mapped"
  - If mtgjson_uuid_map is populated: skip (use existing mapping)

Step 2: Parse AllPricesToday.json
  - Load JSON, iterate data[uuid]
  - For each uuid, look up (set_code, collector_number) from mtgjson_uuid_map
  - For each (provider, price_type, date → price) tuple, collect a row
  - Skip UUIDs with no mapping (digital-only products, supplemental sets)

Step 3: Write prices in a single transaction
  - BEGIN TRANSACTION
  - INSERT OR IGNORE INTO prices (set_code, collector_number, source, price_type, price, observed_at)
  - UNIQUE constraint makes this idempotent — re-importing the same file is a no-op
  - On any error: ROLLBACK (no partial writes)
  - COMMIT

Step 4: Log the import
  - INSERT into price_fetch_log with counts and metadata

Step 5: Report
  - Print: dates imported, rows inserted, unmapped UUIDs skipped, elapsed time
```

### Backfill

AllPricesToday.json contains ~7 days of trailing history per card. On every import, **all dates** in the file are inserted (not just today). The UNIQUE constraint handles idempotency — running the import daily accumulates a growing time series automatically. No special backfill logic needed.

### `mtg data fetch-prices` (modified)

After downloading and decompressing AllPricesToday.json, automatically run `import-prices`. Same pattern as the existing `fetch` → `import` flow.

### `mtg setup` (modified)

Add a new step after MTGJSON data fetch:

```
Step 3: Fetch MTGJSON data (existing)
Step 4: Import prices          ← NEW
  - Run fetch-prices + import-prices
  - If AllPricesToday.json is already present and fresh (<24h), skip download
```

A fresh instance gets prices in SQLite immediately after setup.

### Server startup changes

Remove the AllPricesToday.json existence check (lines 3112-3116 of `crack_pack_server.py`). The server reads prices from SQLite. If the prices table is empty, price fields are `null` — graceful degradation, not a crash.

AllPrintings.json remains a startup requirement for now (PackGenerator needs it for booster generation). That dependency is removed by the mtgjson-import plan.

## Periodic Updates

### systemd timer (Podman Quadlet deployment)

Daily price refresh via a systemd timer that invokes the CLI inside the running container.

`deploy/setup.sh` generates two additional units per instance:

```ini
# ~/.config/containers/systemd/mtgc-prices-{{INSTANCE}}.service
[Unit]
Description=Fetch and import prices for mtgc-{{INSTANCE}}
Requires=mtgc-{{INSTANCE}}.service
After=mtgc-{{INSTANCE}}.service

[Service]
Type=oneshot
ExecStart=podman exec systemd-mtgc-{{INSTANCE}} mtg data fetch-prices
```

```ini
# ~/.config/containers/systemd/mtgc-prices-{{INSTANCE}}.timer
[Unit]
Description=Daily price update for mtgc-{{INSTANCE}}

[Timer]
OnCalendar=*-*-* 06:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
```

- Runs daily at 06:00 ± 30min (MTGJSON updates overnight US time)
- `Persistent=true` ensures a missed run (e.g., machine was off) fires on next boot
- `Requires=` ensures the timer doesn't fire if the container isn't running
- `deploy/teardown.sh` removes both units alongside the container

### `/api/fetch-prices` endpoint (modified)

Currently downloads JSON and reloads into memory. After migration:

1. Download AllPricesToday.json.gz
2. Decompress
3. Run import-prices pipeline (parse → transaction → insert)
4. Return `{last_import: "...", rows_inserted: N, stale: false}`

No in-memory reload needed — SQLite readers see new data immediately.

### Freshness monitoring

New endpoint: `/api/price-status`

```json
{
  "last_import": "2026-02-18T06:12:00Z",
  "latest_price_date": "2026-02-17",
  "total_rows": 1250000,
  "stale": false
}
```

`stale` is `true` if `latest_price_date` is more than 48 hours old. The web UI can show a warning banner when prices are stale.

## Collection API Integration

Once prices are in SQLite, the collection API uses `latest_prices` view in JOINs:

```sql
SELECT ...,
    lp_ck.price AS ck_price,
    lp_tcg.price AS tcg_price
FROM collection c
JOIN printings p ON c.scryfall_id = p.scryfall_id
JOIN cards card ON p.oracle_id = card.oracle_id
JOIN sets s ON p.set_code = s.set_code
LEFT JOIN latest_prices lp_ck
    ON p.set_code = lp_ck.set_code
    AND p.collector_number = lp_ck.collector_number
    AND lp_ck.source = 'cardkingdom'
    AND lp_ck.price_type = CASE WHEN c.finish IN ('foil','etched') THEN 'foil' ELSE 'normal' END
LEFT JOIN latest_prices lp_tcg
    ON p.set_code = lp_tcg.set_code
    AND p.collector_number = lp_tcg.collector_number
    AND lp_tcg.source = 'tcgplayer'
    AND lp_tcg.price_type = CASE WHEN c.finish IN ('foil','etched') THEN 'foil' ELSE 'normal' END
```

No Python loops. No in-memory dicts. Price data joins through the natural key that both systems share.

## API Changes

- `/api/fetch-prices` POST → downloads, imports to SQLite, returns import stats
- `/api/price-status` GET → freshness info from `price_fetch_log`
- `/api/price-history/{set_code}/{collector_number}` GET → time series for sparklines
- Collection API response includes prices via SQL JOINs (unchanged response shape)

### Scryfall price fetch path

Pack generation currently uses a separate Scryfall `/cards/collection` API call for fresh TCG prices on generated packs (`_fetch_prices` in crack_pack_server.py). This is independent of MTGJSON prices and stays as-is — packs get the freshest available price, collection view uses the daily MTGJSON snapshot.

## Validation Plan

### Import-time checks (automated, every import)

Run automatically at the end of `import-prices`. Logged to `price_fetch_log` and printed to stdout.

1. **Row count sanity**: assert `rows_inserted > 0` on a fresh import (not a re-import of same data)
2. **Mapping coverage**: log `uuid_mapped / uuid_total` ratio. Expected: ~90%+ (the gap is digital-only products like MTGA-exclusive sets). Alert if ratio drops below 80% — likely a mapping staleness issue.
3. **Date sanity**: assert all `observed_at` dates in the inserted batch are within the last 14 days. Flags corrupt or wildly stale source data.
4. **Transaction integrity**: entire import is a single transaction. Any failure → full rollback, zero partial rows. The `price_fetch_log` entry is written inside the same transaction, so a missing log entry means no data landed.

### Spot-check command: `mtg data check-prices`

New CLI command for manual verification. Picks N random cards from the collection and cross-checks SQLite against the source.

```
$ mtg data check-prices --sample 10

Spot-checking 10 random collection cards against AllPricesToday.json...

  Lightning Bolt (lea/232)
    tcg normal: SQLite=$245.00  JSON=$245.00  ✓
    ck  normal: SQLite=$299.99  JSON=$299.99  ✓
  Counterspell (tmp/57)
    tcg normal: SQLite=$1.50    JSON=$1.50    ✓
    ck  normal: SQLite=$1.79    JSON=$1.79    ✓
  ...

10/10 cards match. 40/40 price points verified.
```

Steps:
1. Pick N random rows from `collection` table
2. Look up their `(set_code, collector_number)` in `prices` table (latest date)
3. Look up the same card in AllPricesToday.json via UUID (using `mtgjson_uuid_map`)
4. Compare values. Report mismatches with both values.

This lets an auditor verify the import pipeline end-to-end with one command.

### Freshness check: `/api/price-status`

Described above. Answers "when were prices last imported?" and "are they stale?" Machine-readable for monitoring; human-readable for the web UI.

### Unit tests

Test fixtures: a small JSON file with 5-10 cards covering edge cases.

1. **Happy path**: import fixture → verify correct rows in `prices` table, correct `price_fetch_log` entry
2. **Idempotency**: import same fixture twice → same row count (UNIQUE constraint, INSERT OR IGNORE)
3. **Backfill**: fixture with 3 dates per card → all 3 dates inserted
4. **Unmapped UUIDs**: fixture includes a UUID not in `mtgjson_uuid_map` → skipped, counted in log
5. **Transaction rollback**: simulate error mid-import (e.g., corrupt price value) → zero rows inserted, no log entry
6. **UUID map build**: from a small AllPrintings.json fixture → correct `mtgjson_uuid_map` rows, set codes lowercased
7. **latest_prices view**: after importing multiple dates, view returns only the latest date's rows
8. **Collection JOIN**: with prices imported, collection API returns `tcg_price`/`ck_price` (integration test using test fixture DB)

### Integration test

End-to-end from fixture JSON files through import to collection API response:

1. Create test DB with a known collection (reuse existing `tests/fixtures/scryfall-cache.sqlite` pattern)
2. Import price fixture
3. Hit collection API
4. Assert `tcg_price` and `ck_price` values match fixture data

## Data Separation

```
External reference data (replaceable, re-importable):
├── mtgjson_uuid_map   — UUID → (set_code, collector_number) mapping
├── prices             — daily price observations (time series)
└── price_fetch_log    — import audit trail

Scryfall cache (on-demand, append-only):
├── cards      — oracle-level identity
├── sets       — set metadata
└── printings  — printing details + raw_json

User data (precious, never auto-modified):
├── collection      — owned cards
├── orders          — purchase history
├── ingest_cache    — OCR/Claude results
├── ingest_lineage  — provenance tracking
└── settings        — preferences
```

External reference tables can be dropped and re-imported at any time with no data loss. Price history accumulates over time but is always rebuildable from MTGJSON source files.

## Migration Path

### Phase 1: Schema + import (additive, nothing breaks)

1. Add `mtgjson_uuid_map`, `prices`, `price_fetch_log` tables and `latest_prices` view to schema.py (schema version bump)
2. Implement `mtg data import-prices` command
3. Implement `mtg data check-prices` command
4. Update `mtg data fetch-prices` to auto-import after download
5. Add price import step to `mtg setup`

### Phase 2: Server migration (swap reads from JSON to SQLite)

6. Add `/api/price-status` endpoint
7. Update `/api/fetch-prices` to run import pipeline instead of reloading JSON
8. Update collection API to use `latest_prices` JOIN instead of Python dict lookups
9. Update sheet/explore price attachment to query `prices` table
10. Remove AllPricesToday.json startup check from `crack_pack_server.py`

### Phase 3: Cleanup

11. Remove `_prices_data`, `_prices_lock`, `_load_prices()`, `_get_local_price()`, `_get_ck_price()`, `_get_tcg_price()`, `_download_prices()`
12. Remove `get_allpricestoday_path()` usage from server (keep in data_cmd for download)

### Phase 4: Periodic updates (deployment)

13. Add timer/service templates to `deploy/`
14. Update `deploy/setup.sh` to generate timer units
15. Update `deploy/teardown.sh` to remove timer units

## Related: MTGJSON Card Data Import

See `plans/mtgjson-import.md`. That companion effort migrates AllPrintings.json into SQLite (mtgjson_printings, booster sheets/configs), eliminating the ~1.5GB in-memory load. When it lands, `mtgjson_printings` subsumes `mtgjson_uuid_map` — the mapping table can be dropped and price import JOINs directly to `mtgjson_printings.uuid`.

## Open Questions

- **Retention policy**: keep all daily observations forever, or roll up to weekly after 90 days? Daily granularity at ~80k cards × 2 providers × 2 finishes = ~320k rows/day, ~117M rows/year. SQLite handles this fine but disk grows ~5GB/year. Rolling up old data to weekly would cap growth.
- **Sparkline UI scope**: the `/api/price-history` endpoint is defined but the UI sparkline rendering is out of scope for the initial implementation. Ship the endpoint, build the UI later.
- **MTGJSON vs Scryfall prices**: MTGJSON provides both TCGPlayer and Card Kingdom prices. Scryfall provides only TCGPlayer. Sticking with MTGJSON for the time series (richer data) while keeping the Scryfall per-pack fresh lookup for generated packs.
