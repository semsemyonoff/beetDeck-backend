"""BPM compute route blueprint."""

from flask import Blueprint, current_app, jsonify
from spectree import Response

from beetdeck.api import api
from beetdeck.schemas.bpm import TrackBpmComputeResponse
from beetdeck.schemas.common import ErrorResponse
from beetdeck.utils import _init_beets, _resolve_path, log

bp = Blueprint("bpm", __name__)


@bp.route("/api/album/<int:album_id>/track/<int:item_id>/bpm/compute", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=TrackBpmComputeResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["bpm"],
)
def compute_track_bpm(album_id, item_id):
    """Compute and write BPM tag for a single track using the autobpm plugin."""
    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        item = lib.get_item(item_id)
        if not item or item.album_id != album_id:
            return jsonify({"error": "Track not found"}), 404

        from beets import plugins

        autobpm_plugin = None
        for p in plugins.find_plugins():
            if p.name == "autobpm":
                autobpm_plugin = p
                break
        if not autobpm_plugin:
            return jsonify({"error": "autobpm plugin not loaded"}), 500

        log.info("Computing BPM for item_id=%d: %s - %s", item_id, item.artist, item.title)

        # autobpm loads item.filepath (= item.path) with librosa and calls
        # item.try_write() with no explicit path — both go straight to item.path.
        # This repo stores RELATIVE DB paths, so unresolved they target the wrong
        # location (librosa.load fails → swallowed → 400; a tag write would hit
        # the wrong file). Point beets at the LIBRARY_ROOT-resolved absolute path
        # for the compute+write, then restore the relative DB path the rest of
        # the app relies on (mirrors utils._try_write_item).
        from beets.util import bytestring_path

        original_path = item.path
        resolved_path = bytestring_path(_resolve_path(item.path))
        path_was_resolved = resolved_path != original_path
        if path_was_resolved:
            item.path = resolved_path

        # autobpm.calculate_bpm swallows librosa/beat_track errors internally and
        # never raises. Detect failure by value: clear bpm before the call so a
        # failed compute leaves it falsy (force=True skips the plugin's own
        # "already exists" short-circuit, but we need a clean sentinel).
        item.bpm = None
        try:
            autobpm_plugin.calculate_bpm([item], write=True, force=True)
        finally:
            if path_was_resolved:
                item.path = original_path

        if not item.bpm:
            log.warning("BPM computation failed (no value) for item_id=%d", item_id)
            return jsonify({"error": "BPM computation failed"}), 400

        # On success the plugin stored the resolved (absolute) path to the DB;
        # rewrite the original relative path so the DB stays consistent with the
        # rest of the app (rescan preflight, cover/lyrics path resolution).
        if path_was_resolved:
            item.store()

        log.info("BPM computed for item_id=%d: %d", item_id, item.bpm)
        return jsonify({"status": "ok", "bpm": item.bpm})

    except Exception as e:
        log.exception("BPM compute failed for item_id=%d", item_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()
