"""Cover art routes blueprint."""

import os
import sqlite3
import threading

from flask import Blueprint, current_app, jsonify, request, send_file
from spectree import Response

from beetdeck import state
from beetdeck.api import api
from beetdeck.schemas.common import ErrorResponse
from beetdeck.schemas.cover import (
    CoverConfirmBody,
    CoverFetchResponse,
    CoverStatusResponse,
    CoverUploadForm,
)
from beetdeck.utils import (
    COVER_THUMB_SIZES,
    STALE_STASH_ERROR,
    _album_dir_from_items,
    _cover_thumbnail,
    _decode_path,
    _detect_disc_subdirs,
    _drop_cover_stash,
    _find_cover,
    _get_ro_conn,
    _image_size,
    _init_beets,
    _plugin_reasons,
    _resolve_path,
    _save_cover_to_album,
    log,
)

bp = Blueprint("cover", __name__)

# Serialize every `art_for_album` call: the relaxed retry below narrows the
# plugin's own `minwidth`/`enforce_ratio` attributes, and fetchart reads them
# off the singleton at validation time. Without this lock a concurrent ordinary
# fetch would validate against the relaxed values and silently accept art its
# caller never agreed to. The first pass is inside the lock for that reason —
# not because it needs one of its own.
_fetchart_plugin_lock = threading.Lock()

#: fetchart log lines worth repeating to the caller. Its rejections are `debug`
#: and then discarded, so `found: false` otherwise cannot say whether the art is
#: missing or merely refused.
_FETCHART_REASONS = (
    "image too small",
    "image size out of bounds",
    "not a supported image",
    "error receiving response",
    "Could not get size of image",
)


# NOTE: the two binary cover routes (``album_cover`` / ``cover_preview``) are
# intentionally **left undecorated**. They stream image data via ``send_file``,
# which puts the Flask response into direct-passthrough mode; spectree's response
# post-processing calls ``response.get_data()`` unconditionally on every decorated
# view, which raises ``RuntimeError`` on a passthrough response. Decorating them
# would therefore break binary delivery, so they do not appear in the OpenAPI spec.
@bp.route("/api/album/<int:album_id>/cover")
def album_cover(album_id):
    # Optional ``?size=`` returns a cached WebP thumbnail fitting size×size;
    # omitting it streams the original cover file unchanged (legacy behavior).
    size = request.args.get("size", type=int)
    if size is not None and size not in COVER_THUMB_SIZES:
        return jsonify({"error": f"size must be one of {list(COVER_THUMB_SIZES)}"}), 400

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
        src = artpath
    else:
        disc_dirs = [d["dir"] for d in _detect_disc_subdirs(items) if d["dir"]]
        src = _find_cover(album_dir, fallback_dirs=disc_dirs)

    if not src:
        return "", 404

    if size is not None:
        thumb = _cover_thumbnail(src, size, current_app.config["COVER_CACHE_DIR"])
        if thumb:
            resp = send_file(thumb, mimetype="image/webp")
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
        # On any thumbnailing failure, fall back to streaming the original.

    return send_file(src)


