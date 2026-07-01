"""Pydantic models for the ``lyrics`` blueprint's OpenAPI spec.

Covers the eight lyrics routes in ``routes/lyrics.py``:

Single-track:
- ``GET  /api/album/<id>/track/<id>/lyrics``          — read current lyrics
- ``POST /api/album/<id>/track/<id>/lyrics/fetch``    — fetch a preview (no write)
- ``POST /api/album/<id>/track/<id>/lyrics/confirm``  — write the fetched preview
- ``POST /api/album/<id>/track/<id>/lyrics/embed``    — embed the external .lrc file
- ``POST /api/album/<id>/track/<id>/lyrics/save``     — manually save edited lyrics

Bulk (album-wide):
- ``POST /api/album/<id>/lyrics/fetch``    — fetch previews for every track
- ``POST /api/album/<id>/lyrics/confirm``  — write previews for selected tracks
- ``POST /api/album/<id>/lyrics/embed``    — embed every external .lrc file

Request shapes (verified against the handlers):

- read / ``fetch`` (single & bulk) / ``confirm`` (single) / ``embed`` (single &
  bulk) take **no request body** — they act on the path params plus server-side
  ``state.identify_tasks``. No ``json=`` model is added (forcing one would 422 the
  SPA's body-less POSTs); these are documented response-only.
- ``save`` (single) takes ``{"lyrics": "..."}``. ``lyrics`` is kept **optional**
  (default ``""``) so saving an empty body clears the lyrics, matching the
  handler's ``data.get("lyrics", "")``; ``extra="forbid"`` makes unknown fields a
  ``422``.
- bulk ``confirm`` takes ``{"item_ids": [int, ...]}``. ``item_ids`` is kept
  **optional** (default ``[]``) so the handler's own "No tracks selected" ``400``
  stays authoritative for an empty selection, while a non-list / wrong-typed value
  raises pydantic → ``422``.

Response validation is active (spectree's default), but with ``force_resp_serialize``
left ``False`` the handlers' raw ``jsonify(...)`` bytes pass through unchanged; the
models' all-optional fields keep real payloads conformant. ``resp=`` also feeds the
spec.
"""

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class TrackLyricsSaveBody(BaseModel):
    """JSON body for ``POST /api/album/<id>/track/<id>/lyrics/save``.

    Optional ``lyrics`` text; an empty/missing value clears the track's lyrics
    (the handler treats ``""`` as a valid save). ``extra="forbid"`` makes unknown
    fields a ``422``.
    """

    model_config = ConfigDict(extra="forbid")

    lyrics: str = Field(
        default="",
        description="Lyrics text to write. Empty/missing clears the track lyrics.",
    )


