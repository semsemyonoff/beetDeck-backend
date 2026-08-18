"""MusicBrainz sync routes blueprint.

``POST /api/album/<id>/mbsync`` previews a re-sync against the album's own
release and stashes the diff; ``POST /api/album/<id>/mbsync/confirm`` applies
that stash and never looks the release up again. Both live in this module.

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
from flask import Blueprint, current_app, jsonify, request
from spectree import Response

from beetdeck import state
from beetdeck.api import api
from beetdeck.mbsync import (
    build_item_info_pairs,
    check_drift,
    diff_against,
    resolve_data_source,
    restore_fields,
    snapshot_album,
    snapshot_items,
    strip_genre_fields,
)
from beetdeck.schemas.common import ErrorResponse
from beetdeck.schemas.mbsync import MbsyncConfirmBody, MbsyncConfirmResponse, MbsyncPreviewResponse
from beetdeck.utils import STALE_STASH_ERROR, _init_beets, _try_write_item, log

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

    key = _mbsync_stash_key(album_id)
    minted = None
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
        # that lands after this point has something to drop (every route that
        # writes a field this diff could carry calls `_drop_mbsync_stash`),
        # which is what closes the pre-snapshot race by construction rather than
        # by a drift check alone (Review corrections #22).
        with state.identify_lock:
            generation = state.next_stash_generation()
            minted = {"stash_generation": generation}
            state.identify_tasks[key] = minted

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
            # Only fill in the slot this preview minted. Re-assigning
            # unconditionally would *resurrect* a stash a competing write
            # dropped while the snapshot was being taken — under the same
            # generation, so the confirm's precondition would pass and revert
            # the write that overruled it. That is the very race the
            # mint-before-snapshot ordering above exists to close.
            if state.identify_tasks.get(key) is not minted:
                return jsonify({"error": STALE_STASH_ERROR}), 409
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
                # The confirm's drift check runs check_drift() on the live,
                # pre-mutation items/album before it ever calls apply_metadata, so
                # it needs to know which field names to look at *before* it can
                # recompute them the way this preview just did.
                "album_field_names": [f["field"] for f in album_fields],
                "track_field_names": sorted({f["field"] for f in track_fields}),
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
        # A failure between the mint and the fill leaves a slot carrying only a
        # generation; a confirm sent without `expected_generation` would find it
        # and blow up on the missing snapshot. Identity-checked so a slot some
        # other preview has since minted is not collateral damage.
        if minted is not None:
            with state.identify_lock:
                if state.identify_tasks.get(key) is minted:
                    state.identify_tasks.pop(key, None)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()


@bp.route("/api/album/<int:album_id>/mbsync/confirm", methods=["POST"])
@api.validate(
    json=MbsyncConfirmBody,
    resp=Response(
        HTTP_200=MbsyncConfirmResponse,
        HTTP_400=ErrorResponse,
        HTTP_409=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["mbsync"],
)
def confirm_mbsync(album_id):
    """Apply the diff stashed by the preview. Never looks the release up again."""
    expected_generation = request.context.json.expected_generation
    excluded_fields = set(request.context.json.excluded_fields or [])

    # Read before any `identify_lock` is taken, same ordering the preview uses.
    with state.rescan_lock:
        if state.rescan_phase in ("importing", "updating"):
            return jsonify({"error": "A library scan is in progress"}), 409

    key = _mbsync_stash_key(album_id)
    write_failures = []
    lib = None
    try:
        # Held across the check *and* the write, same as cover/lyrics confirms:
        # releasing between them would let a competing write land in the window
        # and be reverted by this one.
        with state.album_write_lock(album_id):
            with state.identify_lock:
                task = state.identify_tasks.get(key)
                if expected_generation is not None and (
                    task is None or task.get("stash_generation") != expected_generation
                ):
                    return jsonify({"error": STALE_STASH_ERROR}), 409
            if task is None:
                return jsonify({"error": "No sync preview found for this album"}), 400

            # `restore_fields` writes the preview-time value back onto every item
            # (or `del`s it), and the drift check only covers the fields the
            # preview reported changed — so a name from outside the diff would be
            # reverted, and written to the audio file, with no freshness check at
            # all. Bad value, not a bad shape, so 400 rather than 422.
            known_fields = set(task.get("album_field_names") or []) | set(
                task.get("track_field_names") or []
            )
            unknown_fields = sorted(excluded_fields - known_fields)
            if unknown_fields:
                return jsonify(
                    {
                        "error": "Excluded field(s) are not part of this preview: "
                        + ", ".join(unknown_fields)
                    }
                ), 400

            library_db = current_app.config["LIBRARY_DB"]
            library_dir = current_app.config.get("LIBRARY_ROOT") or None
            lib = _init_beets(library_db, library_dir)
            album = lib.get_album(album_id)
            if album is None:
                # Folded into the freshness contract, not a plain 404: the album
                # existed when the preview ran (the stash is proof of that), so
                # its disappearance since is staleness like any other drift.
                return jsonify({"error": "Album no longer exists"}), 409

            items = list(album.items())
            item_by_id = {item.id: item for item in items}

            item_snapshot = task["item_snapshot"]
            album_snapshot = task["album_snapshot"]
            item_index = task["item_index"]
            album_info = task["album_info"]
            album_field_names = task["album_field_names"]
            track_field_names = task["track_field_names"]

            # Equality, not a subset: an item *added* since the preview is as
            # disqualifying as one that left. It has no snapshot row, so
            # `restore_fields` would `del` an excluded field off it (wiping a
            # title the preview never showed) and `diff_against` would read
            # every one of its fields as changed.
            if set(item_snapshot) != set(item_by_id):
                return jsonify({"error": "This album's tracks changed since the preview"}), 409

            # Drift check runs on the live, pre-mutation items/album — before
            # apply_metadata touches anything — against the field names the
            # preview itself found changed (Review corrections #22: the mint-
            # before-snapshot ordering already covers the known writers, this is
            # defence-in-depth against a future route that forgets to drop the
            # stash).
            drift_field = check_drift(album_snapshot, album, album_field_names)
            if drift_field is None:
                for item_id in item_index:
                    drift_field = check_drift(
                        item_snapshot.get(item_id, {}), item_by_id[item_id], track_field_names
                    )
                    if drift_field is not None:
                        break
            if drift_field is not None:
                return jsonify({"error": STALE_STASH_ERROR}), 409

            pairs = {item_by_id[iid]: album_info.tracks[idx] for iid, idx in item_index.items()}
            match = AlbumMatch(Distance(), album_info, pairs)
            match.apply_metadata(from_scratch=False)

            if excluded_fields:
                restore_fields(items, item_snapshot, excluded_fields)

            # Recomputed against the stashed (preview-time) snapshot, after the
            # restore: an excluded field is back at its old value by now, so it
            # is simply absent from this diff rather than needing a separate
            # exclusion filter here.
            album_fields, track_fields = diff_against(album_snapshot, item_snapshot, items)
            changed_item_ids = {
                change["item_id"] for field in track_fields for change in field["changes"]
            }

            for item in items:
                if item.id not in changed_item_ids:
                    continue
                # Not plugins.apply_item_changes(): it calls item.try_write() with
                # no path argument, which fails silently on the relative paths the
                # rescan preflight stores (the same bug genres.py's confirm/save
                # already routes around via _try_write_item), and it reports no
                # per-item success/failure for the partial-write response.
                if not _try_write_item(item):
                    log.warning("Failed to write mbsync tags for track %d: %s", item.track, item.id)
                    write_failures.append(item.id)
                item.store()

            if album_fields:
                for field_diff in album_fields:
                    album[field_diff["field"]] = field_diff["new"]
                # `inherit=False` matters: every `album_fields` entry is in
                # `Album.item_keys` by construction, and the default
                # `inherit=True` pushes each of them onto *every* item's DB row
                # — including the unmapped ones the UI promises are left as is,
                # whose files are never rewritten here. That would leave those
                # tracks' database rows disagreeing with their tags. The mapped
                # items already carry these values from `apply_metadata` and
                # were stored (and written) in the loop above.
                album.store(inherit=False)

            with state.identify_lock:
                if state.identify_tasks.get(key) is task:
                    state.identify_tasks.pop(key, None)

        log.info("MusicBrainz sync confirmed for album_id=%d", album_id)
        if write_failures:
            return jsonify({"status": "partial", "write_failures": write_failures})
        return jsonify({"status": "ok"})

    except Exception as e:
        log.exception("MusicBrainz sync confirm failed for album_id=%d", album_id)
        return jsonify({"error": str(e)}), 500
    finally:
        if lib:
            lib._close()
