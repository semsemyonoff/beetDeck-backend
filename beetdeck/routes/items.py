"""Items routes for beetDeek — management of untagged/loose items.

Design decisions:
- Album creation from loose items: lib.add_album(items) creates a new album
  record and updates each item's album_id in the DB. This is NOT atomic with
  file tag writes. Strategy: (1) create album + apply DB metadata first,
  (2) then write tags to files. On DB failure: rollback via album.remove()
  and restore original album_id on each item. On partial file-write failure:
  DB state is committed; already-written files retain new tags (best-effort).
  Warnings are returned in the response — the caller can re-run or fix manually.
- Item reassignment: items may already belong to another album (e.g. a
  catch-all "Unknown" album). After confirm, the old album may become empty;
  we do NOT auto-delete it — that is the user's responsibility.
- Autotag input: autotag.tag_album() receives a Source built from Item objects
  loaded via lib.get_item(id). The resulting AlbumMatch.mapping keys are those
  same items. No temporary album is created for the autotag step.

Endpoints:
    GET    /api/items/untagged                          — list items with NULL/empty albumartist
    POST   /api/items/<item_id>/metadata                — update artist/album on single item
    POST   /api/items/identify                          — start background identification
    GET    /api/items/identify/<task_id>/status         — poll identification status
    POST   /api/items/identify/<task_id>/apply          — preview diff for a candidate
    POST   /api/items/identify/<task_id>/confirm        — create album and write tags
"""

import threading

from flask import Blueprint, current_app, jsonify, request
from spectree import Response

from beetdeck import state
from beetdeck.api import api
from beetdeck.item_fields import (
    CLEAR,
    build_field_catalog,
    coerce_value,
    is_editable,
    is_known_field,
)
from beetdeck.schemas.common import ErrorResponse
from beetdeck.schemas.items import (
    ItemFieldsResponse,
    ItemsApplyResponse,
    ItemsCandidateBody,
    ItemsConfirmBody,
    ItemsConfirmResponse,
    ItemsIdentifyBody,
    ItemsIdentifyResponse,
    ItemsIdentifyStatusResponse,
    ItemTagsPatch,
    ItemTagsPatchResponse,
    MetadataBatchBody,
    MetadataBatchResponse,
    UntaggedItemsResponse,
    UpdateMetadataBody,
    UpdateMetadataResponse,
)
from beetdeck.utils import (
    STALE_GENERATION_ERROR,
    _consume_matches,
    _drop_lyrics_stash,
    _drop_mbsync_stash,
    _get_ro_conn,
    _get_task_json,
    _init_beets,
    _resolve_path,
    _serialize_candidate,
    _try_write_item,
    log,
)

bp = Blueprint("items", __name__)

