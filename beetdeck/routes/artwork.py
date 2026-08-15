"""Cover Art Archive gallery routes blueprint.

Every image in this feature travels through this proxy — the browser never
learns an upstream `coverartarchive.org` / `archive.org` URL. That is what makes
`ARTWORK_STORAGE_DIR` meaningful, keeps one contract for the SPA, and keeps an
external origin out of the page.

The listing route reads through `storage → cache → CAA`: a fresh index in either
root answers without a request, and only a miss (or `?refresh=1`) goes upstream.
What comes back is persisted through `ArtworkStore.replace_metadata`, never
through a blind index overwrite — a refresh racing a tile download must not
discard the dimensions that download just measured.

The binary route reads through the same three layers and mirrors **lazily**:
nothing is pre-downloaded, and a rendition is written to disk the first time
somebody asks the proxy for that exact `(image_id, size)`. The gallery's own
traffic is the mirroring trigger.

Three outcomes stay distinct, per *Absence is not a fact*:

* `available: false` — no gallery is possible at all (the album has no release
  id). Not a 404: the album exists, the gallery does not.
* `available: true` with an empty `images` — CAA answered `404`, which is its
  normal way of saying this release has no art. A fact about the release.
* `502` — CAA could not be reached, so nothing about the release was
  established. Collapsing this into an empty list would state something nobody
  checked.
"""

import sqlite3

from flask import Blueprint, current_app, jsonify, request, send_file
from spectree import Response

from beetdeck import state
from beetdeck.api import api
from beetdeck.artwork_store import IMAGE_SIZES, sanitize_image_id, sanitize_mbid, sanitize_size
from beetdeck.schemas.artwork import ArtworkListQuery, ArtworkListResponse
from beetdeck.schemas.common import ErrorResponse
from beetdeck.schemas.cover import CoverStatusResponse
from beetdeck.utils import (
    COVER_IMAGE_ID_ATTR,
    _drop_cover_stash,
    _get_ro_conn,
    _init_beets,
    _save_cover_to_album,
    log,
)

bp = Blueprint("artwork", __name__)

#: CAA has no per-image page, so the link is the release's MusicBrainz page.
MB_RELEASE_URL = "https://musicbrainz.org/release/"

NO_MBID_REASON = "album has no MusicBrainz id"
BAD_MBID_REASON = "album's MusicBrainz id is not a release id"

#: `source` values, mirroring `ArtworkListResponse.source`.
REMOTE = "remote"

#: What a binary request with no `?size=` means. The grid always states its size,
#: but a bare URL has to resolve to something, and the smallest rendition is the
#: one that costs the least to be wrong about.
DEFAULT_IMAGE_SIZE = "250"

#: The rendition a cover is made of. Applying a thumbnail would install a 250 px
#: image as the album's cover art, which no caller ever means.
FULL_IMAGE_SIZE = "full"

#: The renditions `thumb_sizes` may advertise — `IMAGE_SIZES` minus the original.
#: Derived from the set `sanitize_size` enforces, so the menu the grid picks from
#: and the answers the binary route accepts cannot drift apart: a rendition CAA
#: starts publishing under a key this build does not know would otherwise be
#: advertised here and then `400`ed on request, the API contradicting itself
#: rather than a tile simply not offering a size.
THUMBNAIL_SIZES = tuple(size for size in IMAGE_SIZES if size != FULL_IMAGE_SIZE)

#: How long a browser may keep a proxied image. The bytes behind one
#: `(image_id, size)` never change — CAA gives a new id to a new scan.
IMAGE_CACHE_CONTROL = "public, max-age=86400"

#: What a caller is told when the disk layer itself failed. `ArtworkStore` writes
#: and re-raises rather than swallowing, and neither root is guaranteed writable
#: for the life of the process — a permanent mirror fills up, and a remounted
#: volume goes read-only under a running app. Uncaught, that surfaces as Flask's
#: HTML 500 on a blueprint whose every other answer is `{"error": …}`, so the
#: SPA's `r.json()` yields null and the gallery hangs on its loading state with
#: nothing to retry. The operator's detail goes to the log, not to the client.
STORAGE_FAILED = "the artwork store could not be written"


