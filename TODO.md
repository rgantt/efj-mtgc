# TODO

## Future Enhancements

1. **Enable mobile usage (phone camera workflow)**
   Eliminate the phone-to-desktop photo transfer step. Options: simple web server/API endpoint, iOS Shortcut/share sheet, watched shared folder (iCloud/AirDrop).

2. **Use Claude to disambiguate multiple printings**
   When Scryfall returns multiple printings, make an additional Claude call with the image and candidate printings to pick the correct one (e.g. full-bleed Spiderman variant of Tangle vs original Invasion).

3. **Auto-upload CSV to collection manager** *(blocked by #4)*
   After building the CSV, automatically upload it to the collection platform instead of requiring manual import.

4. **Find a collection platform with API access**
   Moxfield has no public API. Research alternatives (Archidekt, Deckbox, TappedOut, etc.) that support programmatic collection import. Unblocks #3.

5. **Show card images during disambiguation**
   When prompting the user to choose between printings, display Scryfall card images alongside text data. Could use terminal image protocols (iTerm2 inline, Sixel), browser, or a web UI if #1 goes that direction.

## Completed

- ✅ **Fuzzy matching for misread card names** - Uses difflib against locally cached set card lists
- ✅ **Auto set detection** - Claude reads set codes from images, normalizes against Scryfall
- ✅ **Retry/fallback logic** - Claude API retries with exponential backoff, cross-set fallback for fuzzy matching
- ✅ **Local set caching** - Full card lists cached per set in SQLite (sets are immutable)
