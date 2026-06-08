# beetDeek: Bug Fixes and Improvements

## Overview
- Fix two bugs: scan always showing re-creation, unknown artist navigation
- Add unknown artist management UI (new feature)

**Prerequisite**: This plan assumes the migration/refactoring plan (`2026-04-23-migration-and-refactoring.md`) is complete — code lives in `src/` with Flask Blueprints, beets 2.10.0, and test infrastructure is in place.

## Context
- **Code structure**: `src/` with Flask Blueprints (library, albums, cover, genres, lyrics, identify, scan)
- **Test infrastructure**: pytest + pytest-cov + ruff, `tests/` directory, `make test`/`make lint`/`make coverage`
- **App factory**: `create_app(test_config=None)`, config via `current_app.config`
- **In-memory state**: `src/state.py` — `identify_tasks`, `identify_lock`, `rescan_lock`, `rescan_proc`, `rescan_snapshot`
- **Beets version**: 2.10.0 (relative paths, multi-value genres, new lyrics fields)

## Development Approach
- **Testing approach**: Regular (code first, then tests)
- Complete each task fully before moving to the next
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task (exceptions noted per-task)
- **CRITICAL: all tests must pass before starting next task**
- **CRITICAL: update this plan file when scope changes during implementation**
- Run tests after each change

## Progress Tracking
- Mark completed items with `[x]` immediately when done
- Add newly discovered tasks with ➕ prefix
- Document issues/blockers with ⚠️ prefix
- Update plan if implementation deviates from original scope

## Implementation Steps

### Task 1: Bug fix — Scan always shows re-creation for all albums
- [x] **Root cause**: `_compute_scan_diff()` in `src/routes/scan.py` compares item IDs between before/after snapshots. When `beet import` deletes and re-inserts items (new IDs), all items appear as removed+added even if content is unchanged
- [x] fix: add `path` to `_take_snapshot()` query — snapshot becomes `{id: (title, artist, album_id, path)}`
- [x] fix: update `_compute_scan_diff()` to match by normalized path first, then by ID. Items with the same path in both snapshots are unchanged regardless of ID
- [x] note: beets 2.10.0 stores relative paths — normalize via `_resolve_path()` (resolves against `current_app.config["LIBRARY_ROOT"]`, set up during migration plan) before comparison
- [x] write tests for `_compute_scan_diff()`: no changes, only additions, only removals, ID reassignment with same path, mixed scenario
- [x] write tests for `_take_snapshot()` with path data
- [x] run tests — must pass before next task

### Task 2: Bug fix — Unknown artist navigation
- [x] **Root cause**: `/api/library` maps `NULL`/empty `albumartist` to `"Unknown Artist"` display string (utils or library route), but `/api/artist` queries `WHERE a.albumartist = ?` which can't match NULL/empty values when passed `"Unknown Artist"`
- [x] fix: in `src/routes/library.py`, update the `/api/artist` handler to detect `name == "Unknown Artist"` and use `WHERE (a.albumartist IS NULL OR a.albumartist = '')` instead
- [x] write tests for unknown artist query: NULL albumartist, empty string albumartist, normal artist with real name
- [x] run tests — must pass before next task

### Task 3: Unknown artist management UI (new feature)
- [x] **Design subtask — album creation from loose items**: Before implementing, resolve these design questions:
  - **Beets API for album creation**: Use `lib.add_album(items)` which creates an album record and sets each item's `album_id`. Verify this works for items that already have an `album_id` (orphaned items in a placeholder album) vs items with `album_id = None`
  - **Transaction behavior**: `lib.add_album()` + `apply_metadata()` + `item.write()` cannot be truly atomic — filesystem tag writes are not transactional. Strategy: (1) create album + apply DB metadata first, (2) then write tags to files in a loop. If a file write fails mid-batch, DB rollback (`album.remove()`, restore original `album_id`) is best-effort — files already written will retain new tags. Document this explicitly: partial file writes are possible on failure, user can re-run or manually fix. Do NOT attempt to capture/restore original file tags (too complex, fragile)
  - **Item reassignment**: Items may currently belong to another album (e.g., a catch-all "Unknown" album). Before creating a new album, save each item's original `album_id` for rollback. After successful confirm, the old album may become empty — do NOT auto-delete it (user may have other items there)
  - **Autotag input**: `autotag.tag_album()` expects a list of `beets.library.Item` objects. Load items via `lib.get_item(id)` for each ID, verify they all exist, then pass to autotag. No temporary album needed for the autotag step — it operates on items directly
  - Document the chosen approach in a code comment in `src/routes/items.py`
- [x] design new API endpoints for untagged item management:
  - `POST /api/items/<item_id>/metadata` — update artist/album on a single untagged item
  - `POST /api/items/identify` — accept list of item IDs, group them as an album, run identification
