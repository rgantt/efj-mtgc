# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Card Collection Builder - A Python CLI + web UI tool for managing Magic: The Gathering collections. Cards can be identified via Claude Vision (corner photos), local OCR (full card photos), manual ID entry, or order ingestion (TCGPlayer/Card Kingdom). Card data is sourced from Scryfall and stored in SQLite. Includes a web UI for collection browsing, virtual booster pack generation, image-based card ingestion, and order import. Supports import/export to Moxfield, Archidekt, and Deckbox formats.

## Environment
- **Always use `uv`** for all Python operations (not pip/venv/make). Examples:
  - `uv sync` to install deps
  - `uv run pytest ...` to run tests
  - `uv run ruff check mtg_collector/` to lint
  - `uv run mtg ...` to run CLI
- `ruff` is a dev dependency — always available via `uv run ruff`

## Error Handling Philosophy
- **NEVER add fallback logic.** Errors should propagate to the user.
- No fallback content, no silent defaults, no swallowed exceptions.
- As few error paths as possible. Let it crash visibly.

## Commands

```bash
# Setup
uv sync

# Run tests (corner identification tests require ANTHROPIC_API_KEY)
uv run pytest                                # All tests
uv run pytest tests/test_ingest_ids.py -v    # Single test file
uv run pytest tests/test_ingest_ids.py::TestResolveAndAddIds::test_single_card_happy_path -v  # Single test

# Linting
uv run ruff check mtg_collector/

# CLI usage
mtg setup                                              # Initialize DB + cache Scryfall + fetch MTGJSON
mtg setup --demo                                       # Full setup + load demo data (~50 cards)
mtg setup --skip-cache --skip-data                     # DB init only (no network)
mtg db init                                            # Initialize database
mtg ingest-ids --id R 0200 EOE --id C 0075 EOE foil   # Add cards by rarity/CN/set
mtg ingest-corners photo.jpg                           # Read card corners via Claude Vision
mtg ingest-order order.html                            # Import TCGPlayer/CK orders
mtg orders list                                        # List imported orders
mtg cache all                                          # Bulk-cache all Scryfall data
mtg list                                               # List collection
mtg export -f moxfield -o out.csv
mtg crack-pack-server                                  # Start web UI on port 8080

# Deployment — rootless Podman, per-instance isolation
bash deploy/setup.sh my-feature          # Create instance (auto-port, inherits API key)
bash deploy/setup.sh my-feature --init   # Create instance + initialize data with demo dataset
bash deploy/deploy.sh my-feature         # Rebuild image + restart
bash deploy/teardown.sh my-feature       # Stop + remove (add --purge to delete data)
systemctl --user start mtgc-my-feature   # Start instance
systemctl --user status mtgc-my-feature  # Check status
```

## Architecture

See `architecture/CARD_DATA_ACCESS.md` for the card data access policy -- all runtime
card lookups MUST use the local database, never the Scryfall API.

```
mtg_collector/
├── cli/           # Subcommands, each with register(subparsers) and run(args)
│                  #   setup_cmd, ingest_ids, ingest_corners, ingest_ocr, ingest_order,
│                  #   ingest_retry, orders, import_cmd, export, list_cmd, show, edit,
│                  #   delete, stats, db_cmd, cache_cmd, data_cmd, demo_data,
│                  #   crack_pack, crack_pack_server, wishlist
├── db/            # SQLite layer (connection.py, schema.py, models.py with repositories)
├── services/      # claude.py (Vision API), scryfall.py (card data + caching),
│                  #   ocr.py (EasyOCR), pack_generator.py (MTGJSON booster sim),
│                  #   order_parser.py, order_resolver.py
├── static/        # Web UI: collection.html, crack_pack.html, explore_sheets.html,
│                  #   ingest_order.html, upload.html, recent.html, process.html,
│                  #   disambiguate.html, correct.html, index.html
├── importers/     # CSV parsers for moxfield, archidekt, deckbox
└── exporters/     # CSV writers for moxfield, archidekt, deckbox
```

## Database Schema

Schema version tracked in `schema_version` table with auto-migrations (current: v15).

