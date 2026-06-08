# beetDeek: Migration and Refactoring

## Overview
- Migrate from beets 2.8.0 to 2.10.0 (multi-value genre fields, relative paths, lyrics plugin changes)
- Restructure the monolithic beetDeek application into a modular Flask Blueprints architecture under `src/`
- Add unit test coverage for the new modular structure
- Create and maintain CLAUDE.md project documentation

## Context
- **Primary file**: `app.py` (53KB, ~1680 lines) — all routes, helpers, and state in one file
- **Frontend**: `templates/index.html` — vanilla JS SPA with hash routing, ~1500 lines
- **Static**: `static/style.css` (21KB), `static/logo.svg`
- **Build**: Dockerfile, build.sh, Makefile (README contains a compose snippet but no tracked docker-compose.yml)
- **Dependencies**: Flask, gunicorn (1 worker/4 threads), beets==2.8.0 (migrating to 2.10.0), Pillow, pylast, requests, beautifulsoup4
- **In-memory state** (actual globals in app.py:26-30):
  - `_rescan_lock` (threading.Lock) — guards scan process access
  - `_rescan_proc` (subprocess.Popen | None) — running scan process
  - `_rescan_snapshot` (dict | None) — item snapshot `{id: (title, artist, album_id)}` taken before scan
  - `_identify_tasks` (dict) — background autotag tasks keyed by album_id; also stores cover previews under `f"cover_{album_id}"` keys (app.py:593,623)
  - `_identify_lock` (threading.Lock) — guards identify task access
- **No existing tests, CLAUDE.md, pytest config, or linting setup**

## Target Structure
```
src/
├── __init__.py          # Flask app factory, register blueprints
├── routes/
│   ├── __init__.py
│   ├── library.py       # GET /, /api/library, /api/search, /api/artist
│   ├── albums.py        # GET /api/album/<id>, /api/album/<id>/track/<id>/tags
│   ├── cover.py         # cover art CRUD (GET/POST cover, preview, confirm, upload)
│   ├── genres.py        # genre fetch/confirm/save
│   ├── lyrics.py        # lyrics CRUD (fetch/confirm/embed/save, single + bulk)
│   ├── identify.py      # identify/status/apply/confirm
│   └── scan.py          # rescan + status
├── state.py             # shared in-memory state (see actual globals above)
├── utils.py             # shared helpers: _decode_path, _get_ro_conn, _init_beets, _find_cover, _album_dir_from_items, _format_genre, _remove_cover_files, _resize_image, _save_cover_to_album, COVER_NAMES
├── static/              # moved from root
│   ├── style.css
│   └── logo.svg
└── templates/           # moved from root
    └── index.html
```

## Development Approach
- **Testing approach**: Regular (code first, then tests)
- Complete each task fully before moving to the next
- Make small, focused changes
- **Constraint**: Keep single-worker gunicorn architecture for in-memory state
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
- **CRITICAL: all tests must pass before starting next task**
- **CRITICAL: update this plan file when scope changes during implementation**
- Run tests after each change
- Maintain backward compatibility for all API routes