class AlbumLyricsConfirmBody(BaseModel):
    """JSON body for ``POST /api/album/<id>/lyrics/confirm``.

    The ids of the tracks whose previously-fetched lyrics should be written. Kept
    optional so the handler's own "No tracks selected" ``400`` stays authoritative
    for an empty selection; ``extra="forbid"`` makes unknown fields a ``422``.
    """

    model_config = ConfigDict(extra="forbid")

    item_ids: list[int] = Field(
        default_factory=list,
        description="Item ids of tracks to write fetched lyrics for.",
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TrackLyricsReadResponse(BaseModel):
    """``GET /api/album/<id>/track/<id>/lyrics`` — current lyrics for a track."""

    has_lyrics: bool = Field(description="Whether any lyrics are available.")
    lyrics: str = Field(description="The lyrics text ('' when none).")
    source: str | None = Field(
        default=None,
        description="Where the lyrics came from: 'embedded', 'lrc_file', or null.",
    )
    lrc_path: str | None = Field(
        default=None,
        description="Path to the external .lrc file (present only for lrc_file source).",
    )
    has_lrc: bool = Field(
        default=False,
        description="Whether a sidecar .lrc file currently exists (file-existence flag, "
        "independent of has_lyrics). Lets clients track sidecar state after writes.",
    )


class TrackLyricsFetchResponse(BaseModel):
    """``POST /api/album/<id>/track/<id>/lyrics/fetch`` — preview of online lyrics.

    When nothing is found the body is ``{"status": "ok", "found": false}``; on a
    hit it additionally carries the fetched lyrics and the current state.
    """

    status: str = Field(description="Always 'ok' on success.")
    found: bool = Field(description="Whether lyrics were found online.")
    new_lyrics: str | None = Field(
        default=None, description="Fetched lyrics text (present only when found)."
    )
    new_synced: bool | None = Field(
        default=None, description="Whether the fetched lyrics are time-synced."
    )
    new_backend: str | None = Field(
        default=None, description="Backend the lyrics were fetched from."
    )
    current_lyrics: str | None = Field(
        default=None, description="The track's current lyrics, for comparison."
    )
    current_source: str | None = Field(
        default=None, description="Source of the current lyrics ('embedded'/'lrc_file'/null)."
    )


class LyricsStatusResponse(BaseModel):
    """``{"status": "ok"}`` returned by single-track embed."""

    status: str = Field(description="Always 'ok' on success.")


class TrackLyricsSaveResponse(BaseModel):
    """``POST /api/album/<id>/track/<id>/lyrics/save`` — manual save result.

    Carries the authoritative post-save presence so a client whose refresh GET
    fails still records the truth: ``has_lyrics`` reflects embedded text OR a
    readable non-empty sidecar (an empty save preserves the .lrc, which only
    keeps the track "has lyrics" when it is readable), and ``has_lrc`` is the
    sidecar file-existence flag — distinct from ``has_lyrics``.
    """

    status: str = Field(description="Always 'ok' on success.")
    has_lyrics: bool = Field(
        default=False,
        description="Whether the track has lyrics after the save (embedded text "
        "OR a readable non-empty sidecar .lrc).",
    )
    has_lrc: bool = Field(
        default=False,
        description="Whether a sidecar .lrc file still exists after the save "
        "(file-existence flag, independent of has_lyrics).",
    )


class TrackLyricsConfirmResponse(BaseModel):
    """``POST /api/album/<id>/track/<id>/lyrics/confirm`` — written preview result.

    Carries the real post-write sidecar state so a client whose authoritative
    refresh fails still records the truth: ``has_lrc`` is ``True`` only when the
    .lrc removal was attempted but failed (the sidecar lingers on disk).
    """

    status: str = Field(description="Always 'ok' on success.")
    has_lrc: bool = Field(
        default=False,
        description="Whether a sidecar .lrc file still exists after the write "
        "(True only when its removal failed).",
    )


class EmbeddedTrack(BaseModel):
    """An item whose .lrc file was embedded by the bulk-embed route."""

    id: int = Field(description="Item id.")
    title: str | None = Field(default=None, description="Track title.")


class AlbumLyricsEmbedResponse(BaseModel):
    """``POST /api/album/<id>/lyrics/embed`` — result of bulk .lrc embedding."""

    status: str = Field(description="Always 'ok' on success.")
    embedded: list[EmbeddedTrack] = Field(
        default_factory=list, description="Tracks whose .lrc file was embedded."
    )


class AlbumLyricsFetchTrack(BaseModel):
    """A per-track entry in the bulk fetch preview."""

    item_id: int = Field(description="Item id.")
    track: int | None = Field(default=None, description="Track number.")
    disc: int | None = Field(default=None, description="Disc number.")
    title: str | None = Field(default=None, description="Track title.")
    artist: str | None = Field(default=None, description="Track artist.")
    current_lyrics: str = Field(description="The track's current lyrics.")
    current_source: str | None = Field(
        default=None, description="Source of current lyrics ('embedded'/'lrc_file'/null)."
    )
    found: bool = Field(description="Whether lyrics were found online for this track.")
    new_lyrics: str = Field(description="Fetched lyrics text ('' when not found).")
    new_synced: bool = Field(description="Whether the fetched lyrics are time-synced.")
    new_backend: str = Field(description="Backend the lyrics were fetched from ('' when none).")


class AlbumLyricsFetchResponse(BaseModel):
    """``POST /api/album/<id>/lyrics/fetch`` — per-track preview for the album."""

    status: str = Field(description="Always 'ok' on success.")
    tracks: list[AlbumLyricsFetchTrack] = Field(
        default_factory=list, description="Per-track fetch results."
    )


class AlbumLyricsConfirmResponse(BaseModel):
    """``POST /api/album/<id>/lyrics/confirm`` — result of writing selected tracks."""

    status: str = Field(description="Always 'ok' on success.")
    written: int = Field(description="Number of tracks successfully written.")
    written_item_ids: list[int] = Field(
        default_factory=list, description="Item ids that were successfully written."
    )
    retained_lrc_item_ids: list[int] = Field(
        default_factory=list,
        description="Written item ids whose sidecar .lrc removal failed, so the "
        "file still exists on disk (subset of written_item_ids).",
    )
    failed: list[int] = Field(
        default_factory=list, description="Item ids whose audio-file write failed."
    )