Core tables with foreign key relationships:
- `cards` (oracle_id PK) → Oracle-level card identity
- `sets` (set_code PK) → Set info + `cards_fetched_at` for cache status
- `printings` (scryfall_id PK) → Specific printings, FK to cards and sets
- `orders` (id PK) → Purchase orders from TCGPlayer/Card Kingdom with seller, totals, shipping status
- `collection` (id PK) → Owned cards, FK to printings and optionally orders (one row per physical card). Status lifecycle: owned/ordered/listed/sold/removed
- `wishlist` (id PK) → Cards user wants, FK to cards (oracle-level) or printings (specific)
- `mtgjson_uuid_map` (uuid PK) → Maps MTGJSON UUIDs to (set_code, collector_number) for price lookups
- `prices` → Append-only price time series (set_code, collector_number, source, price_type, price, observed_at)
- `price_fetch_log` → Audit trail of price imports with stats
- `latest_prices` (VIEW) → Most recent prices per card/source/type (global max observed_at)
- `status_log` → Append-only audit trail of collection status changes
- `ingest_cache` (image_md5 PK) → Cached OCR + Claude results to avoid reprocessing
- `ingest_lineage` → Tracks which collection entry came from which image
- `settings` (key PK) → Global key-value config (e.g. price_sources, image_display)

Default location: `~/.mtgc/collection.sqlite` (override with `--db` or `MTGC_DB` env)

## Data Flow: Card Ingestion

Four ingestion methods:

1. **ingest-ids**: User provides rarity code, collector number, set code, and optional foil flag directly
2. **ingest-corners**: Claude Vision reads card corner text (rarity/CN/set/foil) from photos
3. **ingest-ocr** (CLI) / **ingest2** (web UI): EasyOCR extracts text, Claude identifies card names, Scryfall resolves. Web UI adds multi-step workflow: upload → process → disambiguate → correct → confirm
4. **ingest-order**: Parses TCGPlayer HTML/text or Card Kingdom text orders, resolves items to Scryfall cards with treatment-aware matching (borderless, extended art, showcase), creates collection entries linked to order records

Methods 1 and 2 feed into `resolve_and_add_ids()` which:
1. Looks up printing in local cache, falls back to Scryfall API by set+collector number
2. Caches Scryfall data (card, set, printing) in SQLite
3. Creates collection entry with finish, condition, source metadata

Method 4 uses `order_parser.py` → `order_resolver.py` → `commit_orders()`:
1. Auto-detects format (tcg_html, tcg_text, ck_text) and parses into `ParsedOrder` objects
2. Maps vendor set names to Scryfall set codes (hardcoded map + DB lookup)
3. Resolves each item to a Scryfall card, matching treatment to correct printing variant
4. Creates order record + collection entries with `status='ordered'` and `order_id` FK
5. Idempotent — duplicate order_number + seller_name combinations are skipped

## Web UI (crack_pack_server.py)

Threaded HTTP server serving static HTML pages and JSON APIs. Start with `mtg crack-pack-server`.

Key pages: `/collection` (browse/filter/manage collection), `/crack` (booster pack simulator), `/sheets` (explore booster sheet layouts), `/ingestor-order` (order ingestion from TCGPlayer/Card Kingdom).

Ingest2 pages (image-based card ingestion pipeline): `/upload` (photo upload), `/recent` (recently ingested images), `/process` (OCR + Claude processing), `/disambiguate` (resolve ambiguous Scryfall matches), `/correct` (fix misidentified cards).

Collection page filtering architecture: only search queries and include-unowned toggle trigger server fetches. All other filters (color, rarity, set, type, finish, status, CMC, date, price) and sorting are applied client-side for instant responsiveness.

Key API patterns:
- `/api/collection?[filters]` — aggregated collection with server-side search, include_unowned mode; response enriched with order data (seller, order number) via JOIN
- `/api/cached-sets` — all sets with cached card lists (for set filter dropdown)
- `/api/set-browse/{set_code}` — all printings in a set with owned/wanted annotations
- `/api/fetch-prices` (POST) — batch price lookup from Scryfall
- `/api/ingest2/*` — DB-backed OCR ingestion pipeline (upload, process via SSE, confirm/skip/correct/disambiguate)
- `/api/order/*` — order parse/resolve/commit pipeline
- `/api/orders` — list orders, show order cards, receive (batch flip ordered→owned)

## Key Implementation Details

