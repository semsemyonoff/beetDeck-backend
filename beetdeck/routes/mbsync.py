"""MusicBrainz sync routes blueprint.

``POST /api/album/<id>/mbsync`` — preview a re-sync against the album's own
release (this module). The confirm route lands in the same blueprint in a
later task.

Both MusicBrainz reads in this module go straight to the plugin, never
through ``beets.metadata_plugins.album_for_id``: that dispatcher wraps every
call in ``maybe_handle_plugin_error``, which logs a propagated exception on
the **``beets``** logger and returns ``None`` — indistinguishable from the
plugin's own 404 path, which logs on ``beets.musicbrainz`` and also returns
``None``. Resolving the plugin with ``get_metadata_source`` and calling it in
a bare ``try/except`` is what lets this route tell "not in MusicBrainz" (a
``None`` result) apart from "MusicBrainz could not be reached" (a raised
exception) — see the backend's ``CLAUDE.md`` and the plan's *Review
corrections* #20.
"""

from beets import metadata_plugins
from beets.autotag.distance import Distance
from beets.autotag.match import AlbumMatch
from flask import Blueprint, current_app, jsonify
from spectree import Response

from beetdeck import state
from beetdeck.api import api
from beetdeck.mbsync import (
    build_item_info_pairs,
    diff_against,
    resolve_data_source,
    snapshot_album,
    snapshot_items,
    strip_genre_fields,
)
from beetdeck.schemas.common import ErrorResponse
from beetdeck.schemas.mbsync import MbsyncPreviewResponse
from beetdeck.utils import _init_beets, log

bp = Blueprint("mbsync", __name__)


def _mbsync_stash_key(album_id):
    return f"mbsync_{album_id}"


@bp.route("/api/album/<int:album_id>/mbsync", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=MbsyncPreviewResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_409=ErrorResponse,
        HTTP_500=ErrorResponse,
        HTTP_502=ErrorResponse,
    ),
    tags=["mbsync"],
)
def preview_mbsync(album_id):
    """Preview a MusicBrainz re-sync of the album's own release. Never writes."""
    # Read before any `identify_lock` is taken: nesting the two in the opposite
    # order is what the confirm route uses, and rescan_lock -> identify_lock
    # would invert it (Review corrections #21).
    with state.rescan_lock:
        if state.rescan_phase in ("importing", "updating"):
            return jsonify({"error": "A library scan is in progress"}), 409

    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        lib = _init_beets(library_db, library_dir)
        album = lib.get_album(album_id)
        if not album:
            return jsonify({"error": "Album not found"}), 404

        mbid = album.get("mb_albumid") or ""
        if not mbid:
            return jsonify({"error": "Album has no MusicBrainz release id"}), 400

        data_source = resolve_data_source(album)
        if data_source != "MusicBrainz":
            return jsonify(
                {"error": f"Album's metadata source is '{data_source}', not MusicBrainz"}
            ), 400

        plugin = metadata_plugins.get_metadata_source("MusicBrainz")
        if plugin is None:
            return jsonify({"error": "musicbrainz plugin not loaded"}), 500

        try:
            info = plugin.album_for_id(mbid)
        except Exception as exc:
            log.exception("MusicBrainz lookup failed for album_id=%d", album_id)
            return jsonify(
                {"error": "MusicBrainz could not be reached", "reasons": [str(exc)]}
            ), 502

        if info is None:
            return jsonify({"error": f"Release {mbid} not found on MusicBrainz"}), 400

        strip_genre_fields(info)

        items = list(album.items())

        # Mint the stash slot (and its generation) before snapshotting: a write
        # that lands after this point has something to drop (a route added in a
        # later task calls `_drop_mbsync_stash`), which is what closes the
        # pre-snapshot race by construction rather than by a drift check alone
        # (Review corrections #22).
        key = _mbsync_stash_key(album_id)
        with state.identify_lock:
            generation = state.next_stash_generation()
            state.identify_tasks[key] = {"stash_generation": generation}

        pairs, unmapped = build_item_info_pairs(info, items)

        item_snapshot = snapshot_items(items)
        album_snapshot = snapshot_album(album)

        # In-memory only: apply_metadata mutates the Item objects but nothing
        # reaches disk until store()/apply_item_changes, neither of which this
        # route calls.
        match = AlbumMatch(Distance(), info, dict(pairs))
        match.apply_metadata(from_scratch=False)

        album_fields, track_fields = diff_against(album_snapshot, item_snapshot, items)

        track_index = {id(track_info): idx for idx, track_info in enumerate(info.tracks)}
        item_index = {item.id: track_index[id(track_info)] for item, track_info in pairs.items()}

        with state.identify_lock:
            state.identify_tasks[key] = {
                "album_id": album_id,
                "mb_albumid": mbid,
                "data_source": data_source,
                "album_info": info,
                "item_index": item_index,
                "item_snapshot": item_snapshot,
                "album_snapshot": album_snapshot,
                "unmapped_item_ids": [item.id for item in unmapped],
                "stash_generation": generation,
            }

        unmapped_payload = [
            {"item_id": item.id, "track": item.track, "title": item.title} for item in unmapped
        ]

        log.info(
            "MusicBrainz sync preview for album_id=%d: %d album field(s), %d track field(s), "
            "%d unmapped",
            album_id,
            len(album_fields),
            len(track_fields),
            len(unmapped_payload),
        )

        return jsonify(
            {
                "album_id": album_id,
                "mb_albumid": mbid,
                "data_source": data_source,
                "stash_generation": generation,
                "album_fields": album_fields,
                "track_fields": track_fields,
                "unmapped": unmapped_payload,
                "changed": bool(album_fields or track_fields),
            }
        )

    except Exception as e:
        log.exception("MusicBrainz sync preview failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()
