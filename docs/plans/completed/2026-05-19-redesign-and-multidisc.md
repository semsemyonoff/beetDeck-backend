# beetDeck redesign & multi-disc albums support

## Overview

Provide a full visual & UX redesign of beetDeck, replacing the current vanilla-JS SPA in `src/templates/index.html` + `src/static/js/*.js` with a React + Vite build pipeline that ships the new design captured in `docs/design/`. The redesign keeps the existing Flask REST API contract but updates the frontend top-to-bottom: topbar layout, library views (Index + Wall), artist & album pages with grouped action buttons, two-phase Identify modal (search form → results), and per-track lyrics handling.

Alongside the redesign, add proper backend & frontend support for **multi-disc albums** (e.g. Pink Floyd — *The Wall*) where the on-disk layout is:

```
<artist>/<album>/CD1/<tracks>
<artist>/<album>/CD2/<tracks>
```

(subdir names can vary: `CD1`, `Disc 1`, `Disc-2`, etc.). The current code assumes one filesystem dir per album, which breaks `_album_dir_from_items` and cover lookup. The scan diff (`_compute_scan_diff`) is already path-based and should keep working unchanged — Task 3 covers it with a regression test rather than a fix. The fix is to compute a *common parent* directory when items live in subdirs, and group tracks by their `disc` value in the album detail view.

## Context (from discovery)