def _current_cover_size(album_id, artpath_raw):
    """``(width, height)`` of the cover the album already has, or ``None``.

    Answered exactly the way `GET /api/album/<id>` and `album_cover` answer it —
    the stored `artpath` first, then a probe of the album directory with the
    per-disc subdirs as fallbacks — so a caller comparing the candidate against
    what it would replace gets both numbers from one request instead of two, and
    gets the same answer either endpoint would give.

    Two details are what make it agree with them: library paths are stored
    *relative* to the DB root and must go through `_resolve_path`, and a
    multi-disc album often keeps its cover only inside `CD1/`.
    """
    artpath = _resolve_path(artpath_raw) if artpath_raw else None
    if artpath and os.path.isfile(artpath):
        return _image_size(artpath)
    try:
        conn = _get_ro_conn()
        try:
            album_dir = _album_dir_from_items(conn, album_id)
            items = conn.execute(
                "SELECT disc, path, length FROM items WHERE album_id = ?", (album_id,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return None
    disc_dirs = [d["dir"] for d in _detect_disc_subdirs(items) if d["dir"]]
    return _image_size(_find_cover(album_dir, fallback_dirs=disc_dirs))


def _relaxed_art(fetchart, album):
    """Retry the lookup with the size filters off.

    Returns `(candidate, constraints)` — the constraints being the values that
    were lifted, so the caller can check the candidate against them. Whether the
    retry *succeeded because* of the lifting is not knowable here: a first pass
    can also come back empty from a timeout, and calling that a policy rejection
    would be a made-up reason.

    `(None, None)` when there is nothing to retry: `minwidth` and `enforce_ratio`
    are the only two checks in fetchart's validation that reject a candidate
    outright (everything else — `maxwidth`, `max_filesize`, `cover_format` — is
    a resize instruction), so with both off the retry is a byte-identical
    duplicate of the sweep that just failed, at the cost of a second one.
    """
    if not (fetchart.minwidth or fetchart.enforce_ratio):
        return None, None

    saved = {"min_width": fetchart.minwidth, "enforce_ratio": bool(fetchart.enforce_ratio)}
    fetchart.minwidth, fetchart.enforce_ratio = 0, False
    try:
        candidate = fetchart.art_for_album(album, paths=[], local_only=False)
    finally:
        fetchart.minwidth = saved["min_width"]
        fetchart.enforce_ratio = saved["enforce_ratio"]
    return (candidate, saved) if candidate is not None else (None, None)


def _violated_constraints(size, constraints):
    """The lifted constraints this candidate actually fails, in words.

    Empty when it meets all of them — which happens when the first pass failed
    for a reason that has nothing to do with size, and is exactly when the
    response must *not* claim a filter refused the image.

    An unmeasurable candidate also yields nothing: fetchart skips its own size
    checks when it cannot read the dimensions, so size was not the reason there
    either.
    """
    if not size:
        return []
    width, height = size
    violated = []
    if constraints["min_width"] and width < constraints["min_width"]:
        violated.append(f"the configured minimum width of {constraints['min_width']} px")
    if constraints["enforce_ratio"] and width != height:
        violated.append("the configured aspect-ratio requirement")
    return violated


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

        current_size = _current_cover_size(album_id, album.artpath)

        # Both passes share one lock — see `_fetchart_plugin_lock`.
        with _fetchart_plugin_lock, _plugin_reasons("beets.fetchart", _FETCHART_REASONS) as capture:
            candidate = fetchart.art_for_album(album, paths=[], local_only=False)
            relaxed_from = None
            if not candidate or not candidate.path:
                candidate, relaxed_from = _relaxed_art(fetchart, album)
        # Deduplicated in first-seen order: the retry walks the same sources and
        # re-reports every rejection the first pass already reported.
        reasons = list(dict.fromkeys(capture.reasons))

        if not candidate or not candidate.path:
            log.info("No cover art found for album_id=%d (%s)", album_id, "; ".join(reasons) or "-")
            return jsonify(
                {
                    "status": "ok",
                    "found": False,
                    "current_width": current_size[0] if current_size else None,
                    "current_height": current_size[1] if current_size else None,
                    # The plugin's own words for why each candidate was dropped:
                    # an empty list means no source offered anything at all.
                    "reasons": reasons,
                }
            )

        candidate_path = _decode_path(candidate.path)
        size = _image_size(candidate_path)

        # Store candidate path temporarily for confirm step
        # Cover previews share the identify_tasks dict with "cover_" prefix keys
        with state.identify_lock:
            # The stash is one slot per album, so a competing fetch replaces it
            # silently. The generation is what lets a client that previewed this
            # candidate refuse to commit somebody else's — see confirm below.
            generation = state.next_stash_generation()
            state.identify_tasks[f"cover_{album_id}"] = {
                "candidate_path": candidate_path,
                "source": getattr(candidate, "source_name", "unknown"),
                "stash_generation": generation,
            }

        log.info(
            "Cover art found for album_id=%d from %s: %s",
            album_id,
            getattr(candidate, "source_name", "?"),
            candidate_path,
        )

        result = {
            "status": "ok",
            "found": True,
            "source": getattr(candidate, "source_name", "unknown"),
            "preview_url": f"/api/album/{album_id}/cover/preview",
            "width": size[0] if size else None,
            "height": size[1] if size else None,
            "current_width": current_size[0] if current_size else None,
            "current_height": current_size[1] if current_size else None,
            "stash_generation": generation,
        }
        if relaxed_from is not None:
            # Only a candidate that actually fails a lifted constraint is
            # reported as relaxed. One that meets them all came back on the
            # retry for some other reason — a timed-out source, a flaky host —
            # and calling that a policy rejection would invent the very kind of
            # reason this endpoint exists to stop inventing.
            violated = _violated_constraints(size, relaxed_from)
            if violated:
                measured = f"{size[0]}×{size[1]}"
                result["relaxed"] = True
                if relaxed_from["min_width"]:
                    result["min_width"] = relaxed_from["min_width"]
                result["warning"] = (
                    f"this cover ({measured}) does not meet {' and '.join(violated)}, and was "
                    "only found once that was lifted for a second lookup — take it only if it "
                    "beats the cover you have"
                )
            elif reasons:
                result["retried"] = True
                result["warning"] = (
                    "the first lookup came back empty and a second one found this cover, which "
                    f"does meet the configured filters — the sources reported: {'; '.join(reasons)}"
                )
        return jsonify(result)

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
    json=CoverConfirmBody,
    resp=Response(
        HTTP_200=CoverStatusResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_409=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["cover"],
)
def confirm_cover(album_id):
    """Save fetched cover to album directory and embed into files."""
    expected_generation = request.context.json.expected_generation

    lib = None
    # Held across the check *and* the write: checking the generation and then
    # releasing would let an upload land in between and be reverted by this
    # write, which is the race the precondition exists to stop.
    try:
        with state.album_write_lock(album_id):
            with state.identify_lock:
                task = state.identify_tasks.get(f"cover_{album_id}")
                # A client that sends the generation its fetch returned must not
                # commit a candidate someone else fetched into the same slot.
                # Absent field ⇒ unchanged behaviour (the SPA does not send it).
                if expected_generation is not None and (
                    task is None or task.get("stash_generation") != expected_generation
                ):
                    return jsonify({"error": STALE_STASH_ERROR}), 409
            if not task or not task.get("candidate_path"):
                return jsonify({"error": "No cover art to confirm"}), 400

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

        # Same lock as the confirm, for the same reason: an upload that ran
        # inside a confirm's check-to-write window would be reverted by it.
        with state.album_write_lock(album_id):
            _save_cover_to_album(album, tmp.name)
            # An upload overrules any fetched candidate waiting in the stash.
            _drop_cover_stash(album_id)

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