- [x] create `src/routes/items.py` blueprint with `POST /api/items/<item_id>/metadata`
  - Accept JSON body `{"artist": "...", "album": "..."}` — update item fields via beets Library
  - Validate that the item exists and fields are non-empty
- [x] implement `POST /api/items/identify` in items blueprint
  - Accept JSON body `{"item_ids": [1, 2, 3], "search_artist": "opt", "search_album": "opt"}` — load items via `lib.get_item()`, pass to `autotag.tag_album()`
  - Return task ID for polling via status endpoint
- [x] implement `GET /api/items/identify/<task_id>/status` — returns candidates (same shape as album identify status)
- [x] implement `POST /api/items/identify/<task_id>/apply` — preview tag diff for selected candidate against the item group
- [x] implement `POST /api/items/identify/<task_id>/confirm` — create album via `lib.add_album(items)`, apply matched metadata to DB, then write tags to files. On DB/creation failure: rollback via `album.remove()`, restore original `album_id` values. On partial file write failure: log which files failed, return success with warnings (DB state is committed, partial file writes are documented as best-effort). Return new `album_id` for frontend navigation
- [x] register items blueprint in app factory (`src/__init__.py`)
- [x] update frontend: on the "Unknown Artist" page, show individual files with inline edit controls for artist/album fields
- [x] update frontend: add multi-select checkboxes to group files and trigger identification via new endpoint
- [x] write tests for `POST /api/items/<item_id>/metadata` in `tests/test_items.py` (success, missing item, empty fields)
- [x] write tests for `POST /api/items/identify` in `tests/test_items.py` (success, empty list, invalid IDs)
- [x] write tests for identify status/apply/confirm endpoints (status polling, apply diff preview, confirm creates album and writes tags)
- [x] write test for partial file-write failure in confirm: mock `item.write()` to raise on one item, verify response has `"status": "ok"` with `"warnings"` array listing the failed file path, and verify DB album was still created
- [x] run tests — must pass before next task

### Task 4: Verify acceptance criteria
- [x] verify both bugs are fixed (scan diff, unknown artist navigation)
- [x] verify unknown artist management UI works end-to-end (edit single item, identify group) [x] manual test (skipped - not automatable)
- [x] run full test suite — 236 passed
- [x] run linter (`make lint`) — all issues must be fixed — clean
- [x] verify test coverage meets 80%+ standard (`make coverage`) — 87% total

### Task 5: [Final] Update documentation
- [x] update CLAUDE.md with new items blueprint and endpoints
- [x] update README.md API reference if needed

## Technical Details

### Scan Diff Bug — Root Cause Analysis
The current `_take_snapshot()` captures `{id: (title, artist, album_id)}`. The diff compares `set(before.keys()) vs set(after.keys())` — pure ID comparison. When `beet import` re-imports files, beets may delete old items and create new ones with different IDs for the same physical files. This makes every item appear as "removed" (old ID gone) and "added" (new ID appeared).

**Fix**: Include file `path` in the snapshot. Match by path first — if a path exists in both snapshots, the item is unchanged regardless of ID changes. Only report items whose paths are genuinely new or missing. With beets 2.10.0 relative paths, normalize before comparison.

### Unknown Artist Bug — Root Cause
`/api/library`: `artist = r["albumartist"] or "Unknown Artist"` — maps NULL/empty to a display string.
`/api/artist`: `WHERE a.albumartist = ?` with `name="Unknown Artist"` — no row has `albumartist = 'Unknown Artist'`, so query returns empty.

**Fix**: When `name == "Unknown Artist"`, change query to `WHERE (a.albumartist IS NULL OR a.albumartist = '')`.

### Unknown Artist Management — API Design
```
POST /api/items/<item_id>/metadata
  Body: {"artist": "Artist Name", "album": "Album Name"}
  Response: {"status": "ok", "item_id": 123}

POST /api/items/identify
  Body: {"item_ids": [1, 2, 3], "search_artist": "opt", "search_album": "opt"}
  Response: {"status": "started", "task_id": "items_<uuid>"}

GET /api/items/identify/<task_id>/status
  Response: {"status": "done", "candidates": [...]}  (same candidate shape as album identify)

POST /api/items/identify/<task_id>/apply
  Body: {"candidate_index": 0}
  Response: {"status": "ok", "diff": {...}}  (tag diff preview for the selected candidate)

POST /api/items/identify/<task_id>/confirm
  Body: {"candidate_index": 0}
  Response: {"status": "ok", "album_id": 456, "warnings": ["file write failed: /path/to/track.mp3"]}
  Note: creates album via lib.add_album(), applies metadata to DB, writes tags to files.
        DB creation failure → full rollback. Partial file write failure → committed with warnings
        (best-effort: already-written files retain new tags).
```

## Post-Completion
*Items requiring manual intervention or external systems — no checkboxes, informational only*

**Manual verification:**
- Test scan with a real beets library to verify diff shows only actual changes
- Test unknown artist flow with actual untagged files (navigate, edit, identify)
