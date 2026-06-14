"""Cover art routes blueprint."""

import os
import sqlite3

from flask import Blueprint, current_app, jsonify, send_file
from spectree import Response

from src import state
from src.api import api
from src.schemas.common import ErrorResponse
from src.schemas.cover import (
    CoverFetchResponse,
    CoverStatusResponse,
    CoverUploadForm,
)
from src.utils import (
    _album_dir_from_items,
    _decode_path,
    _detect_disc_subdirs,
    _find_cover,
    _get_ro_conn,
    _init_beets,
    _resolve_path,
    _save_cover_to_album,
    log,
)

bp = Blueprint("cover", __name__)


# NOTE: the two binary cover routes (``album_cover`` / ``cover_preview``) are
# intentionally **left undecorated**. They stream image data via ``send_file``,
# which puts the Flask response into direct-passthrough mode; spectree's response
# post-processing calls ``response.get_data()`` unconditionally on every decorated
# view, which raises ``RuntimeError`` on a passthrough response. Decorating them
# would therefore break binary delivery, so they do not appear in the OpenAPI spec.
@bp.route("/api/album/<int:album_id>/cover")
def album_cover(album_id):
    try:
        conn = _get_ro_conn()
        try:
            a = conn.execute("SELECT artpath FROM albums WHERE id = ?", (album_id,)).fetchone()
            album_dir = _album_dir_from_items(conn, album_id)
            items = conn.execute(
                "SELECT disc, path, length FROM items WHERE album_id = ?", (album_id,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return "", 404

    artpath = _resolve_path(a["artpath"]) if a and a["artpath"] else None
    if artpath and os.path.isfile(artpath):
        return send_file(artpath)

    disc_dirs = [d["dir"] for d in _detect_disc_subdirs(items) if d["dir"]]
    cover = _find_cover(album_dir, fallback_dirs=disc_dirs)
    if cover:
        return send_file(cover)

    return "", 404


@bp.route("/api/album/<int:album_id>/cover/fetch", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=CoverFetchResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["cover"],
)
def fetch_cover(album_id):
    """Fetch cover art from online sources via fetchart plugin (preview)."""
    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        album = lib.get_album(album_id)
        if not album:
            return jsonify({"error": "Album not found"}), 404

        from beets import plugins

        fetchart = None
        for p in plugins.find_plugins():
            if p.name == "fetchart":
                fetchart = p
                break
        if not fetchart:
            return jsonify({"error": "fetchart plugin not loaded"}), 500

        log.info(
            "Fetching cover art for album_id=%d: %s - %s",
            album_id,
            album.albumartist,
            album.album,
        )

        candidate = fetchart.art_for_album(album, paths=[], local_only=False)

        if not candidate or not candidate.path:
            log.info("No cover art found for album_id=%d", album_id)
            return jsonify({"status": "ok", "found": False})

        # Store candidate path temporarily for confirm step
        # Cover previews share the identify_tasks dict with "cover_" prefix keys
        with state.identify_lock:
            state.identify_tasks[f"cover_{album_id}"] = {
                "candidate_path": _decode_path(candidate.path),
                "source": getattr(candidate, "source_name", "unknown"),
            }

        log.info(
            "Cover art found for album_id=%d from %s: %s",
            album_id,
            getattr(candidate, "source_name", "?"),
            _decode_path(candidate.path),
        )

        return jsonify(
            {
                "status": "ok",
                "found": True,
                "source": getattr(candidate, "source_name", "unknown"),
                "preview_url": f"/api/album/{album_id}/cover/preview",
            }
        )

    except Exception as e:
        log.exception("Cover fetch failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/cover/preview")  # binary stream — see note above
def cover_preview(album_id):
    """Serve the fetched cover art candidate for preview."""
    with state.identify_lock:
        task = state.identify_tasks.get(f"cover_{album_id}")
    if not task or not task.get("candidate_path"):
        return "", 404
    path = task["candidate_path"]
    if os.path.isfile(path):
        return send_file(path)
    return "", 404


@bp.route("/api/album/<int:album_id>/cover/confirm", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=CoverStatusResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["cover"],
)
def confirm_cover(album_id):
    """Save fetched cover to album directory and embed into files."""
    with state.identify_lock:
        task = state.identify_tasks.get(f"cover_{album_id}")
    if not task or not task.get("candidate_path"):
        return jsonify({"error": "No cover art to confirm"}), 400

    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        album = lib.get_album(album_id)
        if not album:
            return jsonify({"error": "Album not found"}), 404

        candidate_path = task["candidate_path"]
        if not os.path.isfile(candidate_path):
            return jsonify({"error": "Cover art file not found"}), 404

        log.info("Saving cover art for album_id=%d from %s", album_id, candidate_path)

        _save_cover_to_album(album, candidate_path)

        with state.identify_lock:
            if state.identify_tasks.get(f"cover_{album_id}") is task:
                state.identify_tasks.pop(f"cover_{album_id}", None)

        log.info("Cover art saved and embedded for album_id=%d", album_id)
        return jsonify({"status": "ok"})

    except Exception as e:
        log.exception("Cover confirm failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/cover/upload", methods=["POST"])
@api.validate(
    form=CoverUploadForm,
    resp=Response(
        HTTP_200=CoverStatusResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["cover"],
)
def upload_cover(album_id):
    """Upload a cover art image, save to album dir and embed into files."""
    import tempfile

    from flask import request

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = os.path.splitext(f.filename)[1].lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    tmp = None
    lib = None
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        f.save(tmp)
        tmp.close()

        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        album = lib.get_album(album_id)
        if not album:
            return jsonify({"error": "Album not found"}), 404

        log.info("Uploading cover art for album_id=%d: %s", album_id, f.filename)

        _save_cover_to_album(album, tmp.name)

        log.info("Cover art uploaded and embedded for album_id=%d", album_id)
        return jsonify({"status": "ok"})

    except Exception as e:
        log.exception("Cover upload failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if tmp and os.path.exists(tmp.name):
            os.unlink(tmp.name)
        if lib:
            lib._close()
