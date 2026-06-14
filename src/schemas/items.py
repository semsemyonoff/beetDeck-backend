"""Pydantic models for the ``items`` blueprint's OpenAPI spec.

Covers the seven routes in ``routes/items.py`` (untagged-item management):

- ``GET  /api/items/untagged``                       — list loose/untagged items
- ``POST /api/items/<id>/metadata``                  — edit a single item
- ``POST /api/items/metadata-batch``                 — album/per-track batch write
- ``POST /api/items/identify``                       — start a background autotag task
- ``GET  /api/items/identify/<task_id>/status``      — poll the background task
- ``POST /api/items/identify/<task_id>/apply``       — preview a candidate diff
- ``POST /api/items/identify/<task_id>/confirm``     — create album + write tags

Validation-layer decision for ``metadata-batch`` (the plan's Task 9 open
decision — documented here, applied all-or-nothing):

- **Structural / strict-type rules move to pydantic → 422.** Body must be an
  object; ``album`` an object; ``items`` a list of objects; each item ``id`` a
  strict ``int`` (JSON booleans rejected); the string scalars
  (``album.album``/``albumartist``/``genre`` and item ``title``/``artist``)
  must be strings. These were the handler's hand-rolled ``400`` type checks;
  they are now enforced uniformly by pydantic as ``422``.
- **Lenient numeric handling stays in the handler.** ``year``/``track``/``disc``
  are intentionally NOT typed strictly: the handler coerces them with ``int()``
  and emits a *warning* (not an error) on a bad value, returning ``200`` with a
  populated ``warnings`` list. Typing them in pydantic would turn those warnings
  into ``422`` and break the documented partial-success contract, so they are
  modelled as ``int | float | str | None`` (any JSON scalar passes — including a
  fractional float like ``1.5``, which the handler's ``int()`` coerces; the
  handler decides).
- **Semantic / cross-field rules stay in the handler → 400.** "at least one
  album field or item override is required" and "≥1 item id required when album
  fields are present" remain the handler's authoritative ``400`` checks. They
  belong with the partial-write/warning logic they share state with.

This keeps a single, predictable contract per endpoint: a wrong *type* on a
strict field → ``422``; a bad *value*/missing-requirement → ``400`` (or a
``warning`` for the lenient numeric fields). Response validation is active
(spectree's default), but with ``force_resp_serialize`` left ``False`` the raw
``jsonify(...)`` bytes pass through unchanged; ``resp=`` also feeds the spec.

``Candidate`` (autotag candidates) and ``ApplyResponse`` (diff preview) are
imported from ``schemas.identify``: items identification uses the exact same
``_serialize_candidate`` helper and emits the same old/new diff shape, so the
models are shared rather than duplicated.
"""

from pydantic import BaseModel, ConfigDict, Field, RootModel

from src.schemas.identify import ApplyResponse, Candidate

# Re-exported so ``routes/items.py`` can import the shared diff/candidate models
# from a single place alongside the items-specific ones.
__all__ = [
    "ApplyResponse",
    "Candidate",
    "ItemsApplyResponse",
    "ItemsConfirmResponse",
    "ItemsIdentifyBody",
    "ItemsIdentifyResponse",
    "ItemsIdentifyStatusResponse",
    "MetadataBatchAlbum",
    "MetadataBatchBody",
    "MetadataBatchItem",
    "MetadataBatchResponse",
    "UntaggedItem",
    "UntaggedItemsResponse",
    "UpdateMetadataBody",
    "UpdateMetadataResponse",
    "ItemsCandidateBody",
]

# The apply preview reuses identify's diff shape verbatim.
ItemsApplyResponse = ApplyResponse


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class UpdateMetadataBody(BaseModel):
    """JSON body for ``POST /api/items/<id>/metadata``.

    All fields optional; the handler strips them and requires at least one
    non-empty value (else its own ``400``). ``extra="forbid"`` makes unknown
    fields a ``422``; a wrong-typed scalar (e.g. ``title: [..]``) → ``422``.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", description="New track title.")
    artist: str = Field(default="", description="New track artist.")
    album: str = Field(default="", description="New album title.")


class MetadataBatchAlbum(BaseModel):
    """Album-level fields for ``POST /api/items/metadata-batch``.

    String scalars are strictly typed (wrong type → ``422``). ``year`` is left
    lenient (``int | float | str | None``) so the handler's coerce-and-warn
    behavior is preserved. ``extra="forbid"`` rejects unknown album keys.
    """

    model_config = ConfigDict(extra="forbid")

    album: str | None = Field(default=None, description="Album title.")
    albumartist: str | None = Field(default=None, description="Album artist.")
    year: int | float | str | None = Field(
        default=None,
        description="Release year (lenient: bad values warn, not 422).",
    )
    genre: str | None = Field(
        default=None,
        description="Comma-separated genres; split into a list by the handler.",
    )


class MetadataBatchItem(BaseModel):
    """A per-item entry in ``POST /api/items/metadata-batch``.

    ``id`` is a strict ``int`` (JSON booleans rejected — ``bool`` is an ``int``
    subclass but ``strict`` excludes it). ``title``/``artist`` are strict
    strings; ``track``/``disc`` stay lenient (handler coerces and warns).
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(strict=True, description="Item id (integer; JSON booleans rejected).")
    title: str | None = Field(default=None, description="Per-track title override.")
    artist: str | None = Field(default=None, description="Per-track artist override.")
    track: int | float | str | None = Field(
        default=None, description="Track number (lenient: bad values warn)."
    )
    disc: int | float | str | None = Field(
        default=None, description="Disc number (lenient: bad values warn)."
    )


