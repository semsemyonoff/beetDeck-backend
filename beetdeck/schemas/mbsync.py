"""Pydantic models for the ``mbsync`` blueprint's OpenAPI spec.

Covers ``routes/mbsync.py`` (Tasks 3-4):

- ``POST /api/album/<id>/mbsync``          — preview a MusicBrainz re-sync (no write)
- ``POST /api/album/<id>/mbsync/confirm``  — apply the previewed diff

Response validation is active (spectree's default), but with ``force_resp_serialize``
left ``False`` the handlers' raw ``jsonify(...)`` bytes pass through unchanged; the
models' all-optional fields keep real payloads conformant. ``resp=`` also feeds the
spec.
"""

from pydantic import BaseModel, ConfigDict, Field


class MbsyncTrackChange(BaseModel):
    """A single item's old/new value for one changed field."""

    item_id: int = Field(description="Item (track) id.")
    track: int | None = Field(default=None, description="Track number on the medium.")
    old: object | None = Field(default=None, description="Value before the sync.")
    new: object | None = Field(default=None, description="Value the release proposes.")


class MbsyncTrackFieldDiff(BaseModel):
    """One field name that changed on at least one track, with every affected item."""

    field: str = Field(description="Item field name, e.g. 'title'.")
    changes: list[MbsyncTrackChange] = Field(
        default_factory=list, description="Per-item old/new pairs for this field."
    )


class MbsyncAlbumFieldDiff(BaseModel):
    """A single album-row field the sync would change."""

    field: str = Field(description="Album field name, e.g. 'label'.")
    old: object | None = Field(default=None, description="Value before the sync.")
    new: object | None = Field(default=None, description="Value the release proposes.")


class MbsyncUnmapped(BaseModel):
    """A local item the release track mapping could not place."""

    item_id: int = Field(description="Item (track) id.")
    track: int | None = Field(default=None, description="Track number on the medium.")
    title: str | None = Field(default=None, description="Track title, for display.")


class MbsyncPreviewResponse(BaseModel):
    """``POST /api/album/<id>/mbsync`` success — the diff against the stored release.

    A preview never writes: ``store()`` is never called on the path that produces
    this response. ``stash_generation`` is the token the confirm must echo back
    (as ``expected_generation``) for its own freshness check.
    """

    album_id: int = Field(description="Album id the preview was built for.")
    mb_albumid: str = Field(description="The release id the album is (still) pinned to.")
    data_source: str = Field(description="Resolved metadata source, e.g. 'MusicBrainz'.")
    stash_generation: int = Field(
        description="Token to echo back as 'expected_generation' on confirm."
    )
    album_fields: list[MbsyncAlbumFieldDiff] = Field(
        default_factory=list, description="Album-row fields that changed."
    )
    track_fields: list[MbsyncTrackFieldDiff] = Field(
        default_factory=list, description="Item fields that changed, grouped by field name."
    )
    unmapped: list[MbsyncUnmapped] = Field(
        default_factory=list,
        description="Local items the release track mapping could not place; left as is.",
    )
    changed: bool = Field(description="Whether the release proposes any change at all.")


class MbsyncConfirmBody(BaseModel):
    """JSON body for ``POST /api/album/<id>/mbsync/confirm``.

    Both fields are optional: omitting ``expected_generation`` keeps the
    pre-existing behaviour of the other confirms (the SPA will send it).
    ``extra="forbid"`` makes an unknown field a 422.
    """

    model_config = ConfigDict(extra="forbid")

    expected_generation: int | None = Field(
        default=None,
        description="Stash generation from the preview; a mismatch or missing stash is a 409.",
    )
    excluded_fields: list[str] | None = Field(
        default=None,
        description="Field names to leave untouched, restored from the preview's snapshot.",
    )


class MbsyncConfirmResponse(BaseModel):
    """Result of ``POST /api/album/<id>/mbsync/confirm``.

    ``status`` is ``ok`` when every changed track was written, or ``partial``
    when some audio files failed to write (their item ids are in
    ``write_failures``) — the same shape ``genres.py``'s confirm/save uses.
    """

    status: str = Field(description="``ok`` or ``partial``.")
    write_failures: list[int] | None = Field(
        default=None,
        description="Item ids whose audio-file write failed (present when status is 'partial').",
    )
