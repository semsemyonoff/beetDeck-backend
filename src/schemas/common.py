"""Shared pydantic models reused across blueprint schema modules.

This module holds:
- the uniform validation-error contract (``ErrorResponse`` / ``ValidationErrorDetail``)
  used to document 4xx/422 responses in the OpenAPI spec, and
- reusable nested response types (``AlbumSummary``, ``ArtistGroup``) shared by the
  ``library`` blueprint's ``/api/library`` and ``/api/artist`` endpoints.

The error **status code** is locked to 422 on the ``SpecTree`` singleton (see
``src/api.py``); the error **body** shape is finalized here (Task 11). Every
invalid request returns this uniform ``ErrorResponse`` body::

    {"error": "Validation failed",
     "detail": [{"loc": [...], "msg": "...", "type": "..."}, ...]}

``error`` is a short machine-readable message and ``detail`` carries one
field-level entry per offending field (mirroring pydantic's error format). The
``_render_validation_error`` hook in ``src/api.py`` produces this body, and
``validation_error_model=ErrorResponse`` documents it as the 422 response of
every validated endpoint in the spec.

Note: handler-owned semantic errors (e.g. a missing album → 404, "Genre cannot
be empty" → 400) keep their own ``{"error": "..."}`` bodies with ``detail``
absent — they are not request-validation failures and are out of scope of this
contract.
"""

from pydantic import BaseModel, Field


class ValidationErrorDetail(BaseModel):
    """A single field-level validation error (mirrors pydantic's error entries)."""

    loc: list[str | int] = Field(
        default_factory=list,
        description="Location of the offending field (e.g. ['body', 'items', 0, 'id']).",
    )
    msg: str = Field(description="Human-readable description of the error.")
    type: str = Field(description="Machine-readable pydantic error type.")


class ErrorResponse(BaseModel):
    """Uniform error body for failed requests.

    ``error`` is a short machine-readable code/message; ``detail`` carries
    per-field validation errors when the failure is a request-validation error.
    """

    error: str = Field(description="Short machine-readable error code or message.")
    detail: list[ValidationErrorDetail] | None = Field(
        default=None,
        description="Field-level validation errors, when applicable.",
    )


class AlbumSummary(BaseModel):
    """An album entry as returned in artist/library listings.

    Matches the per-album dict emitted by ``GET /api/library`` and
    ``GET /api/artist`` (validated against that output, then passed through
    unchanged since ``force_resp_serialize`` is ``False``; also feeds the spec).
    """

    id: int = Field(description="Album id.")
    album: str | None = Field(default=None, description="Album title.")
    year: int | None = Field(default=None, description="Original or release year.")
    tagged: bool = Field(description="Whether the album is marked beetdeck_tagged.")
    ignored: bool = Field(description="Whether the album is marked beetdeck_ignored.")
    has_cover: bool = Field(description="Whether cover art is available.")


class ArtistGroup(BaseModel):
    """An artist with their albums, as returned by ``GET /api/library``."""

    artist: str = Field(description="Album artist name ('Unknown Artist' if empty).")
    albums: list[AlbumSummary] = Field(
        default_factory=list, description="Albums by this artist."
    )
