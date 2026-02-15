# Code Reduction Plan

~1000 lines savings estimated. Big wins are shared CSS/JS (items 1-2).

## 1. Create `common.css`
Extract from 6-9 HTML files into one shared stylesheet:
- CSS reset, body, header, header links (~25 lines)
- Button styles + secondary variant (~18 lines)
- Spinner + @keyframes spin (~12 lines)
- Empty state styles (~8 lines)
- Photo modal styles (~15 lines)
- Candidate row styles (~40 lines, disambiguate + correct)
- Pill/toggle styles (~25 lines)

Each HTML file gets `<link rel="stylesheet" href="/static/common.css">` and drops its copy.

## 2. Create `common.js`
Extract from 2-4 HTML files into one shared script:
- `escapeHtml()` — 4 files
- `KEYRUNE_FALLBACKS` + `keyruneSetCode()` — 2 files
- Settings load/apply (`_settings`, `applySettings()`, fetch) — 3 files
- `showPhotoModal()` — 3 files
- `setupCropThumb()` — 2 files (~25 lines each)
- `renderCandidates()` — 2 files (~60 lines each)
- `searchCard()` — 2 files

Each HTML file gets `<script src="/static/common.js"></script>` and drops its copy.

## 3. Delete `/process` route
Line 650 in crack_pack_server.py — serves `recent.html`, same as `/recent` on line 648.

## 4. Extract `_get_conn()` helper
Replace 11 occurrences of `conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row`.

## 5. Extract `_add_to_collection()` helper
Combines CollectionEntry creation + lineage INSERT. Used in 5 places.

## 6. Extract shared Scryfall resolution function
3 endpoints repeat the same try-stripped-CN / try-raw-CN / try-name-search pattern (~20 lines each).