- Scryfall API rate limited to 100ms between requests (via `_rate_limit()`)
- Claude API retries with exponential backoff (3s, 6s, 12s, 24s) but bails immediately on 400 errors
- JSON arrays stored as TEXT in SQLite (colors, finishes, promo_types)
- RARITY_MAP: C (common), U (uncommon), R (rare), M (mythic), P (promo), L (land, treated as common), T (token)
- Tests use a pre-populated `tests/fixtures/scryfall-cache.sqlite` for offline testing
- `mtg cache all` uses Scryfall bulk data endpoint (3 HTTP requests total) to cache all ~80k cards
- Price data comes from MTGJSON AllPricesToday.json (TCGplayer + CardKingdom), imported into SQLite `prices` table as time series. `mtg data import-prices` for manual import, `mtg data fetch-prices` auto-imports after download
- Order resolver uses `SET_NAME_MAP` for vendor→Scryfall set code mapping (e.g. "FINAL FANTASY" → "fin")

## Deployment

Rootless Podman Quadlet. Each instance is a separate repo clone with its own image (`mtgc:<instance>`), data volume, env file, and port. No sudo required. See `deploy/README.md` for full docs.

Key files: `Containerfile` (multi-stage build), `deploy/setup.sh`, `deploy/deploy.sh`, `deploy/teardown.sh`, `deploy/mtgc.container` (Quadlet template with `{{INSTANCE}}`/`{{PORT}}` placeholders).

- `~/.config/mtgc/default.env` has the shared API key; setup.sh copies it to new instances automatically
- `~/.config/mtgc/<instance>.env` — per-instance env file
- `~/.config/containers/systemd/mtgc-<instance>.container` — generated Quadlet unit
- Service name: `mtgc-<instance>`, container name: `systemd-mtgc-<instance>`
- Server checks `ANTHROPIC_API_KEY` at startup and fails fast if missing
- CI: push to main → auto-deploys `prod` at `/opt/mtgc-prod/`. Workflow dispatch for other instances.
- Deploy repo (private CI config): `rgantt/efj-mtgc-deploy`

## Container Validation

**Always validate new features in isolated containers before creating PRs.** This uses the standard deployment scripts with demo data pre-loaded. Do not run the application locally or use `mtg` commands directly on the host.

### Setup

From the repo clone with your feature branch checked out:

```bash
bash deploy/setup.sh <instance> --init     # Build image + initialize data volume (~15-30 min first time)
systemctl --user start mtgc-<instance>     # Start the service
sleep 5                                     # Wait for server startup
```

Discover the assigned port:

```bash
podman port systemd-mtgc-<instance> 8081/tcp
```

### Validate

The server uses HTTPS with a self-signed cert. Use `curl -ks` for all requests.

```bash
PORT=$(podman port systemd-mtgc-<instance> 8081/tcp | grep -oP ':\K[0-9]+' | head -1)

# 1. Verify new page loads (HTTP 200 + non-empty body)
curl -ks -o /dev/null -w "%{http_code} %{size_download}" "https://localhost:${PORT}/<your-page>"

# 2. Verify nav link exists on homepage
curl -ks "https://localhost:${PORT}/" | grep -o 'href="/<your-page>"'

# 3. Test API endpoints with edge-case inputs (empty body, missing fields)
curl -ks -X POST "https://localhost:${PORT}/api/<your-endpoint>" \
  -H "Content-Type: application/json" -d '{}'
```

Check logs if anything fails:

```bash
journalctl --user -u mtgc-<instance> -f
```

### Teardown

```bash
bash deploy/teardown.sh <instance> --purge   # Stop + remove container, volume, env, and image
```

### Notes

- Instance name should match your branch/feature (e.g., `issue44`, `my-feature`)
- `--init` runs `mtg setup --demo` inside the volume: initializes the DB, caches Scryfall data, downloads MTGJSON data files (AllPrintings.json + AllPricesToday.json), and loads ~50 demo cards
- Data persists on the volume across container restarts. Only `--purge` removes it.
- If the Scryfall bulk cache step fails (FK constraint at ~75k cards), the remaining steps still complete and the server will start. Demo data may be partial.

## Web UI Shared Conventions (crack_pack.html)

- **Rarity/set border gradients**: Cards use CSS custom properties `--rarity-color` and `--set-color` with `linear-gradient(to bottom, ...)` to show rarity (top) and guest-set status (bottom). Shared JS helpers `getRarityColor(rarity)` and `getSetColor(cardSetCode, packSetCode)` return the colors. Use these for any new card display (lists, grids, etc.).
- **Badge builder**: `buildCardBadges(card, packSetCode)` returns HTML for SF/CK links with prices, foil, and treatment badges. `buildBadges(card, packSetCode)` wraps it with a zoom badge for the pack grid.
