# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Card Collection Builder - A Python CLI tool that identifies Magic: The Gathering cards using Claude Vision (corner photos) or manual ID entry, queries Scryfall for card data, and stores collections in SQLite. Supports import/export to Moxfield, Archidekt, and Deckbox formats.

## Environment
- **Always use `uv`** for all Python operations (not pip/venv/make). Examples:
  - `uv sync` to install deps
  - `uv run pytest ...` to run tests
  - `uv run mtg ...` to run CLI

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
uv run black --check mtg_collector/

# CLI usage
mtg db init                                            # Initialize database
mtg ingest-ids --id R 0200 EOE --id C 0075 EOE foil   # Add cards by rarity/CN/set
mtg ingest-corners photo.jpg                           # Read card corners via Claude Vision
mtg list                                               # List collection
mtg export -f moxfield -o out.csv
```

## Architecture

```
mtg_collector/
├── cli/           # Subcommands: ingest_ids, ingest_corners, import_cmd, export,
│                  #   list_cmd, show, edit, delete, stats, db_cmd
│                  # Each module has register(subparsers) and run(args) functions
├── db/            # SQLite layer (connection.py, schema.py, models.py with repositories)
├── services/      # Claude Vision API (claude.py) and Scryfall API (scryfall.py)
├── importers/     # CSV parsers for moxfield, archidekt, deckbox
└── exporters/     # CSV writers for moxfield, archidekt, deckbox
```

## Database Schema

Four tables with foreign key relationships:
- `cards` (oracle_id PK) → Oracle-level card identity
- `sets` (set_code PK) → Set info + `cards_fetched_at` for caching status
- `printings` (scryfall_id PK) → Specific printings, FK to cards and sets
- `collection` (id PK) → Owned cards, FK to printings (one row per physical card)

Default location: `~/.mtgc/collection.sqlite` (override with `--db` or `MTGC_DB` env)

## Data Flow: Card Ingestion

Two ingestion methods, both resolve to the same pipeline:

1. **ingest-ids**: User provides rarity code, collector number, set code, and optional foil flag directly
2. **ingest-corners**: Claude Vision reads card corner text (rarity/CN/set/foil) from photos

Both feed into `resolve_and_add_ids()` which:
1. Looks up printing in local cache, falls back to Scryfall API by set+collector number
2. Caches Scryfall data (card, set, printing) in SQLite
3. Creates collection entry with finish, condition, source metadata

## Key Implementation Details

- Scryfall API rate limited to 100ms between requests
- Claude API retries with exponential backoff (3s, 6s, 12s, 24s) but bails immediately on 400 errors
- JSON arrays stored as TEXT in SQLite (colors, finishes, promo_types)
- Schema version tracked in `schema_version` table for auto-migrations (current: v3)
- RARITY_MAP: C (common), U (uncommon), R (rare), M (mythic), P (promo)
- Fuzzy match threshold: 0.75 (difflib, used for name matching against cached set lists)
- Tests use a pre-populated `tests/fixtures/scryfall-cache.sqlite` for offline testing

## Web UI Shared Conventions (crack_pack.html)

- **Rarity/set border gradients**: Cards use CSS custom properties `--rarity-color` and `--set-color` with `linear-gradient(to bottom, ...)` to show rarity (top) and guest-set status (bottom). Shared JS helpers `getRarityColor(rarity)` and `getSetColor(cardSetCode, packSetCode)` return the colors. Use these for any new card display (lists, grids, etc.).
- **Badge builder**: `buildCardBadges(card, packSetCode)` returns HTML for SF/CK links with prices, foil, and treatment badges. `buildBadges(card, packSetCode)` wraps it with a zoom badge for the pack grid.
