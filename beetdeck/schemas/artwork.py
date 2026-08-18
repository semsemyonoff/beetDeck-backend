"""Pydantic models for the ``artwork`` blueprint's OpenAPI spec.

Covers the Cover Art Archive gallery routes:

- ``GET  /api/album/<id>/artwork``                    — the release's image list
- ``GET  /api/album/<id>/artwork/<image_id>``         — bytes (undecorated,
  ``send_file``, so it has no success model here — same reason as the two binary
  routes in ``schemas/cover.py``)
- ``POST /api/album/<id>/artwork/<image_id>/apply``   — make it the album cover,
  answering the shared ``CoverStatusResponse``

Two contracts this module exists to pin:

- ``ArtworkImage`` is the **public projection** of the internal record the store
  keeps in ``index.json``. The internal record carries a ``urls`` map of upstream
  Cover Art Archive / archive.org URLs; every byte in this feature goes through
  the backend proxy, so handing one of those to the browser would defeat the
  point. ``extra="forbid"`` turns "the route forgot to project" into a loud
  ``ValidationError`` instead of a leaked URL.
- ``image_id`` and ``current_image_id`` are **decimal strings**. CAA sends the id
  as a JSON number and beets flex attrs are TEXT, so an implementation that
  passes each through untouched returns a number in one field and a string in the
  other, the SPA's ``current_image_id === image_id`` is false after a reload, and
  the *Current cover* marker silently vanishes. Same class of bug as the several
  truthy spellings of ``lyrics_instrumental``.

Response validation is active (spectree's default), but with
``force_resp_serialize`` left ``False`` the handlers' raw ``jsonify(...)`` bytes
pass through unchanged; ``resp=`` also feeds the spec.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ArtworkListQuery(BaseModel):
    """Query params for ``GET /api/album/<id>/artwork``.

    ``refresh=1`` bypasses ``ARTWORK_META_TTL`` and re-asks the Cover Art Archive
    even when the on-disk index is still fresh. Anything that is not a boolean is
    a 422, like every other declared query param in this app.

    Extra query keys are tolerated (no ``extra="forbid"``, matching
    ``GenrePreviewQuery``): this is a plain listing GET the SPA may cache-bust,
    and rejecting the gallery over a stray parameter would be a worse failure
    than ignoring it.
    """

    refresh: bool = Field(
        default=False,
        description=(
            "Bypass the metadata TTL and re-fetch the listing from the Cover Art "
            "Archive. Absent/false serves the on-disk index while it is fresh."
        ),
    )


class ArtworkImage(BaseModel):
    """One Cover Art Archive scan, as the browser sees it.

    The public projection of the store's internal record: the ``urls`` map of
    upstream URLs is deliberately **absent**, and ``extra="forbid"`` makes an
    attempt to pass it through a validation error rather than a silent leak.
    """

    model_config = ConfigDict(extra="forbid")

    image_id: str = Field(
        description=(
            "CAA image id as a **decimal string**. CAA sends a number and the "
            "``beetdeck_cover_image_id`` flex attr comes back as TEXT; pinning one "
            "representation is what keeps ``current_image_id === image_id`` true in "
            "the browser after a reload."
        )
    )
    types: list[str] = Field(
        default_factory=list,
        description=(
            "MusicBrainz cover art types for this scan (``Front``, ``Back``, "
            "``Booklet``, ``Medium``, …). Empty when the image is untyped."
        ),
    )
    front: bool = Field(description="CAA's own 'this is the front cover' flag.")
    back: bool = Field(description="CAA's own 'this is the back cover' flag.")
    approved: bool = Field(description="Whether the image was approved by a MusicBrainz editor.")
    comment: str = Field(
        default="", description="Editor comment on the scan; empty string when there is none."
    )
    thumb_sizes: list[int] = Field(
        default_factory=list,
        description=(
            "Thumbnail widths available for this image, **ascending ints** and "
            "always a subset of 250/500/1200. CAA's ``small`` / ``large`` keys are "
            "translated to 250 / 500 upstream of this field; any other key CAA "
            "publishes is filtered out — the binary route rejects a size outside "
            "this set with a 400, so advertising one would make the API contradict "
            "itself."
        ),
    )
    mb_url: str = Field(
        description=(
            "MusicBrainz release page for the album. Derived, not stored: CAA has no "
            "per-image page."
        )
    )
    width: int | None = Field(
        default=None,
        description=(
            "Pixel width of the **original** scan, or null when it has not been "
            "measured yet. CAA does not report dimensions, so this is filled only "
            "after the full-size file has actually been fetched and stored "
            "(fullscreen or apply). A thumbnail's measurement is never reported here."
        ),
    )
    height: int | None = Field(
        default=None, description="Pixel height of the original scan (see width)."
    )


class ArtworkListResponse(BaseModel):
    """``GET /api/album/<id>/artwork`` — every scan CAA holds for the release.

    ``available: false`` means no gallery is possible at all (today: the album has
    no MusicBrainz id) and ``reason`` says why in one phrase. It is **not** the
    same as an empty ``images`` list with ``available: true``, which is the
    factual "CAA has nothing for this release" — and neither is a transport
    failure, which is a 502 and never a 200 with an empty list.
    """

    mb_albumid: str = Field(
        description="Release MBID the listing is for; empty string when the album has none."
    )
    available: bool = Field(
        description=(
            "False when no gallery is possible at all — see ``reason``. True with an "
            "empty ``images`` means CAA simply holds no art for this release."
        )
    )
    reason: str | None = Field(
        default=None,
        description=(
            "Why no gallery is possible, e.g. 'album has no MusicBrainz id'. Null "
            "whenever ``available`` is true, including on an empty listing."
        ),
    )
    storage_enabled: bool = Field(
        description=(
            "Whether ``ARTWORK_STORAGE_DIR`` is configured — i.e. whether what the "
            "gallery downloads is mirrored permanently rather than into the "
            "TTL-bounded cache. Configured is not the same as working: see "
            "``storage_error``."
        )
    )
    storage_error: str | None = Field(
        default=None,
        description=(
            "Non-null when storage is configured but unusable; writes fall back to "
            "the cache and this says why, rather than failing the gallery over a "
            "one-line misconfiguration."
        ),
    )
    source: Literal["storage", "cache", "remote"] | None = Field(
        default=None,
        description=(
            "Where this listing came from: an index in the storage root, one in the "
            "cache root, or a fresh Cover Art Archive request. Null when no listing "
            "was produced (``available: false``)."
        ),
    )
    fetched_at: str | None = Field(
        default=None,
        description=(
            "ISO-8601 UTC timestamp of the stored listing; null when it was fetched "
            "remotely just now, or when there is no listing."
        ),
    )
    current_image_id: str | None = Field(
        default=None,
        description=(
            "CAA id previously applied as this album's cover (the "
            "``beetdeck_cover_image_id`` flex attr), or null when no CAA image was "
            "applied. A **decimal string**, for the same reason as "
            "``ArtworkImage.image_id``."
        ),
    )
    images: list[ArtworkImage] = Field(
        default_factory=list,
        description="The release's scans; empty when CAA has none or none is possible.",
    )
