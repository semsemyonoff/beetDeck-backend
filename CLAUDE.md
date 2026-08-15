# beetDeck backend — agent notes

Flask REST API over a [beets](https://beets.io) music library: album browsing,
cover art, genre editing, lyrics, BPM computation, MusicBrainz identification,
library rescanning. The React SPA that consumes this API lives in a **separate
repository**; its production build is served by Flask from `beetdeck/static/dist/`.

This file records the non-obvious constraints. Everything mechanical — the route
list, request/response shapes — is generated from code; see **API** below.

## Tech stack

- Python (`requires-python = ">=3.10"`, runs on 3.14), Flask, gunicorn.
- SQLite (beets `library.db`): **reads go mostly through direct SQL**, the beets
  `Library` object is used for writes.
- beets is pinned to an exact release **tarball** in `pyproject.toml` — that pin
  is the single source of truth for the version; bump the tag to change it.
- BPM computation goes through the beets `autobpm` plugin (librosa + resampy).
  CPU-bound, ~9s/track. ffmpeg is required at runtime (pyacoustid fingerprinting,
  librosa audio loading).
- Dependencies and tooling config all live in `pyproject.toml` (runtime under
  `[project]`, dev tools under the `dev` extra; pytest and ruff configured there).

## Layout

One blueprint per feature, with a matching schema module and test module:
`beetdeck/routes/<name>.py` ↔ `beetdeck/schemas/<name>.py` ↔ `tests/test_<name>.py`.
The MCP server is a second top-level package, `beetdeck_mcp/` — see *MCP server*
below. Full tree in [`docs/layout.md`](docs/layout.md). The modules that are not
obvious from their name:

- `beetdeck/api.py` — the single `SpecTree` singleton and the 422 hook.
- `beetdeck/state.py` — module-level globals shared across request threads.
- `beetdeck/scan_progress.py` — pure parser: one beets verbose line → progress
  update + UI log level. No I/O, no threads, so it is unit-testable in isolation.
- `beetdeck/item_fields.py` — the source of truth for which beets fields are
  editable and which are album-level; both the catalog route and the write path
  validate against it.
- `beetdeck/coverart.py` — Cover Art Archive HTTP client, a dataclass with an
  injectable `transport` and `gate` (the shape `beetdeck_mcp/musicbrainz.py`
  established, deliberately duplicated rather than imported). Its `fetch_image`
  is handed a URL replayed from `index.json`, so it checks rather than trusts it:
  the host must be `coverartarchive.org`/`archive.org` or a subdomain **on every
  redirect hop** (which is why it follows them by hand instead of letting httpx
  do it), and the body is streamed against `MAX_IMAGE_BYTES` — a `502` either
  way, and nothing partial reaches the mirror.
- `beetdeck/artwork_store.py` — the two-root disk layer under the gallery:
  index, image resolution, and the cache sweep. Pure filesystem, no Flask.
- `beetdeck/templates/index.html` — SPA shell; injects the hashed JS/CSS by
  reading the Vite manifest at request time.
- `beetdeck/static/dist/` — SPA build output, gitignored.

> The production image (Dockerfile, build, example beets config) lives in the
> separate `beetDeck/deploy` repo, which pins this repository as a submodule.

## Configuration (`app.config`, set in `create_app`)

| key | env | default |
|---|---|---|
| `LIBRARY_DB` | `BEETS_LIBRARY_DB` | `/data/beets/library.db` |
| `IMPORT_DIR` | `BEETS_IMPORT_DIR` | `/music` |
| `LIBRARY_ROOT` | `BEETS_LIBRARY_ROOT` | falls back to `IMPORT_DIR` |
| `COVER_CACHE_DIR` | `BEETS_COVER_CACHE_DIR` | `<tmp>/beetdeck-cover-cache` |
| `MAX_CONTENT_LENGTH` | — | 32 MB (cover upload limit) |
| `ARTWORK_STORAGE_DIR` | `BEETS_ARTWORK_STORAGE_DIR` | `None` → storage mode off |
| `ARTWORK_CACHE_DIR` | `BEETS_ARTWORK_CACHE_DIR` | `<tmp>/beetdeck-artwork-cache` |
| `ARTWORK_CACHE_MAX_BYTES` | `BEETS_ARTWORK_CACHE_MAX_BYTES` | 512 MB |
| `ARTWORK_CACHE_TTL` | `BEETS_ARTWORK_CACHE_TTL` | 7 days |
| `ARTWORK_META_TTL` | `BEETS_ARTWORK_META_TTL` | 24 h (the `index.json` listing only) |
| `ARTWORK_USER_AGENT` | `BEETS_ARTWORK_USER_AGENT` | `DEFAULT_ARTWORK_USER_AGENT` |
| `ARTWORK_CAA_TRANSPORT` | — | `None`; test-only `httpx` transport injection |

The three numeric artwork keys are parsed defensively: a non-numeric env value
logs a warning and falls back to the default rather than failing app
construction. `0` and negatives are **passed through** — the store reads a
non-positive TTL/budget as "that half of the cache sweep is off", which is not
the same as garbage input.

`ARTWORK_STORAGE_DIR` is opt-in. Set, every rendition the gallery proxies is
mirrored there permanently and re-reads never hit the network; unset, the same
files land in the TTL- and size-bounded cache root. Reads consult both roots
(storage first) so flipping the mode never orphans what is already on disk, but
nothing is ever *promoted* between them.

The `CaaClient` and the `ArtworkStore` are built **once per app** in
`create_app` and live in `app.extensions["beetdeck_caa"]` /
`["beetdeck_artwork_store"]`. That lifetime is load-bearing, not tidiness: a
client built per request hands every thread a fresh `RateGate` with a clean
timestamp, so concurrent gallery loads would all call CAA at once while every
unit test of the gate still passed. The transport is closed via
`atexit.register` — Flask's `teardown_*` hooks fire per *request*, and closing
there makes the second gallery load raise on a closed client.

`LIBRARY_ROOT` defaults to `IMPORT_DIR` because the deployment convention is
beets `copy: no, move: no` — library dir and import dir are the same tree. It is
**not** probed from the beets `Library`.

The `beet` CLI (used by the rescan pipeline) reads its own config from `BEETSDIR`,
which the deployment provides. Two settings the app depends on:

- `duplicate_action: remove` — a re-import of a track in another format
  (mp3 → flac) must replace the old item in one pass. With `skip` the track
  disappears on the first import and only reappears on the second.
- the `autobpm` plugin must be enabled with `auto: no` — without it the BPM route
  returns `500 {"error": "autobpm plugin not loaded"}`; with `auto: yes` every
  rescan would stall on librosa.

## Shared in-memory state (`beetdeck/state.py`)

The file documents its own fields; two things it does not say:

- `identify_tasks` is one dict with four key namespaces: `f"album_{album_id}"`,
  `f"items_{uuid}"` (untagged-group identify), `f"cover_{album_id}"` (fetched
  cover previews), `f"lyrics_{item_id}"` (fetched lyrics stash). Album tasks are
  keyed by a **string**, not the raw int.
- `metadata_write_lock` serializes the batch write in `POST /api/items/metadata-batch`
  so only one beets `Library` handle writes at a time — SQLite will not tolerate
  concurrent writers here.
- `rescan_snapshot` matches items by **filesystem path**, not by ID, because a
  re-import assigns new IDs; paths are resolved through `_resolve_path()` first.

## API

The OpenAPI spec generated by spectree is the single source of truth:

- `GET /apidoc/openapi.json` — the spec
- `GET /apidoc/scalar/` — interactive viewer (bare `/apidoc/scalar` 308-redirects)

Request/response models live in `beetdeck/schemas/` (one module per blueprint plus
shared `common.py`). Do not maintain an endpoint table here — read the spec.

**Contracts the spec does not convey:**

- `POST /api/rescan` does **not** return `run_id`; obtain it by polling
  `GET /api/rescan/status`.
- `POST …/bpm/compute` always recomputes (`force=True`). The plugin swallows
  librosa errors internally and never raises, so failure is detected **by value**
  (pre-clear `item.bpm`, require a non-null value afterwards) → `400`.
- `POST /api/album/<id>/genre` takes `?mode=replace|merge`. Both run the same
  lookup; `mode` only decides what the previewed `new_genre` is made of, and
  `fetched_genre` always reports Last.fm's own answer. A lookup that **could not
  run** (lastgenre logged an error and produced nothing) is a `502`, never a
  `200` with an empty genre — see *Absence is not a fact* below.
- `POST …/cover/fetch` retries with `fetchart`'s `minwidth`/`enforce_ratio`
  lifted when the first pass finds nothing, and marks such a hit `relaxed: true`
  with a `warning`. Both outcomes carry `current_width`/`current_height`, and
  `found: false` carries `reasons` — the art plugin's own rejection lines.
- `GET /api/album/<id>/artwork` keeps **three** outcomes apart, per *Absence is
  not a fact*: `available: false` + a `reason` (no gallery is possible — the
  album has no release MBID; still a `200`, the album exists), `available: true`
  with an empty `images` (CAA answered `404`, which is its normal way of saying
  this release has no art — a fact about the release), and `502` (CAA could not
  be reached, so nothing was established). `source` (`storage`/`cache`/`remote`)
  and `fetched_at` say where the listing came from and how old it is; `?refresh=1`
  bypasses `ARTWORK_META_TTL`.
- All three artwork routes answer `500 {"error": "the artwork store could not be
  written"}` when the **disk layer** fails — a permanent mirror that filled up, a
  volume remounted read-only under a running app. `ArtworkStore` re-raises rather
  than swallowing, and an uncaught `OSError` would surface as Flask's HTML 500 on
  a blueprint whose every other answer is JSON: the SPA's `r.json()` then yields
  null, which it reads as "still loading", and the gallery sits on its skeleton
  with nothing to retry. The operator's detail goes to the log, not to the
  client. On the binary route the guard spans `send_file` too, not just the
  resolve — the inline cache sweep can evict the file between the two.
- `width`/`height` on an artwork image are the **original's** measured pixel
  size and are `null` until the original has actually been fetched (fullscreen
  or apply). CAA publishes no dimensions at all, so anything reported here was
  measured off a file on disk; reporting the 250 px tile's size instead would
  publish a false fact about the scan. `thumb_sizes` is ascending **ints**, and
  the upstream URLs are stripped from the response on purpose — every byte goes
  through `GET …/artwork/<image_id>?size=…`, which is what makes local storage
  meaningful and keeps an external origin out of the page.
- **`image_id` is a decimal string everywhere** — route path, `ArtworkImage`,
  `current_image_id`, the `beetdeck_cover_image_id` flex attr, the on-disk
  filenames. CAA sends it as a number and beets flex attrs are TEXT; anything
  less than one canonical spelling makes the SPA's `current_image_id ===
  image_id` false after a reload and the *Current cover* marker silently vanish
  (the same class of bug as `lyrics_instrumental`'s truthy spellings). The
  binary route `400`s a non-numeric id — the check doubles as path-traversal
  protection, since the id becomes a filename.
- `current_image_id` is recorded by `POST …/artwork/<image_id>/apply` into the
  `beetdeck_cover_image_id` flex attr, and `_save_cover_to_album` — the single
  funnel for every cover write inside Flask — clears it, so any other cover
  write drops the marker. ⚠️ **Known limitation:** a rescan (`beet import` /
  `beet update -M` with fetchart) rewrites `artpath` without going through that
  funnel, so the marker can outlive the cover it describes. Clearing it there
  would mean hooking the beets import path.
- Both lyrics fetches carry `reasons` on a miss — the lyrics plugin's own words
  for a source that failed. Empty means every source answered and none had the
  track, which is the only case where `found: false` is a fact about the track.
- `POST /api/album/<id>/lyrics/confirm` silently skips ids with no stashed fetch
  or from another album, so `written_item_ids` can be shorter than the request
  with `failed` empty. `retained_lrc_item_ids` is the subset that was written but
  whose sidecar `.lrc` could not be deleted.
- `PATCH /api/items/<id>/tags` is a **partial success** contract: a file-write
  failure returns HTTP 200 with a `warnings` list, not an error. An empty value
  clears the field (`del item[name]`, uniform for fixed and flex attrs).
- `POST /api/items/identify/<task_id>/confirm`: a DB failure rolls back; a partial
  file-write failure returns warnings.
- On a track, `has_lrc` (a sidecar `.lrc` exists) and `has_lyrics` (embedded
  lyrics **or** a readable non-empty `.lrc`) are deliberately different flags.
- An instrumental track is a **third lyrics state**, not a miss: the fetch
  endpoints return `found: false` together with `new_instrumental: true` and
  still stash the result, so confirming it stores beets' `lyrics_instrumental`
  flag (and clears the file tag) instead of lyrics text.

## Absence is not a fact

beets plugins swallow their own failures: `fetchart` drops a candidate with a
`debug` line and returns `None`, `lastgenre` logs `last.fm error: …` and returns
nothing. What reaches a route is an absence, and a route that forwards it as
`found: false` / an empty genre states something about the *library* that nobody
established — which is how an agent ends up filing "this album has no cover" over
an image its own size filter rejected.

`_plugin_reasons(logger, markers)` in `beetdeck/utils.py` is the one mechanism:
it captures the plugin's marker-matching log lines for the duration of the call
and hands them back. Three properties are load-bearing, all of them because a
logger is process-wide while a capture is per-request:

- **Propagation is off** for the duration — beets formats with `{}`, which the
  app's (and pytest's) `%`-style handlers raise on. A handler that blows up while
  explaining why a lookup found nothing would turn the diagnosis into a `500`.
- **Records are filtered to the capturing thread.** The album lyrics flow runs
  six fetches at once from the SPA, and without this a blocked backend's line
  would be attributed to whichever track happened to be capturing.
- **The level/propagate change is refcounted** across overlapping captures. Saved
  per capture, the second one would record the already-lowered level as the
  original and leave the logger at `DEBUG` for the rest of the process.

Used by `…/cover/fetch` (`beets.fetchart`), `…/genre` (`beets.lastgenre`) and both
lyrics fetches (`beets.lyrics`). The rule for a new plugin-backed route: report
*why*, and keep "the plugin found nothing" distinct from "the plugin could not
look".

## Error contract: 422 vs 400/404

Request validation is spectree/pydantic; every validation failure returns `422`
with a uniform body (`{error, detail[]}`) produced by `_render_validation_error`
in `beetdeck/api.py` and enforced by `tests/test_error_contract.py`.

Handler-owned *semantic* errors keep their own `{"error": "…"}` body with no
`detail`. The rule when adding an endpoint: wrong **type/shape/unknown field** →
`422` (uniform body, framework-produced); bad **value** / missing requirement /
not found → `400`/`404` (handler body). Clients branch on the status code.

`409` is the third handler-owned class — an optimistic-concurrency refusal, same
`{"error": …}` body: `POST …/confirm` with a mismatched (or in-flight)
`expected_generation`, and `POST …/identify` when a run is already in flight.

`502` is the fourth: an **upstream** service could not be reached, so the request
established nothing about the library. `POST /api/album/<id>/genre` answers it
when the Last.fm lookup itself failed — reporting that as a `200` with an empty
genre would state something about the album that nobody checked (see *Absence is
not a fact*). Same `{"error": …}` body.

## Architecture constraints

**CRITICAL: the app must run as a single process.** It relies on the module-level
globals in `beetdeck/state.py` for state shared across request threads. Multiple
worker processes would each get their own memory space, siloing state per worker
and breaking background task tracking (identify jobs, scan status). The gunicorn
invocation must always be `-w 1` with `--threads N`.

## App factory

Routes are registered as Flask Blueprints in `create_app()` (`beetdeck/__init__.py`).
Route modules read config from `current_app.config`, never module-level constants,
so tests can inject a temp SQLite file via `create_app(test_config={…})`.

`_init_beets()` in `beetdeck/utils.py` takes an explicit `library_db` parameter
instead of reading `current_app.config`, because background threads (identify
tasks) run without a Flask app context.

## Scan controller and progress parser

`beetdeck/routes/scan.py` uses a **daemon controller thread**:

- `POST /api/rescan` generates a `run_id`, starts the thread, returns immediately.
- The thread runs two sequential `Popen`s — `beet import`, then conditionally
  `beet update -M` (skipped when the post-import diff is empty). Both write
  stdout+stderr to `/data/beets/scan-logs/<run_id>.log`.
- A log-tail thread feeds each new line to `scan_progress.py` and applies updates
  under `rescan_lock`. All state access in handlers goes through that lock too.

Progress modes: a full scan (`-I`) pre-counts album directories under the import
dir via `os.walk` → real percentage; a quick scan (`-i`, incremental) has no
denominator → `total = None` and the UI shows an indeterminate bar.

Keeping the parser pure is deliberate — it isolates fragile beets-output parsing
from the threading, and `tests/test_scan_progress.py` covers it over fixed samples.

## Beets integration

Behaviours this app works around:

- Genres are a native list field (`genres`); `_format_genre()` in
  `beetdeck/utils.py` handles both list and legacy string values.
- Library paths are stored **relative** to the DB root; `_resolve_path()` joins
  `LIBRARY_ROOT` + relative path (absolute paths pass through unchanged).
- The lyrics plugin exposes separate `lyrics_backend` / `lyrics_url` /
  `lyrics_language` fields — no "Source: URL" suffix in the lyrics text.
- Cover embedding imports `embed_album` from the private
  `beetsplug._utils.art` (the old `beets.art` shim is slated for removal in beets
  3.0.0). Cover tests patch `beetsplug._utils.art.embed_album`.
- `_save_cover_to_album()` bypasses `album.set_art()` (multi-disc, see its
  docstring), so it must send `art_set` itself — that event is the only way
  plugins like `permissions` ever see cover art. It sends it **after** its own
  `embed_album`: beets' `embedart` listens on the same event and, with its `auto`
  enabled, re-embeds the album and can delete the art file, which from inside our
  embed would be re-entrant. It also chmods the resized file: `tempfile` hands
  out 0600 regardless of umask, and `move()` carries that mode into the library.
  It **resizes before it removes**, and the order is load-bearing: nothing
  upstream proves `src_path` is a decodable image (`…/cover/upload` checks only
  the filename extension, and the artwork apply route stores an unrecognised
  `Content-Type` under `.bin` rather than dropping the bytes), so deleting the
  album's existing `cover/folder/front.*` first would mean a PIL failure answers
  `500` with the user's cover already gone and `artpath` pointing at nothing.
- An instrumental verdict lives in the `lyrics_instrumental` flex attr and has
  more than one truthy spelling (`'1'` from the 2.13.0 migration, `1` from a
  `types.BOOLEAN` write) — `_is_instrumental()` is the single rule. It is surfaced
  per track on `GET /api/album/<id>`: `has_lyrics: false` alone cannot tell
  "checked, nothing to fetch" from "never looked up".

Per-release upgrade notes: [`docs/beets-upgrades.md`](docs/beets-upgrades.md).

## MCP server (`beetdeck_mcp`)

A second top-level package in this repo, shipped in the same image and installed by
the same `pip install -e ".[dev]"`, but run as a **separate process**
(`python -m beetdeck_mcp`) that reaches the Flask app over HTTP at
`BEETDECK_API_URL`. It exposes the library to an AI agent over MCP
(streamable-http). Its dependencies (`mcp`, `httpx`) are in `[project] dependencies`
rather than an extra because the image's venv sync installs exactly `-e ".[dev]"`.

**It must never import Flask request state.** `beetdeck.state`, `current_app`, and the
in-memory identify tasks belong to the other process — reading them here would see a
different, empty memory space. The only supported coupling is the REST API. This is
also what makes the tests possible: they drive the real app in-process through
`httpx.Client(transport=httpx.WSGITransport(app=app))`, so the MCP↔REST contract is
exercised for real instead of against mocks. Everything in the package is
**synchronous** for the same reason — an async client cannot use a WSGI transport.

### An empty read during a rescan is not a fact about the library

`GET /api/library` answers `200` with whatever is committed at that instant, so a
running `beet import` makes it a *partial* library and nothing in the payload says
so. `list_albums` and `search_library` therefore ask `…/rescan/status` **when they
come back empty** and add a note naming the run. The extra round trip is paid only
on the answer that would otherwise mislead, and a failed status call adds no note
rather than downgrading a valid result (issue #21 was filed against exactly this
shape: rows imported seconds earlier, an empty result, and no way to tell it from
"not in the library").

### Two-phase writes and the per-kind guarantee

Every mutating capability is split: a `preview_*` tool returns the diff plus a
single-use `preview_token`, and `apply_change(preview_token)` is the **only** tool
that writes. How strongly a token is defended against a concurrent change is **not
uniform** — `beetdeck_mcp/preview.py` owns the table and returns the wording with
every minted token:

| kind | guarantee | mechanism |
|---|---|---|
| `identify`, `items_identify` | enforced by the backend | `match_generation` precondition, compared inside `state.identify_lock` |
| `cover`, `lyrics` | enforced by the backend | `stash_generation` precondition on the fetch slot, same lock; all-or-nothing across a lyrics selection |
| `tags`, `genres` | best-effort, TOCTOU-racy | old values re-read immediately before the write; refuses on drift |

Only the tag kinds are left without a backend check, and that is a property of the
endpoints: a tag write has no server-side staging to pin a generation to. The
fetch kinds *did* have that gap — one anonymous stash slot per target, silently
overwritten by a competing fetch — and closed it by stamping every fetch (see
below), which is also why they no longer carry a shorter TTL than everything else.

### `match_generation` and `stash_generation`

One process-wide counter (`state.next_match_generation`, aliased
`next_stash_generation`) backs both, so a value never means two things.

Identify tasks carry a monotonic `match_generation`,
exposed on the status responses of `…/identify/status` and the untagged-items
equivalent alongside `matches_available`. Both confirm endpoints accept an optional
`expected_generation` and compare it **inside `state.identify_lock`**, so a stale
confirm is rejected atomically. A task already in `confirming` state counts as a
mismatch: the generation is only bumped when the first confirm *finishes*, so
without that arm the loser of a genuine race would fall through to `400 No
identification results` instead of the `409` its precondition earned.

This exists because the status endpoint could not otherwise express consumption:
`_get_task_json` strips every `_`-prefixed key, and `confirm_match` pops `_matches`
but restores `status: "done"` under the same `task_id` — an already-consumed task was
indistinguishable from a confirmable one over HTTP.

The cover/lyrics fetches stamp `stash_generation` on the stash entry and return it;
the matching confirms take the same optional `expected_generation` and answer `409`
on a mismatch. The album-wide lyrics confirm checks it for **every** requested id
before writing anything, and writes from the snapshot it validated rather than
re-reading each entry in the loop — the selection came out of one fetch, so a
partial write is a change nobody previewed, and the loop spends a real file write
per track, which is a wide enough window for a competing fetch to land in.
Omitting the field everywhere keeps the old behaviour, which is what the SPA relies
on (it POSTs those confirms with no body at all).

The precondition only sees what goes *through* the stash, so **every** route that
writes one of those targets directly clears it on success — `…/cover/upload`,
`POST …/artwork/<image_id>/apply`, `…/lyrics/save`, both `…/lyrics/embed` routes,
and `PATCH /api/items/<id>/tags` when the patch touches `lyrics` or a `lyrics_*`
attr (`is_editable` is a deny-list, so that endpoint can write them).
`_drop_cover_stash` / `_drop_lyrics_stash` are the single implementation; a route
that writes lyrics and forgets to call one leaves the stash intact with its
original generation, and a token minted before it still passes its precondition
and reverts the edit.

The artwork apply drops the stash **before** its first persistent mutation, not
after it: `_save_cover_to_album` persists `artpath` via `album.store()` and only
then embeds and sends `art_set`, either of which can raise. Dropping afterwards
means a `500` with the cover already changed and the old generation still valid
— exactly the window the call exists to close. Invalidating a preview that
turned out not to have been replaced is the cheap direction to be wrong in.

A generation check alone is not enough, because it is checked in microseconds and
the write that follows takes seconds. `state.album_write_lock(album_id)` is held
across check *and* write by every cover/lyrics write path, so a direct upload or
save cannot land inside a confirm's window and be reverted by it. It is keyed by
**album**, not item, so no handler ever holds two — which is what would make lock
ordering a hazard — and it is always acquired *outside* `identify_lock`. The one
deliberate exception is `PATCH …/tags`: it is keyed by item, has no album in its
route, and taking an album lock for every tag edit would serialize unrelated
writes; it invalidates the stash but does not serialize against a confirm.

### Genres commit through `save`, not `confirm` — and so does the SPA

`POST …/genre/confirm` performs a **second, independent Last.fm lookup** before
writing, so it can commit a different value than the preview showed. `apply_change`
therefore sends `POST …/genre/save` with the explicit previewed string. The previewed
old value is read from `GET /api/album/<id>` (which falls back to the legacy singular
`genre` column) and not from the lookup's `old_genre` (which reads only the native
`genres` list) — otherwise the drift check would refuse every album tagged before
beets 2.10.

`merge` mode makes this binding mandatory rather than merely correct: the merged
value exists only in the preview, and `confirm` — which re-looks-up — cannot
express it at all. The SPA moved to `…/genre/save` for the same reason, so
`…/genre/confirm` now has no caller left in this repo and is kept only for
compatibility.

### What `BEETDECK_MCP_READONLY` does and does not disable

It disables `apply_change` **alone** — the gate is the preview→apply contract, not
"writes". Three tools sit outside that contract and keep writing in read-only mode:
`rescan_library` (re-imports files into the library), `compute_bpm` (writes a BPM tag
into the audio file) and `toggle_album_ignored` (flips an album flag). Previews have
real backend side effects too: identification starts a background autotag job, and
cover/lyrics previews fetch remote data and overwrite the backend stash. The escape
hatch `beetdeck_api_call` is `GET`-only and `/api/`-prefixed always, in both modes —
a write escape would bypass the contract that is the server's point.

### MusicBrainz rate gate

`beetdeck_mcp/musicbrainz.py` gates outgoing calls to ≤1/second process-wide
(`threading.Lock` + monotonic timestamp) and requires a descriptive user agent.
⚠️ The budget is **per source IP** and the backend's own autotag queries MusicBrainz
too, so an agent session running alongside a rescan can still exceed it. This is a
known limitation, not something the gate can fix from one process.

### Refuse-to-start invariants

`load_config()` raises `ConfigError` (exit 2, no server) when `BEETDECK_MCP_TOKEN` or
`MUSICBRAINZ_USER_AGENT` is empty — an empty token table would publish an
unauthenticated write surface, and MusicBrainz rejects anonymous clients. Neither
degrades to a default. `BEETDECK_MCP_HOST`/`_PORT` are the **in-container** bind only;
the published host port is a separate compose variable so `dwe test` can remap it.
The allowlist is *not* a refuse-to-start invariant: an empty
`BEETDECK_MCP_ALLOWED_HOSTS` starts the server with no `Host`/`Origin` check at all
(see below), so the bearer token is the only gate.

`GET /health` is registered as a custom route **after** the MCP endpoints are wrapped
in `RequireAuthMiddleware`, so it stays reachable without a token — the container
healthcheck depends on that ordering.

`BEETDECK_MCP_ALLOWED_HOSTS` is compared against the `Host` header, **which carries
the port**, so `build_transport_security` registers each entry both bare and as the
SDK's `host:*` wildcard (and derives the `Origin` allowlist from both). An empty
allowlist does not narrow to localhost — the SDK disables the check entirely.

## Testing

- pytest + pytest-cov + pytest-mock; ruff for lint/format; config in `pyproject.toml`.
- `tests/` mirrors `beetdeck/routes/`, plus unit tests for the pure modules
  (`test_scan_progress.py`, `test_item_fields.py`, `test_utils.py`) and contract
  tests (`test_error_contract.py`, `test_openapi.py`, `test_schemas.py`).
- `tests/test_mcp_*.py` covers `beetdeck_mcp/` against the real Flask app over an
  in-process WSGI transport; only MusicBrainz is mocked. Fixtures live in the same
  `tests/conftest.py` — pytest auto-loads only files literally named `conftest.py`.
- `tests/conftest.py` builds the app via `create_app(test_config=…)` against a
  temp SQLite file seeded with the beets schema.
- Coverage is reported but **not gated** (`fail_under = 0`); CI runs plain pytest.
  Aim for 80%+ on new modular code as a convention, not a check.

This repository's own toolchain (`.venv` + `make test/lint/fmt/coverage`) is
described in `README.md` → *Local development*.
