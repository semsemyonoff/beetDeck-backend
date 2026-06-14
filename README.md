# beetDeck backend

Flask + gunicorn API and [beets](https://beets.io/) integration for beetDeck — the
**backend part** of the product. It exposes the `/api` endpoints and serves the
pre-built SPA on `:5000`. There is no shared code or filesystem with the
frontend; the React SPA lives in a **separate repository** and talks to this
backend purely over HTTP.

The full product overview, install, configuration (env vars, volumes, beets
plugins), and release process live in the separate **`beetDeck/deploy`**
repository — the **entry point**, which pins this backend and the frontend as
submodules and builds the single published image (`semsemyonoff/beetdeck`). This
repo is source for the backend only.

## Requirements

- Python 3.12+ (the production image uses 3.14)
- beets 2.10.0

## Architecture

- **Flask + gunicorn** (1 worker, 4 threads). Single worker is required because
  in-memory state (identification tasks, cover previews) must be shared across
  request threads. Routes are organized as Flask Blueprints under `src/routes/`.
- **beets integration**: uses the beets Python API directly (not the CLI) for
  identification, genre lookup, cover art, and lyrics. The CLI is only used for
  `beet import` (rescan) and `beet update` (prune stale entries). Genres are
  stored as native list fields; library paths are stored relative to the database
  root and resolved at runtime.
- **SPA serving**: in production the frontend's build is dropped into
  `src/static/dist/` and served by Flask — the Vite manifest is read at request
  time to inject the hashed JS/CSS into `src/templates/index.html`.

The app reads `BEETS_LIBRARY_DB`, `BEETS_IMPORT_DIR`, `BEETS_LIBRARY_ROOT`, and
`BEETSDIR` from the environment — see the table in `src/CLAUDE.md` for defaults.

## Local development

Dependencies are declared in `pyproject.toml` (runtime under `[project]`, tooling
under the `dev` extra). Install them into a virtualenv with an editable install:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# Run the Flask dev server on :5000
make run

# Run tests
make test

# Lint / format
make lint
make fmt

# Coverage report
make coverage
```

The frontend SPA lives in its own repository and is served by Flask in
production from `src/static/dist/` (the Vite manifest is read at request time to
inject the hashed JS/CSS into `src/templates/index.html`). To exercise the full
UI locally, use the DWE dev workspace — the `frontend` service runs the Vite dev
server (HMR) and proxies the API to this backend.

Producing the production image (which builds the SPA and bakes it in) is the job
of the `beetDeck/deploy` repository; see its README.

## API reference

### Library & browsing

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/library` | List all artists with their albums |
| GET | `/api/search?q=<query>` | Search artists, albums, and tracks by name |
| GET | `/api/artist?name=<name>` | Get all albums by a specific artist |
| GET | `/api/album/<id>` | Album details with full track listing |

### Rescan

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/rescan?mode=quick\|full` | Start library rescan (default: quick) |
| GET | `/api/rescan/status` | Poll rescan status (`idle` / `running` / `done`) |

### Cover art

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/album/<id>/cover` | Serve current album cover |
| POST | `/api/album/<id>/cover/fetch` | Search for cover art online (preview) |
| GET | `/api/album/<id>/cover/preview` | Serve fetched cover preview |
| POST | `/api/album/<id>/cover/confirm` | Save high-res + embed thumbnail |
| POST | `/api/album/<id>/cover/upload` | Upload custom cover image |

### Identification & tags

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/album/<id>/track/<tid>/tags` | Read all tags from a track file |
| POST | `/api/album/<id>/ignore` | Toggle album ignored flag (returns `{"ignored": bool}`) |
| POST | `/api/album/<id>/identify` | Start MusicBrainz autotag |
| GET | `/api/album/<id>/identify/status` | Poll identification progress and candidates |
| POST | `/api/album/<id>/apply` | Preview tag changes for a candidate |
| POST | `/api/album/<id>/confirm` | Write selected tags to files |

### Genre

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/album/<id>/genre` | Fetch genre preview from Last.fm |
| POST | `/api/album/<id>/genre/confirm` | Write genre tags to files |
| POST | `/api/album/<id>/genre/save` | Save manually entered genre tags |

### Lyrics

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/album/<id>/track/<tid>/lyrics` | Get current lyrics (database or .lrc file) |
| POST | `/api/album/<id>/track/<tid>/lyrics/fetch` | Search lyrics online (preview) |
| POST | `/api/album/<id>/track/<tid>/lyrics/confirm` | Write fetched lyrics |
| POST | `/api/album/<id>/track/<tid>/lyrics/embed` | Embed external .lrc into audio file tag |
| POST | `/api/album/<id>/track/<tid>/lyrics/save` | Save manually edited lyrics |
| POST | `/api/album/<id>/lyrics/fetch` | Bulk fetch lyrics for all album tracks |
| POST | `/api/album/<id>/lyrics/confirm` | Write lyrics for selected tracks |
| POST | `/api/album/<id>/lyrics/embed` | Bulk embed .lrc files for all album tracks |

### Untagged items

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/items/untagged` | List items belonging to albums with no albumartist |
| POST | `/api/items/<id>/metadata` | Update artist/album fields on a single item |
| POST | `/api/items/metadata-batch` | Album/folder-aware batch write: album-level fields (`album`/`albumartist`/`year`/`genre`) propagated to the album row **and** flushed to every member file, plus per-track overrides (`title`/`artist`/`track`/`disc`), in one locked request. Returns `{status, warnings, albums_updated}` |
| POST | `/api/items/identify` | Start MusicBrainz autotag for a group of item IDs |
| GET | `/api/items/identify/<task_id>/status` | Poll identification progress and candidates |
| POST | `/api/items/identify/<task_id>/apply` | Preview tag changes for a candidate |
| POST | `/api/items/identify/<task_id>/confirm` | Create album and write matched tags to files |