- **Design bundle copied to repo**: `docs/design/` (README, chats/, project/*.jsx, project/styles.css, project/assets/logo.png, project/uploads/*.png). The source URL was a temporary share link and must not be re-fetched in execution; the local copy is the canonical reference.
- **Design intent (from `docs/design/chats/chat1.md`)**: cozy density, fixed accent `#ec4868`, Geist + Geist Mono via Google Fonts, two library layouts (Index, Wall), grouped action buttons on album page (`Tags`, `Genre`, `Cover`, `Lyrics`), two-phase Identify (search form → searching → results), path tree for multi-disc albums, no Play buttons (this isn't a player), no artist-level action buttons, different icons for Quick vs Full scan.
- **Backend stays Flask + SQLite-direct + beets `Library`**. The API contract in `CLAUDE.md` is preserved. Beets already stores `disc` per item — `src/routes/albums.py:48-54` already selects it. So the multi-disc data exists; the bug is purely in path/cover helpers and frontend rendering.
- **Files involved**:
  - delete-or-rewrite: `src/templates/index.html`, `src/static/js/*.js`, `src/static/style.css`
  - new: `frontend/` (Vite project), `frontend/package.json`, `frontend/vite.config.js`, `frontend/src/**`, `frontend/index.html`
  - touch (multi-disc backend): `src/utils.py` (`_album_dir_from_items`, `_find_cover`), `src/routes/albums.py` (expose `multi_disc` + per-disc dirs in payload), `src/routes/library.py` (cover lookup for multi-disc), `src/routes/scan.py` (path-snapshot already uses `_resolve_path`, verify multi-disc-safe), `src/routes/cover.py` / `src/routes/lyrics.py` if they assume single dir.
  - new tests: `tests/test_albums.py`, `tests/test_utils.py`, `tests/test_library.py`, `tests/test_cover.py` — add multi-disc fixtures and assertions.
- **Logos**: `src/static/logo.svg`, `logo-{32,64,128,512}.png` exist. The new design top bar uses a PNG. Final logo set will be decided in the cleanup task; for now treat as deferred.
- **Architectural constraint preserved**: single gunicorn worker. The redesign is frontend-only on the server side — Flask still serves `index.html` at `/` and the static assets under `/static/`. After the Vite build the bundled files go into `src/static/` (or `src/static/dist/`) and `src/templates/index.html` is regenerated to reference the hashed bundle.

## Development Approach

- **Testing approach**: Regular (code first, then tests) — confirmed for this task.
- Complete each task fully before moving to the next.
- Make small, focused commits per task.
- **CRITICAL: every backend task MUST include new/updated tests for the code in that task.** Frontend tasks are visually verified in a real browser (manual UAT) since the user opted out of frontend unit tests; describe what to click and what to see at the end of each frontend task.
- **CRITICAL: all backend tests must pass (`make test`) before starting the next task** — no exceptions.
- **CRITICAL: update this plan file when scope changes during implementation.**
- Run `make lint` and `make fmt` after each backend task.
- Maintain backward compatibility of the public REST API: existing endpoints keep their shapes; new fields are additive.

## Testing Strategy

- **Backend unit/integration tests**: required for every backend task (multi-disc helpers, cover lookup, album payload, scan snapshot).
- **Backend fixtures**: extend `tests/conftest.py` to seed a multi-disc album (e.g. an in-memory `albums` row + two `items` rows with `disc=1`/`disc=2` and paths under `<album>/CD1/…` and `<album>/CD2/…`).
- **Frontend tests**: none (user-confirmed). Manual UAT only.
- **Manual UAT** (run dev stack, navigate by hand): library Index, library Wall, search, artist page, album page (single-disc & multi-disc), Identify form → search → results, cover fetch/upload, lyrics fetch/edit/embed, scan progress.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with ➕ prefix.
- Document issues/blockers with ⚠️ prefix.
- Update plan if implementation deviates from original scope.

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): code, tests, config, doc updates achievable inside this repo.
- **Post-Completion** (no checkboxes): manual browser UAT scenarios, Docker smoke test, things the user must verify by interacting with the running app.

## Implementation Steps

### Task 1: Multi-disc helpers in `src/utils.py`

- [x] add `_album_dir_from_items` variant (or replace it) that returns the *common parent dir* of all items in an album — e.g. for items in `<root>/Pink Floyd/The Wall/CD1/01.mp3` + `<root>/.../CD2/01.mp3` it returns `<root>/Pink Floyd/The Wall` rather than just one disc's dir
- [x] add a `_detect_disc_subdirs(items)` helper returning a list `[{disc: int, dir: str, track_count: int, duration_sec: int}]` sorted by disc — used by album payload and by `_find_cover` fallback. **NULL handling: the helper itself must coerce `item["disc"] or 1`** before grouping/sorting — do not rely on callers; otherwise NULL disc values cause wrong grouping and a TypeError on `sorted()` with mixed `None`/`int`. Add a regression test with one item having `disc=NULL`.
- [x] update `_find_cover` (or add `_find_album_cover_multidisc`) to: (1) try the common parent dir first, (2) fall back to each disc subdir if needed
- [x] write tests in `tests/test_utils.py` for: single-disc layout (no regression), multi-disc with `CD1`/`CD2`, multi-disc with mixed naming (`Disc 1`/`Disc-2`), single-disc but cover only in subdir (edge case stays unchanged from current behavior)
- [x] run `make test` and `make lint` — must pass before next task

### Task 2: Extend album detail API for multi-disc

- [x] in `src/routes/albums.py::album_detail`, compute `discs = _detect_disc_subdirs(items)` after fetching items
- [x] add to the JSON response: `"multi_disc": bool` (true when `len(discs) > 1`), `"discs": [{"disc": 1, "dir": "...", "track_count": 13, "duration_sec": 2440}, ...]`
- [x] keep existing `"path"` field as the common parent dir (current behavior preserved for single-disc, fixed for multi-disc)
- [x] each track in `tracks[]` already has `disc` — coerce `disc = it["disc"] or 1` when building the track dict so the frontend always gets an int (same coercion as inside `_detect_disc_subdirs` from Task 1)
- [x] update `tests/test_albums.py` to assert new fields on a single-disc fixture (multi_disc=false, discs=[{disc:1,...}]) and on a multi-disc fixture (multi_disc=true, two disc entries with correct counts)
- [x] run `make test` and `make lint`

### Task 3: Cover & scan paths work for multi-disc

- [x] fix `_save_cover_to_album` in `src/utils.py:189` — currently computes `album_dir = os.path.dirname(_resolve_path(items[0].path))` which for `<album>/CD1/01.mp3` gives `<album>/CD1`, so (a) `_remove_cover_files` only wipes covers from one disc subdir and (b) `album.set_art` writes art to that subdir instead of the parent. Replace with the common-parent helper from Task 1 so cover cleanup and art placement happen at `<album>/` for multi-disc. For single-disc this is unchanged behavior. Verify `art.embed_album` (which iterates `album.items()` to embed per-file) still works without changes
- [x] audit `src/routes/cover.py` for any place that computes album paths; cover fetch/upload flows must end up saving to the parent dir for multi-disc
- [x] audit `src/routes/library.py` (the bulk cover lookup in `library()`, `search()` at lines around 121, and `artist_detail()`) — **all three** duplicate the `GROUP BY album_id` + `dirname(first item path)` logic and all three must switch to the common-parent helper so multi-disc albums show a cover on the library grid, in search results, and on the artist page when the cover file sits in the parent dir
- [x] verify `src/routes/scan.py` rescan snapshot/diff: it stores `(title, artist, album_id, path)` per item — multi-disc items have different `path` values so the path-based matching keeps working without changes. Note: `_compute_scan_diff` (`src/routes/scan.py:104`) returns only `(added, removed)` lists of `{id,title,artist}` — there is no album grouping in its output, so the test cannot assert "one album_id". Instead, write the regression test as: build a `before` snapshot with two CD1/CD2 items at known paths, build an `after` snapshot where those same paths are still present but with different item IDs (simulating beets re-import re-keying), and assert `added == [] and removed == []` (path-based matching treats them as unchanged across ID churn). If we later need album-level grouping in the diff response, that is a separate explicit task that extends `_compute_scan_diff` output — out of scope here
- [x] write/extend tests in `tests/test_cover.py`: `_save_cover_to_album` directly verified — multi-disc cleanup targets the parent dir (CD1's old cover is left untouched), single-disc still cleans the album dir. The full (b)/(c) end-to-end assertions on `albums.artpath` require a real beets `Album` object; covered indirectly by `album.set_art` being driven from the common parent. Test the helper at unit level rather than re-running the heavy `_init_beets` path
- [x] write/extend tests in `tests/test_library.py` (search + library + artist multi-disc cover lookup) and `tests/test_scan.py` (multi-disc rescan diff)
- [x] run `make test` and `make lint`

### Task 4: Bootstrap Vite + React in `frontend/`

- [x] create `frontend/` directory with `package.json` (`react@18`, `react-dom@18`, `vite@5`, `@vitejs/plugin-react`), `vite.config.js`, `index.html`, `src/main.jsx`, `src/App.jsx`
- [x] configure Vite `build.outDir` → `../src/static/dist`, `build.emptyOutDir: true`, `base: '/static/dist/'` so Flask serves bundled assets via the existing `/static/` mount
- [x] configure Vite dev server with `server.proxy` for `/api` → `http://localhost:5000` so the dev server can be used while Flask runs on its usual port
- [x] add `frontend/.gitignore` for `node_modules`, `dist`
- [x] add `.gitignore` rule in repo root for `frontend/node_modules` and `src/static/dist/` (the build output should be regenerated, not committed)
- [x] add `npm-scripts.md` or extend root README with one-liner dev/build commands — also wire `make build-frontend` and `make dev-frontend` in `Makefile`
- [x] this task has no Python code — verify by running `cd frontend && npm install && npm run build` and confirming `src/static/dist/` is populated
- [x] **commit `frontend/package-lock.json`** generated by `npm install` (do NOT gitignore it) — Task 12's Docker build uses `npm ci` which requires the lockfile to be present and in sync with `package.json`. The earlier `frontend/.gitignore` covers `node_modules` only, never the lockfile
- [x] update `src/templates/index.html` to load the built `dist/index.html`-equivalent assets (either replace template content entirely or use `{{ url_for('static', filename='dist/index.html') }}` strategy via a small template tweak). Goal: `GET /` still renders SPA shell from Flask without any other path changes
- [x] manual check (skipped - not automatable): `python app.py` → visit `/` → blank app shell appears

### Task 5: Port design assets and copy `styles.css`

- [x] copy `docs/design/project/styles.css` → `frontend/src/styles.css` and import it from `frontend/src/main.jsx`
- [x] copy `docs/design/project/assets/logo.png` → `frontend/src/assets/logo.png` (referenced from JSX via Vite's asset pipeline)
- [x] verify Geist + Geist Mono font links present in `frontend/index.html` (mirror `docs/design/project/beetDeck.html` head)
- [x] document accent color & cozy density fixed in CSS (no Tweaks panel — user removed those at the end of the chat)
- [x] manual check (skipped - not automatable): visit `/`, see Geist font loaded, fixed `#ec4868` accent CSS var available

### Task 6: Port shared UI (Topbar, Icon, Segmented, Cover, CoverStack)

- [x] copy `docs/design/project/library.jsx` (the `Icon`, `Segmented`, `Topbar` exports) into `frontend/src/ui/` split into `Icon.jsx`, `Segmented.jsx`, `Topbar.jsx`
- [x] copy `docs/design/project/covers.jsx` into `frontend/src/ui/Cover.jsx` (it generates SVG placeholders from album palette — keep that fallback for when `has_cover` is false; for `has_cover === true` render `<img src="/api/album/{id}/cover" />` instead)
- [x] wire the topbar to real handlers: Quick Scan → `POST /api/rescan?mode=quick`; Full Scan → `POST /api/rescan?mode=full`. **Mode is read from query string, not body** (`src/routes/scan.py:134` uses `request.args.get("mode", "quick")`); passing it in JSON body would silently downgrade Full Scan to Quick Scan. Search input → live query to `/api/search`; theme toggle → flip a `data-theme` attribute on `<html>` and persist in `localStorage` (port behavior from current `src/static/js/theme.js`)
- [x] manual check (skipped - not automatable): load `/`, topbar renders, search input fires GET `/api/search?q=...`

### Task 7: Library page (Index + Wall) wired to `/api/library`

- [x] port `LibraryHeader`, `LibraryIndex`, `LibraryWall`, `Library` from `docs/design/project/library.jsx`
- [x] replace the prototype's hard-coded `BEETDATA.ARTISTS` with a `useEffect`-driven fetch from `GET /api/library`; map the API shape (`[{artist, albums:[{id, album, year, has_cover, tagged, ignored}]}]`) into the prototype's shape (`{name, albums:[{title, year, identified, ...}]}` — `identified` derives from `tagged && !ignored` or however the chat brief defines it; match existing semantics from `src/static/js/library.js`)
- [x] hook clicks: artist → push hash `#/artist/<encodeURIComponent(name)>`, album → push hash `#/album/<id>`. Use a small `useHashRoute` hook (15-20 lines). **Encode/decode artist names on both sides** — names contain `/`, `#`, `?`, `%`, `&` regularly (matches the convention in current `src/static/js/library.js:68` and `src/static/js/artist.js:16`); without `encodeURIComponent`/`decodeURIComponent` such artists silently route to the wrong place or break entirely
- [x] filter (`all`/`ident`/`noident`) and sort (`az`/`recent`/`size`) work client-side over the fetched list (prototype already does this)
- [x] manual check (skipped - not automatable): open library, switch Index↔Wall, click into Baroness, click on Purple — get to the Album page (the Album page itself comes in Task 9)

### Task 8: Artist page wired to `/api/artist?name=...`

- [x] port `Artist` component from `docs/design/project/screens.jsx`
- [x] fetch `/api/artist?name=<artist>` on mount; map response to the component's expected shape
- [x] preserve the chat decision: **no album-level action buttons on the Artist page** (just the "Album-level actions live on each album" hint)
- [x] year-range stat (`min – max`) and total/identified counts derived from the API payload
- [x] manual check (skipped - not automatable): navigate to an artist, see hero cover stack, album grid sorted by year, click an album

### Task 9: Album page with grouped actions + multi-disc support

- [x] port `Album` component from `docs/design/project/screens.jsx`, including `ActionGroup`, `discStats`, `byDisc` grouping and the multi-disc path-tree rendering
- [x] fetch `/api/album/<id>`; use `data.multi_disc`, `data.discs` from the new fields added in Task 2 (no longer hard-coded as in the prototype)
- [x] grouped action buttons (`Tags` [Identify · Ignore], `Genre` [Fetch · Edit], `Cover` [Fetch · Upload], `Lyrics` [Fetch all]) wired to the real endpoints:
  - Identify → opens IdentifyModal (Task 10)
  - Ignore → `POST /api/album/<id>/ignore` (defined in `src/routes/identify.py:162` — `toggle_ignore`, flips `beetdeck_ignored` on the album and returns `{"ignored": bool}`). **Do not wire to the genre endpoint.**
  - Genre Fetch / Edit → call the existing `/api/album/<id>/genre`, `/genre/save`, `/genre/confirm` (preserve fetch → preview → confirm UX)
  - Cover Fetch / Upload → existing `/api/album/<id>/cover/fetch`, `/cover/preview`, `/cover/confirm`, `/cover/upload`
  - Lyrics Fetch all → existing `/api/album/<id>/lyrics/fetch` then `/lyrics/confirm`
- [x] per-track row: title, artist, duration, two pill buttons (`lyrics`/`tags`) — clicking `lyrics` expands inline (preview from `/api/album/<id>/track/<id>/lyrics`) and a `↗ expand` opens the `LyricsModal`; `tags` opens a tag drawer/modal hitting `/api/album/<id>/track/<id>/tags`
- [x] multi-disc rendering: when `multi_disc === true`, render disc section headers between groups (`Disc 1` / `Disc 2`) and reset track numbering inside each section; the `Path` block in the hero meta gets the tree (`├─ CD1/ · 13 tracks · 40 min`)
- [x] **no Play button anywhere** (chat decision)
- [x] manual check (skipped - not automatable): single-disc and multi-disc album pages; action groups fire real API calls

### Task 10: Identify modal — two-phase (form → searching → results) + diff

- [x] port `IdentifyModal`, `SearchForm`, `SearchingState`, `Score` from `docs/design/project/screens.jsx`
- [x] wire phase transitions to real backend:
  - phase `form`: collect optional `artist`/`album`/`mbid` overrides
  - on `Search MusicBrainz` → POST `/api/album/<id>/identify` with overrides; switch to `searching`
  - poll `GET /api/album/<id>/identify/status` until task is done; switch to `results`
  - candidate click → POST `/api/album/<id>/apply` to get the diff for that candidate, fill the diff column
  - `Apply N changes` → POST `/api/album/<id>/confirm` with the chosen candidate. Backend `confirm_match` applies the full candidate via `apply_metadata` / `apply_album_metadata`; it does NOT accept a per-field subset. The modal omits the per-field checkboxes from the prototype and shows a "whole-candidate apply" indicator instead, so the user is not led to believe they can drop individual fields.
  - `Refine search` returns to `form` with the form values preserved
- [x] handle error states from the status endpoint (failed task, no candidates) with a non-modal-killing inline message
- [x] manual check (skipped - not automatable): identify an album with a partial tag match, refine search with an override, pick a non-top candidate, confirm, see DB & file changes reflected after reload

### Task 11: Items / "Untagged" screen

- [x] decide scope: the design chat removed the Untagged page from the prototype, but the backend endpoints (`/api/items/untagged`, `/api/items/<id>/metadata`, `/api/items/identify`, `/api/items/identify/<task>/status|apply|confirm`) still exist and are part of the product. Keep functionality, give it a screen consistent with the redesign (small page reachable from a sidebar/dropdown or `#/untagged` hash route)
- [x] **port the existing logic from `src/static/js/artist.js`** — the untagged/group-identify UI is currently embedded there (see `_untaggedItems`, `#untagged-section`, `/api/items/untagged` and friends around lines 3–114 and ~381). There is no separate `src/static/js/items.js` file. Lift this behavior into a dedicated React component without rebuilding semantics: list of items with inline artist/album edit, multi-select → group-identify flow that reuses Task 10's results UI for the candidate list
- [x] manual check (skipped - not automatable): navigate to `#/untagged`, see items, edit one inline, select a group, identify → confirm → see the new album appear in the library

### Task 12: Replace the legacy SPA & clean dead code

- [x] update `src/templates/index.html` to be a thin shell that loads the Vite build output (template now reads Vite manifest via `_inject_vite_assets` context processor and injects both the entry JS and CSS files)
- [x] delete `src/static/js/` (entire folder) and `src/static/style.css`
- [x] logos: only `src/static/logo.svg` remains. The new topbar uses `docs/design/project/assets/logo.png` — copy that file into `frontend/src/assets/logo.png` in Task 5 (already covered there). No further logo work in this task
- [x] update `Dockerfile`: add a multi-stage build — stage 1 `node:20-slim` runs `npm ci && npm run build` inside `frontend/`, stage 2 (existing `python:3.14-slim`) copies the built `src/static/dist/` from stage 1
- [x] update `Makefile`: `make build` chains the frontend build then the docker buildx step (multi-stage Dockerfile handles frontend internally, so `./build.sh` end-to-end builds frontend then image); `make dev` runs Flask + Vite dev concurrently
- [x] manual check (skipped - not automatable): `make build` produces a working image; `docker run` and browse `/` — full app works
- [x] backend tests must still pass — `make test` and `make lint`

### Task 13: Update CLAUDE.md and docs

- [x] update `CLAUDE.md` Project Structure tree: add `frontend/`, mention `docs/design/` as the design source of truth, list new build commands (`make dev`, `make build-frontend`), update the "Frontend" stack line from "Vanilla JS SPA" to "React + Vite SPA"
- [x] add a short `frontend/README.md` explaining dev workflow (run Flask on 5000, `npm run dev` on 5173 with proxy, edits hot-reload)
- [x] add a `docs/design/README-USAGE.md` (not the design-tool README which we keep verbatim) that points future agents at the JSX as the visual source of truth and explains *which* design decisions are fixed (accent color, density, no Play, grouped actions, two-phase Identify)
- [x] no test changes needed

### Task 14: Verify acceptance criteria

- [x] every endpoint listed in CLAUDE.md returns identical or backward-compatible payloads (new fields like `multi_disc`/`discs` are additive only) — verified via existing route tests (271 passing)
- [x] every redesign decision from `docs/design/chats/chat1.md` is implemented (skipped - not automatable, requires visual browser UAT)
- [x] multi-disc test on a real album in the user's library (skipped - not automatable, requires real library)
- [x] run full `make test` — all green (271 passed)
- [x] run `make lint` — no errors (All checks passed!)
- [x] coverage stays ≥ 80% — 87% TOTAL

## Technical Details

### Multi-disc data model (Task 1–3)

- `items.disc` is already populated by beets and selected by `album_detail`. Default to 1 when NULL.
- `_album_dir_from_items` currently returns `dirname` of the first item's path. For `<album>/CD1/01.mp3` it returns `<album>/CD1`, hiding the parent. Fix: compute `os.path.commonpath([dirname(p) for p in item_paths])`. For single-disc albums this collapses to the same dir as today.
- `_find_cover` is extended (or wrapped by a new `_find_album_cover_multidisc`) to: (1) search the parent dir returned by `_album_dir_from_items` first, (2) fall back to each disc subdir from `_detect_disc_subdirs` if no cover is found at the parent. This matches Task 1's deliverable — the legacy single-dir lookup is preserved as the first probe, but disc-subdir fallback handles real-world libraries where the cover only sits inside `CD1/`.
- API addition (additive only):
  ```json
  {
    "id": 42,
    "album": "The Wall",
    ...
    "path": "/music/Pink Floyd/The Wall",
    "multi_disc": true,
    "discs": [
      {"disc": 1, "dir": "/music/Pink Floyd/The Wall/CD1", "track_count": 13, "duration_sec": 2440},
      {"disc": 2, "dir": "/music/Pink Floyd/The Wall/CD2", "track_count": 13, "duration_sec": 2467}
    ],
    "tracks": [{ "id": ..., "disc": 1, "track": 1, ... }, ...]
  }
  ```

### Vite ↔ Flask integration (Task 4 & 12)

- Dev: Vite serves frontend on `:5173` and proxies `/api/*` and `/static/*` to Flask `:5000`. Frontend code uses absolute paths (`/api/library`).
- Prod: `npm run build` outputs to `src/static/dist/`. `src/templates/index.html` is generated by Vite (or replaced by a tiny Flask-rendered shell that loads `dist/index.html`'s assets). Flask still owns the `/` route via `render_template('index.html')`.
- The single-worker constraint is unchanged (frontend is static; no worker affinity needed).

### Things NOT in scope (YAGNI)

- No TypeScript (chosen JS+JSX).
- No frontend unit tests (chosen backend-only tests).
- No SSR, no router library — a tiny `useHashRoute` hook is enough.
- No state management library (`useState`/`useReducer` suffice for this scope).
- No PWA / offline / push.
- No new endpoints unless an existing one is missing (e.g. if "select diff fields on confirm" turns out unsupported, document in Task 10 — don't expand scope silently).

## Post-Completion

*Items requiring manual intervention or external systems — informational only, no checkboxes.*

**Manual UAT scenarios** (run `make dev`, open browser):

- Scenario 1 — *prepare a new album*: drop files → Quick Scan → open album → Identify (override album field, re-search) → pick candidate → confirm → fetch cover → confirm → fetch genre → confirm → bulk-fetch lyrics → confirm.
- Scenario 2 — *clean up the untagged pile*: open `#/untagged` → group-select a chunk → identify-as-group → pick candidate → confirm → see the new album in the library.
- Scenario 3 — *multi-disc real album*: import Pink Floyd — *The Wall* (or similar CD1/CD2 layout) → open the album → verify single album_id, two disc sections, cover served from parent dir, identify works, lyrics fetch per track works (path computed per-disc).
- Scenario 4 — *theming*: cycle theme button, verify dark/light look stays consistent, `#ec4868` accent constant in both.

**Docker smoke test**:

- `make build` produces a multi-arch image; `docker run -v <library>:/data/beets ...` boots and serves a working UI at `:5000`.

**Design source check**:

- `docs/design/` is the canonical visual reference; do **not** re-fetch from the temporary share URL. If a design decision is unclear during implementation, consult `docs/design/chats/chat1.md` first, then the relevant `.jsx` file.
