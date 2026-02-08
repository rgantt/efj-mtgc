# TODO

## Up Next

DONE 6. **Crack a Pack**
   - [ ] **6a. Pack generation engine** — Given a set code + booster type, generate a virtual pack. Use MTGJSON AllPrintings.json booster definitions: pick a pack variant by weight, then for each slot draw from the correct sheet using card weights. Output: list of card UUIDs/names with finish, rarity, treatments.
   - [ ] **6b. Basic UI** — Web UI to pick a set and booster type, generate a pack, and display the cards with images (Scryfall image URIs or downloaded). Flip/reveal animation optional but nice.
   - [ ] **6c. "Save" cards from packs** — Tap a card in the UI to mark it as a pick. Open multiple packs, pick cards from each. Persist picks across packs in a session.
   - [ ] **6d. Highlight owned cards** — Cross-reference generated pack against the collection DB. Visually flag cards you already own.
   - [ ] **6e. Add to cart** — For picked cards, generate links or auto-add to TCGPlayer/CardKingdom cart.

DONE 6.1 the drop-down for set selection is unwieldy, too long. what other options are there?

DONE 6.2 the pack type should be a radio button, not a drop-down

DONE6.4 if i click "create" and then refresh the page, i want the same pack to reload. i also want to be able to send links to friends and have the pack show for them (the Picks do not need to be encoded in the URL in the same way). 

DONE 6.5 add pricing information from somewhere

6.5 cards need to be drawn without replacement

6.5.1 fix CK prices, still aren't showing up

6.6 MTG.WTF/PACK: A better UI.

Now that we have the math and images, we can recreate the MTG.WTF/PACK UI but better. Let's do that.

there should be a button next to "Open Pack" that says "Explore Sheets". once a set is selected, that button should take you to a page that shows the pack distributions and sheet distributions, like mtg.wtf/pack . however, mtg.pack shows you *all* the cards by default in one enormous scrolling thing. i want to be able to expand/collapse the various sections. you can look at their UI to see what they have. a url like https://mtg.wtf/pack/eoe-collector this should give you that.

7. **Move bling odds calculator into repo** — `~/mtg/blingodds.py` + `bling.yml` + AllPrintings.json data. Calculates probability of opening special cards (borderless, showcase, extended art, etc.) per pack/box.

8. **Move local OCR into repo** — `local-ocr/ocr_cli.py` uses easyocr to read collector numbers from card corner photos without Claude API. Replace Claude Vision in the ingest pipeline for cards with visible collector numbers.

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
