"""Identification routes for beetDeek.

Endpoints:
    POST   /api/album/<id>/ignore           — toggle beetdeck_ignored flag
    POST   /api/album/<id>/identify         — start background autotag
    GET    /api/album/<id>/identify/status  — poll task status
    POST   /api/album/<id>/apply            — preview diff for a candidate
    POST   /api/album/<id>/confirm          — write chosen candidate to files/DB
"""

import sqlite3
import threading
import uuid

from flask import Blueprint, current_app, jsonify, request
from spectree import Response

from beetdeck import state
from beetdeck.api import api
from beetdeck.schemas.common import ErrorResponse
from beetdeck.schemas.identify import (
    ApplyResponse,
    CandidateBody,
    ConfirmBody,
    ConfirmResponse,
    IdentifyStartBody,
    IdentifyStartResponse,
    IdentifyStatusResponse,
    IgnoreResponse,
)
from beetdeck.utils import (
    STALE_GENERATION_ERROR,
    _consume_matches,
    _drop_mbsync_stash,
    _get_ro_conn,
    _get_task_json,
    _init_beets,
    _serialize_candidate,
    _try_write_item,
    log,
)

bp = Blueprint("identify", __name__)


# ---------------------------------------------------------------------------
# Private helpers (identify-specific)
# ---------------------------------------------------------------------------


def _run_identify(
    task_id, album_id, search_artist, search_album, search_id, library_db, library_dir
):
    """Background thread: run beets autotag and store candidates.

    Receives library_db/library_dir as explicit arguments because background
    threads have no Flask app context (current_app would raise RuntimeError).
    """
    task = state.identify_tasks[task_id]
    lib = None
    try:
        from beets import autotag

        log.info(
            "Identify album_id=%d artist=%r album=%r id=%r",
            album_id,
            search_artist,
            search_album,
            search_id,
        )

        lib = _init_beets(library_db, library_dir)
        album = lib.get_album(album_id)
        if not album:
            with state.identify_lock:
                task["status"] = "error"
                task["error"] = "Album not found"
            return

        items = list(album.items())
        if not items:
            with state.identify_lock:
                task["status"] = "error"
                task["error"] = "No tracks in album"
            return

        with state.identify_lock:
            task["current_artist"] = album.albumartist
            task["current_album"] = album.album

        search_ids = [search_id] if search_id else []

        log.info(
            "Running tag_album for %r - %r (%d items)",
            album.albumartist,
            album.album,
            len(items),
        )

        try:
            artist, album_name, proposal = autotag.tag_album(
                items,
                search_artist=search_artist or None,
                search_name=search_album or None,
                search_ids=search_ids or [],
            )
        except Exception as e:
            log.exception("Autotag failed for album_id=%d", album_id)
            with state.identify_lock:
                task["status"] = "error"
                task["error"] = f"Autotag failed: {e}"
            return

        matches = list(proposal.candidates[:5])
        matches.sort(key=lambda m: float(m.distance))

        candidates = [_serialize_candidate(i, m) for i, m in enumerate(matches)]

        for c in candidates:
            log.info(
                "Candidate: %r - %r (dist=%.4f, source=%s)",
                c["artist"],
                c["album"],
                c["distance"],
                c["data_source"],
            )

        log.info("Identify done for album_id=%d: %d candidates", album_id, len(candidates))

        # Transfer ownership of lib to task so confirm_match can reuse it.
        # Set lib=None to prevent the finally block from closing it.
        with state.identify_lock:
            task["_matches"] = matches
            task["_lib"] = lib
            lib = None
            task["candidates"] = candidates
            task["match_generation"] = state.next_match_generation()
            task["status"] = "done"

    except Exception as e:
        log.exception("Identify error for album_id=%d", album_id)
        with state.identify_lock:
            task["status"] = "error"
            task["error"] = str(e)
    finally:
        if lib is not None:
            lib._close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("/api/album/<int:album_id>/ignore", methods=["POST"])
