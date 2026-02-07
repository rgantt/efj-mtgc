# MTG Collection Builder

A CLI tool for managing Magic: The Gathering card collections. Add cards by reading their corner info (rarity, collector number, set code) either manually or from photos using Claude Vision. Your collection is stored locally in SQLite and can be exported to Moxfield, Archidekt, or Deckbox.

## Features

- **Corner photo ingestion**: Photograph card corners and Claude Vision extracts rarity, collector number, set code, and foil status
- **Manual ID entry**: Add cards directly by rarity/collector-number/set (no API key needed)
- **Local caching**: Scryfall data cached in SQLite to minimize API calls
- **Multi-platform import/export**: Moxfield, Archidekt, Deckbox CSV formats
- **Individual card tracking**: Each physical card is a separate entry with condition, finish, purchase price

## Quick Start

```bash
# Clone and setup
git clone https://github.com/ryangantt/efj-mtgc.git
cd efj-mtgc
make setup
source .venv/bin/activate

# Initialize database
mtg db init

# Add cards by ID (no API key needed)
mtg ingest-ids --id R 0200 EOE --id C 0075 EOE foil

# Or set your Anthropic API key and add cards from corner photos
export ANTHROPIC_API_KEY="sk-ant-..."
mtg ingest-corners ~/photos/corners.jpg

# See what you've got
mtg list
mtg stats

# Export to Moxfield
mtg export -f moxfield -o collection.csv
```

## Requirements

- Python 3.10+
- [Anthropic API key](https://console.anthropic.com/) (only needed for `ingest-corners`)

## Installation

```bash
# Using Make (recommended)
make setup
source .venv/bin/activate

# Manual
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configuration

**API Key**: Set `ANTHROPIC_API_KEY` environment variable or add to your shell profile. Only required for the `ingest-corners` command.

**Database**: Default location is `~/.mtgc/collection.sqlite`. Override with:
- `--db /path/to/db.sqlite` flag
- `MTGC_DB` environment variable

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

Rarity codes: C (common), U (uncommon), R (rare), M (mythic), P (promo)

### Ingest cards from corner photos

Photograph the bottom-left corners of your cards. Claude Vision reads the rarity, collector number, set code, and foil status from the printed text.

```bash
mtg ingest-corners photo.jpg                           # Single photo
mtg ingest-corners *.jpg                               # Multiple photos
mtg ingest-corners photo.jpg --review                  # Review before adding
mtg ingest-corners photo.jpg --source "GP Vegas"       # Tag with source
mtg ingest-corners photo.jpg --condition LP            # Set condition
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

# Delete entries
mtg delete 42                     # Remove entry (with confirmation)
mtg delete 42 -y                  # Skip confirmation

# Stats
mtg stats                         # Collection summary
```

### Database management

```bash
mtg db init                       # Initialize database
mtg db init --force               # Recreate tables
mtg db refresh                    # Re-fetch Scryfall data for cached printings
mtg db refresh --all              # Refresh all printings
```

## Data Model

Each physical card you own is stored as a separate row with:

| Field | Description |
|-------|-------------|
| Printing | Scryfall ID linking to specific set/collector number |
| Condition | Near Mint, Lightly Played, Moderately Played, Heavily Played, Damaged |
| Finish | nonfoil, foil, etched |
| Language | Default: English |
| Purchase price | Optional |
| Acquired date | Auto-set on import |
| Source | corner_ingest, manual_id, moxfield_import, etc. |
| Flags | tradelist, alter, proxy, signed, misprint |

## How It Works

1. **Card Identification**: Either manual entry (rarity/CN/set) or Claude Vision reads card corners
2. **Scryfall Lookup**: Cards resolved by set code + collector number via Scryfall API
3. **Local Caching**: Full card data cached in SQLite to avoid repeated API calls
4. **Collection Storage**: Cards added to your collection with finish, condition, and source metadata

## Development

```bash
make dev          # Install with dev dependencies
make test         # Run tests (corner identification tests need ANTHROPIC_API_KEY)
make lint         # Run ruff + black
make clean        # Remove venv and build artifacts
```

## License

MIT
