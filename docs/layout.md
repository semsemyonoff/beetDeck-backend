# Repository layout

Reference for `AGENTS.md` → *Layout*. Regenerate by reading the tree; this file
is orientation, not a contract.

```
beetDeck/
├── app.py                  # Thin entry point: calls create_app() from beetdeck/
├── beetdeck/
│   ├── __init__.py         # Flask app factory (create_app); calls api.register(app)
│   ├── api.py              # SpecTree singleton + 422 validation-error hook (OpenAPI/Scalar)
│   ├── state.py            # Shared in-memory state (globals)
│   ├── scan_progress.py    # Pure parser: beets verbose line → progress update + log level
│   ├── item_fields.py      # Field classification: READONLY_FIELDS, ALBUM_LEVEL_FIELDS,
│   │                       #   is_editable(), is_album_level(), build_field_catalog(), coerce_value()
│   ├── utils.py            # Shared helpers and constants (_init_beets, _resolve_path,
│   │                       #   _format_genre, _try_write_item, embed_album import)
│   ├── coverart.py         # Cover Art Archive client: CaaClient(transport, gate),
│   │                       #   normalize_images(), RateGate on the JSON lookup only
│   ├── artwork_store.py    # Two-root disk layer (storage → cache): index.json read/
│   │                       #   replace, find_image/store_image, TTL+LRU sweep_cache()
│   ├── mbsync.py           # Pure MusicBrainz-sync helpers: item↔TrackInfo mapping,
│   │                       #   snapshot_items/snapshot_album, diff_against,
│   │                       #   restore_fields, check_drift. No Flask, no writes.
│   ├── schemas/            # spectree/pydantic request & response models, one module per blueprint
│   │   ├── __init__.py     # re-exports shared models
│   │   ├── common.py       # ErrorResponse + reusable nested types (AlbumSummary, ArtistGroup)
│   │   ├── albums.py  artwork.py  bpm.py  cover.py  genres.py  identify.py
│   │   └── items.py  library.py  lyrics.py  mbsync.py  meta.py  scan.py
│   ├── routes/             # One Flask blueprint per feature
│   │   ├── library.py      # GET /, /api/library, /api/search, /api/artist
│   │   ├── albums.py       # album detail + track tags
│   │   ├── cover.py        # cover art CRUD (fetch / preview / confirm / upload)
│   │   ├── artwork.py      # Cover Art Archive gallery: listing, image proxy, apply
│   │   ├── genres.py       # genre fetch / confirm / save (Last.fm + MusicBrainz sources)
│   │   ├── lyrics.py       # lyrics CRUD, single + bulk
│   │   ├── bpm.py          # per-track BPM compute (autobpm plugin)
│   │   ├── identify.py     # album identify / status / apply / confirm
│   │   ├── items.py        # untagged items: field catalog, per-track tag write,
│   │   │                   #   metadata batch, group identify
│   │   ├── mbsync.py       # MusicBrainz re-sync of an album's own release:
│   │   │                   #   preview (diff, no writes) / confirm (applies the
│   │   │                   #   stashed diff, never re-looks-up)
│   │   ├── meta.py         # GET /api/version (beetDeck + beets versions)
│   │   └── scan.py         # rescan start / status / log tail / dismiss
│   ├── static/
│   │   ├── logo.svg
│   │   └── dist/           # SPA build output (generated, gitignored)
│   └── templates/
│       └── index.html      # SPA shell; injects hashed JS/CSS via the Vite manifest
├── beetdeck_mcp/           # MCP server — a separate process, same install (see AGENTS.md)
│   ├── __main__.py         # `python -m beetdeck_mcp`; refuses to start on bad config
│   ├── config.py           # env → validated Config; ConfigError is startup-fatal
│   ├── server.py           # MCPServer wiring: auth, transport security, /health
│   ├── auth.py             # static bearer-token verifier
│   ├── client.py           # httpx client for the REST API + result normalization
│   ├── preview.py          # preview-token store; owns the per-kind freshness guarantee
│   ├── musicbrainz.py      # ws/2 client with a process-wide 1 req/s gate
│   ├── resources.py        # MCP resources: live OpenAPI spec + workflow guide
│   └── tools/              # one module per tool group; apply.py is the only writer
│       ├── read.py  identify.py  musicbrainz.py  tags.py
│       └── media.py  genres.py  library.py  raw.py  apply.py  mbsync.py
│                                          # mbsync.py: preview_mbsync tool; apply
│                                          #   goes through apply.py like every
│                                          #   other write kind
├── tests/                  # pytest suite; conftest.py builds the app on a temp SQLite DB
│   ├── conftest.py  fixtures/
│   ├── test_albums.py  test_artwork.py  test_bpm.py  test_cover.py
│   ├── test_genres.py  test_identify.py  test_items.py  test_library.py
│   ├── test_lyrics.py  test_mbsync.py  test_mbsync_core.py  test_meta.py  test_scan.py
│   │                                          # route suites, mirroring beetdeck/routes/
│   ├── test_item_fields.py  test_scan_progress.py  test_utils.py
│   ├── test_artwork_store.py  test_coverart.py
│   │                                          # unit tests for the pure modules
│   ├── test_config.py                         # create_app config parsing + extensions
│   ├── test_mcp_*.py                          # beetdeck_mcp, driven against the real
│   │                                          #   app over an in-process WSGI transport
│   ├── test_scan_replace.py                   # format-replace integration (mp3→flac)
│   ├── test_db_fixture.py                     # the fixture itself
│   └── test_error_contract.py  test_openapi.py  test_schemas.py
│                                              # cross-cutting contract tests
└── pyproject.toml          # Dependencies, pytest, and ruff configuration
```

The production image (Dockerfile, build, example beets config) lives in the
separate `beetDeck/deploy` repo, which pins this repository as a submodule.
