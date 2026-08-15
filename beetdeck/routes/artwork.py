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

from flask import Blueprint, current_app, jsonify, request
from spectree import Response

from beetdeck.api import api
from beetdeck.artwork_store import sanitize_image_id, sanitize_mbid
from beetdeck.schemas.artwork import ArtworkListQuery, ArtworkListResponse
from beetdeck.schemas.common import ErrorResponse
from beetdeck.utils import _get_ro_conn, log

bp = Blueprint("artwork", __name__)

#: CAA has no per-image page, so the link is the release's MusicBrainz page.
MB_RELEASE_URL = "https://musicbrainz.org/release/"

#: The flex attr recording which CAA image was applied as the album cover.
#: Cleared by writing `""` (see `_save_cover_to_album`), so an empty value means
#: "no marker", not "id number zero".
COVER_IMAGE_ID_ATTR = "beetdeck_cover_image_id"

NO_MBID_REASON = "album has no MusicBrainz id"
BAD_MBID_REASON = "album's MusicBrainz id is not a release id"

#: `source` values, mirroring `ArtworkListResponse.source`.
REMOTE = "remote"


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
        int(key)
        for key in (urls if isinstance(urls, dict) else {})
        if str(key).isascii() and str(key).isdigit()
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
        stored = store.replace_metadata(mbid, result.images)
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