class MetadataBatchBody(BaseModel):
    """JSON body for ``POST /api/items/metadata-batch``.

    Structural validation only (object shapes, strict scalar types). The
    cross-field "at least one field" / "≥1 item when album fields present" rules
    remain the handler's authoritative ``400`` checks (see module docstring).
    """

    model_config = ConfigDict(extra="forbid")

    album: MetadataBatchAlbum = Field(
        default_factory=MetadataBatchAlbum, description="Album-level fields."
    )
    items: list[MetadataBatchItem] = Field(
        default_factory=list, description="Per-item entries (each requires an id)."
    )


class ItemsIdentifyBody(BaseModel):
    """JSON body for ``POST /api/items/identify``.

    ``item_ids`` is typed ``list[int]`` (a non-int element → ``422``). It is kept
    optional (default ``[]``) so the handler's own "non-empty list required"
    ``400`` stays authoritative for the empty/missing case. ``extra="forbid"``
    rejects unknown fields.
    """

    model_config = ConfigDict(extra="forbid")

    item_ids: list[int] = Field(default_factory=list, description="Item ids to identify together.")
    search_artist: str = Field(default="", description="Search hint: album artist.")
    search_album: str = Field(default="", description="Search hint: album title.")


class ItemsCandidateBody(BaseModel):
    """JSON body for ``.../apply`` and ``.../confirm``.

    Selects which identified candidate to act on. Optional (defaults to the top
    candidate, index ``0``); ``extra="forbid"`` rejects unknown fields, and a
    non-integer value raises pydantic → ``422`` (replacing the handlers' manual
    "candidate_index must be an integer" ``400``). An out-of-range index still
    reaches the handler and yields its own ``400``.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_index: int = Field(
        default=0, description="Index into the task's candidate list (0 = best match)."
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class UntaggedItem(BaseModel):
    """One row from ``GET /api/items/untagged``."""

    id: int = Field(description="Item id.")
    title: str = Field(description="Track title ('' if empty).")
    artist: str = Field(description="Track artist ('' if empty).")
    album: str = Field(description="Album title ('' if empty).")
    path: str = Field(description="Resolved on-disk path ('' if unavailable).")
    track: int = Field(description="Track number (0 if unset).")
    album_id: int | None = Field(default=None, description="Owning album id.")


class UntaggedItemsResponse(RootModel[list[UntaggedItem]]):
    """``GET /api/items/untagged`` — a bare top-level JSON array."""


class UpdateMetadataResponse(BaseModel):
    """``POST /api/items/<id>/metadata`` — single-item update acknowledgement.

    ``warnings`` is present only when the DB write succeeded but the file-tag
    write failed (still HTTP 200).
    """

    status: str = Field(description="Always 'ok' on success.")
    item_id: int = Field(description="The updated item's id.")
    warnings: list[str] | None = Field(
        default=None, description="Non-fatal issues (e.g. file tag write failed)."
    )


class MetadataBatchResponse(BaseModel):
    """``POST /api/items/metadata-batch`` — batch write result.

    Partial-success: ``warnings`` collects per-item/album non-fatal issues
    (unknown ids, bad numeric values, file-write failures); ``albums_updated``
    lists the album ids whose album-level fields were written.
    """

    status: str = Field(description="Always 'ok' on success.")
    warnings: list[str] = Field(
        default_factory=list, description="Non-fatal per-item/album issues."
    )
    albums_updated: list[int] = Field(
        default_factory=list, description="Album ids whose album-level fields were written."
    )


class ItemsIdentifyResponse(BaseModel):
    """``POST /api/items/identify`` — background task acknowledgement."""

    status: str = Field(description="Always 'started' for a fresh task.")
    task_id: str = Field(description="Identifier of the background identify task.")


class ItemsIdentifyStatusResponse(BaseModel):
    """``GET /api/items/identify/<task_id>/status`` — task snapshot.

    When no task exists the body is ``{"status": "idle"}``. Otherwise it mirrors
    the in-memory task (private ``_``-prefixed keys stripped). All fields besides
    ``status`` are optional so response validation never rejects a partial shape.
    """

    status: str = Field(description="``idle``, ``running``, ``confirming``, ``done`` or ``error``.")
    task_id: str | None = Field(default=None, description="Background task id (absent when idle).")
    item_ids: list[int] = Field(
        default_factory=list, description="Item ids the task is identifying."
    )
    candidates: list[Candidate] = Field(
        default_factory=list, description="Identified candidates (populated when done)."
    )
    error: str | None = Field(default=None, description="Error message when status is 'error'.")


class ItemsConfirmResponse(BaseModel):
    """``POST /api/items/identify/<task_id>/confirm`` — album creation result.

    ``warnings`` is present only on best-effort partial file-write failures
    (still HTTP 200 with the new ``album_id``).
    """

    status: str = Field(description="Always 'ok' on success.")
    album_id: int = Field(description="Id of the newly created album.")
    warnings: list[str] | None = Field(default=None, description="Non-fatal file-write failures.")
