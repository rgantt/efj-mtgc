## Project Overview

MTG Card Collection Builder — Python CLI + web UI for managing Magic: The Gathering collections. Cards enter via Claude Vision (corner photos), OCR (full card photos), manual ID entry, or order ingestion (TCGPlayer/Card Kingdom). Card data lives in a local SQLite database (populated via `mtg setup` / `mtg cache all` from Scryfall bulk data). All runtime lookups use the local DB — no network calls. Web UI for collection browsing, virtual booster pack generation, image-based card ingestion, and order import. Import/export to Moxfield, Archidekt, Deckbox.

## Development notes

- **Always use `uv`** for all Python operations (not pip/venv/make): `uv sync`, `uv run pytest`, `uv run ruff check mtg_collector/`, `uv run mtg ...`
- Code, models, deploy scripts, all go in the repo.
- STORE DATA IN THE LOCAL DB. DO NOT add website queries that require the internet at runtime.
- `ruff` is a dev dependency — always available via `uv run ruff`
- **NEVER add fallback logic.** Errors should propagate to the user.
- No fallback content, no silent defaults, no swallowed exceptions.
- As few error paths as possible. Let it crash visibly.
- Tests use pre-populated `tests/fixtures/test-cards.sqlite` for offline testing. Corner identification tests require `ANTHROPIC_API_KEY`.
- Aggressively limit modality. Defaults are good enough for everyone.

## Commands

```bash
uv sync                                                # Install deps
uv run pytest                                          # All tests
uv run ruff check mtg_collector/                       # Lint
uv run shot-scraper install                            # One-time: install Chromium for Playwright

# UI scenario tests (requires running container instance + ANTHROPIC_API_KEY)
uv run pytest tests/ui/ -v --instance <instance>       # Run all UI scenarios

mtg setup                                              # Init DB + cache Scryfall + fetch MTGJSON
mtg setup --demo                                       # Full setup + load demo data (~50 cards)
mtg setup --demo --from-fixture tests/fixtures/test-data.sqlite  # Fast setup from pre-built fixture
mtg setup --skip-cache --skip-data                     # Fast start if data is already loaded.
mtg crack-pack-server                                  # Start web UI on port 8080

# Deployment — rootless Podman, per-instance isolation
bash deploy/seed.sh                      # One-time: create reusable seed data volume (~15-30 min)
bash deploy/seed.sh --force              # Recreate seed volume (after schema changes)
bash deploy/setup.sh my-feature --init   # Create instance + clone seed volume (~seconds)
bash deploy/setup.sh my-feature --test   # Fast setup from pre-built fixture (~seconds, no network)
bash deploy/setup.sh my-feature          # Create instance without data (auto-port, inherits API key)
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
| `crack_pack_server.py` | 4692 | **Web server**: all HTTP routes, API handlers, SSE endpoints |
| `data_cmd.py` | 922 | MTGJSON + price data import/export commands |
| `ingest_ocr.py` | 393 | CLI image-based card ingestion via EasyOCR + Claude |
| `ingest_corners.py` | 320 | CLI corner-photo card ingestion via Claude Vision |
| `demo_data.py` | 300 | Load demo collection for testing |
| `ingest_ids.py` | 246 | Manual card entry by rarity/collector-number/set |
| `sample_ingest.py` | 234 |  |

### `mtg_collector/db/` — SQLite layer

| File | Lines | Purpose |
|------|------:|---------|
| `models.py` | 1604 | Dataclasses + repository classes (CRUD for every table) |
| `schema.py` | 1450 | Schema DDL, all migrations, `init_db()` |

Repository classes in `models.py`: `CardRepository`, `SetRepository`, `PrintingRepository`, `CollectionRepository`, `OrderRepository`, `WishlistRepository`.

### `mtg_collector/services/` — External integrations

| File | Lines | Purpose |
|------|------:|---------|
| `agent.py` | 532 | Agentic OCR: Claude tool-use loop with `query_local_db` and `analyze_image` tools |
| `claude.py` | 504 | Claude Vision API: corner reading, card identification |
| `order_parser.py` | 414 | Parse TCGPlayer HTML/text and Card Kingdom text into `ParsedOrder` |
| `pack_generator.py` | 329 | MTGJSON-based booster pack simulation from SQLite |
| `order_resolver.py` | 303 | Resolve parsed orders to local DB cards, treatment-aware matching |
| `bulk_import.py` | 263 | `ScryfallBulkClient` class (bulk cache only), `cache_card_data()`, `ensure_set_populated()` |

### `mtg_collector/static/` — Web UI (single-file HTML pages)

| File | Lines | Purpose |
|------|------:|---------|
| `collection.html` | 3504 | **Collection browser**: filters, sorting, card grid, inline editing. Canonical card display. |
| `sealed.html` | 2116 |  |
| `recent.html` | 1358 | Recently ingested images gallery |
| `correct.html` | 1048 | Fix misidentified cards in ingest pipeline |
| `crack_pack.html` | 1040 | Booster pack simulator with rarity borders and badge system |
| `explore_sheets.html` | 824 | Browse MTGJSON booster sheet layouts |
| `ingest_ids.html` | 680 | Manual card entry web UI |
| `disambiguate.html` | 634 | Resolve ambiguous card matches |
| `ingest_corners.html` | 561 | Corner photo ingest web UI |
| `ingest_order.html` | 494 | Order ingestion web UI |
| `import_csv.html` | 492 | CSV import web UI |
| `process.html` | 406 | OCR processing + Claude identification |
| `upload.html` | 395 | Photo upload for image ingest |
| `index.html` | 309 | Homepage / navigation |

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
| `test_sealed_products.py` | 1346 |  |
| `test_import.py` | 620 | CSV import (Moxfield, Archidekt, Deckbox, decklist) |
| `test_price_import.py` | 530 | MTGJSON price import pipeline |
| `test_mtgjson_import.py` | 515 | MTGJSON AllPrintings import |
| `test_ingest_ids.py` | 392 | Manual card entry + `resolve_and_add_ids()` |
| `test_order_parser.py` | 368 | Order parsing (TCGPlayer HTML/text, Card Kingdom) |
| `test_order_resolver.py` | 302 | Order resolution to local DB cards |

### UI scenario tests (`tests/ui/`)

Claude Vision agent loop that drives a headless browser through UX flows. Each scenario is a YAML file with a goal description — Claude decides what to click, type, and navigate at each step.

| File | Purpose |
|------|---------|
| `test_sealed_products.py` | 1346 |  |
| `test_import.py` | 620 |  |
| `test_price_import.py` | 530 |  |
| `test_mtgjson_import.py` | 515 |  |
| `test_ingest_ids.py` | 392 |  |
| `test_order_parser.py` | 368 |  |
| `test_order_resolver.py` | 302 |  |

## Data Model

### Core join chain

```
cards (oracle_id PK)          — Abstract card identity (name, colors, mana cost)
  └─ printings (printing_id PK, FK oracle_id, FK set_code)  — Specific printing (art, rarity, image)
       └─ collection (id PK, FK printing_id, FK? order_id)  — One row per physical card owned
            └─ orders (id PK)  — Purchase order (TCGPlayer/CK seller, totals, shipping)

