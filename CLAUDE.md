## Project Overview

MTG Card Collection Builder — Python CLI + web UI for managing Magic: The Gathering collections. Cards enter via Claude Vision (corner photos), OCR (full card photos), manual ID entry, or order ingestion (TCGPlayer/Card Kingdom). Card data is sourced from Scryfall, stored in SQLite. Web UI for collection browsing, virtual booster pack generation, image-based card ingestion, and order import. Import/export to Moxfield, Archidekt, Deckbox.

## Development notes

- **Always use `uv`** for all Python operations (not pip/venv/make): `uv sync`, `uv run pytest`, `uv run ruff check mtg_collector/`, `uv run mtg ...`
- Code, models, deploy scripts, all go in the repo.
- STORE DATA IN THE LOCAL DB. DO NOT add website queries that require the internet at runtime.
- `ruff` is a dev dependency — always available via `uv run ruff`
- **NEVER add fallback logic.** Errors should propagate to the user.
- No fallback content, no silent defaults, no swallowed exceptions.
- As few error paths as possible. Let it crash visibly.
- Tests use pre-populated `tests/fixtures/scryfall-cache.sqlite` for offline testing. Corner identification tests require `ANTHROPIC_API_KEY`.
- Aggressively limit modality. Defaults are good enough for everyone.

## Commands

```bash
uv sync                                                # Install deps
uv run pytest                                          # All tests
uv run ruff check mtg_collector/                       # Lint

mtg setup                                              # Init DB + cache Scryfall + fetch MTGJSON
mtg setup --demo                                       # Full setup + load demo data (~50 cards)
mtg setup --skip-cache --skip-data                     # Fast start if data is already loaded. 
mtg crack-pack-server                                  # Start web UI on port 8080

# Deployment — rootless Podman, per-instance isolation
bash deploy/setup.sh my-feature          # Create instance (auto-port, inherits API key)
bash deploy/setup.sh my-feature --init   # Create instance + initialize data with demo dataset
bash deploy/deploy.sh my-feature         # Rebuild image + restart
bash deploy/teardown.sh my-feature       # Stop + remove (add --purge to delete data)
systemctl --user start mtgc-my-feature   # Start instance
systemctl --user status mtgc-my-feature  # Check status
```

## File Index

Files not listed here are smaller.

### `mtg_collector/cli/` — CLI subcommands

Each module has `register(subparsers)` and `run(args)`.

| File | Lines | Purpose |
|------|------:|---------|
| `crack_pack_server.py` | 4088 | **Web server**: all HTTP routes, API handlers, SSE endpoints |
| `data_cmd.py` | 518 | MTGJSON + price data import/export commands |
| `ingest_ocr.py` | 411 | CLI image-based card ingestion via EasyOCR + Claude |
| `ingest_corners.py` | 340 | CLI corner-photo card ingestion via Claude Vision |
| `ingest_ids.py` | 286 | Manual card entry by rarity/collector-number/set |
| `demo_data.py` | 254 | Load demo collection for testing |

### `mtg_collector/db/` — SQLite layer

| File | Lines | Purpose |
|------|------:|---------|
| `models.py` | 1251 | Dataclasses + repository classes (CRUD for every table) |
| `schema.py` | 1076 | Schema DDL, all migrations, `init_db()` |

Repository classes in `models.py`: `CardRepository`, `SetRepository`, `PrintingRepository`, `CollectionRepository`, `OrderRepository`, `WishlistRepository`.

### `mtg_collector/services/` — External integrations

| File | Lines | Purpose |
|------|------:|---------|
| `agent.py` | 528 | Agentic OCR: Claude tool-use loop with `query_local_db` and `analyze_image` tools |
| `claude.py` | 504 | Claude Vision API: corner reading, card identification |
| `scryfall.py` | 450 | `ScryfallAPI` class, caching, `cache_scryfall_data()`, `ensure_set_cached()` |
| `order_parser.py` | 414 | Parse TCGPlayer HTML/text and Card Kingdom text into `ParsedOrder` |
| `order_resolver.py` | 353 | Resolve parsed orders to Scryfall cards, treatment-aware matching |
| `pack_generator.py` | 329 | MTGJSON-based booster pack simulation from SQLite |

### `mtg_collector/static/` — Web UI (single-file HTML pages)

| File | Lines | Purpose |
|------|------:|---------|
| `collection.html` | 3187 | **Collection browser**: filters, sorting, card grid, inline editing. Canonical card display. |
| `correct.html` | 1048 | Fix misidentified cards in ingest pipeline |
| `crack_pack.html` | 1007 | Booster pack simulator with rarity borders and badge system |
| `explore_sheets.html` | 824 | Browse MTGJSON booster sheet layouts |
| `ingest_ids.html` | 680 | Manual card entry web UI |
| `disambiguate.html` | 634 | Resolve ambiguous Scryfall matches |
| `ingest_corners.html` | 561 | Corner photo ingest web UI |
| `recent.html` | 507 | Recently ingested images gallery |
| `ingest_order.html` | 494 | Order ingestion web UI |
| `import_csv.html` | 492 | CSV import web UI |
| `process.html` | 406 | OCR processing + Claude identification |
| `upload.html` | 395 | Photo upload for image ingest |
| `index.html` | 308 | Homepage / navigation |

### `mtg_collector/importers/` and `exporters/`

| File | Lines | Purpose |
|------|------:|---------|

### Other key files

| File | Lines | Purpose |
|------|------:|---------|
| `mtg_collector.py` | 470 | Legacy entrypoint (predates package structure) |

### Tests

