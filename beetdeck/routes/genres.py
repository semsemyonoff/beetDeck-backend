"""Genre routes blueprint."""

import threading

from flask import Blueprint, current_app, jsonify, request
from spectree import Response

from beetdeck.api import api
from beetdeck.schemas.common import ErrorResponse
from beetdeck.schemas.genres import (
    GenrePreviewQuery,
    GenrePreviewResponse,
    GenreSaveBody,
    GenreWriteResponse,
)
from beetdeck.utils import (
    _format_genre,
    _genre_names,
    _init_beets,
    _plugin_reasons,
    _try_write_item,
    log,
)

bp = Blueprint("genres", __name__)

# Serialize concurrent lastgenre preview calls: the beets lastgenre plugin
# stores pretend state as a singleton config key, so concurrent requests would
# corrupt each other's pretend flag without mutual exclusion.
_genre_plugin_lock = threading.Lock()

#: lastgenre swallows every upstream failure and returns no genre, so without
#: these lines "Last.fm has nothing for this album" and "Last.fm refused us" are
#: the same response — and the first is a claim about the album that nobody
#: verified. See `_plugin_reasons`.
_LASTGENRE_REASONS = ("last.fm error:", "error in pylast library:")


def _merge_genres(old, new):
    """`old` first, then the entries of `new` it does not already carry.

    Both sides go through `_genre_names`, which is what keeps a legacy
    separator-joined string from being iterated one character at a time.

    Compared case-insensitively because Last.fm answers lowercase and beets
    title-cases: without that, merging `Rock` with `rock` would keep both.
    Order is preserved rather than sorted — the album's own genres stay in
    front, which is what makes the merge readable as "kept, plus these".
    """
    old, new = _genre_names(old), _genre_names(new)
    merged = list(old)
    seen = {genre.casefold() for genre in merged}
    for genre in new:
        if genre.casefold() not in seen:
            merged.append(genre)
            seen.add(genre.casefold())
    return merged


