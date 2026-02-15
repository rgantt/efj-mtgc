# MTG Collection Builder

A CLI + web UI tool for managing Magic: The Gathering card collections. Add cards by reading their corner info from photos (Claude Vision), full card photos (local OCR + Claude), manual ID entry, or importing purchase orders from TCGPlayer and Card Kingdom. Your collection is stored locally in SQLite and can be exported to Moxfield, Archidekt, or Deckbox. Includes a web UI for browsing your collection, cracking virtual booster packs, image-based card ingestion, and order import.

## Features

- **Corner photo ingestion**: Photograph card corners and Claude Vision extracts rarity, collector number, set code, and foil status
- **Full card photo ingestion**: Upload full card images via the web UI — local OCR + Claude identifies card names
- **Manual ID entry**: Add cards directly by rarity/collector-number/set (no API key needed)
- **Order ingestion**: Import purchase orders from TCGPlayer (HTML or text) and Card Kingdom (text) with treatment-aware card matching
- **Order tracking**: Track orders by seller with per-card pricing, batch-receive when orders arrive
- **Bulk Scryfall caching**: Download all card data from Scryfall in 3 API calls for offline browsing
- **Web UI**: Browse collection, crack virtual booster packs, explore sheet layouts, ingest cards from images or orders
- **Card lifecycle tracking**: Track card status (owned → ordered → listed → sold → removed) with audit log
- **Wishlist**: Track cards you want, with priority and price alerts
- **Local caching**: Scryfall data cached in SQLite to minimize API calls
- **Multi-platform import/export**: Moxfield, Archidekt, Deckbox CSV formats
- **Price data**: TCGplayer and CardKingdom prices via MTGJSON

## Quick Start

```bash
# Clone and setup
git clone https://github.com/thaen/efj-mtgc.git
cd efj-mtgc
uv sync

# One-command setup: init DB, cache Scryfall data, fetch MTGJSON
mtg setup

# Or with demo data (~50 cards) to browse immediately
mtg setup --demo

# Start the web UI
mtg crack-pack-server   # Open http://localhost:8080

# Add cards by ID (no API key needed)
mtg ingest-ids --id R 0200 EOE --id C 0075 EOE foil

# Or set your Anthropic API key and add cards from corner photos
export ANTHROPIC_API_KEY="sk-ant-..."
mtg ingest-corners ~/photos/corners.jpg

# Import a TCGPlayer order
mtg ingest-order ~/Downloads/order-page.html

# See what you've got
mtg list
mtg stats

# Export to Moxfield
mtg export -f moxfield -o collection.csv
```

