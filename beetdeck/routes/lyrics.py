"""Lyrics routes blueprint."""

import os

from flask import Blueprint, current_app, jsonify, request
from spectree import Response

from beetdeck import state
from beetdeck.api import api
from beetdeck.schemas.common import ErrorResponse
from beetdeck.schemas.lyrics import (
    AlbumLyricsConfirmBody,
    AlbumLyricsConfirmResponse,
    AlbumLyricsEmbedResponse,
    AlbumLyricsFetchResponse,
    LyricsConfirmPrecondition,
    LyricsStatusResponse,
    TrackLyricsConfirmResponse,
    TrackLyricsFetchResponse,
    TrackLyricsReadResponse,
    TrackLyricsSaveBody,
    TrackLyricsSaveResponse,
)
from beetdeck.utils import (
    STALE_STASH_ERROR,
    _drop_lyrics_stash,
    _find_lrc_file,
    _get_ro_conn,
    _init_beets,
    _is_instrumental,
    _read_lrc_file,
    _resolve_path,
    _try_write_item,
    log,
)

bp = Blueprint("lyrics", __name__)


@bp.route("/api/album/<int:album_id>/track/<int:item_id>/lyrics")
@api.validate(
    resp=Response(
        HTTP_200=TrackLyricsReadResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["lyrics"],
)
def track_lyrics(album_id, item_id):
    """Get current lyrics for a track (from DB/tags or external .lrc file)."""
    import sqlite3

    try:
        conn = _get_ro_conn()
        try:
            row = conn.execute(
                """
                SELECT i.lyrics, i.path,
                       (SELECT value FROM item_attributes
                        WHERE entity_id = i.id AND key = 'lyrics_instrumental')
                       AS instrumental
                FROM items i WHERE i.id = ? AND i.album_id = ?
                """,
                (item_id, album_id),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({"error": "Track not found"}), 404
    except sqlite3.OperationalError as e:
        return jsonify({"error": str(e)}), 500

    lyrics_text = row["lyrics"] or ""
    item_path = _resolve_path(row["path"])

    lrc_path = _find_lrc_file(item_path)
    lrc_text = _read_lrc_file(lrc_path) if lrc_path else None
    has_lrc = bool(lrc_path)
    # beets 2.13.0+ records an instrumental track as empty lyrics plus this flag.
    # Without it an instrumental track is indistinguishable from one nobody has
    # looked up.
    instrumental = _is_instrumental(row["instrumental"])

    if lyrics_text:
        return jsonify(
            {
                "has_lyrics": True,
                "lyrics": lyrics_text,
                "source": "embedded",
                "has_lrc": has_lrc,
                "instrumental": instrumental,
            }
        )
    elif lrc_text:
        return jsonify(
            {
                "has_lyrics": True,
                "lyrics": lrc_text,
                "source": "lrc_file",
                "lrc_path": lrc_path,
                "has_lrc": has_lrc,
                "instrumental": instrumental,
            }
        )
    else:
        return jsonify(
            {
                "has_lyrics": False,
                "lyrics": "",
                "source": None,
                "has_lrc": has_lrc,
                "instrumental": instrumental,
            }
        )


@bp.route("/api/album/<int:album_id>/track/<int:item_id>/lyrics/fetch", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=TrackLyricsFetchResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["lyrics"],
)
def fetch_track_lyrics(album_id, item_id):
    """Fetch lyrics for a single track from online sources (preview)."""
    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        item = lib.get_item(item_id)
        if not item or item.album_id != album_id:
            return jsonify({"error": "Track not found"}), 404

        from beets import plugins

        lyrics_plugin = None
        for p in plugins.find_plugins():
            if p.name == "lyrics":
                lyrics_plugin = p
                break
        if not lyrics_plugin:
            return jsonify({"error": "lyrics plugin not loaded"}), 500

        log.info("Fetching lyrics for item_id=%d: %s - %s", item_id, item.artist, item.title)

        old_lyrics = item.lyrics or ""
        item_path = _resolve_path(item.path)
        lrc_path = _find_lrc_file(item_path)
        lrc_text = _read_lrc_file(lrc_path) if lrc_path else None
        current_lyrics = old_lyrics or lrc_text or ""
        current_source = "embedded" if old_lyrics else ("lrc_file" if lrc_text else None)

        result = lyrics_plugin.find_lyrics(item)
        # beets 2.13.0+ blanks .text for an instrumental match and raises a flag
        # instead, so testing text alone would report a positive answer from the
        # source as a miss. `found` stays "there is text to show", and the
        # instrumental verdict rides alongside it — it is still worth storing.
        instrumental = bool(result) and getattr(result, "instrumental", False)

        if not result or (not result.text and not instrumental):
            log.info("No lyrics found for item_id=%d", item_id)
            return jsonify(
                {
                    "status": "ok",
                    "found": False,
                    "new_instrumental": False,
                    "current_lyrics": current_lyrics,
                    "current_source": current_source,
                }
            )

        with state.identify_lock:
            # One slot per item, so a competing fetch replaces this silently; the
            # generation is what a confirm can hold against it (see below).
            generation = state.next_stash_generation()
            state.identify_tasks[f"lyrics_{item_id}"] = {
                "_lyrics_obj": result,
                "stash_generation": generation,
            }

        log.info(
            "%s for item_id=%d from %s",
            "Instrumental reported" if instrumental else "Lyrics found",
            item_id,
            result.backend or "unknown",
        )

        return jsonify(
            {
                "status": "ok",
                "found": not instrumental,
                "new_instrumental": instrumental,
                "new_lyrics": result.full_text,
                "new_synced": result.synced,
                "new_backend": result.backend or "unknown",
                "current_lyrics": current_lyrics,
                "current_source": current_source,
                "stash_generation": generation,
            }
        )

    except Exception as e:
        log.exception("Lyrics fetch failed for item_id=%d", item_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/track/<int:item_id>/lyrics/confirm", methods=["POST"])
@api.validate(
    json=LyricsConfirmPrecondition,
    resp=Response(
        HTTP_200=TrackLyricsConfirmResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_409=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["lyrics"],
)
def confirm_track_lyrics(album_id, item_id):
    """Write fetched lyrics to a single track."""
    expected_generation = request.context.json.expected_generation

    lib = None
    # Held across the check *and* the write, like the cover confirm: releasing in
    # between would let a save or an .lrc embed land in the gap and be reverted.
    try:
        with state.album_write_lock(album_id):
            with state.identify_lock:
                task = state.identify_tasks.get(f"lyrics_{item_id}")
                # Same precondition as the album confirm: a client that sends the
                # generation its fetch returned must not commit lyrics that a
                # competing fetch put in the slot afterwards.
                if expected_generation is not None and (
                    task is None or task.get("stash_generation") != expected_generation
                ):
                    return jsonify({"error": STALE_STASH_ERROR}), 409
            if not task or not task.get("_lyrics_obj"):
                return jsonify({"error": "No lyrics to confirm"}), 400

            library_db = current_app.config["LIBRARY_DB"]
            library_dir = current_app.config.get("LIBRARY_ROOT") or None
            lib = _init_beets(library_db, library_dir)
            item = lib.get_item(item_id)
            if not item or item.album_id != album_id:
                return jsonify({"error": "Track not found"}), 404

            new_lyrics = task["_lyrics_obj"]

            # "instrumental" is stored the same way as the other lyrics_* flex
            # attrs, and deliberately keeps our delete-on-falsy rule rather than
            # upstream's `is not None`: GET .../tags surfaces every flex attr
            # verbatim, so writing lyrics_instrumental=0 would put a permanent noise
            # row in the tag editor of every track we ever fetch lyrics for.
            for key in ("backend", "url", "language", "translation_language", "instrumental"):
                item_key = f"lyrics_{key}"
                value = getattr(new_lyrics, key, None)
                if value:
                    item[item_key] = value
                elif item_key in item:
                    del item[item_key]

            # Empty for an instrumental match — which is the point: it also clears
            # the legacy "[Instrumental]" marker from the file tag on write, so a
            # later `beet update` cannot resurrect it.
            item.lyrics = new_lyrics.full_text
            item.store()

            log.info("Lyrics written for item_id=%d", item_id)

            if _try_write_item(item):
                with state.identify_lock:
                    if state.identify_tasks.get(f"lyrics_{item_id}") is task:
                        state.identify_tasks.pop(f"lyrics_{item_id}", None)
                item_path = _resolve_path(item.path)
                lrc_path = _find_lrc_file(item_path)
                sidecar_remains = False
                if lrc_path:
                    try:
                        os.remove(lrc_path)
                        log.info("Removed .lrc file: %s", lrc_path)
                    except OSError:
                        sidecar_remains = True
                        log.warning("Failed to remove .lrc file: %s", lrc_path)
            else:
                log.warning("Failed to write tags for item_id=%d", item_id)
                return jsonify({"error": "Failed to write tags to audio file"}), 500

            # Report the real post-write sidecar state: a swallowed os.remove failure
            # leaves the .lrc on disk, so the client must not assume it is gone.
            return jsonify({"status": "ok", "has_lrc": sidecar_remains})

    except Exception as e:
        log.exception("Lyrics confirm failed for item_id=%d", item_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/track/<int:item_id>/lyrics/embed", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=LyricsStatusResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["lyrics"],
)
def embed_lrc_lyrics(album_id, item_id):
    """Embed lyrics from external .lrc file into track and delete the .lrc."""
    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        item = lib.get_item(item_id)
        if not item or item.album_id != album_id:
            return jsonify({"error": "Track not found"}), 404

        item_path = _resolve_path(item.path)
        lrc_path = _find_lrc_file(item_path)
        if not lrc_path:
            return jsonify({"error": "No .lrc file found"}), 404

        lrc_text = _read_lrc_file(lrc_path)
        if not lrc_text:
            return jsonify({"error": "Could not read .lrc file"}), 500

        # Same album lock as the confirms: this writes the very lyrics a pending
        # confirm might overwrite, and the stash has to go with the write or that
        # confirm's precondition still passes and reverts it.
        with state.album_write_lock(album_id):
            item.lyrics = lrc_text
            item.store()
            if not _try_write_item(item):
                log.warning("Failed to write tags for item_id=%d: %s", item_id, item.title)
                return jsonify({"error": "Failed to write tags to audio file"}), 500

            _drop_lyrics_stash(item_id)
            os.remove(lrc_path)
        log.info("Embedded .lrc and removed file for item_id=%d: %s", item_id, lrc_path)

        return jsonify({"status": "ok"})

    except Exception as e:
        log.exception("Embed .lrc failed for item_id=%d", item_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/lyrics/embed", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=AlbumLyricsEmbedResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["lyrics"],
)
def embed_all_lrc(album_id):
    """Embed all external .lrc files for an album into tracks."""
    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        album = lib.get_album(album_id)
        if not album:
            return jsonify({"error": "Album not found"}), 404

        embedded = []
        # One acquisition for the whole loop — see the single-track embed above.
        with state.album_write_lock(album_id):
            for item in album.items():
                item_path = _resolve_path(item.path)
                lrc_path = _find_lrc_file(item_path)
                if not lrc_path:
                    continue
                lrc_text = _read_lrc_file(lrc_path)
                if not lrc_text:
                    continue
                item.lyrics = lrc_text
                item.store()
                if not _try_write_item(item):
                    log.warning("Failed to write tags for item_id=%d: %s", item.id, item.title)
                    continue
                _drop_lyrics_stash(item.id)
                os.remove(lrc_path)
                embedded.append({"id": item.id, "title": item.title})
                log.info("Embedded .lrc for item_id=%d: %s", item.id, lrc_path)

        return jsonify({"status": "ok", "embedded": embedded})

    except Exception as e:
        log.exception("Embed all .lrc failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/track/<int:item_id>/lyrics/save", methods=["POST"])
@api.validate(
    json=TrackLyricsSaveBody,
    resp=Response(
        HTTP_200=TrackLyricsSaveResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["lyrics"],
)
def save_track_lyrics(album_id, item_id):
    """Manually save edited lyrics to a track."""
    lyrics_text = request.context.json.lyrics
    # Whitespace-only is "no lyrics" — collapse it to "" so presence semantics
    # match the client (which trims) and the empty-save path (keeps the sidecar,
    # reports has_lyrics=False) rather than storing a truthy blank.
    if not lyrics_text.strip():
        lyrics_text = ""

    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        item = lib.get_item(item_id)
        if not item or item.album_id != album_id:
            return jsonify({"error": "Track not found"}), 404

        # Same album lock as the confirms — see confirm_track_lyrics.
        with state.album_write_lock(album_id):
            item.lyrics = lyrics_text
            # A manual edit is the user overruling whatever a source said, so
            # beets' instrumental verdict no longer holds — including when the
            # save is empty, which is how the client clears lyrics.
            if "lyrics_instrumental" in item:
                del item["lyrics_instrumental"]
            item.store()

            # A manual save overrules any fetched result waiting in the stash.
            _drop_lyrics_stash(item_id)

            if _try_write_item(item):
                item_path = _resolve_path(item.path)
                lrc_path = _find_lrc_file(item_path)
                if lrc_path and lyrics_text:
                    os.remove(lrc_path)
                    log.info("Removed .lrc file after manual edit: %s", lrc_path)
                    lrc_path = None
            else:
                log.warning("Failed to write tags for item_id=%d", item_id)
                return jsonify({"error": "Failed to write tags to audio file"}), 500

        # Report authoritative post-save presence so the client seeds real
        # content rather than the has_lrc file-existence flag. An empty save
        # keeps the sidecar, but it only counts as lyrics when it is readable and
        # non-empty (same definition as GET .../lyrics): an empty/unreadable .lrc
        # has has_lrc=True yet has_lyrics=False.
        lrc_text = _read_lrc_file(lrc_path) if lrc_path else None
        has_lrc = bool(lrc_path)
        has_lyrics = bool(lyrics_text) or bool(lrc_text)

        log.info("Lyrics manually saved for item_id=%d", item_id)
        return jsonify({"status": "ok", "has_lyrics": has_lyrics, "has_lrc": has_lrc})

    except Exception as e:
        log.exception("Lyrics save failed for item_id=%d", item_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/lyrics/fetch", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=AlbumLyricsFetchResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["lyrics"],
)
def fetch_album_lyrics(album_id):
    """Fetch lyrics for all tracks in an album (preview)."""
    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        album = lib.get_album(album_id)
        if not album:
            return jsonify({"error": "Album not found"}), 404

        from beets import plugins

        lyrics_plugin = None
        for p in plugins.find_plugins():
            if p.name == "lyrics":
                lyrics_plugin = p
                break
        if not lyrics_plugin:
            return jsonify({"error": "lyrics plugin not loaded"}), 500

        items = list(album.items())
        items.sort(key=lambda it: (it.disc or 1, it.track or 0))

        log.info(
            "Fetching lyrics for album_id=%d: %s - %s (%d tracks)",
            album_id,
            album.albumartist,
            album.album,
            len(items),
        )

        # One generation for the whole fetch, stamped on every row it stashes:
        # the album confirm writes the selection as a unit, so the precondition
        # it checks has to be per-fetch, not per-item.
        with state.identify_lock:
            generation = state.next_stash_generation()

        results = []
        for item in items:
            item_path = _resolve_path(item.path)
            lrc_path = _find_lrc_file(item_path)
            lrc_text = _read_lrc_file(lrc_path) if lrc_path else None
            current_lyrics = item.lyrics or lrc_text or ""
            current_source = "embedded" if item.lyrics else ("lrc_file" if lrc_text else None)

            found_lyrics = lyrics_plugin.find_lyrics(item)

            entry = {
                "item_id": item.id,
                "track": item.track,
                "disc": item.disc,
                "title": item.title,
                "artist": item.artist,
                "current_lyrics": current_lyrics,
                "current_source": current_source,
                "found": False,
                "new_instrumental": False,
                "new_lyrics": "",
                "new_synced": False,
                "new_backend": "",
            }

            # Same instrumental handling as the single-track fetch above.
            instrumental = bool(found_lyrics) and getattr(found_lyrics, "instrumental", False)

            if found_lyrics and (found_lyrics.text or instrumental):
                entry["found"] = not instrumental
                entry["new_instrumental"] = instrumental
                entry["new_lyrics"] = found_lyrics.full_text
                entry["new_synced"] = found_lyrics.synced
                entry["new_backend"] = found_lyrics.backend or "unknown"

                with state.identify_lock:
                    state.identify_tasks[f"lyrics_{item.id}"] = {
                        "_lyrics_obj": found_lyrics,
                        "stash_generation": generation,
                    }

                log.info(
                    "%s for %s - %s from %s",
                    "Instrumental reported" if instrumental else "Lyrics found",
                    item.artist,
                    item.title,
                    found_lyrics.backend or "unknown",
                )
            else:
                log.info("No lyrics found for %s - %s", item.artist, item.title)

            results.append(entry)

        return jsonify({"status": "ok", "tracks": results, "stash_generation": generation})

    except Exception as e:
        log.exception("Album lyrics fetch failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/lyrics/confirm", methods=["POST"])
@api.validate(
    json=AlbumLyricsConfirmBody,
    resp=Response(
        HTTP_200=AlbumLyricsConfirmResponse,
        HTTP_400=ErrorResponse,
        HTTP_409=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["lyrics"],
)
def confirm_album_lyrics(album_id):
    """Write fetched lyrics for selected tracks in album."""
    item_ids = request.context.json.item_ids
    expected_generation = request.context.json.expected_generation

    if not item_ids:
        return jsonify({"error": "No tracks selected"}), 400

    # One snapshot of the stash, taken under the lock and written from below.
    # Re-reading each entry inside the write loop instead would let a fetch that
    # lands mid-apply slip past a precondition that already passed — the loop
    # spends a real file write per track, so that window is seconds wide.
    # Held across the check *and* the loop: the precondition would otherwise be
    # checked, released, and only then written against — long enough for a save
    # or an .lrc embed to land in between and be reverted.
    with state.album_write_lock(album_id):
        with state.identify_lock:
            tasks = {item_id: state.identify_tasks.get(f"lyrics_{item_id}") for item_id in item_ids}
            # All-or-nothing: the selection came out of one fetch, so a single id
            # whose stash was replaced (or already consumed) means the caller is
            # looking at a state that no longer exists. Skipping just that id would
            # write a partial change the caller never previewed.
            if expected_generation is not None and any(
                (task or {}).get("stash_generation") != expected_generation
                for task in tasks.values()
            ):
                return jsonify({"error": STALE_STASH_ERROR}), 409

        lib = None
        try:
            library_db = current_app.config["LIBRARY_DB"]
            library_dir = current_app.config.get("LIBRARY_ROOT") or None
            lib = _init_beets(library_db, library_dir)
            written_item_ids = []
            retained_lrc_item_ids = []
            failed = []

            for item_id in item_ids:
                task = tasks.get(item_id)
                if not task or not task.get("_lyrics_obj"):
                    continue

                item = lib.get_item(item_id)
                if not item or item.album_id != album_id:
                    continue

                new_lyrics = task["_lyrics_obj"]

                # Same flex-attr contract as the single-track confirm above.
                for key in ("backend", "url", "language", "translation_language", "instrumental"):
                    item_key = f"lyrics_{key}"
                    value = getattr(new_lyrics, key, None)
                    if value:
                        item[item_key] = value
                    elif item_key in item:
                        del item[item_key]

                item.lyrics = new_lyrics.full_text
                item.store()

                if _try_write_item(item):
                    with state.identify_lock:
                        if state.identify_tasks.get(f"lyrics_{item_id}") is task:
                            state.identify_tasks.pop(f"lyrics_{item_id}", None)
                    item_path = _resolve_path(item.path)
                    lrc_path = _find_lrc_file(item_path)
                    if lrc_path:
                        try:
                            os.remove(lrc_path)
                            log.info("Removed .lrc file: %s", lrc_path)
                        except OSError:
                            # The lyrics write succeeded but the sidecar lingers; tell
                            # the client so it doesn't record has_lrc=False for this id.
                            retained_lrc_item_ids.append(item_id)
                            log.warning("Failed to remove .lrc file: %s", lrc_path)
                    written_item_ids.append(item_id)
                    log.info("Lyrics written for item_id=%d", item_id)
                else:
                    log.warning("Failed to write tags for item_id=%d", item_id)
                    failed.append(item_id)

            return jsonify(
                {
                    "status": "ok",
                    "written": len(written_item_ids),
                    "written_item_ids": written_item_ids,
                    "retained_lrc_item_ids": retained_lrc_item_ids,
                    "failed": failed,
                }
            )

        except Exception as e:
            log.exception("Album lyrics confirm failed for album_id=%d", album_id)
            return jsonify({"error": str(e)}), 500
        finally:
            if lib:
                lib._close()