@bp.route("/api/album/<int:album_id>/genre", methods=["POST"])
@api.validate(
    query=GenrePreviewQuery,
    resp=Response(
        HTTP_200=GenrePreviewResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
        HTTP_502=ErrorResponse,
    ),
    tags=["genres"],
)
def fetch_genre_preview(album_id):
    """Fetch genre from Last.fm without writing. Returns old and new values."""
    mode = request.context.query.mode
    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        album = lib.get_album(album_id)
        if not album:
            return jsonify({"error": "Album not found"}), 404

        from beets import plugins

        lastgenre = None
        for p in plugins.find_plugins():
            if p.name == "lastgenre":
                lastgenre = p
                break

        if not lastgenre:
            return jsonify({"error": "lastgenre plugin not loaded"}), 500

        log.info(
            "Fetching genre preview for album_id=%d: %s - %s",
            album_id,
            album.albumartist,
            album.album,
        )

        # Fetch without writing (pretend mode); restore old value unconditionally.
        # Hold _genre_plugin_lock for the entire sequence: lastgenre.config is a
        # singleton, and concurrent preview requests would otherwise race on the
        # pretend flag (thread A sets True, thread B sets False, thread A runs
        # _process with pretend=False and actually writes the genre).
        with _genre_plugin_lock:
            # Re-load the album inside the lock so _process() operates on the
            # current DB state. Without this, a concurrent /genre/save that
            # committed between the initial load and lock acquisition could leave
            # the stale object with a non-empty genres field, causing _process()
            # with force=false to skip the Last.fm lookup entirely and return the
            # stale value as new_genre — a bogus diff. Loading fresh here also
            # gives us the correct old_genre to restore afterwards.
            fresh = lib.get_album(album_id)
            if not fresh:
                return jsonify({"error": "Album not found"}), 404
            # Two values, deliberately. `restore_genres` is the field exactly as
            # the model handed it over and is the only thing written back: the
            # normalised list is for showing and merging, and putting *that*
            # back would rewrite the stored value as a side effect of looking.
            #
            # No legacy fallback is needed here, unlike in `albums.py`: that
            # route reads raw SQLite rows, where `genres` and the pre-2.10
            # singular `genre` are separate columns, while the beets `Album`
            # model already surfaces the singular value through `genres`
            # (verified: `get("genres") == ["Rock"]`, `get("genre") is None`).
            restore_genres = fresh.get("genres") or []
            old_genre = _genre_names(restore_genres)
            fetched = ""
            lastgenre.config["pretend"].set(True)
            try:
                # Clear in-memory first, exactly as confirm_genre does. Two things
                # depend on it: with `force: no` the plugin skips an album that
                # already has a genre and the old value would come back as the
                # lookup's answer, and a *failed* lookup writes nothing at all — so
                # without clearing, "Last.fm gave us nothing" is indistinguishable
                # from "Last.fm gave us what the album already had".
                fresh.genres = []
                with _plugin_reasons("beets.lastgenre", _LASTGENRE_REASONS) as capture:
                    lastgenre._process(fresh, write=False)
                fetched = fresh.get("genres", "") or ""
            finally:
                lastgenre.config["pretend"].set(False)
                # Restore in-memory state inside the lock so it always runs,
                # even if _process raises. Suppress store() errors so they don't
                # replace the original exception from _process.
                fresh.genres = restore_genres
                try:
                    # Unconditional on purpose: `pretend` is a plugin flag, not a
                    # guarantee, and this store is the net under a lookup that
                    # persisted anyway. It writes the value that was already
                    # there, so it cannot change the album's genre.
                    #
                    # (It cannot change the *representation* either: a pre-2.10
                    # row's `genre` is copied into `genres` by beets' own
                    # migration inside `_init_beets`, before any route runs —
                    # verified with a legacy fixture, `('Rock', None)` becomes
                    # `('Rock', 'Rock')` on library init alone.)
                    fresh.store()
                except Exception:
                    log.exception("Failed to restore genre for album_id=%d", album_id)

        # An upstream refusal must not be reported as "this album has no genre":
        # that is a claim about the library nobody checked. Only when the lookup
        # itself failed — lastgenre logged a reason and produced nothing.
        if not fetched and capture.reasons:
            log.warning("Last.fm lookup failed for album_id=%d: %s", album_id, capture.reasons)
            return jsonify(
                {
                    "error": (
                        "Last.fm lookup failed, so no genre was retrieved (this says "
                        f"nothing about the album): {'; '.join(capture.reasons)}"
                    )
                }
            ), 502

        fetched_list = _genre_names(fetched)
        new_genre = _merge_genres(old_genre, fetched_list) if mode == "merge" else fetched_list

        log.info(
            "Genre preview for album_id=%d (%s): %r + %r -> %r",
            album_id,
            mode,
            old_genre,
            fetched_list,
            new_genre,
        )

        return jsonify(
            {
                "status": "ok",
                "mode": mode,
                "old_genre": _format_genre(old_genre),
                "fetched_genre": _format_genre(fetched_list),
                "new_genre": _format_genre(new_genre),
            }
        )

    except Exception as e:
        log.exception("Genre preview failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/genre/confirm", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=GenreWriteResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["genres"],
)
def confirm_genre(album_id):
    """Write the fetched genre to album and its tracks."""
    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        album = lib.get_album(album_id)
        if not album:
            return jsonify({"error": "Album not found"}), 404

        from beets import plugins

        lastgenre = None
        for p in plugins.find_plugins():
            if p.name == "lastgenre":
                lastgenre = p
                break

        if not lastgenre:
            return jsonify({"error": "lastgenre plugin not loaded"}), 500

        log.info(
            "Confirming genre for album_id=%d: %s - %s",
            album_id,
            album.albumartist,
            album.album,
        )

        # Hold _genre_plugin_lock across the confirm call too: the pretend flag
        # is a singleton on the plugin, and a concurrent preview request could
        # set pretend=True before _process runs, causing confirm to silently
        # skip writing the genre.
        # Re-load the album inside the lock so _process() operates on the
        # current DB state.
        # Clear genres in-memory before calling _process so lastgenre always
        # fetches from Last.fm regardless of the force config. Without this,
        # if the album already has genres set (e.g. from a prior save_genre),
        # _process with force=False skips the lookup entirely and writes
        # nothing, making confirm a silent no-op.
        with _genre_plugin_lock:
            fresh = lib.get_album(album_id)
            if not fresh:
                return jsonify({"error": "Album not found"}), 404
            fresh.genres = []
            lastgenre._process(fresh, write=True)

        new_genre = fresh.get("genres", "") or ""
        log.info("Genre written for album_id=%d: %s", album_id, new_genre)

        # _process(write=True) calls item.try_write() without path resolution,
        # which silently fails for relative paths (stored after rescan preflight
        # normalization). Drive file writes explicitly via _try_write_item so
        # genres are embedded into audio files the same way save_genre does.
        write_failures = []
        for item in fresh.items():
            if not _try_write_item(item):
                log.warning("Failed to write genre tag for track %d: %s", item.track, item.title)
                write_failures.append(item.id)

        if write_failures:
            return jsonify(
                {
                    "status": "partial",
                    "genre": _format_genre(new_genre),
                    "write_failures": write_failures,
                }
            )
        return jsonify({"status": "ok", "genre": _format_genre(new_genre)})

    except Exception as e:
        log.exception("Genre confirm failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/genre/save", methods=["POST"])
@api.validate(
    json=GenreSaveBody,
    resp=Response(
        HTTP_200=GenreWriteResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["genres"],
)
def save_genre(album_id):
    """Manually set genre for album and its tracks."""
    genre = (request.context.json.genre or "").strip()
    if not genre:
        return jsonify({"error": "Genre cannot be empty"}), 400

    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        album = lib.get_album(album_id)
        if not album:
            return jsonify({"error": "Album not found"}), 404

        genres_list = [g.strip() for g in genre.split(",") if g.strip()]
        # beets 2.10.0: genres is the native list field; genre (singular) is
        # deprecated and removed in 3.0. Write only to genres.
        # Hold _genre_plugin_lock across the entire save (album + all items) so
        # that a concurrent confirm_genre() / preview-restore cannot interleave
        # its _process() or old_genre write between album.store() and the
        # per-item stores, which would leave the album and track genres mixed.
        write_failures = []
        with _genre_plugin_lock:
            album.genres = genres_list
            album.store()
            for item in album.items():
                item.genres = genres_list
                item.store()
                if not _try_write_item(item):
                    log.warning(
                        "Failed to write genre tag for track %d: %s", item.track, item.title
                    )
                    write_failures.append(item.id)

        log.info("Genre manually set for album_id=%d: %s", album_id, genre)
        if write_failures:
            return jsonify(
                {
                    "status": "partial",
                    "genre": genre,
                    "write_failures": write_failures,
                }
            )
        return jsonify({"status": "ok", "genre": genre})

    except Exception as e:
        log.exception("Genre save failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()