# Fields that apply_metadata() may write to item rows.  Captured before any
# store() call so the rollback path can fully restore the original DB state.
# Covers all fields that beets writes via item.update(data) in AlbumMatch.apply_metadata():
# - TrackInfo.item_data fields (after MEDIA_FIELD_MAP renaming: track_id→mb_trackid,
#   release_track_id→mb_releasetrackid, medium→disc, medium_index→track,
#   artist_id→mb_artistid, artists_ids→mb_artistids)
# - AlbumInfo.item_data fields merged in via merge_with_album() (after renaming:
#   album_id→mb_albumid, artist→albumartist, artists→albumartists,
#   artist_id→mb_albumartistid, artists_ids→mb_albumartistids,
#   artist_credit→albumartist_credit, artists_credit→albumartists_credit,
#   artist_sort→albumartist_sort, artists_sort→albumartists_sort,
#   mediums→disctotal, releasegroup_id→mb_releasegroupid, va→comp)
# Also covers fields cleared by item.clear() when import.from_scratch is enabled:
# item.clear() sets all Item._media_tag_fields to None before item.update(data) is
# called, so a mid-flight failure can permanently zero out any _media_tag_field not
# in this snapshot.  All writable media tag fields are included below to handle
# both the normal update path and the from_scratch clear+update path.
_ROLLBACK_FIELDS = (
    # Core title/artist fields
    "title",
    "artist",
    "artist_credit",
    "artists",
    "artists_credit",
    "artist_sort",
    "artists_sort",
    # Album-level artist fields
    "album",
    "albumartist",
    "albumartist_credit",
    "albumartists",
    "albumartists_credit",
    "albumartist_sort",
    "albumartists_sort",
    # Date fields
    "year",
    "month",
    "day",
    "original_year",
    "original_month",
    "original_day",
    # Track/disc numbering
    "track",
    "tracktotal",
    "disc",
    "disctotal",
    "track_alt",
    "disctitle",
    # Release metadata
    "comp",
    "genres",
    "albumtype",
    "albumtypes",
    "albumstatus",
    "albumdisambig",
    "label",
    "catalognum",
    "barcode",
    "country",
    "media",
    "language",
    "asin",
    "isrc",
    "script",
    "style",
    "release_group_title",
    "releasegroupdisambig",
    # Discogs fields
    "discogs_albumid",
    "discogs_artistid",
    "discogs_labelid",
    # Classical/extended track fields. These four are the multi-valued forms
    # beets renamed the singulars to (the multi_{arranger,composer,lyricist,
    # remixer}_field migrations); the singulars are no longer Item fields at
    # all, so snapshotting them restored nothing and made rollback stamp a NULL
    # flex attribute onto every item.
    "arrangers",
    "bpm",
    "composers",
    "composer_sort",
    "initial_key",
    "length",
    "lyricists",
    "remixers",
    "subtitle",
    "trackdisambig",
    "work",
    "work_disambig",
    # Provenance
    "data_source",
    "data_url",
    # MusicBrainz IDs
    "mb_trackid",
    "mb_albumid",
    "mb_albumartistid",
    "mb_albumartistids",
    "mb_artistid",
    "mb_artistids",
    "mb_releasetrackid",
    "mb_releasegroupid",
    "mb_workid",
    # Fields cleared by item.clear() in from_scratch mode that are NOT written
    # back by item.update(match_data) — must be snapped so rollback restores them.
    "comments",
    "encoder",
    "grouping",
    "lyrics",
    # ReplayGain / R128 loudness fields
    "rg_track_gain",
    "rg_track_peak",
    "rg_album_gain",
    "rg_album_peak",
    "r128_track_gain",
    "r128_album_gain",
    # AcoustID fingerprint fields
    "acoustid_fingerprint",
    "acoustid_id",
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _run_items_identify(task_key, item_ids, search_artist, search_album, library_db, library_dir):
    """Background thread: run beets autotag on a list of item IDs."""
    task = state.identify_tasks[task_key]
    lib = None
    try:
        from beets import autotag

        log.info(
            "Items identify task=%s item_ids=%r artist=%r album=%r",
            task_key,
            item_ids,
            search_artist,
            search_album,
        )

        lib = _init_beets(library_db, library_dir)

        items = []
        for item_id in item_ids:
            item = lib.get_item(item_id)
            if item is None:
                with state.identify_lock:
                    task["status"] = "error"
                    task["error"] = f"Item {item_id} not found"
                return
            items.append(item)

        if not items:
            with state.identify_lock:
                task["status"] = "error"
                task["error"] = "No items to identify"
            return

        try:
            proposal = autotag.tag_album(
                autotag.Source.from_items(items),
                search_artist=search_artist or None,
                search_name=search_album or None,
                search_ids=[],
            )
        except Exception as e:
            log.exception("Autotag failed for items task %s", task_key)
            with state.identify_lock:
                task["status"] = "error"
                task["error"] = f"Autotag failed: {e}"
            return

        matches = list(proposal.candidates[:5])
        matches.sort(key=lambda m: float(m.distance))
        candidates = [_serialize_candidate(i, m) for i, m in enumerate(matches)]

        log.info("Items identify done task=%s: %d candidates", task_key, len(candidates))

        with state.identify_lock:
            task["_matches"] = matches
            task["_lib"] = lib
            lib = None
            task["candidates"] = candidates
            task["match_generation"] = state.next_match_generation()
            task["status"] = "done"

    except Exception as e:
        log.exception("Items identify error task=%s", task_key)
        with state.identify_lock:
            task["status"] = "error"
            task["error"] = str(e)
    finally:
        if lib is not None:
            lib._close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("/api/items/untagged")
@api.validate(
    resp=Response(HTTP_200=UntaggedItemsResponse, HTTP_500=ErrorResponse),
    tags=["items"],
)
def untagged_items():
    """Return items belonging to albums with NULL or empty albumartist."""
    try:
        conn = _get_ro_conn()
        try:
            rows = conn.execute(
                """
                SELECT i.id, i.title, i.artist, i.album, i.path, i.track,
                       i.album_id, a.albumartist
                FROM items i
                JOIN albums a ON a.id = i.album_id
                WHERE (a.albumartist IS NULL OR a.albumartist = '')
                ORDER BY i.album, i.track, i.title
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = []
    for r in rows:
        result.append(
            {
                "id": r["id"],
                "title": r["title"] or "",
                "artist": r["artist"] or "",
                "album": r["album"] or "",
                "path": _resolve_path(r["path"]) if r["path"] else "",
                "track": r["track"] or 0,
                "album_id": r["album_id"],
            }
        )
    return jsonify(result)


@bp.route("/api/items/fields")
@api.validate(
    resp=Response(HTTP_200=ItemFieldsResponse, HTTP_500=ErrorResponse),
    tags=["items"],
)
def items_fields():
    """Return the catalog of all known beets Item fixed fields with editability flags."""
    try:
        return jsonify(build_field_catalog())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/items/<int:item_id>/tags", methods=["PATCH"])
@api.validate(
    json=ItemTagsPatch,
    resp=Response(
        HTTP_200=ItemTagsPatchResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["items"],
)
def patch_item_tags(item_id):
    """Write a delta of tag fields for a single item.

    Body: { "fields": { "<name>": <value>, ... } }

    Unknown or read-only field names → 400.  Bad numeric/bool values → 400.
    Item not found → 404.  DB write commits first; file-write failure →
    warnings list + HTTP 200 (partial-success, mirrors metadata_batch).
    """
    fields = request.context.json.fields

    for name in fields:
        if not is_editable(name):
            return jsonify({"error": f"field not editable: {name}"}), 400

    library_db = current_app.config["LIBRARY_DB"]
    library_dir = current_app.config.get("LIBRARY_ROOT") or None
    lib = None
    try:
        with state.metadata_write_lock:
            lib = _init_beets(library_db, library_dir)
            item = lib.get_item(item_id)
            if item is None:
                return jsonify({"error": "Item not found"}), 404

            # Only known beets fields or flexible attrs already present on this
            # item are writable. Reject arbitrary new keys so direct API callers
            # cannot create brand-new flexible attrs (the dropdown offers known
            # fields only; existing flex attrs stay editable/clearable).
            for name in fields:
                if not is_known_field(name) and name not in item:
                    return jsonify({"error": f"field not editable: {name}"}), 400

            for name, raw in fields.items():
                try:
                    coerced = coerce_value(name, raw)
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 400
                if coerced is CLEAR:
                    try:
                        del item[name]
                    except KeyError:
                        # Clearing a field that isn't present (e.g. an absent
                        # flexible attr) is a no-op, not an error.
                        pass
                else:
                    item[name] = coerced

            item.store()

            # This generic path can write `lyrics` and any `lyrics_*` attr
            # (`is_editable` is a deny-list), so it owes the same stash
            # invalidation as the dedicated lyrics routes — a fetched result left
            # behind keeps its generation, and a confirm holding it would revert
            # this edit. It is deliberately *not* under `album_write_lock`: this
            # endpoint is keyed by item, not album, and taking an album lock for
            # every tag edit would serialize unrelated writes across the album.
            if any(name == "lyrics" or name.startswith("lyrics_") for name in fields):
                _drop_lyrics_stash(item_id)

            # Any written field could be one an mbsync preview diffed, so this
            # generic path owes the same stash invalidation regardless of which
            # fields changed.
            _drop_mbsync_stash(item.album_id)

            warnings = []
            if not _try_write_item(item):
                path = getattr(item, "path", "") or ""
                if isinstance(path, bytes):
                    path = path.decode("utf-8", errors="replace")
                resolved = _resolve_path(path) if path else path
                log.warning("File write failed for item %d: %s", item_id, resolved)
                warnings.append(f"file write failed: {resolved}")

        return jsonify({"status": "ok", "warnings": warnings})

    except Exception as e:
        log.exception("patch_item_tags failed for item %d", item_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib is not None:
            lib._close()


@bp.route("/api/items/<int:item_id>/metadata", methods=["POST"])
@api.validate(
    json=UpdateMetadataBody,
    resp=Response(
        HTTP_200=UpdateMetadataResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["items"],
)
def update_metadata(item_id):
    """Update artist and album fields on a single item via beets Library."""
    data = request.context.json
    title = (data.title or "").strip()
    artist = (data.artist or "").strip()
    album = (data.album or "").strip()

    if not title and not artist and not album:
        return jsonify({"error": "At least one of title, artist, or album must be provided"}), 400

    library_db = current_app.config["LIBRARY_DB"]
    library_dir = current_app.config.get("LIBRARY_ROOT") or None
    lib = None
    try:
        lib = _init_beets(library_db, library_dir)
        item = lib.get_item(item_id)
        if item is None:
            return jsonify({"error": "Item not found"}), 404

        if title:
            item.title = title
        if artist:
            item.artist = artist
        if album:
            item.album = album
        item.store()
        _drop_mbsync_stash(item.album_id)

        if not _try_write_item(item):
            log.warning("Failed to write tags for item %d after metadata update", item_id)
            return jsonify(
                {"status": "ok", "item_id": item_id, "warnings": ["file tag write failed"]}
            ), 200

        return jsonify({"status": "ok", "item_id": item_id})
    except Exception as e:
        log.exception("Failed to update metadata for item %d", item_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib is not None:
            lib._close()


@bp.route("/api/items/metadata-batch", methods=["POST"])
@api.validate(
    json=MetadataBatchBody,
    resp=Response(
        HTTP_200=MetadataBatchResponse,
        HTTP_400=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["items"],
)
def metadata_batch():
    """Write album-level and per-track metadata in one request under a write lock.

    Body: { album: {album?, albumartist?, year?, genre?},
            items: [{id, title?, artist?, track?, disc?}, ...] }

    Structural / strict-type validation (object shapes, scalar types, integer
    item ids) is enforced by ``MetadataBatchBody`` → 422.  The cross-field
    semantic rules ("at least one field", "≥1 item when album fields present")
    and the lenient numeric coerce-and-warn handling stay here → 400 / warnings
    (see ``schemas/items.py`` for the documented split).

    album-level fields are written via the beets Album object (propagates to
    album row + clears untagged filter); per-track fields are written to item
    rows.  Unknown item ids produce warnings, not 500 errors.
    """
    # Validated model → plain dicts so the existing dict-based logic below stays
    # unchanged.  ``exclude_none=True`` keeps absent optional keys absent, so the
    # handler's ``.get(k) is None`` semantics are preserved.
    body = request.context.json
    album_data = body.album.model_dump(exclude_none=True)
    items_data = [entry.model_dump(exclude_none=True) for entry in body.items]

    # Extract album-level fields
    al_album = (album_data.get("album") or "").strip()
    al_albumartist = (album_data.get("albumartist") or "").strip()
    al_year_raw = album_data.get("year")
    al_genre = (album_data.get("genre") or "").strip()
    al_genres = [g.strip() for g in al_genre.split(",") if g.strip()] if al_genre else []

    _ALBUM_KEYS = ("album", "albumartist", "year", "genre")
    has_album_fields = any(
        album_data.get(k) is not None and album_data.get(k) != "" for k in _ALBUM_KEYS
    )

    _ITEM_OVERRIDE_KEYS = ("title", "artist", "track", "disc")
    has_item_overrides = any(
        any(entry.get(k) is not None and entry.get(k) != "" for k in _ITEM_OVERRIDE_KEYS)
        for entry in items_data
    )

    if not has_album_fields and not has_item_overrides:
        return jsonify({"error": "At least one album field or item override is required"}), 400
    if has_album_fields and not items_data:
        return jsonify(
            {"error": "items must contain at least one id when album fields are present"}
        ), 400

    # Item ids are guaranteed integers by ``MetadataBatchItem`` (strict=True
    # rejects JSON booleans → 422), so no manual id-type check is needed here.

    library_db = current_app.config["LIBRARY_DB"]
    library_dir = current_app.config.get("LIBRARY_ROOT") or None
    lib = None
    try:
        with state.metadata_write_lock:
            lib = _init_beets(library_db, library_dir)
            warnings = []
            albums_updated = []

            # Parse the album-level year once; an invalid value is reported a
            # single time rather than once per album.
            al_year = None
            if al_year_raw is not None:
                try:
                    al_year = int(al_year_raw)
                except (TypeError, ValueError):
                    warnings.append(f"invalid year value: {al_year_raw!r}")

            def _item_disk_path(it):
                """Resolved on-disk path for an Item, for warning messages."""
                path = getattr(it, "path", "") or ""
                if isinstance(path, bytes):
                    path = path.decode("utf-8", errors="replace")
                return _resolve_path(path) if path else path

            def _apply_album_fields(obj):
                """Set the supplied album-level fields on an Album or Item."""
                if al_album:
                    obj.album = al_album
                if al_albumartist:
                    obj.albumartist = al_albumartist
                if al_year is not None:
                    obj.year = al_year
                if al_genres:
                    obj.genres = al_genres

            # Load all item objects; warn on unknown ids
            item_objects = {}
            for entry in items_data:
                item_id = entry["id"]
                item = lib.get_item(item_id)
                if item is None:
                    warnings.append(f"item {item_id} not found")
                else:
                    item_objects[item_id] = item

            # Album pass — write album-level fields via the beets Album object
            updated_album_ids = set()
            if has_album_fields:
                album_ids = {
                    item.album_id for item in item_objects.values() if item.album_id is not None
                }
                for album_id in album_ids:
                    al = lib.get_album(album_id)
                    if al is None:
                        warnings.append(f"album {album_id} not found")
                        continue
                    _apply_album_fields(al)
                    al.store()
                    updated_album_ids.add(album_id)
                    albums_updated.append(album_id)

                    # Album.store() updates the album + member rows in the DB
                    # but never touches files.  Flush the propagated album-level
                    # fields to the files of members NOT named in this request
                    # (multi-disc dirs, shared untagged album rows) — otherwise
                    # their on-disk tags go stale.  Request members are rewritten
                    # in the item pass below with their per-track overrides.
                    # Mirrors genres.py save_genre's album.items() loop.
                    for member in al.items():
                        if member.id in item_objects:
                            continue
                        if not _try_write_item(member):
                            resolved = _item_disk_path(member)
                            log.warning("File write failed for item %d: %s", member.id, resolved)
                            warnings.append(f"file write failed: {resolved}")

            # Item pass — write per-track overrides AND flush propagated
            # album-level fields to every member file.  Album.store() only
            # touches the DB, so the file write below is what keeps on-disk
            # tags from going stale (mirrors genres.py save_genre).
            for entry in items_data:
                item_id = entry["id"]
                item = item_objects.get(item_id)
                if item is None:
                    continue

                # An item belongs to an updated album → its file must be
                # rewritten with the propagated album-level fields.
                album_changed = item.album_id in updated_album_ids
                if album_changed:
                    _apply_album_fields(item)

                changed = album_changed
                title = (entry.get("title") or "").strip()
                artist = (entry.get("artist") or "").strip()
                track_raw = entry.get("track")
                disc_raw = entry.get("disc")

                if title:
                    item.title = title
                    changed = True
                if artist:
                    item.artist = artist
                    changed = True
                if track_raw is not None:
                    try:
                        item.track = int(track_raw)
                        changed = True
                    except (TypeError, ValueError):
                        warnings.append(f"invalid track value for item {item_id}: {track_raw!r}")
                if disc_raw is not None:
                    try:
                        item.disc = int(disc_raw)
                        changed = True
                    except (TypeError, ValueError):
                        warnings.append(f"invalid disc value for item {item_id}: {disc_raw!r}")

                if changed:
                    item.store()
                    if not _try_write_item(item):
                        resolved = _item_disk_path(item)
                        log.warning("File write failed for item %d: %s", item_id, resolved)
                        warnings.append(f"file write failed: {resolved}")

            # The batch spans albums — every album an item in this request
            # belongs to may have had a field an mbsync diff carries rewritten,
            # not just the ones with album-level overrides.
            request_album_ids = {
                item.album_id for item in item_objects.values() if item.album_id is not None
            }
            for aid in request_album_ids:
                _drop_mbsync_stash(aid)

        return jsonify({"status": "ok", "warnings": warnings, "albums_updated": albums_updated})

    except Exception as e:
        log.exception("metadata-batch failed")
        return jsonify({"error": str(e)}), 500
    finally:
        if lib is not None:
            lib._close()


@bp.route("/api/items/identify", methods=["POST"])
@api.validate(
    json=ItemsIdentifyBody,
    resp=Response(HTTP_200=ItemsIdentifyResponse, HTTP_400=ErrorResponse),
    tags=["items"],
)
def items_identify():
    """Start background identification for a list of item IDs."""
    data = request.context.json
    item_ids = data.item_ids
    search_artist = data.search_artist
    search_album = data.search_album

    # ``item_ids`` is typed ``list[int]`` (wrong element type → 422); the
    # non-empty requirement stays here as the handler's own 400.
    if not item_ids:
        return jsonify({"error": "item_ids must be a non-empty list"}), 400

    import uuid

    library_db = current_app.config["LIBRARY_DB"]
    library_dir = current_app.config.get("LIBRARY_ROOT") or None

    task_id = str(uuid.uuid4())
    task_key = f"items_{task_id}"

    task = {
        "task_id": task_id,
        "item_ids": item_ids,
        "status": "running",
        "candidates": [],
        "error": None,
        # Set when the background thread builds _matches; None while running.
        "match_generation": None,
    }
    with state.identify_lock:
        # Remove any previously completed items tasks; close open _lib handles to
        # avoid leaking SQLite connections. Items tasks are UUID-keyed so they
        # accumulate without cleanup.
        stale_keys = [
            k
            for k, v in state.identify_tasks.items()
            if k.startswith("items_") and v.get("status") in ("done", "error")
        ]
        for k in stale_keys:
            old_lib = state.identify_tasks.pop(k).get("_lib")
            if old_lib is not None:
                try:
                    old_lib._close()
                except Exception:
                    pass
        state.identify_tasks[task_key] = task

    t = threading.Thread(
        target=_run_items_identify,
        args=(task_key, item_ids, search_artist, search_album, library_db, library_dir),
        daemon=True,
    )
    t.start()

    return jsonify({"status": "started", "task_id": task_id})


@bp.route("/api/items/identify/<task_id>/status")
@api.validate(
    resp=Response(HTTP_200=ItemsIdentifyStatusResponse),
    tags=["items"],
)
def items_identify_status(task_id):
    task_key = f"items_{task_id}"
    with state.identify_lock:
        task = state.identify_tasks.get(task_key)
        task_json = _get_task_json(task) if task else None
    if task_json is None:
        return jsonify({"status": "idle"})
    return jsonify(task_json)


@bp.route("/api/items/identify/<task_id>/apply", methods=["POST"])
@api.validate(
    json=ItemsCandidateBody,
    resp=Response(HTTP_200=ItemsApplyResponse, HTTP_400=ErrorResponse),
    tags=["items"],
)
def items_apply(task_id):
    """Preview what would change if the selected candidate is applied."""
    # candidate_index is validated as int by ItemsCandidateBody (wrong type → 422);
    # an out-of-range index still reaches the handler's own 400 below.
    candidate_index = request.context.json.candidate_index

    task_key = f"items_{task_id}"
    with state.identify_lock:
        task = state.identify_tasks.get(task_key)
        if not task or task.get("status") != "done":
            return jsonify({"error": "No identification results"}), 400
        matches = task.get("_matches", [])

    if candidate_index < 0 or candidate_index >= len(matches):
        return jsonify({"error": "Invalid candidate index"}), 400

    album_match = matches[candidate_index]
    info = album_match.info

    track_diffs = []
    for item, track_info in album_match.mapping.items():
        diff_entry = {"track": item.track}
        for field in ["title", "artist"]:
            old_val = getattr(item, field, "") or ""
            new_val = getattr(track_info, field, old_val) or ""
            diff_entry[field] = {"old": str(old_val), "new": str(new_val)}
        track_diffs.append(diff_entry)
    track_diffs.sort(key=lambda t: t["track"] or 0)

    # Album-level diff: items are loose (no existing album), old values are empty
    album_data = info.item_data if hasattr(info, "item_data") else {}
    album_diff = {}
    for field in ["album", "albumartist", "year", "label", "mb_albumid"]:
        old_val = ""
        if field == "mb_albumid":
            new_val = info.album_id or ""
        elif field == "albumartist":
            new_val = album_data.get("albumartist", info.artist) or ""
        elif field == "album":
            new_val = album_data.get("album", info.album) or ""
        elif field == "year":
            new_val = album_data.get("year", info.year) or ""
        else:
            new_val = album_data.get(field, "") or ""
        album_diff[field] = {"old": str(old_val), "new": str(new_val)}

    return jsonify(
        {
            "candidate_index": candidate_index,
            "album": album_diff,
            "tracks": track_diffs,
        }
    )


@bp.route("/api/items/identify/<task_id>/confirm", methods=["POST"])
@api.validate(
    json=ItemsConfirmBody,
    resp=Response(
        HTTP_200=ItemsConfirmResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_409=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["items"],
)
def items_confirm(task_id):
    """Create a new album from items and write matched tags to files."""
    # candidate_index validated as int by ItemsConfirmBody (wrong type → 422);
    # an out-of-range index still reaches the handler's own 400 below.
    candidate_index = request.context.json.candidate_index
    expected_generation = request.context.json.expected_generation

    task_key = f"items_{task_id}"
    lib = None
    with state.identify_lock:
        task = state.identify_tasks.get(task_key)
        # Checked before the status/index checks and inside the same critical
        # section that marks the task "confirming", so a re-identify or a
        # competing confirm is rejected atomically. A missing task counts as a
        # mismatch: items tasks are purged by the next items-identify run, and
        # "gone" is a staleness answer, not "no results". Absent field ⇒
        # unchanged behaviour (the SPA does not send it).
        if expected_generation is not None and (
            task is None
            or task.get("match_generation") != expected_generation
            # A confirm already in flight has not bumped the generation yet, so
            # without this arm the loser of the race is told "no results"
            # instead of "stale". Same reasoning as in routes/identify.py.
            or task.get("status") == "confirming"
        ):
            return jsonify({"error": STALE_GENERATION_ERROR}), 409
        if not task or task.get("status") != "done":
            return jsonify({"error": "No identification results"}), 400
        matches = task.get("_matches", [])
        if candidate_index < 0 or candidate_index >= len(matches):
            return jsonify({"error": "Invalid candidate index"}), 400
        task["status"] = "confirming"
        lib = task.pop("_lib", None)

    album_match = matches[candidate_index]
    item_ids = task.get("item_ids", [])

    try:
        if lib is None:
            lib = _init_beets(
                current_app.config["LIBRARY_DB"],
                current_app.config.get("LIBRARY_ROOT") or None,
            )
        # Load item objects and record original state for rollback.
        # Snapshot all _ROLLBACK_FIELDS now (items are freshly loaded from DB,
        # so this captures pre-modification values).  If apply_metadata() stores
        # some items before failing, the rollback can restore the full original
        # metadata — not just album_id.
        items = []
        original_album_ids = {}
        original_metadata = {}
        for item_id in item_ids:
            item = lib.get_item(item_id)
            if item is None:
                with state.identify_lock:
                    task["status"] = "done"
                return jsonify({"error": f"Item {item_id} not found"}), 404
            items.append(item)
            original_album_ids[item_id] = item.album_id
            snap = {f: getattr(item, f, None) for f in _ROLLBACK_FIELDS}
            snap["album_id"] = item.album_id
            original_metadata[item_id] = snap

        log.info(
            "Confirming items identify task=%s candidate=%d: %r - %r",
            task_id,
            candidate_index,
            album_match.info.artist,
            album_match.info.album,
        )

        # Create new album record first; on failure attempt cleanup below.
        # NOTE: beets' transaction __exit__ always calls commit() even when
        # unwinding due to an exception, so add_album() may commit a partial
        # state (album row created, only some items updated) before raising.
        #
        # Snapshot the max album ID before calling add_album so we can detect
        # the edge case where album.add() commits a new album row but then
        # raises before the item loop (leaving no in-memory album_id changes on
        # items to detect from). An album created by add_album that has no items
        # linked to it is guaranteed to be an orphan.
        album = None
        try:
            conn = _get_ro_conn()
            try:
                rows = conn.execute("SELECT COALESCE(MAX(id), 0) FROM albums").fetchall()
                max_album_id_before = rows[0][0]
            finally:
                conn.close()
        except Exception:
            max_album_id_before = 0
            log.warning("Failed to record max album ID before add_album in task %s", task_id)
        try:
            album = lib.add_album(items)
        except Exception as e:
            log.exception("Failed to create album for items task %s", task_id)
            # add_album() sets item.album_id = album.id in-memory before each
            # item.store(); detect the orphan album id from items whose in-memory
            # album_id changed, then remove the orphan row.
            orphan_album_ids = {
                item.album_id
                for item in items
                if item.album_id != original_album_ids.get(item.id) and item.album_id is not None
            }
            # Also query for albums created during add_album that are not linked
            # to any items — covers the case where album.add() committed a row
            # but the exception fired before the item loop assigned album_id to
            # any item, so no in-memory change is visible.
            try:
                conn = _get_ro_conn()
                try:
                    rows = conn.execute(
                        "SELECT a.id FROM albums a "
                        "WHERE a.id > ? "
                        "AND NOT EXISTS "
                        "(SELECT 1 FROM items i WHERE i.album_id = a.id)",
                        (max_album_id_before,),
                    ).fetchall()
                    for row in rows:
                        orphan_album_ids.add(row[0])
                finally:
                    conn.close()
            except Exception:
                log.exception(
                    "Failed to scan for orphan albums after add_album failure "
                    "in task %s — some album rows may be orphaned",
                    task_id,
                )
            for orphan_id in orphan_album_ids:
                try:
                    orphan = lib.get_album(orphan_id)
                    if orphan is not None:
                        orphan.remove(with_items=False)
                except Exception:
                    log.exception(
                        "Failed to remove orphan album %s after add_album failure "
                        "in task %s — album row may be orphaned",
                        orphan_id,
                        task_id,
                    )
            for item in items:
                item.album_id = original_album_ids.get(item.id, item.album_id)
                item.store()
            with state.identify_lock:
                task["status"] = "done"
            return jsonify({"error": f"Failed to create album: {e}"}), 500

        # Sync the new album_id onto mapping items (they are different Python objects
        # from the fresh items passed to add_album, which already had album_id set).
        for item in album_match.mapping.keys():
            item.album_id = album.id

        # Apply track-level and album-level metadata; roll back album on failure
        try:
            album_match.apply_metadata()
            for item in album_match.mapping.keys():
                item.store()

            # Apply album-level metadata and persist
            album_match.apply_album_metadata(album)
            album.store()
        except Exception as e:
            log.exception("Failed to apply metadata for items task %s", task_id)
            try:
                # with_items=False: only remove the album record; keep the item
                # rows in the DB so the subsequent item.store() calls can restore
                # their original album_id values (store() issues UPDATE, not INSERT).
                album.remove(with_items=False)
            except Exception:
                log.exception(
                    "Rollback album.remove() failed for items task %s album_id=%s — "
                    "album row may be orphaned",
                    task_id,
                    album.id,
                )
            # Restore full original metadata so items don't reference the
            # deleted album and any already-stored field changes are reverted.
            # apply_metadata() writes to album_match.mapping.keys() objects
            # (separate Python objects), so some of those items may have been
            # flushed to DB before the failure.  We reload each item fresh from
            # the DB (picking up any partially-written values) so that assigning
            # the original snapshot values is seen as a real change by beets'
            # dirty-tracking (which skips unchanged assignments).  This ensures
            # item.store() writes all original values back, not just album_id.
            rollback_failures = []
            for iid, snap in original_metadata.items():
                fresh = lib.get_item(iid)
                if fresh is None:
                    continue
                for field, value in snap.items():
                    setattr(fresh, field, value)
                try:
                    fresh.store()
                except Exception:
                    log.exception(
                        "Rollback item.store() failed for item %s in task %s — "
                        "item metadata may be partially modified",
                        iid,
                        task_id,
                    )
                    rollback_failures.append(iid)
            # Clear _matches so a retry cannot reuse the now-stale in-memory
            # mapping items (their dirty-tracking _original reflects the
            # candidate values after apply_metadata was called, so a second
            # apply_metadata + store() on the same objects would silently skip
            # writing fields that were already flushed before the failure).
            with state.identify_lock:
                _consume_matches(task)
                task["status"] = "done"
            err_resp = {"error": f"Failed to apply metadata: {e}"}
            if rollback_failures:
                err_resp["rollback_failures"] = rollback_failures
            return jsonify(err_resp), 500

        # Write tags to files (best-effort; partial failures return warnings)
        warnings = []
        for item in album_match.mapping.keys():
            if not _try_write_item(item):
                path = getattr(item, "path", "") or ""
                if isinstance(path, bytes):
                    path = path.decode("utf-8", errors="replace")
                resolved = _resolve_path(path) if path else path
                log.warning("Failed to write tags for item %d: %s", item.id, resolved)
                warnings.append(f"file write failed: {resolved}")
            else:
                log.info("Wrote tags for item %d: %s", item.id, item.title)

        # This flow re-parents items: an mbsync stash keyed by the album an
        # item used to belong to is now describing an album these items have
        # left, and the destination album may itself have had a preview
        # pending against its own pre-confirm state.
        for aid in set(original_album_ids.values()) | {album.id}:
            _drop_mbsync_stash(aid)

        with state.identify_lock:
            _consume_matches(task)
            task["status"] = "done"
        log.info(
            "Items identify confirm done task=%s album_id=%d warnings=%d",
            task_id,
            album.id,
            len(warnings),
        )

        resp = {"status": "ok", "album_id": album.id}
        if warnings:
            resp["warnings"] = warnings
        return jsonify(resp)

    except Exception as e:
        with state.identify_lock:
            _consume_matches(task)
            task["status"] = "done"
        log.exception("Confirm failed for items task %s", task_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib is not None:
            lib._close()