@api.validate(
    resp=Response(HTTP_200=IgnoreResponse, HTTP_404=ErrorResponse, HTTP_500=ErrorResponse),
    tags=["identify"],
)
def toggle_ignore(album_id):
    lib = None
    try:
        lib = _init_beets(
            current_app.config["LIBRARY_DB"],
            current_app.config.get("LIBRARY_ROOT") or None,
        )
        album = lib.get_album(album_id)
        if not album:
            return jsonify({"error": "Album not found"}), 404
        current = getattr(album, "beetdeck_ignored", None)
        if current == "1":
            album.beetdeck_ignored = "0"
            ignored = False
        else:
            album.beetdeck_ignored = "1"
            ignored = True
        album.store()
        return jsonify({"ignored": ignored})
    except Exception as e:
        log.exception("Toggle ignore failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib is not None:
            lib._close()


@bp.route("/api/album/<int:album_id>/identify", methods=["POST"])
@api.validate(
    json=IdentifyStartBody,
    resp=Response(HTTP_200=IdentifyStartResponse, HTTP_409=IdentifyStartResponse),
    tags=["identify"],
)
def identify(album_id):
    data = request.context.json

    library_db = current_app.config["LIBRARY_DB"]
    library_dir = current_app.config.get("LIBRARY_ROOT") or None

    old_lib = None
    with state.identify_lock:
        existing = state.identify_tasks.get(f"album_{album_id}")
        if existing and existing.get("status") in ("running", "confirming"):
            return jsonify({"status": "running", "task_id": existing["task_id"]}), 409

        # Close any open Library from a previous done task before overwriting.
        if existing:
            old_lib = existing.pop("_lib", None)

        task_id = str(uuid.uuid4())[:8]
        task = {
            "task_id": task_id,
            "album_id": album_id,
            "status": "running",
            "candidates": [],
            "current_artist": "",
            "current_album": "",
            "error": None,
            # Set when the background thread builds _matches; None while running.
            "match_generation": None,
        }
        state.identify_tasks[f"album_{album_id}"] = task

    if old_lib is not None:
        try:
            old_lib._close()
        except Exception:
            pass

    t = threading.Thread(
        target=_run_identify,
        args=(
            f"album_{album_id}",
            album_id,
            data.artist,
            data.album,
            data.search_id,
            library_db,
            library_dir,
        ),
        daemon=True,
    )
    t.start()

    return jsonify({"status": "started", "task_id": task_id})


@bp.route("/api/album/<int:album_id>/identify/status")
@api.validate(
    resp=Response(HTTP_200=IdentifyStatusResponse),
    tags=["identify"],
)
def identify_status(album_id):
    with state.identify_lock:
        task = state.identify_tasks.get(f"album_{album_id}")
        task_json = _get_task_json(task) if task else None
    if task_json is None:
        return jsonify({"status": "idle"})
    return jsonify(task_json)


@bp.route("/api/album/<int:album_id>/apply", methods=["POST"])
@api.validate(
    json=CandidateBody,
    resp=Response(
        HTTP_200=ApplyResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["identify"],
)
def apply_match(album_id):
    """Preview what would change if this candidate is applied."""
    candidate_index = request.context.json.candidate_index

    with state.identify_lock:
        task = state.identify_tasks.get(f"album_{album_id}")
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

    try:
        conn = _get_ro_conn()
        try:
            a = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return jsonify({"error": str(e)}), 500

    if not a:
        return jsonify({"error": "Album not found"}), 404

    album_data = info.item_data if hasattr(info, "item_data") else {}
    album_diff = {}
    for field in ["album", "albumartist", "year", "label", "mb_albumid"]:
        old_val = a[field] if a[field] is not None else ""
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


@bp.route("/api/album/<int:album_id>/confirm", methods=["POST"])
@api.validate(
    json=ConfirmBody,
    resp=Response(
        HTTP_200=ConfirmResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_409=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["identify"],
)
def confirm_match(album_id):
    """Apply the selected match using beets API and write tags to files."""
    candidate_index = request.context.json.candidate_index
    expected_generation = request.context.json.expected_generation

    lib = None
    with state.identify_lock:
        task = state.identify_tasks.get(f"album_{album_id}")
        # The precondition is checked before the status/index checks and inside
        # the same critical section that marks the task "confirming", so a
        # re-identify or a competing confirm is rejected atomically rather than
        # detected afterwards. Absent field ⇒ unchanged behaviour (the SPA does
        # not send it).
        if expected_generation is not None and (
            task is None
            or task.get("match_generation") != expected_generation
            # A confirm already in flight has not bumped the generation yet
            # (`_consume_matches` does that when it finishes), so without this
            # arm the loser of the race falls through to the 400 below and a
            # precondition holder is told "no results" instead of "stale".
            or task.get("status") == "confirming"
        ):
            return jsonify({"error": STALE_GENERATION_ERROR}), 409
        if not task or task.get("status") not in ("done",):
            return jsonify({"error": "No identification results"}), 400
        matches = task.get("_matches", [])
        if candidate_index < 0 or candidate_index >= len(matches):
            return jsonify({"error": "Invalid candidate index"}), 400
        # Mark as confirming inside the lock so concurrent requests see a
        # non-"done" status and are rejected instead of applying tags twice.
        task["status"] = "confirming"
        lib = task.pop("_lib", None)

    album_match = matches[candidate_index]
    try:
        if lib is None:
            lib = _init_beets(
                current_app.config["LIBRARY_DB"],
                current_app.config.get("LIBRARY_ROOT") or None,
            )
        album = lib.get_album(album_id)
        if not album:
            with state.identify_lock:
                _consume_matches(task)
                task["status"] = "done"
            return jsonify({"error": "Album not found"}), 404

        log.info(
            "Confirming match for album_id=%d: %r - %r (source=%s, dist=%.4f)",
            album_id,
            album_match.info.artist,
            album_match.info.album,
            album_match.info.data_source,
            float(album_match.distance),
        )

        album_match.apply_metadata()
        write_failures = []
        for item in album_match.mapping.keys():
            item.store()
            if not _try_write_item(item):
                log.warning("Failed to write tags for track %d: %s", item.track, item.title)
                write_failures.append(item.id)
            else:
                log.info("Wrote tags for track %d: %s", item.track, item.title)

        album_match.apply_album_metadata(album)
        album.store()
        _drop_mbsync_stash(album_id)

        if not write_failures:
            album.beetdeck_tagged = "1"
            album.store()
            log.info("Album %d tagged successfully", album_id)
            with state.identify_lock:
                _consume_matches(task)
                task["status"] = "done"
        else:
            log.warning(
                "Album %d: tag write failed for %d track(s), not marking as tagged",
                album_id,
                len(write_failures),
            )
            with state.identify_lock:
                _consume_matches(task)
                task["status"] = "done"
            return (
                jsonify({"error": f"Failed to write tags for {len(write_failures)} track(s)"}),
                500,
            )

        return jsonify({"status": "ok"})

    except Exception as e:
        with state.identify_lock:
            _consume_matches(task)
            task["status"] = "done"
        log.exception("Confirm failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib is not None:
            lib._close()