## Testing Strategy
- **Unit tests**: pytest with Flask test client for each route blueprint
- **Test structure**: `tests/` directory at project root with test files mirroring `src/routes/`
- **Fixtures**: shared Flask app fixture with temporary SQLite file (not `:memory:` — `_get_ro_conn()` opens new URI connections that can't share `:memory:` state, and routes check `os.path.isfile(LIBRARY_DB)`)
- **Coverage target**: 80%+ for new modular code
- **Tooling**: pytest + pytest-cov + ruff (lint/format), configured in `pyproject.toml`

## Progress Tracking
- Mark completed items with `[x]` immediately when done
- Add newly discovered tasks with ➕ prefix
- Document issues/blockers with ⚠️ prefix
- Update plan if implementation deviates from original scope

## Implementation Steps

### Task 1: Create CLAUDE.md with current structure documentation
- [x] create CLAUDE.md at project root documenting current architecture, tech stack, dev commands, and conventions
- [x] document the current monolithic structure (app.py, templates, static)
- [x] document the target modular structure and refactoring goals
- [x] document constraint: single gunicorn worker required for in-memory state

### Task 2: Bootstrap test and lint tooling
- [x] create `requirements.txt` with runtime dependencies currently only listed in Dockerfile (flask, gunicorn, pyacoustid, requests, pylast, beautifulsoup4, Pillow) plus `beets` **without version pin** — beets version is controlled exclusively via `constraints.txt`
- [x] create `constraints.txt` with `beets==2.8.0` — single source of truth for beets version. Both local dev (`pip install -r requirements.txt -c constraints.txt`) and Dockerfile use the same file. Task 12 (beets migration) bumps this to `2.10.0`
- [x] create `pyproject.toml` with pytest, pytest-cov, and ruff configuration
- [x] create `requirements-dev.txt` with test/lint tools (pytest, pytest-cov, ruff) that layers on top of `requirements.txt`
- [x] update Dockerfile: `COPY requirements.txt constraints.txt ./` and `pip install -r requirements.txt -c constraints.txt` — constraints file pins beets. Remove `ARG BEETS_VERSION` and the old inline `pip install "beets==${BEETS_VERSION}"` — `constraints.txt` is now the single source of truth, no duplicate version reference
- [x] create `tests/__init__.py` and `tests/conftest.py` with DB-level fixtures only (no Flask app fixture yet — `create_app()` doesn't exist until Task 3): temporary SQLite file via `tmp_path` seeded with beets schema tables (`albums`, `items`, `album_attributes`, `item_attributes`), a helper to insert test rows
- [x] add `Makefile` targets: `test` (pytest), `lint` (ruff check), `fmt` (ruff format), `coverage` (pytest-cov)
- [x] write a trivial smoke test that creates the temp DB, inserts a row, and reads it back — verifies the fixture works without needing Flask
- [x] run `make test` — must pass before next task

### Task 3: Create src/ package with app factory and shared modules
- [x] create `src/__init__.py` with `create_app(test_config=None)` factory function that accepts optional config dict for overriding `LIBRARY_DB`, `IMPORT_DIR`, and other settings (enables test fixtures to inject temporary DB file paths without monkeypatching module-level constants)
- [x] move `LIBRARY_DB`, `IMPORT_DIR` from module-level env reads into `app.config` defaults inside `create_app()`, so all route modules read from `current_app.config` instead of global constants
- [x] create `src/state.py` — extract the actual globals: `_rescan_lock`, `_rescan_proc`, `_rescan_snapshot`, `_identify_tasks`, `_identify_lock` (note: cover previews are stored in `_identify_tasks` under `f"cover_{album_id}"` keys — preserve this, do not create a separate `cover_previews` dict)
- [x] create `src/utils.py` — extract shared helpers used across multiple route groups:
  - **Cross-cutting** (used by 3+ blueprints): `_decode_path()`, `_get_ro_conn()`, `_find_cover()`, `_album_dir_from_items()`, `_init_beets()`, `COVER_NAMES`, `ULOWER` registration
  - **Multi-route helpers**: `_format_genre()` (library, albums, genres), `_remove_cover_files()` (cover), `_resize_image()` (cover), `_save_cover_to_album()` (cover)
  - **Constants and module-level state these helpers depend on** (must move with them):
    - `_COVER_STEMS`, `_IMAGE_EXTS` (used by `_remove_cover_files()`, app.py:63-64)
    - `COVER_HIRES_MAX`, `COVER_EMBED_MAX`, `COVER_EMBED_QUALITY` (used by `_save_cover_to_album()`, app.py:507-509)
    - `_beets_initialized` flag (used by `_init_beets()`, app.py:128)
    - `_beets_log` / `_BeetsLogAdapter` (used by `_save_cover_to_album()` and beets internals, app.py:84-93)
    - `log` logger instance (used throughout helpers, app.py:24)
  - **Lyrics-specific** (keep in `src/routes/lyrics.py`): `_find_lrc_file()`, `_read_lrc_file()`
  - **Identify-specific** (keep in `src/routes/identify.py`): `_serialize_candidate()`, `_run_identify()`, `_get_task_json()`
  - **Scan-specific** (keep in `src/routes/scan.py`): `_take_snapshot()`, `_compute_scan_diff()`
  - Update `_get_ro_conn()` to read DB path from `current_app.config["LIBRARY_DB"]`
  - Update `_init_beets()` to accept an explicit `library_db` parameter (`_init_beets(library_db)`) instead of reading from `current_app.config` — this is required because `_run_identify()` calls `_init_beets()` from a background thread that has no Flask app context (`current_app` would raise `RuntimeError`). Callers in request handlers pass `current_app.config["LIBRARY_DB"]`; the background thread receives the path as an argument when spawned
- [x] create `src/routes/__init__.py`
- [x] update `tests/conftest.py` app fixture to use `create_app(test_config={"LIBRARY_DB": "<tmp_file_path>", ...})` with a `tmp_path`-based SQLite file seeded with beets schema
- [x] verify app factory can create a minimal Flask app with both default and test configs
- [x] write tests for utility functions in `tests/test_utils.py`
- [x] run tests — must pass before next task

### Task 4: Extract library routes (library blueprint)
- [x] create `src/routes/library.py` — blueprint with `GET /`, `GET /api/library`, `GET /api/search`, `GET /api/artist`
- [x] register library blueprint in app factory
- [x] write tests for library endpoints in `tests/test_library.py`
- [x] run tests — must pass before next task

### Task 5: Extract album and track routes (albums blueprint)
- [x] create `src/routes/albums.py` — blueprint with `GET /api/album/<id>`, `GET /api/album/<id>/track/<id>/tags`
- [x] register albums blueprint in app factory
- [x] write tests for album endpoints in `tests/test_albums.py`
- [x] run tests — must pass before next task

### Task 6: Extract cover art routes (cover blueprint)
- [x] create `src/routes/cover.py` — blueprint with all cover art endpoints (GET cover, POST fetch, GET preview, POST confirm, POST upload)
- [x] preserve existing cover preview storage: cover previews are stored in `state.identify_tasks[f"cover_{album_id}"]` (app.py:593,623) — do NOT create a separate `cover_previews` dict, keep using the shared `identify_tasks` store with `cover_` prefix keys
- [x] register cover blueprint in app factory
- [x] write tests for cover endpoints in `tests/test_cover.py` (backend route behavior: response codes, JSON shape, state mutations)
- [x] run tests — must pass before next task

### Task 7: Extract genre routes (genres blueprint)
- [x] create `src/routes/genres.py` — blueprint with genre fetch/confirm/save endpoints
- [x] register genres blueprint in app factory
- [x] write tests for genre endpoints in `tests/test_genres.py`
- [x] run tests — must pass before next task

### Task 8: Extract identification routes (identify blueprint)
- [x] create `src/routes/identify.py` — blueprint with identify/status/apply/confirm endpoints
- [x] use `state.identify_tasks` and `state.identify_lock` from `src/state.py`
- [x] update `_run_identify()` to accept `library_db` as a parameter instead of calling `_init_beets()` with no args; the request handler reads `current_app.config["LIBRARY_DB"]` and passes it when spawning the thread
- [x] register identify blueprint in app factory
- [x] write tests for identify endpoints in `tests/test_identify.py`
- [x] run tests — must pass before next task

### Task 9: Extract lyrics routes (lyrics blueprint)
- [x] create `src/routes/lyrics.py` — blueprint with all lyrics endpoints (single + bulk fetch/confirm/embed/save)
- [x] register lyrics blueprint in app factory
- [x] write tests for lyrics endpoints in `tests/test_lyrics.py`
- [x] run tests — must pass before next task

### Task 10: Extract scan routes (scan blueprint)
- [x] create `src/routes/scan.py` — blueprint with rescan and status endpoints
- [x] use `state.rescan_lock`, `state.rescan_proc`, `state.rescan_snapshot` from `src/state.py`
- [x] extract `_take_snapshot()` and `_compute_scan_diff()` into scan module
- [x] register scan blueprint in app factory
- [x] write tests for scan endpoints in `tests/test_scan.py`
- [x] run tests — must pass before next task

### Task 11: Remove old app.py and update entry point
- [x] create new `app.py` at project root as thin entry point that calls `create_app()`
- [x] move `static/` and `templates/` into `src/`
- [x] update Dockerfile to reflect new structure (COPY src/, entry point)
- [x] update Makefile if needed
- [x] verify Docker build succeeds (`docker build .`) [x] manual test (skipped - not automatable in CI)
- [x] run all tests — must pass before next task

### Task 12: Migrate from beets 2.8.0 to 2.10.0
- [x] update `constraints.txt` from `beets==2.8.0` to `beets==2.10.0` (single source of truth for beets version — `ARG BEETS_VERSION` was removed in Task 2, so this is the only place to change)
- [x] verify `beets==2.10.0` is available on PyPI (`pip install beets==2.10.0`). If not yet published, use the GitHub release tarball URL in `constraints.txt`: `beets @ https://github.com/beetbox/beets/archive/refs/tags/v2.10.0.tar.gz` (does not require `git` in the Docker image, unlike `git+https://` which would need adding `git` to Dockerfile build deps)
- [x] verify Dockerfile base image is Python >=3.10 and compatible with beets 2.10.0 (current image is `python:3.14-slim` — already satisfies the requirement, no change needed unless beets 2.10.0 has an upper bound)
- [x] **Genre field migration**: beets 2.10.0 stores genres as a multi-value list field (`genres`) instead of a single `genre` string. The lastgenre plugin's `separator` config option is removed. Update:
  - `_format_genre()` in `src/utils.py` — handle list-type `genres` values natively (no more null-byte/separator parsing needed for new data, but keep backward compat for pre-migration DB entries)
  - Genre routes (`src/routes/genres.py`): `save_genre()` now writes only `album.genres = genres_list` (removed `hasattr` guards and singular `genre` field writes — deprecated in 2.10.0, removed in 3.0)
  - Genre preview (`fetch_genre_preview`): reads `album.get("genres", "")` — works with 2.10.0 list storage
  - Album detail (`src/routes/albums.py`): genre display reads `a["genres"]` then falls back to `a["genre"]` — backward compat preserved
- [x] **Relative paths**: beets 2.10.0 stores library paths relative to database root instead of absolute. Existing databases auto-migrate on first `beet` command. Update:
  - Added `LIBRARY_ROOT` to `app.config`, sourced from `beets.library.Library(db_path).directory` at startup (with try/except for environments without beets); also configurable via `BEETS_LIBRARY_ROOT` env var
  - Added `_resolve_path()` helper in `src/utils.py` that joins `LIBRARY_ROOT` + relative path when path is not absolute (backward compat: absolute paths pass through unchanged)
  - Updated `_album_dir_from_items()` to use `_resolve_path()`
  - Updated `albums.py` artpath and item path resolution to use `_resolve_path()`
  - Updated `cover.py` artpath resolution to use `_resolve_path()`
  - Scan `_take_snapshot()` uses IDs/titles/artist (no filesystem paths) — no change needed
- [x] **Lyrics plugin changes**: 2.10.0 removed "Source: URL" suffix from lyrics text; backend name now in `lyrics_backend`, URL in `lyrics_url`, language in `lyrics_language`. lyrics route already stores these separate fields via `getattr(new_lyrics, key)` in confirm handler — no changes needed
- [x] **fetchart plugin**: 2.10.0 adds WebP support. `_resize_image()` already handles RGBA/P (WebP with transparency) via `.convert("RGB")` before JPEG save — no changes needed
- [x] **MusicBrainz API changes**: 2.10.0 uses normalized field names. `_serialize_candidate()` uses `info.artist`, `info.album`, `info.year`, `info.data_source` — these field names are stable across 2.8–2.10
- [x] update tests for all changed behavior
- [x] run full test suite — must pass before next task

### Task 13: Split frontend JavaScript if needed
- [x] analyze `src/templates/index.html` JS size and complexity (file moved to `src/` in Task 11)
- [x] if over 500 lines of JS: extract into separate `.js` files under `src/static/js/` by feature area (routing, library, album, cover, lyrics, identify, scan, search)
- [x] if under 500 lines: keep inline but add clear section comments
- [x] verify frontend works correctly after changes [x] manual test (skipped - not automatable)
- [x] run tests — must pass before next task

### Task 14: Verify acceptance criteria
- [x] verify all routes from original app.py are accessible and functional
- [x] verify in-memory state works correctly across requests (identify tasks, rescan process/snapshot)
- [x] verify beets 2.10.0 migration: genre multi-value, relative paths, lyrics fields
- [x] run full test suite
- [x] run linter (`make lint`) — all issues must be fixed
- [x] verify test coverage meets 80%+ standard (`make coverage`)

### Task 15: [Final] Update documentation
- [x] update CLAUDE.md with final modular structure, test commands, and conventions
- [x] update README.md if needed (new project structure, dev setup with tests, beets 2.10.0 requirement)

## Technical Details

### App Factory Pattern
```python
# src/__init__.py
def create_app(test_config=None):
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["LIBRARY_DB"] = os.environ.get("BEETS_LIBRARY_DB", "/data/beets/library.db")
    app.config["IMPORT_DIR"] = os.environ.get("BEETS_IMPORT_DIR", "/music")
    # LIBRARY_ROOT is added in Task 12 (beets 2.10.0 migration) — sourced from
    # beets.library.Library(...).directory at startup. Required for resolving
    # relative paths that beets 2.10.0 stores in the DB.
    if test_config:
        app.config.update(test_config)
    from .routes.library import bp as library_bp
    from .routes.albums import bp as albums_bp
    # ... register all blueprints
    app.register_blueprint(library_bp)
    app.register_blueprint(albums_bp)
    return app
```

### Shared State Module (mirrors actual app.py:26-30 globals)
```python
# src/state.py
import threading

# Identification + cover preview state
# Cover previews are stored here too, keyed as f"cover_{album_id}"
# (see app.py:593 for write, app.py:623 for read)
identify_tasks = {}          # {album_id: {"status": ..., "candidates": ..., "lib": ...},
                             #  f"cover_{album_id}": {"candidate_path": ..., "source": ...}}
identify_lock = threading.Lock()

# Rescan state
rescan_lock = threading.Lock()
rescan_proc = None           # subprocess.Popen | None
rescan_snapshot = None       # {item_id: (title, artist, album_id)} | None
```

### Beets 2.8.0 → 2.10.0 Migration Impact

**2.9.0 changes:**
- Multi-value fields: `remixer`→`remixers`, `lyricist`→`lyricists`, `composer`→`composers`, `arranger`→`arrangers`. Auto-migrated. beetDeek doesn't use these fields directly — low impact.
- lastgenre: user-configurable ignorelist added. No breaking change for beetDeek.
- fetchart: WebP support added. `_resize_image()` must handle WebP input via Pillow.

**2.10.0 changes:**
- **Relative paths (HIGH IMPACT)**: Library paths stored relative to DB root. Auto-migrates on first `beet` command. All direct SQL queries reading `items.path` or `albums.artpath` return relative paths — must be resolved against `beets.library.Library(...).directory` (the beets `directory` config). Add `LIBRARY_ROOT` to app config, sourced from Library instance. Affects: `_album_dir_from_items()`, `_find_cover()`, cover serving, lyrics `.lrc` file lookup, scan snapshot paths.
- **Genre as list (HIGH IMPACT)**: `genres` is now a native list field, no more null-byte separators. lastgenre `separator` config removed. `_format_genre()` already handles list input (app.py:35-36) but SQL reads via `_get_ro_conn()` will return the raw DB representation — verify list deserialization.
- **Lyrics plugin**: Removed "Source: URL" suffix from text. `lyrics_backend`, `lyrics_url`, `lyrics_language` stored as separate fields. Low impact for beetDeek — we just display `result.text`.
- **MusicBrainz API**: Normalized field names (underscores). TypedDict models. Verify `_serialize_candidate()` field access.
- **Python >=3.10 required**: Current `python:3.14-slim` already satisfies this — verify no upper-bound conflict with beets 2.10.0.
- **`album_for_id()`/`track_for_id()` require `data_source` arg** (since 2.8.0): beetDeek uses `lib.get_album(album_id)` — not affected.

## Post-Completion
*Items requiring manual intervention or external systems — no checkboxes, informational only*

**Manual verification:**
- Test with a real beets library to verify all routes work with new modular structure
- Test beets 2.10.0 auto-migration of existing library (genre fields, relative paths)
- Verify Docker image builds and runs correctly with new structure
- Test in both light and dark themes