sets (set_code PK)            — Set metadata, cards_fetched_at for cache status
```

This is the fundamental chain: **card** → **printing** → **collection entry** (→ optional **order**).

- `collection_view` (VIEW) denormalizes this entire chain into one queryable view.
- `latest_prices` (VIEW) gives most recent price per card/source/type.

### Key lookups

- Card by name: `cards.name` (indexed)
- Printing by set+CN: `printings(set_code, collector_number)` (unique)
- Collection by printing ID: `collection.printing_id` (indexed)
- Prices join to printings via `(set_code, collector_number)` — **not** by printing_id

### Price data pipeline

MTGJSON UUIDs → `mtgjson_uuid_map(uuid → set_code, collector_number)` → `prices` table (append-only time series). `latest_prices` view uses global max `observed_at`. Sources: TCGplayer, CardKingdom.

### Other tables

- `wishlist` — FK to `cards` (oracle-level) or `printings` (specific). Priority, max price, fulfilled status.
- `ingest_cache` — Cached OCR + Claude results by image MD5 (avoids reprocessing).
- `ingest_images` — Persistent web UI ingest pipeline state (READY_FOR_OCR → PROCESSING → DONE/ERROR).
- `ingest_lineage` — Maps collection entries back to source images.
- `status_log` — Append-only audit of collection status changes.
- `settings` — Key-value config (e.g. `price_sources`, `image_display`).
- Schema v21 with auto-migrations in `schema.py`.

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

### Bulk data import

Scryfall bulk data is used only during `mtg setup` / `mtg cache all` to populate the local DB. `ScryfallBulkClient` in `services/bulk_import.py` handles this with 100ms rate limiting. 3 HTTP requests total for ~80k cards.

### Web server architecture

`crack_pack_server.py` is a single-file threaded HTTP server (stdlib `http.server`). Routes are dispatched in `do_GET`/`do_POST`/`do_PUT`/`do_DELETE` via URL path matching. SSE for long-running operations (ingest processing). No framework — raw request handling.

### Order ingestion

`order_parser.py` auto-detects format (tcg_html, tcg_text, ck_text) → `ParsedOrder`. `order_resolver.py` maps vendor set names to DB set codes via `SET_NAME_MAP` + DB lookup, then resolves to specific printings with treatment-aware matching. Idempotent — duplicate order_number + seller_name skipped.

### Agentic OCR (`services/agent.py`)

Claude tool-use loop with two tools: `query_local_db` (SQL against local SQLite) and `analyze_image` (Claude Vision). Used by the web UI ingest pipeline for card identification from photos.

### Claude API retry behavior

Exponential backoff at 3s, 6s, 12s, 24s intervals. Bails immediately on 400 errors (no retry). See `services/claude.py`.

### Card ingestion via `resolve_and_add_ids()`

Both `ingest-ids` and `ingest-corners` funnel through `resolve_and_add_ids()` in `cli/ingest_ids.py`:
1. Look up printing in local DB by set + collector number
2. Create collection entry with finish, condition, source metadata
3. If card not found, fail with error telling user to run `mtg cache all`

## Deployment

Rootless Podman Quadlet. Each instance: separate repo clone, own image (`mtgc:<instance>`), data volume, env file, port. No sudo.

Key files: `Containerfile` (multi-stage build), `deploy/seed.sh` (one-time seed volume), `deploy/setup.sh`, `deploy/deploy.sh`, `deploy/teardown.sh`, `deploy/mtgc.container` (Quadlet template with `{{INSTANCE}}`/`{{PORT}}` placeholders). All instances share a single `mtgc:latest` image; per-instance tags (`mtgc:<instance>`) are aliases. macOS equivalents: `deploy/mac-setup.sh`, `deploy/mac-deploy.sh`, `deploy/mac-teardown.sh` (use `podman run` directly, no systemd).

- `~/.config/mtgc/default.env` has the shared API key; setup.sh copies it to new instances automatically
- `~/.config/mtgc/<instance>.env` — per-instance env file
- `~/.config/containers/systemd/mtgc-<instance>.container` — generated Quadlet unit
- Service name: `mtgc-<instance>`, container name: `systemd-mtgc-<instance>`
- Server checks `ANTHROPIC_API_KEY` at startup and fails fast if missing
- CI: push to main → auto-deploys `prod` at `/opt/mtgc-prod/`. Workflow dispatch for other instances.
- Deploy repo (private CI config): `rgantt/efj-mtgc-deploy`

## Container Validation

**Always validate new features in isolated containers before creating PRs.** This uses the standard deployment scripts with demo data pre-loaded. Do not run the application locally or use `mtg` commands directly on the host.

### Setup (Linux)

From the repo clone with your feature branch checked out:

```bash
# Fast path: pre-built fixture, no network needed (~seconds)
bash deploy/setup.sh <instance> --test
systemctl --user start mtgc-<instance>
sleep 5

