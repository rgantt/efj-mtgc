# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Card Collection Builder - A Python CLI tool that uses Claude's Vision API to identify Magic: The Gathering cards from images and queries the Scryfall API to build a collection database. Supports import/export to Moxfield, Archidekt, and Deckbox formats.

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

# Run tests (integration tests require ANTHROPIC_API_KEY)
uv run pytest                                # All tests
uv run pytest tests/test_ingest_ids.py -v    # Single test file

# Linting
uv run ruff check mtg_collector/
uv run black --check mtg_collector/

# CLI usage
mtg db init                        # Initialize database
mtg ingest photo.jpg               # Analyze card image
mtg ingest photo.jpg --batch       # Auto-select first match
mtg list                           # List collection
mtg export -f moxfield -o out.csv
```

## Architecture

```
mtg_collector/
├── cli/           # Subcommands (ingest, import_cmd, export, list_cmd, show, edit, delete, stats, db_cmd)
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

1. **Image analysis** (claude.py): Claude Vision identifies card names AND set codes
2. **Set normalization** (scryfall.py): Raw set codes validated against Scryfall's known sets
3. **Set caching**: Full card lists cached locally in SQLite per set (sets are immutable)
4. **Fuzzy matching** (scryfall.py): Card names matched against cached set lists using difflib (threshold 0.75)
5. **Cross-set fallback**: If not found in detected set, tries other detected sets before global Scryfall search
6. **Collection storage**: Matched cards added with finish, condition, source metadata

## Key Implementation Details

- Scryfall API rate limited to 100ms between requests
- Claude API has retry logic with exponential backoff (3s, 6s, 12s, 24s)
- JSON arrays stored as TEXT in SQLite (colors, finishes, promo_types)
- Schema version tracked in `schema_version` table for migrations
- CLI uses argparse with each subcommand in its own module (register/run pattern)
