"""Scan routes for beetDeek.

Endpoints:
    POST  /api/rescan         — start library rescan
    GET   /api/rescan/status  — poll rescan status
"""

import datetime
import os
import sqlite3
import subprocess
import threading

from flask import Blueprint, current_app, jsonify, request
from spectree import Response

from beetdeck import state
from beetdeck.api import api
from beetdeck.schemas.common import ErrorResponse
from beetdeck.schemas.scan import (
    RescanDismissResponse,
    RescanLogQuery,
    RescanLogResponse,
    RescanQuery,
    RescanRunningResponse,
    RescanStartResponse,
    RescanStatusResponse,
)
from beetdeck.utils import _decode_path, _get_ro_conn, _resolve_path, log

bp = Blueprint("scan", __name__)


# ---------------------------------------------------------------------------
# Private helpers (scan-specific)
# ---------------------------------------------------------------------------


_AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".flac", ".ogg", ".wav", ".aac", ".m4a", ".wma", ".opus", ".aiff", ".ape"}
)


def _count_album_dirs(import_dir):
    """Count directories directly containing audio files under import_dir.

    Used by full-mode scans to establish a deterministic total for the
    progress bar.  Returns 0 when import_dir does not exist or is empty.
    """
    count = 0
    try:
        for _dirpath, _dirnames, filenames in os.walk(import_dir):
            if any(os.path.splitext(f)[1].lower() in _AUDIO_EXTENSIONS for f in filenames):
                count += 1
    except OSError:
        pass
    return count


