# MTG Collection Builder

A CLI tool for managing Magic: The Gathering card collections. Snap photos of your cards, and the tool uses Claude's Vision API to identify them and Scryfall for card data. Your collection is stored locally in SQLite and can be exported to Moxfield, Archidekt, or Deckbox.

## Features

- **Photo ingestion**: Point at a pile of cards, Claude identifies them
- **Fuzzy matching**: Handles OCR misreads by matching against cached set card lists
- **Auto set detection**: Claude reads set codes from card images, no manual input needed
- **Local caching**: Scryfall data cached in SQLite to minimize API calls
- **Multi-platform export**: Moxfield, Archidekt, Deckbox CSV formats
- **Individual card tracking**: Each physical card is a separate entry with condition, finish, purchase price

## Quick Start

```bash
# Clone and setup
git clone https://github.com/ryangantt/efj-mtgc.git
cd efj-mtgc
make setup
source .venv/bin/activate

# Set your Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."

# Initialize database and ingest some cards
mtg db init
mtg ingest photo.jpg

# See what you've got
mtg list
mtg stats

# Export to Moxfield
mtg export -f moxfield -o collection.csv
```

## Requirements

- Python 3.10+
- [Anthropic API key](https://console.anthropic.com/) for card photo analysis

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

**API Key**: Set `ANTHROPIC_API_KEY` environment variable or add to your shell profile.

**Database**: Default location is `~/.mtgc/collection.sqlite`. Override with:
- `--db /path/to/db.sqlite` flag
- `MTGC_DB` environment variable

## Usage

### Ingest cards from photos

```bash
mtg ingest photo.jpg                      # Interactive mode
mtg ingest photo.jpg --batch              # Auto-select first match
mtg ingest *.jpg --batch                  # Batch process multiple photos
mtg ingest photo.jpg --source "GP Vegas"  # Tag with acquisition source
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
mtg export -f moxfield --set DMU -o dmu.csv   # Filter by set
```

### Browse and manage collection

```bash
# List and filter
mtg list                          # All cards
mtg list --set DMU                # By set
mtg list --name "Lightning"       # By name
mtg list --foil                   # Only foils
mtg list --condition LP           # By condition

# View and edit
mtg show 42                       # Details for entry #42
mtg edit 42 --condition LP        # Update condition
mtg edit 42 --finish foil         # Update finish
mtg edit 42 --price 5.99          # Set purchase price
mtg delete 42                     # Remove entry

# Stats
mtg stats                         # Collection summary
```

### Database management

```bash
mtg db init                       # Initialize database
mtg db refresh                    # Re-fetch Scryfall data for cached cards
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
| Source | photo_ingest, moxfield_import, manual, etc. |
| Flags | tradelist, alter, proxy, signed, misprint |

## How It Works

1. **Image Analysis**: Claude Vision reads card names and set codes from your photo
2. **Set Normalization**: Raw set codes validated against Scryfall's master list
3. **Local Caching**: Full card lists for detected sets cached in SQLite (sets don't change)
4. **Fuzzy Matching**: Card names matched against cached lists using difflib (handles OCR errors)
5. **Scryfall Lookup**: Matched cards fetched from Scryfall API with full printing details
6. **Collection Storage**: Cards added to your collection with metadata

## Development

```bash
make dev          # Install with dev dependencies
make test         # Run tests (integration tests need ANTHROPIC_API_KEY)
make lint         # Run ruff + black
make clean        # Remove venv and build artifacts
```

## License

MIT
