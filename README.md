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

## Quick start

### 1. Prepare beets config

Create a directory for beets configuration and copy the example config:

```bash
mkdir -p ./beetdeck-config
cp config.yaml ./beetdeck-config/config.yaml
```

See [`config.yaml`](config.yaml) for an example beets configuration.

### 2. Run with docker compose

```yaml
# docker-compose.yml
services:
  beetdeck:
    image: semsemyonoff/beetdeck
    container_name: beetdeck
    user: "1000:1000"  # match your music library file ownership
    environment:
      - TZ=Europe/London
      - BEETSDIR=/config/beets
      - BEETS_LIBRARY_DB=/data/beets/library.db
      - BEETS_IMPORT_DIR=/music
    volumes:
      - ./beetdeck-config:/config/beets:ro   # beets config (read-only)
      - ./beetdeck-data:/data/beets           # database & state (writable)
      - /path/to/your/music:/music            # music library (writable)
    ports:
      - "5000:5000"
    restart: unless-stopped
```

```bash
docker compose up -d
```

Open `http://localhost:5000` in your browser.

### 4. Initial library scan

Click **Rescan Library** → **Full Scan** in the UI to populate the beets database from your music directory. After the initial scan, use **Quick Scan** for incremental updates.

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `UTC` | Container timezone |
| `BEETSDIR` | `/config/beets` | beets configuration directory |
| `BEETS_LIBRARY_DB` | `/data/beets/library.db` | Path to beets SQLite database |
| `BEETS_IMPORT_DIR` | `/music` | Music library root for rescan |
| `BEETS_LIBRARY_ROOT` | auto-detected | beets library directory for resolving relative paths (auto-detected from beets config if unset) |

### Volumes

| Container path | Description |
|----------------|-------------|
| `/config/beets` | beets configuration — **read-only** |
| `/data/beets` | Database (`library.db`) and state (`state.pickle`) — **must be writable** |
| `/music` | Music library — **must be writable** for tag writing, cover embedding, lyrics |

### Running as non-root

The container runs as whatever `user:` is set in compose. Make sure the UID/GID has:
- **Read** access to the beets config volume (`/config/beets`)
- **Read/write** access to the data volume (`/data/beets`)
- **Read/write** access to the music library volume (for writing tags, covers, lyrics)

### beets plugins

The following beets plugins are used and should be listed in `config.yaml`:

| Plugin | Purpose |
|--------|---------|
| `musicbrainz` | Album identification / autotag |
| `fetchart` | Cover art fetching from online sources |
| `embedart` | Embedding cover art into audio files |
| `lastgenre` | Genre tagging via Last.fm |
| `lyrics` | Lyrics fetching (lrclib) |
| `info` | Reading raw file tags |

All plugins have `auto: no` — beetDeck triggers them on demand through the UI.

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

The frontend SPA lives in its own repository. To exercise the full UI locally,
build it there and copy `dist/` into `src/static/dist/`, then `make run` and
open `http://localhost:5000`.

### Syncing the frontend build (production hand-off)

The production Docker image bundles the pre-built SPA. Flask reads
`src/static/dist/.vite/manifest.json` at request time to inject the hashed
JS/CSS into `src/templates/index.html`; `build.sh` aborts with an error if
that file is missing.

One-command sync (requires the DWE stack running):

```bash
make sync-frontend-dist   # builds SPA in-container, then copies dist/ here
make build                # build and push the multi-arch Docker image
```

What it does:
1. Runs `npm run build` inside the `frontend` DWE container (outputs to
   `services/frontend/src/dist/` on the host via the bind mount).
2. Copies that `dist/` tree into `src/static/dist/` so `build.sh` can find it.

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