| File | Lines | What it covers |
|------|------:|---------|
| `test_import.py` | 620 | CSV import (Moxfield, Archidekt, Deckbox, decklist) |
| `test_price_import.py` | 526 | MTGJSON price import pipeline |
| `test_mtgjson_import.py` | 516 | MTGJSON AllPrintings import |
| `test_ingest_ids.py` | 423 | Manual card entry + `resolve_and_add_ids()` |
| `test_order_parser.py` | 368 | Order parsing (TCGPlayer HTML/text, Card Kingdom) |
| `test_order_resolver.py` | 302 | Order resolution to Scryfall cards |

## Data Model

### Core join chain

```
cards (oracle_id PK)          — Abstract card identity (name, colors, mana cost)
  └─ printings (scryfall_id PK, FK oracle_id, FK set_code)  — Specific printing (art, rarity, image)
       └─ collection (id PK, FK scryfall_id, FK? order_id)  — One row per physical card owned
            └─ orders (id PK)  — Purchase order (TCGPlayer/CK seller, totals, shipping)

sets (set_code PK)            — Set metadata, cards_fetched_at for cache status
```

This is the fundamental chain: **card** → **printing** → **collection entry** (→ optional **order**).

- `collection_view` (VIEW) denormalizes this entire chain into one queryable view.
- `latest_prices` (VIEW) gives most recent price per card/source/type.

### Key lookups

- Card by name: `cards.name` (indexed)
- Printing by set+CN: `printings(set_code, collector_number)` (unique)
- Collection by Scryfall ID: `collection.scryfall_id` (indexed)
- Prices join to printings via `(set_code, collector_number)` — **not** by scryfall_id

### Price data pipeline

MTGJSON UUIDs → `mtgjson_uuid_map(uuid → set_code, collector_number)` → `prices` table (append-only time series). `latest_prices` view uses global max `observed_at`. Sources: TCGplayer, CardKingdom.

### Other tables

- `wishlist` — FK to `cards` (oracle-level) or `printings` (specific). Priority, max price, fulfilled status.
- `ingest_cache` — Cached OCR + Claude results by image MD5 (avoids reprocessing).
- `ingest_images` — Persistent web UI ingest pipeline state (READY_FOR_OCR → PROCESSING → DONE/ERROR).
- `ingest_lineage` — Maps collection entries back to source images.
- `status_log` — Append-only audit of collection status changes.
- `settings` — Key-value config (e.g. `price_sources`, `image_display`).
- Schema v17 with auto-migrations in `schema.py`.

Default DB location: `~/.mtgc/collection.sqlite` (override: `--db` or `MTGC_DB` env).

### Conventions

- Collection status lifecycle: `owned` → `ordered` → `listed` → `sold` → `removed`
- RARITY_MAP codes: `C` common, `U` uncommon, `R` rare, `M` mythic, `P` promo, `L` land (treated as common), `T` token
- `colors`, `finishes`, `promo_types` columns store JSON arrays as TEXT — use `json.loads()`, not SQL array ops

## Key Patterns

### Card image display

`collection.html` is the canonical reference for how cards are displayed. Card images come from Scryfall CDN via `printings.image_uri`. The `image_display` setting (`crop` or `normal`) controls which Scryfall image size is used.

### Rarity/set border gradients

Cards use CSS custom properties `--rarity-color` and `--set-color` with `linear-gradient(to bottom, ...)`. Shared JS helpers in `crack_pack.html`: `getRarityColor(rarity)`, `getSetColor(cardSetCode, packSetCode)`. `buildCardBadges(card, packSetCode)` generates SF/CK price links, foil/treatment badges. Use these for any new card display.

### Collection page filtering

Only search queries and include-unowned toggle trigger server fetches. All other filters (color, rarity, set, type, finish, status, CMC, date, price) and sorting are applied **client-side** for instant responsiveness.

### Card data access policy

All runtime card lookups MUST use the local database, never the Scryfall API. See `architecture/CARD_DATA_ACCESS.md`. Scryfall API is only used during `mtg setup` / `mtg cache` to populate the local DB.

### Scryfall API rate limiting

100ms between requests (via `ScryfallAPI._rate_limit()`). Bulk caching uses the Scryfall bulk data endpoint (3 HTTP requests total for ~80k cards).

### Web server architecture

`crack_pack_server.py` is a single-file threaded HTTP server (stdlib `http.server`). Routes are dispatched in `do_GET`/`do_POST`/`do_PUT`/`do_DELETE` via URL path matching. SSE for long-running operations (ingest processing). No framework — raw request handling.

### Order ingestion

`order_parser.py` auto-detects format (tcg_html, tcg_text, ck_text) → `ParsedOrder`. `order_resolver.py` maps vendor set names to Scryfall codes via `SET_NAME_MAP` + DB lookup, then resolves to specific printings with treatment-aware matching. Idempotent — duplicate order_number + seller_name skipped.

### Agentic OCR (`services/agent.py`)

Claude tool-use loop with two tools: `query_local_db` (SQL against local SQLite) and `analyze_image` (Claude Vision). Used by the web UI ingest pipeline for card identification from photos.

### Claude API retry behavior

Exponential backoff at 3s, 6s, 12s, 24s intervals. Bails immediately on 400 errors (no retry). See `services/claude.py`.

### Card ingestion via `resolve_and_add_ids()`

Both `ingest-ids` and `ingest-corners` funnel through `resolve_and_add_ids()` in `cli/ingest_ids.py`:
1. Look up printing in local DB, fall back to Scryfall API by set + collector number
2. Cache Scryfall response (card, set, printing) in SQLite
3. Create collection entry with finish, condition, source metadata

## Deployment

Rootless Podman Quadlet. Each instance: separate repo clone, own image (`mtgc:<instance>`), data volume, env file, port. No sudo.

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