The `mtg setup` command handles database initialization, Scryfall bulk data caching (~80k cards), and MTGJSON data download in one step. Use `--skip-cache` or `--skip-data` to skip individual steps.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) for dependency management
- [Anthropic API key](https://console.anthropic.com/) (only needed for `ingest-corners` and OCR ingestion)

## Configuration

**API Key**: Set `ANTHROPIC_API_KEY` environment variable or add to your shell profile. Only required for photo-based ingestion commands.

**Database**: Default location is `~/.mtgc/collection.sqlite`. Override with:
- `--db /path/to/db.sqlite` flag
- `MTGC_DB` environment variable

**Data files**: Stored in `~/.mtgc/` (override with `MTGC_HOME` env):
- `collection.sqlite` — card collection database
- `AllPrintings.json` — MTGJSON card data (for booster simulation)
- `AllPricesToday.json` — MTGJSON price data

## Usage

### Ingest cards by ID

Add cards using their printed rarity letter, collector number, and set code. No Anthropic API key required.

```bash
mtg ingest-ids --id R 0200 EOE                        # Single card
mtg ingest-ids --id R 0200 EOE --id C 0075 EOE        # Multiple cards
mtg ingest-ids --id C 0187 EOE foil                    # Foil card
mtg ingest-ids --id P 0012 SPG --source "promo pack"   # With source tag
mtg ingest-ids --id R 0200 EOE --condition LP          # Set condition
```

Rarity codes: C (common), U (uncommon), R (rare), M (mythic), P (promo), L (land), T (token)

### Ingest cards from corner photos

Photograph the bottom-left corners of your cards. Claude Vision reads the rarity, collector number, set code, and foil status from the printed text.

```bash
mtg ingest-corners photo.jpg                           # Single photo
mtg ingest-corners *.jpg                               # Multiple photos
mtg ingest-corners photo.jpg --review                  # Review before adding
mtg ingest-corners photo.jpg --source "GP Vegas"       # Tag with source
mtg ingest-corners photo.jpg --condition LP            # Set condition
```

### Ingest orders from TCGPlayer or Card Kingdom

Import purchase orders to track cards as "ordered" with seller and pricing info. Supports TCGPlayer saved HTML pages (including paginated multi-file orders), TCGPlayer text (clipboard paste), and Card Kingdom text.

```bash
mtg ingest-order order.html                            # Single order page
mtg ingest-order page1.html page2.html page3.html      # Multiple pages (paginated)
mtg ingest-order order.txt -f tcg_text                  # Explicit format
mtg ingest-order --dry-run order.html                   # Preview without saving
mtg ingest-order --status owned order.html              # Import as owned (default: ordered)
pbpaste | mtg ingest-order                              # From clipboard via stdin
```

Ingestion is idempotent — re-importing the same order (same order number + seller) is a no-op. Non-MTG products (Pokemon, Lorcana, Disney Lorcana) are automatically skipped. Treatment variants (borderless, extended art, showcase) are matched to the correct Scryfall printing.

### Manage orders

```bash
mtg orders list                  # List all orders with card counts and totals
mtg orders show 27               # Show order details with per-card printings
mtg orders receive 27            # Batch flip all cards in order from ordered → owned
```

### Import/Export

```bash
# Import from other platforms
mtg import cards.csv                      # Auto-detect format
mtg import cards.csv -f moxfield          # Explicit format
mtg import cards.csv --dry-run            # Preview without saving

# Export to other platforms
mtg export -f moxfield -o collection.csv
mtg export -f archidekt -o collection.csv
mtg export -f deckbox -o collection.csv
mtg export -f moxfield --set DMU -o dmu.csv       # Filter by set
mtg export -f moxfield --name "Lightning" -o l.csv # Filter by name
```

### Browse and manage collection

```bash
# List and filter
mtg list                          # All cards (default limit: 50)
mtg list --set DMU                # By set
mtg list --name "Lightning"       # By name (partial match)
mtg list --foil                   # Only foils/etched
mtg list --nonfoil                # Only non-foils
mtg list --condition LP           # By condition
mtg list --source "GP Vegas"      # By source
mtg list --limit 100              # Change result limit
mtg list --offset 50              # Pagination

# View details
mtg show 42                       # Full details for entry #42

# Edit entries
mtg edit 42 --condition LP        # Update condition
mtg edit 42 --finish foil         # Update finish (nonfoil, foil, etched)
mtg edit 42 --price 5.99          # Set purchase price
mtg edit 42 --language Japanese   # Set language
mtg edit 42 --source "trade"      # Set source
mtg edit 42 --notes "SP card"     # Set notes
mtg edit 42 --tags "modern,staple" # Set tags
mtg edit 42 --tradelist 1         # Flag as tradelist (also: --alter, --proxy, --signed, --misprint)
mtg edit 42 --status sold         # Update status (owned/ordered/listed/sold/removed)

# Delete entries
mtg delete 42                     # Remove entry (with confirmation)
mtg delete 42 -y                  # Skip confirmation

# Stats
mtg stats                         # Collection summary
```

### Wishlist

```bash
mtg wishlist add "Lightning Bolt"             # Add by card name
mtg wishlist list                             # List all wishlist entries
mtg wishlist fulfill 1                        # Mark entry as fulfilled
mtg wishlist remove 1                         # Delete entry
```

### Caching and data management

```bash
# Cache all Scryfall card data (bulk download, 3 API calls)
mtg cache all                     # First run: downloads ~300MB, caches ~80k cards
mtg cache all                     # Subsequent: skips cached sets, processes only new ones
mtg cache all --force             # Reprocess all sets

# MTGJSON data (for booster pack simulation and prices)
mtg data fetch                    # Download AllPrintings.json
mtg data fetch-prices             # Download AllPricesToday.json

# Database management
mtg db init                       # Initialize database
mtg db init --force               # Recreate tables
mtg db refresh                    # Re-fetch Scryfall data for cached printings
mtg db refresh --all              # Refresh all printings
```

### Web UI

```bash
mtg crack-pack-server                        # Start on default port 8080
mtg crack-pack-server --port 3000            # Custom port
```

Pages available at `http://localhost:8080`:
- **Collection** (`/collection`) — Browse, filter, and manage your collection with grid/table views, set browsing, and "include unowned" mode
- **Crack-a-Pack** (`/crack`) — Virtual booster pack simulator with price data
- **Explore Sheets** (`/sheets`) — Browse booster sheet layouts by set and product type
- **Card Ingestor** (`/ingestor-ocr`) — Upload card images for OCR-based identification and collection entry
- **Order Ingestor** (`/ingestor-order`) — Import TCGPlayer/Card Kingdom orders via paste or file upload

## Data Model

Each physical card you own is stored as a separate row with:

| Field | Description |
|-------|-------------|
| Printing | Scryfall ID linking to specific set/collector number |
| Status | owned, ordered, listed, sold, removed (with audit log) |
| Condition | Near Mint, Lightly Played, Moderately Played, Heavily Played, Damaged |
| Finish | nonfoil, foil, etched |
| Language | Default: English |
| Purchase price | Optional (auto-populated from order ingestion) |
| Sale price | Optional (for sold cards) |
| Acquired date | Auto-set on import |
| Source | corner_ingest, manual_id, moxfield_import, order_import, etc. |
| Order | Optional link to purchase order (seller, order number, totals) |
| Flags | tradelist, alter, proxy, signed, misprint |

## How It Works

1. **Card Identification**: Manual entry (rarity/CN/set), Claude Vision (corner photos), local OCR + Claude (full card photos), or order import (TCGPlayer/Card Kingdom)
2. **Scryfall Lookup**: Cards resolved by set code + collector number via Scryfall API. Order imports use treatment-aware matching to resolve borderless, extended art, and showcase variants to the correct printing.
3. **Local Caching**: Full card data cached in SQLite to avoid repeated API calls. `mtg cache all` bulk-caches all ~80k cards from Scryfall's bulk data endpoint.
4. **Collection Storage**: Cards added to your collection with finish, condition, source metadata, and lifecycle status tracking. Order-imported cards are linked to their purchase order for per-seller tracking.

## Development

```bash
uv sync                   # Install dependencies
uv run pytest             # Run tests (some require ANTHROPIC_API_KEY)
uv run ruff check mtg_collector/  # Lint
```

## License

MIT