def _album_row(album_id):
    """`(mb_albumid, current_image_id)` for the album, or `None` when it is gone.

    Both come out of the read-only connection in one statement: the marker is an
    album flex attr, which lives in its own table.
    """
    conn = _get_ro_conn()
    try:
        row = conn.execute(
            """
            SELECT a.mb_albumid,
                   (SELECT value FROM album_attributes
                    WHERE entity_id = a.id AND key = ?) AS cover_image_id
            FROM albums a WHERE a.id = ?
            """,
            (COVER_IMAGE_ID_ATTR, album_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return (row["mb_albumid"] or "").strip(), _marker(row["cover_image_id"])


def _marker(value):
    """The stored marker as a decimal string, or `None`.

    Absent, empty and unparseable all collapse to `None` — the field claims "this
    tile is the current cover", and a value no `image_id` can equal would make
    that claim about nothing.
    """
    try:
        return sanitize_image_id(value)
    except ValueError:
        return None


def _payload(
    store,
    *,
    mb_albumid="",
    available=True,
    reason=None,
    source=None,
    fetched_at=None,
    current_image_id=None,
    images=None,
):
    """One `ArtworkListResponse` body, with the storage fields filled from the store."""
    return {
        "mb_albumid": mb_albumid,
        "available": available,
        "reason": reason,
        "storage_enabled": store.storage_enabled,
        "storage_error": store.storage_error,
        "source": source,
        "fetched_at": fetched_at,
        "current_image_id": current_image_id,
        "images": images or [],
    }


def _project(record, dims, mb_url):
    """One internal record projected onto the public `ArtworkImage` shape.

    Drops `urls` — handing the browser an upstream URL defeats the proxy this
    whole feature is built on — and derives `thumb_sizes` from its numeric keys.
    `width`/`height` come from `dims[image_id]["full"]` **and nothing else**: a
    thumbnail's measurement is a fact about the thumbnail, not about the scan.
    """
    try:
        image_id = sanitize_image_id(record.get("image_id"))
    except ValueError:
        return None

    urls = record.get("urls")
    thumb_sizes = sorted(
        int(key) for key in (urls if isinstance(urls, dict) else {}) if key in THUMBNAIL_SIZES
    )
    types = record.get("types")
    comment = record.get("comment")
    measured = (dims.get(image_id) or {}).get("full")
    return {
        "image_id": image_id,
        "types": [t for t in types if isinstance(t, str)] if isinstance(types, list) else [],
        "front": bool(record.get("front")),
        "back": bool(record.get("back")),
        "approved": bool(record.get("approved")),
        "comment": comment if isinstance(comment, str) else "",
        "thumb_sizes": thumb_sizes,
        "mb_url": mb_url,
        "width": measured[0] if measured else None,
        "height": measured[1] if measured else None,
    }


@bp.route("/api/album/<int:album_id>/artwork")
@api.validate(
    query=ArtworkListQuery,
    resp=Response(
        HTTP_200=ArtworkListResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
        HTTP_502=ErrorResponse,
    ),
    tags=["artwork"],
)
def album_artwork(album_id):
    """Every scan the Cover Art Archive holds for this album's release."""
    store = current_app.extensions["beetdeck_artwork_store"]

    try:
        row = _album_row(album_id)
    except sqlite3.OperationalError as exc:
        return jsonify({"error": str(exc)}), 500
    if row is None:
        return jsonify({"error": "Album not found"}), 404
    mb_albumid, current_image_id = row

    if not mb_albumid:
        return jsonify(
            _payload(
                store,
                available=False,
                reason=NO_MBID_REASON,
                current_image_id=current_image_id,
            )
        )
    try:
        mbid = sanitize_mbid(mb_albumid)
    except ValueError:
        # A row whose `mb_albumid` is not a release id cannot address CAA at all.
        # Reported as "no gallery is possible", with its own reason, rather than
        # as a 500 — the album page still works and says what is wrong.
        log.warning("album %s has an unusable mb_albumid %r", album_id, mb_albumid)
        return jsonify(
            _payload(
                store,
                mb_albumid=mb_albumid,
                available=False,
                reason=BAD_MBID_REASON,
                current_image_id=current_image_id,
            )
        )

    index = None if request.context.query.refresh else store.read_index(mbid)
    if index is not None and index.fresh:
        source, fetched_at = index.source, index.fetched_at
        records, dims = index.images, index.dims
    else:
        result = current_app.extensions["beetdeck_caa"].fetch_release(mbid)
        if not result.ok:
            return jsonify({"error": result.message}), 502
        # `not_found` is persisted like any other listing: CAA answering "no art
        # for this release" is a fact with a shelf life, and re-asking it on
        # every page load would spend the courtesy budget on a known answer.
        try:
            stored = store.replace_metadata(mbid, result.images)
        except OSError as exc:
            log.error("artwork index for %s could not be written: %s", mbid, exc)
            return jsonify({"error": STORAGE_FAILED}), 500
        source, fetched_at = REMOTE, None
        records, dims = stored.images, stored.dims

    images = [_project(record, dims, f"{MB_RELEASE_URL}{mbid}") for record in records]
    return jsonify(
        _payload(
            store,
            mb_albumid=mbid,
            source=source,
            fetched_at=fetched_at,
            current_image_id=current_image_id,
            images=[image for image in images if image is not None],
        )
    )


def _resolve_cold(store, mbid, image_id, size):
    """Fetch one image CAA has and this disk does not; `(stored, error, status)`.

    The URL comes from the internal record's `urls` map — the only reason the
    index persists it, and the only way to reach a cold image without guessing
    the original's extension by convention.

    A **stale** index is used as it stands: `urls` does not expire, and the
    listing route is what refreshes metadata. A **missing** one is established
    first, so "this image is not in the release" is something that was checked.
    An unknown id against an index that does exist is *not* a reason to go
    upstream — that would spend the courtesy budget once per bad request.
    """
    caa = current_app.extensions["beetdeck_caa"]
    index = store.read_index(mbid)
    if index is None:
        result = caa.fetch_release(mbid)
        if not result.ok:
            return None, result.message, 502
        index = store.replace_metadata(mbid, result.images)

    record = next(
        (entry for entry in index.images if _marker(entry.get("image_id")) == image_id),
        None,
    )
    if record is None:
        return None, "Image not found", 404

    urls = record.get("urls")
    url = urls.get(size) if isinstance(urls, dict) else None
    if not url:
        # The size is a valid one, this image just does not have it — CAA stopped
        # generating `1200` for older uploads. A 400 says "ask for another size",
        # which a 404 ("no such image") would misstate.
        return None, f"this image has no {size} rendition", 400

    fetched = caa.fetch_image(url)
    if not fetched.ok:
        return None, fetched.message, 502
    stored = store.store_image(mbid, image_id, size, fetched.data, fetched.content_type)
    if stored is None:
        # Answered, but with something that is not an image — an error page under
        # a `200`. Same class as an unreachable CAA (nothing about this rendition
        # was established), and deliberately *not* mirrored: in storage mode a
        # stored non-image would be permanent and would short-circuit every
        # later request. A `502` leaves the next one free to try again.
        log.warning(
            "CAA answered for %s/%s (%s) with something that is not an image", mbid, image_id, size
        )
        return None, "the Cover Art Archive did not answer with an image", 502
    return stored, None, None


def _resolve_image(store, mbid, image_id, size):
    """One rendition on disk, fetching it first when it is not; `(stored, error, status)`.

    The disk layers answer first — that is the mirror — and only a genuine miss
    reaches `_resolve_cold`. Shared by the binary route and the apply route so
    the two cannot drift on what "this image is not available" means.
    """
    stored = store.find_image(mbid, image_id, size)
    if stored is not None:
        return stored, None, None
    return _resolve_cold(store, mbid, image_id, size)


# NOTE: **left undecorated**, for the reason `cover.py:59-64` spells out — this
# route streams bytes through ``send_file``, which puts the response into
# direct-passthrough mode, and spectree's response post-processing calls
# ``response.get_data()`` unconditionally on every decorated view, which raises
# ``RuntimeError`` on such a response. So it does not appear in the OpenAPI spec.
@bp.route("/api/album/<int:album_id>/artwork/<image_id>")
def album_artwork_image(album_id, image_id):
    """One scan's bytes, served from `storage → cache → CAA`.

    Nothing is pre-downloaded: a rendition lands on disk the first time somebody
    asks the proxy for *that* `(image_id, size)`. The gallery's own traffic is
    what mirrors the release, which is why a request for `full` never pulls the
    thumbnails and a thumbnail never pulls the original.
    """
    try:
        image_id = sanitize_image_id(image_id)
    except ValueError:
        return jsonify({"error": "image_id must be a Cover Art Archive image id"}), 400
    try:
        size = sanitize_size(request.args.get("size", DEFAULT_IMAGE_SIZE))
    except ValueError:
        return jsonify({"error": f"size must be one of {list(IMAGE_SIZES)}"}), 400

    store = current_app.extensions["beetdeck_artwork_store"]
    try:
        row = _album_row(album_id)
    except sqlite3.OperationalError as exc:
        return jsonify({"error": str(exc)}), 500
    if row is None:
        return jsonify({"error": "Album not found"}), 404

    try:
        mbid = sanitize_mbid(row[0])
    except ValueError:
        # No usable release id means no gallery, so there is no image here to
        # serve. The listing route is where that is explained in words.
        return jsonify({"error": "Image not found"}), 404

    # `send_file` is inside the same guard as the resolve: `find_image` hands
    # back a path, and the inline cache sweep runs on whichever thread crossed
    # the byte threshold — so the file can be evicted between the two, and
    # `send_file` opens it eagerly.
    try:
        stored, error, status = _resolve_image(store, mbid, image_id, size)
        if stored is None:
            return jsonify({"error": error}), status

        response = (
            send_file(stored.path, mimetype=stored.content_type)
            if stored.content_type
            else send_file(stored.path)
        )
    except OSError as exc:
        log.error("artwork image %s/%s (%s) could not be served: %s", mbid, image_id, size, exc)
        return jsonify({"error": STORAGE_FAILED}), 500

    response.headers["Cache-Control"] = IMAGE_CACHE_CONTROL
    return response


@bp.route("/api/album/<int:album_id>/artwork/<image_id>/apply", methods=["POST"])
@api.validate(
    resp=Response(
        HTTP_200=CoverStatusResponse,
        HTTP_400=ErrorResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
        HTTP_502=ErrorResponse,
    ),
    tags=["artwork"],
)
def apply_artwork_image(album_id, image_id):
    """Make one Cover Art Archive scan the album cover.

    The original is resolved (and, on a miss, fetched and mirrored) **outside**
    the album write lock: it is the slow part and it mutates nothing the lock
    protects. Everything that does mutate happens inside it, held across check
    and write for the same reason `confirm_cover` holds it — a check that passes
    in microseconds guards a write that takes seconds.

    There is no stash of this route's own, so no `expected_generation` and no
    `409`; what it *must* do is invalidate the cover stash, because a token
    minted before this write would otherwise still pass its precondition and
    revert it.
    """
    try:
        image_id = sanitize_image_id(image_id)
    except ValueError:
        return jsonify({"error": "image_id must be a Cover Art Archive image id"}), 400

    store = current_app.extensions["beetdeck_artwork_store"]
    try:
        row = _album_row(album_id)
    except sqlite3.OperationalError as exc:
        return jsonify({"error": str(exc)}), 500
    if row is None:
        return jsonify({"error": "Album not found"}), 404

    try:
        mbid = sanitize_mbid(row[0])
    except ValueError:
        return jsonify({"error": "Image not found"}), 404

    try:
        stored, error, status = _resolve_image(store, mbid, image_id, FULL_IMAGE_SIZE)
    except OSError as exc:
        log.error("artwork original %s/%s could not be stored: %s", mbid, image_id, exc)
        return jsonify({"error": STORAGE_FAILED}), 500
    if stored is None:
        return jsonify({"error": error}), status

    lib = None
    try:
        library_db = current_app.config["LIBRARY_DB"]
        library_dir = current_app.config.get("LIBRARY_ROOT") or None
        with state.album_write_lock(album_id):
            lib = _init_beets(library_db, library_dir)
            album = lib.get_album(album_id)
            if not album:
                return jsonify({"error": "Album not found"}), 404

            log.info(
                "Applying CAA image %s as cover for album_id=%d from %s",
                image_id,
                album_id,
                stored.path,
            )
            # Dropped **before** the first persistent mutation, not after it.
            # `_save_cover_to_album` persists `artpath` and only then embeds and
            # sends `art_set`; either can raise, and a failure there would answer
            # 500 with the cover already changed while leaving the old stash
            # generation valid — so a stale confirm would still pass its
            # precondition and overwrite the half-applied change. Invalidating a
            # preview that turns out not to have been replaced is the cheap
            # direction to be wrong in.
            _drop_cover_stash(album_id)

            _save_cover_to_album(album, stored.path)
            # The helper cleared the marker and has already run its own
            # `store()`, so re-setting it needs one of its own. The value is the
            # same decimal string the listing route compares against.
            album[COVER_IMAGE_ID_ATTR] = image_id
            album.store()

        log.info("CAA image %s applied for album_id=%d", image_id, album_id)
        return jsonify({"status": "ok"})

    except Exception as exc:
        log.exception("Artwork apply failed for album_id=%d", album_id)
        return jsonify({"error": str(exc)}), 500
    finally:
        if lib:
            lib._close()
