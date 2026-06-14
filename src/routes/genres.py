"""Genre routes blueprint."""

import threading

from flask import Blueprint, current_app, jsonify, request
from spectree import Response

from src.api import api
from src.schemas.common import ErrorResponse
from src.schemas.genres import (
    GenrePreviewResponse,
    GenreSaveBody,
    GenreWriteResponse,
)
from src.utils import _format_genre, _init_beets, _try_write_item, log

bp = Blueprint("genres", __name__)

# Serialize concurrent lastgenre preview calls: the beets lastgenre plugin
# stores pretend state as a singleton config key, so concurrent requests would
# corrupt each other's pretend flag without mutual exclusion.
_genre_plugin_lock = threading.Lock()


@bp.route("/api/album/<int:album_id>/genre", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=GenrePreviewResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["genres"],
)
def fetch_genre_preview(album_id):
    """Fetch genre from Last.fm without writing. Returns old and new values."""
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
            old_genre = fresh.get("genres") or []
            new_genre = ""
            lastgenre.config["pretend"].set(True)
            try:
                lastgenre._process(fresh, write=False)
                new_genre = fresh.get("genres", "") or ""
            finally:
                lastgenre.config["pretend"].set(False)
                # Restore in-memory state inside the lock so it always runs,
                # even if _process raises. Suppress store() errors so they don't
                # replace the original exception from _process.
                fresh.genres = old_genre
                try:
                    fresh.store()
                except Exception:
                    log.exception("Failed to restore genre for album_id=%d", album_id)

        log.info("Genre preview for album_id=%d: %r -> %r", album_id, old_genre, new_genre)

        return jsonify(
            {
                "status": "ok",
                "old_genre": _format_genre(old_genre),
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