# Full path: clone seed volume (requires seed.sh first)
bash deploy/seed.sh
bash deploy/setup.sh <instance> --init
systemctl --user start mtgc-<instance>
sleep 5                                     # Wait for server startup
```

`--test` uses a pre-built fixture DB baked into the container image — no seed volume or network required. `--init` clones the seed volume (run `seed.sh` first). Both load demo data (~50 cards + sealed products).

Discover the assigned port:

```bash
podman port systemd-mtgc-<instance> 8081/tcp
```

### Setup (macOS)

Prerequisites (one-time):

```bash
brew install podman
podman machine init
podman machine start    # Also needed after each reboot
```

Then create an instance:

```bash
bash deploy/mac-setup.sh <instance> --test   # Fast: pre-built fixture (~seconds)
bash deploy/mac-setup.sh <instance> --init   # Full: download + init data (~15-30 min)
```

The script auto-starts the container and prints the URL. Discover the port:

```bash
podman port mtgc-<instance> 8081/tcp
```

### Validate

The server uses HTTPS with a self-signed cert. Use `curl -ks` for all requests.

```bash
# Linux:
PORT=$(podman port systemd-mtgc-<instance> 8081/tcp | grep -oP ':\K[0-9]+' | head -1)
# macOS:
PORT=$(podman port mtgc-<instance> 8081/tcp | cut -d: -f2 | head -1)

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
# Linux:
journalctl --user -u mtgc-<instance> -f
# macOS:
podman logs -f mtgc-<instance>
```

### Visual Validation

Use `shot-scraper` to screenshot key pages for visual regression checks. The self-signed cert requires `--browser-arg '--ignore-certificate-errors'`.

```bash
mkdir -p screenshots

