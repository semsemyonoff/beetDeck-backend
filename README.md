# beetDeck

![logo](src/static/logo.svg)

A web interface for managing a [beets](https://beets.io/) music library. beetDeck does not handle file importing — it enhances an existing library with identification (MusicBrainz autotag), genres (Last.fm), cover art, lyrics, and tag browsing.

Works well alongside tools like Lidarr that manage the file library, while beets serves as an additional tagging layer on top.

## Features

- **Library browser** — collapsible artist list with album thumbnails, year, and tagged status (Index and Wall layout)
- **Artist page** — dedicated page with album grid for a single artist
- **Album detail** — cover art, metadata, file path, full track listing with per-track action buttons
- **Multi-disc albums** — disc sections shown separately on the album page; cover art and file paths resolve to the common parent directory (e.g. `CD1/` / `CD2/` layouts)
- **Search** — full-text search across artists, albums, and tracks (full Unicode support)
- **Filter by status** — filter albums by tagged / untagged on library and artist pages
- **Identification** — MusicBrainz autotag with candidate selection, diff preview, confirm & write
- **Genre tagging** — Last.fm genre lookup with old/new preview and confirm
- **Cover art** — fetch from multiple sources (filesystem, Cover Art Archive, iTunes, Amazon), preview, confirm; manual upload supported. High-res file + embedded thumbnail
- **Lyrics** — per-track and bulk album lyrics via lrclib (synced and plain text). Inline viewer/editor, online search with diff preview, external `.lrc` file embed
- **Rescan** — quick (incremental) or full library rescan to add new files and remove stale entries
- **Untagged triage** — loose files (no albumartist) are grouped by directory and surfaced in a pinned banner atop the library; a per-folder editor offers bulk album-level fields plus a per-track grid, then a MusicBrainz identify hand-off to create a properly tagged album
- **Tag editing** — edit album-level and per-track tags (title, artist, track #, year, genre) directly from the album page via an *Edit tags* modal; album-level changes propagate to the album row and are written to every track file
- **Light/dark theme** — auto-detects system preference with manual toggle, persisted in localStorage

## Deployment

beetDeck is published as a single Docker image (`semsemyonoff/beetdeck`) that
bundles this API and the pre-built SPA. **Building and self-hosting the release
is handled in the separate `beetDeck/deploy` repository**, which pins this
backend and the frontend as submodules, builds the multi-arch image, and ships
the production `docker-compose.yml`, `.env.example`, and an example beets
`config.yaml`. See that repo's README for install, configuration (env vars,
volumes, beets plugins), and release instructions.

The app reads `BEETS_LIBRARY_DB`, `BEETS_IMPORT_DIR`, `BEETS_LIBRARY_ROOT`, and
`BEETSDIR` from the environment — see the table in `src/CLAUDE.md` for defaults.

## Architecture

- **Backend**: Flask + gunicorn (1 worker, 4 threads). Single worker is required because in-memory state (identification tasks, cover previews) must be shared across requests. Routes are organized as Flask Blueprints under `src/routes/`.
- **Frontend**: React 18 + Vite SPA with hash-based routing (`#/` = library, `#/artist/<name>` = artist page, `#/album/<id>` = album page, `#/untagged` = untagged items). It lives in a **separate repository**; its production build is dropped into `src/static/dist/` and served by Flask (the Vite manifest is read at request time to inject the hashed JS/CSS into `src/templates/index.html`).
- **beets integration**: uses the beets Python API directly (not the CLI) for identification, genre lookup, cover art, and lyrics. The CLI is only used for `beet import` (rescan) and `beet update` (prune stale entries).
- **beets version**: requires beets 2.10.0. Genres are stored as native list fields; library paths are stored relative to the database root and resolved at runtime.

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
