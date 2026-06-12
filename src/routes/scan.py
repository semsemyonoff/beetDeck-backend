"""Scan routes for beetDeek.

Endpoints:
    POST  /api/rescan         — start library rescan
    GET   /api/rescan/status  — poll rescan status
"""

import shlex
import sqlite3
import subprocess
import sys

from flask import Blueprint, current_app, jsonify, request

from src import state
from src.utils import _get_ro_conn, _resolve_path, log

bp = Blueprint("scan", __name__)


# ---------------------------------------------------------------------------
# Private helpers (scan-specific)
# ---------------------------------------------------------------------------


def _take_snapshot():
    """Snapshot current items {id: (title, artist, album_id, path)} from the DB."""
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("/api/rescan", methods=["POST"])
def rescan():
    mode = request.args.get("mode", "quick")
    if mode not in {"quick", "full"}:
        return jsonify({"error": f"Invalid mode: {mode!r}. Must be 'quick' or 'full'"}), 400
    import_dir = current_app.config["IMPORT_DIR"]
    library_db = current_app.config["LIBRARY_DB"]
    library_root = current_app.config.get("LIBRARY_ROOT", "")
    with state.rescan_lock:
        if state.rescan_proc and state.rescan_proc.poll() is None:
            return jsonify({"status": "running"}), 409
        try:
            _normalize_absolute_paths(library_db, library_root)
        except Exception:
            log.exception("Rescan preflight: path normalization failed")
            return jsonify({"error": "Failed to normalize library paths before rescan"}), 500
        state.rescan_snapshot = _take_snapshot()
        inc = "-i" if mode == "quick" else "-I"
        cmd = f"beet -v import -A -C {inc} {shlex.quote(import_dir)} && beet -v update -M"
        log.info("Starting rescan (%s): %s", mode, cmd)
        state.rescan_proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
    return jsonify({"status": "started", "mode": mode})


@bp.route("/api/rescan/status")
def rescan_status():
    with state.rescan_lock:
        proc = state.rescan_proc
        snapshot = state.rescan_snapshot
    if proc is None:
        return jsonify({"status": "idle"})
    if proc.poll() is None:
        return jsonify({"status": "running"})
    # Process finished: compute diff, then clear state so future polls return
    # "idle" and repeated calls don't re-query the entire DB each time.
    result = {"status": "done", "returncode": proc.returncode}
    if snapshot is not None:
        after = _take_snapshot()
        added, removed = _compute_scan_diff(snapshot, after)
        result["added"] = added
        result["removed"] = removed
    with state.rescan_lock:
        # Only clear if no new rescan was started between our two lock acquisitions.
        if state.rescan_proc is proc:
            state.rescan_proc = None
            state.rescan_snapshot = None
    return jsonify(result)