uv run shot-scraper "https://localhost:${PORT}/" \
  --browser-arg '--ignore-certificate-errors' \
  -o screenshots/index.png

uv run shot-scraper "https://localhost:${PORT}/collection" \
  --browser-arg '--ignore-certificate-errors' \
  -o screenshots/collection.png

uv run shot-scraper "https://localhost:${PORT}/sealed" \
  --browser-arg '--ignore-certificate-errors' \
  -o screenshots/sealed.png
```

### Teardown

```bash
# Linux:
bash deploy/teardown.sh <instance> --purge       # Stop + remove container, volume, env, and image
# macOS:
bash deploy/mac-teardown.sh <instance> --purge   # Stop + remove container, volume, env, and image
```

### Notes

- Instance name should match your branch/feature (e.g., `issue44`, `my-feature`)
- **For UI tests, prefer `--test`** — uses a pre-built fixture DB (~27 MB, 11 sets) baked into the image. No seed volume or network needed. Starts in seconds.
- **For full data, use `--init`** — clones the seed volume. Run `bash deploy/seed.sh` first (fast no-op if it exists).
- `--test` uses `tests/fixtures/test-data.sqlite` (regenerate with `uv run python scripts/build_test_fixture.py`).
- `--init` clones the `mtgc-seed-data` volume (DB, Scryfall cache, MTGJSON data, ~50 demo cards). If no seed volume exists, it falls back to the slow `mtg setup --demo` path.
- After schema migrations, recreate the seed volume with `bash deploy/seed.sh --force` and regenerate the test fixture.
- Data persists on the volume across container restarts. Only `--purge` removes it.

## UI Scenario Tests

Data-driven UX regression tests using Claude Vision + Playwright. Each scenario is a YAML file describing a UX goal in natural language. A Claude Vision agent loop drives a headless browser to accomplish the goal, screenshotting at every step.

### How it works

1. Harness loads the homepage in headless Chromium
2. Screenshots the page + extracts all visible interactive elements
3. Sends screenshot + element list + goal to Claude (tool-use mode)
4. Claude picks an action: `navigate`, `click`, `fill`, `select_option`, `scroll`, `done`, or `fail`
5. Harness executes the action via Playwright, waits for async updates
6. Repeats until Claude calls `done` (pass) or `fail` (fail), or 20 steps hit

### Writing scenarios

Create a YAML file in `tests/ui/scenarios/`:

```yaml
# Related:
#   issues: [42]
#   pull_requests: [93]

description: >
  I can do the thing and then verify the result.
```

That's it — just a goal description and metadata. Claude figures out the steps.

### Running

```bash
# Requires a running container instance + ANTHROPIC_API_KEY
uv run pytest tests/ui/ -v --instance <instance>

# Override the model (default: claude-sonnet-4-6)
UI_TEST_MODEL=claude-haiku-4-5-20251001 uv run pytest tests/ui/ -v --instance <instance>
```

Screenshots are saved to `screenshots/ui/<timestamp>/` (gitignored).

### When to write UI scenarios

**Every UX-focused issue and PR should include a UI scenario.** When planning, implementing, or reviewing a UX change:

1. Write a scenario YAML describing what the user should be able to do
2. Annotate it with the relevant issue/PR numbers
3. Run it against a test instance to verify the feature works end-to-end
4. The scenario becomes a permanent regression test

### Key files

- `tests/ui/harness.py` — `UIHarness` class (Playwright + Claude Vision agent loop)
- `tests/ui/conftest.py` — Fixtures (browser, port discovery, screenshot dir)
- `tests/ui/test_scenarios.py` — Parametrized pytest runner for YAML scenarios
- `tests/ui/scenarios/*.yaml` — One file per scenario

## Web UI Shared Conventions (crack_pack.html)

- **Rarity/set border gradients**: Cards use CSS custom properties `--rarity-color` and `--set-color` with `linear-gradient(to bottom, ...)` to show rarity (top) and guest-set status (bottom). Shared JS helpers `getRarityColor(rarity)` and `getSetColor(cardSetCode, packSetCode)` return the colors. Use these for any new card display (lists, grids, etc.).
- **Badge builder**: `buildCardBadges(card, packSetCode)` returns HTML for SF/CK links with prices, foil, and treatment badges. `buildBadges(card, packSetCode)` wraps it with a zoom badge for the pack grid.