def _run_tail_thread(run_id, log_path, stop_event):
    """Background tail thread: read new log lines and update progress state.

    Polls the run-log file at ~100 ms intervals, feeds each new line to the
    progress parser, and applies counter updates to shared state under the
    lock.  Runs until *stop_event* is set, then does one final drain.
    """
    from beetdeck.scan_progress import parse_beets_line

    offset = 0

    def _drain(final=False):
        nonlocal offset
        try:
            with open(log_path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
            if not chunk:
                return
            # The log is read mid-write, so a chunk often ends in the middle of
            # a line. Process only complete lines (up to the last newline) and
            # carry the trailing partial into the next drain — otherwise a marker
            # split across two reads is parsed as two non-matching fragments and
            # the album is never counted. The final drain processes everything,
            # since the subprocess has exited and no more bytes will arrive.
            if final:
                consumed = chunk
            else:
                last_nl = chunk.rfind(b"\n")
                if last_nl == -1:
                    return
                consumed = chunk[: last_nl + 1]
            for line in consumed.decode("utf-8", errors="replace").splitlines():
                result = parse_beets_line(line)
                if result is None or result.update is None:
                    continue
                with state.rescan_lock:
                    if state.rescan_run_id != run_id:
                        return
                    if result.update.increment_processed:
                        new_val = state.rescan_processed + 1
                        if state.rescan_total is not None:
                            new_val = min(new_val, state.rescan_total)
                        state.rescan_processed = new_val
                    if result.update.current_item is not None:
                        state.rescan_current_item = result.update.current_item
            offset += len(consumed)
        except (FileNotFoundError, OSError):
            pass

    while not stop_event.wait(timeout=0.1):
        _drain()
    # Final drain to capture any lines written between the last poll and stop
    _drain(final=True)


def _make_run_id():
    """Generate a unique run identifier based on current timestamp."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _make_log_path(run_id, library_db):
    """Return the log file path for a run, creating the scan-logs dir on demand."""
    log_dir = os.path.join(os.path.dirname(library_db), "scan-logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"{run_id}.log")


def _take_snapshot():
    """Snapshot current items {id: (title, artist, album_id, path)} from the DB.

    Requires Flask app context. Use _take_snapshot_db() from background threads.
    """
    try:
        conn = _get_ro_conn()
        try:
            rows = conn.execute("SELECT id, title, artist, album_id, path FROM items").fetchall()
        finally:
            conn.close()
        return {
            r["id"]: (r["title"], r["artist"], r["album_id"], _resolve_path(r["path"]))
            for r in rows
        }
    except Exception:
        return {}


def _take_snapshot_db(library_db, library_root):
    """Snapshot current items without requiring Flask app context (for threads)."""
    try:
        conn = sqlite3.connect(f"file:{library_db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT id, title, artist, album_id, path FROM items").fetchall()
        finally:
            conn.close()

        def _resolve(path):
            p = _decode_path(path)
            if not p or os.path.isabs(p):
                return p
            return os.path.join(library_root, p) if library_root else p

        return {
            r["id"]: (r["title"], r["artist"], r["album_id"], _resolve(r["path"])) for r in rows
        }
    except Exception:
        return {}


def _normalize_absolute_paths(library_db, library_root):
    """Rewrite items.path and albums.artpath absolute paths under library_root to relative.

    beets 2.10.0 stores paths relative to the library root. A library migrated
    from older beets still containing absolute-path rows would cause beet import
    to treat existing files as new and create duplicate albums. This runs as a
    preflight before every rescan so the invariant is guaranteed.

    On a fresh library the DB file may not exist yet (or be an empty file with no
    schema) because beets only creates the items/albums tables on the first
    ``beet import``. In that case there is nothing to normalize, so the missing
    tables are treated as a no-op rather than an error.

    Returns the total number of rows updated.
    """
    if not library_root:
        return 0
    prefix_bytes = (library_root.rstrip("/") + "/").encode("utf-8")
    prefix_len = len(prefix_bytes)
    conn = sqlite3.connect(library_db, timeout=10)
    try:
        cur = conn.cursor()
        existing = {
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('items', 'albums')"
            ).fetchall()
        }
        if "items" not in existing and "albums" not in existing:
            return 0
        items_updated = 0
        if "items" in existing:
            cur.execute(
                "UPDATE items SET path = substr(path, ?) WHERE substr(path, 1, ?) = ?",
                (prefix_len + 1, prefix_len, prefix_bytes),
            )
            items_updated = cur.rowcount
        art_updated = 0
        if "albums" in existing:
            cur.execute(
                "UPDATE albums SET artpath = substr(artpath, ?) "
                "WHERE artpath IS NOT NULL AND substr(artpath, 1, ?) = ?",
                (prefix_len + 1, prefix_len, prefix_bytes),
            )
            art_updated = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    total = items_updated + art_updated
    if total:
        log.warning(
            "Rescan preflight: normalized %d absolute paths under %s (items=%d, artpaths=%d)",
            total,
            library_root,
            items_updated,
            art_updated,
        )
    return total


def _compute_scan_diff(before, after):
    """Compare snapshots and return added/removed lists.

    Match items by normalized path first. Items sharing the same path in both
    snapshots are unchanged regardless of ID changes (beet import may delete and
    re-insert items with new IDs for the same physical file). Items without a
    path fall back to ID-based comparison.
    """
    before_path_map = {path: iid for iid, (_, _, _, path) in before.items() if path}
    after_path_map = {path: iid for iid, (_, _, _, path) in after.items() if path}

    common_paths = set(before_path_map) & set(after_path_map)
    new_paths = set(after_path_map) - common_paths
    gone_paths = set(before_path_map) - common_paths

    before_no_path_ids = {iid for iid, (_, _, _, path) in before.items() if not path}
    after_no_path_ids = {iid for iid, (_, _, _, path) in after.items() if not path}

    added = []
    for path in new_paths:
        iid = after_path_map[path]
        title, artist, *_ = after[iid]
        added.append({"id": iid, "title": title, "artist": artist})
    for iid in after_no_path_ids - before_no_path_ids:
        title, artist, *_ = after[iid]
        added.append({"id": iid, "title": title, "artist": artist})
    added.sort(key=lambda x: x["id"])

    removed = []
    for path in gone_paths:
        iid = before_path_map[path]
        title, artist, *_ = before[iid]
        removed.append({"id": iid, "title": title, "artist": artist})
    for iid in before_no_path_ids - after_no_path_ids:
        title, artist, *_ = before[iid]
        removed.append({"id": iid, "title": title, "artist": artist})
    removed.sort(key=lambda x: x["id"])

    return added, removed


def _run_scan_controller(
    run_id, log_path, inc_flag, import_dir, pre_snap, library_db, library_root, mode
):
    """Daemon controller thread: run import → compute diff → run update → finalize.

    Guards every state write with a run_id staleness check so a stale thread
    (from a superseded test or dismissed scan) never corrupts shared state.
    """
    # Pre-count album dirs (full mode only) to establish the progress total
    total = _count_album_dirs(import_dir) if mode == "full" else None

    with state.rescan_lock:
        if state.rescan_run_id != run_id:
            return
        state.rescan_total = total
        state.rescan_processed = 0
        state.rescan_current_item = None

    stop_tail = threading.Event()
    tail_t = threading.Thread(
        target=_run_tail_thread,
        args=(run_id, log_path, stop_tail),
        daemon=True,
    )
    tail_t.start()

    try:
        with open(log_path, "wb") as log_file:
            # Phase 1: beet import
            import_cmd = ["beet", "-v", "import", "-A", "-C", inc_flag, import_dir]
            log.info("Rescan %s: import %s", run_id, " ".join(import_cmd))
            import_proc = subprocess.Popen(
                import_cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            with state.rescan_lock:
                state.rescan_proc = import_proc
            import_proc.wait()
            import_rc = import_proc.returncode

            # Bail out if state was reset (e.g. test teardown or new scan started)
            with state.rescan_lock:
                if state.rescan_run_id != run_id:
                    return
                if import_rc != 0:
                    state.rescan_proc = None
                    state.rescan_phase = "error"
                    state.rescan_result = {
                        "status": "error",
                        "returncode": import_rc,
                        "added": [],
                        "removed": [],
                    }
                    log.error("Rescan %s: import failed (rc=%d)", run_id, import_rc)
                    return

            # Compute diff (no lock needed — pure local computation)
            post_snap = _take_snapshot_db(library_db, library_root)
            added, removed = _compute_scan_diff(pre_snap, post_snap)

            # Phase 2: beet update — only when import changed something
            if added or removed:
                with state.rescan_lock:
                    if state.rescan_run_id != run_id:
                        return
                    state.rescan_phase = "updating"

                update_cmd = ["beet", "-v", "update", "-M"]
                log.info("Rescan %s: update", run_id)
                update_proc = subprocess.Popen(
                    update_cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                with state.rescan_lock:
                    state.rescan_proc = update_proc
                update_proc.wait()
                update_rc = update_proc.returncode
                final_phase = "error" if update_rc != 0 else "done"
            else:
                update_rc = 0
                final_phase = "done"
                log.info("Rescan %s: no changes, skipping update", run_id)

            with state.rescan_lock:
                if state.rescan_run_id != run_id:
                    return
                state.rescan_proc = None
                state.rescan_phase = final_phase
                state.rescan_result = {
                    "status": final_phase,
                    "returncode": update_rc,
                    "added": added,
                    "removed": removed,
                }
            log.info("Rescan %s: %s (rc=%d)", run_id, final_phase, update_rc)

    except Exception:
        log.exception("Rescan %s: controller thread crashed", run_id)
        with state.rescan_lock:
            if state.rescan_run_id != run_id:
                return
            state.rescan_proc = None
            state.rescan_phase = "error"
            state.rescan_result = {
                "status": "error",
                "returncode": -1,
                "added": [],
                "removed": [],
            }
    finally:
        stop_tail.set()
        tail_t.join(timeout=5)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("/api/rescan", methods=["POST"])
@api.validate(
    query=RescanQuery,
    resp=Response(
        HTTP_200=RescanStartResponse,
        HTTP_409=RescanRunningResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["scan"],
)
def rescan():
    # `mode` is validated by RescanQuery (Literal['quick','full']); an invalid
    # value is rejected upstream by spectree with a 422.
    mode = request.context.query.mode
    import_dir = current_app.config["IMPORT_DIR"]
    library_db = current_app.config["LIBRARY_DB"]
    library_root = current_app.config.get("LIBRARY_ROOT", "")
    with state.rescan_lock:
        if state.rescan_phase in ("importing", "updating"):
            return jsonify({"status": "running"}), 409
        try:
            _normalize_absolute_paths(library_db, library_root)
        except Exception:
            log.exception("Rescan preflight: path normalization failed")
            return jsonify({"error": "Failed to normalize library paths before rescan"}), 500
        pre_snap = _take_snapshot()
        inc = "-i" if mode == "quick" else "-I"
        run_id = _make_run_id()
        log_path = _make_log_path(run_id, library_db)
        state.rescan_snapshot = pre_snap
        state.rescan_run_id = run_id
        state.rescan_log_path = log_path
        state.rescan_phase = "importing"
        state.rescan_result = None
        state.rescan_processed = 0
        state.rescan_total = None
        state.rescan_current_item = None
    log.info("Starting rescan (%s) run_id=%s", mode, run_id)
    t = threading.Thread(
        target=_run_scan_controller,
        args=(run_id, log_path, inc, import_dir, pre_snap, library_db, library_root, mode),
        daemon=True,
    )
    with state.rescan_lock:
        state.rescan_thread = t
    t.start()
    return jsonify({"status": "started", "mode": mode})


@bp.route("/api/rescan/status")
@api.validate(
    resp=Response(HTTP_200=RescanStatusResponse),
    tags=["scan"],
)
def rescan_status():
    with state.rescan_lock:
        phase = state.rescan_phase
        result = state.rescan_result
        processed = state.rescan_processed
        total = state.rescan_total
        current_item = state.rescan_current_item
        run_id = state.rescan_run_id

    progress = {
        "phase": phase,
        "processed": processed,
        "total": total,
        "current_item": current_item,
        "run_id": run_id,
    }

    if phase == "idle":
        return jsonify({"status": "idle", **progress})
    if phase in ("importing", "updating"):
        return jsonify({"status": "running", **progress})
    # phase is "done" or "error" — return the persisted result with progress fields
    if result is not None:
        return jsonify({**result, **progress})
    return jsonify({"status": "idle", **progress})


@bp.route("/api/rescan/dismiss", methods=["POST"])
@api.validate(
    resp=Response(HTTP_200=RescanDismissResponse),
    tags=["scan"],
)
def rescan_dismiss():
    """Clear a persisted finished result so the next status poll returns idle."""
    with state.rescan_lock:
        if state.rescan_phase in ("done", "error"):
            state.rescan_phase = "idle"
            state.rescan_result = None
    return jsonify({"status": "ok"})


@bp.route("/api/rescan/log")
@api.validate(
    query=RescanLogQuery,
    resp=Response(HTTP_200=RescanLogResponse, HTTP_404=ErrorResponse),
    tags=["scan"],
)
def rescan_log():
    """Return incremental log content for a run starting at a byte offset."""
    run_id = request.context.query.run_id
    offset = request.context.query.offset
    library_db = current_app.config["LIBRARY_DB"]
    log_dir = os.path.join(os.path.dirname(library_db), "scan-logs")
    log_path = os.path.join(log_dir, f"{run_id}.log")
    # Guard against path traversal (run_id must resolve inside log_dir)
    if not os.path.abspath(log_path).startswith(os.path.abspath(log_dir) + os.sep):
        return jsonify({"error": "Unknown run_id"}), 404
    if not os.path.isfile(log_path):
        return jsonify({"error": "Unknown run_id"}), 404
    try:
        with open(log_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        new_offset = offset + len(chunk)
    except OSError:
        return jsonify({"error": "Unknown run_id"}), 404
    return jsonify({"text": text, "offset": new_offset})
