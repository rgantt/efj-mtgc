# TODO

1. **Enable mobile usage (phone camera workflow)**
   Eliminate the phone-to-desktop photo transfer step. Options: simple web server/API endpoint, iOS Shortcut/share sheet, watched shared folder (iCloud/AirDrop).

2. **Use Claude to disambiguate multiple printings**
   When Scryfall returns multiple printings, make an additional Claude call with the image and candidate printings to pick the correct one (e.g. full-bleed Spiderman variant of Tangle vs original Invasion).

3. **Add retry/fallback logic for failed Scryfall lookups**
   When Claude misreads card details and Scryfall returns 404/no results: retry with another Claude call to re-read, and/or broaden the query (just card name, no set/collector number), then proceed with disambiguation.

4. **Auto-upload CSV to collection manager** *(blocked by #5)*
   After building the CSV, automatically upload it to the collection platform instead of requiring manual import.

5. **Find a collection platform with API access**
   Moxfield has no public API. Research alternatives (Archidekt, Deckbox, TappedOut, etc.) that support programmatic collection import. Unblocks #4.

6. **Show card images during disambiguation**
   When prompting the user to choose between printings, display Scryfall card images alongside text data. Could use terminal image protocols (iTerm2 inline, Sixel), browser, or a web UI if #1 goes that direction.
