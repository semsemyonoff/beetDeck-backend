"""Pydantic models for the ``cover`` blueprint's OpenAPI spec.

Covers the five cover-art routes in ``routes/cover.py``:

- ``GET  /api/album/<id>/cover``          — serve current cover (binary)
- ``POST /api/album/<id>/cover/fetch``    — fetch a cover candidate (preview)
- ``GET  /api/album/<id>/cover/preview``  — serve the fetched candidate (binary)
- ``POST /api/album/<id>/cover/confirm``  — save the fetched candidate
- ``POST /api/album/<id>/cover/upload``   — upload a cover image (multipart)

Request shapes (verified against the handlers):

- ``fetch`` takes **no request body** — it acts purely on the ``<int:album_id>``
  path param, so it is documented response-only.
- ``confirm`` takes an optional ``{"expected_generation": int}`` precondition
  (``CoverConfirmBody``). Every field defaults, so a body-less POST stays valid
  and the SPA is unaffected; ``extra="forbid"`` makes unknown fields a ``422``.
- ``upload`` is **multipart/form-data** with a single ``file`` field, modeled via
  ``form=CoverUploadForm`` (NOT ``json=``). The field is kept optional so
  spectree does not pre-empt the handler's own ``400`` checks (missing file /
  empty filename / unsupported extension) — only the multipart shape is
  documented, the bespoke validation stays in the handler.
- The two ``GET`` routes stream binary image data, so they only document their
  ``404`` error response (no success body model).

Response validation is active (spectree's default), but with ``force_resp_serialize``
left ``False`` the handlers' raw ``jsonify(...)`` bytes pass through unchanged; the
models' all-optional fields keep real payloads conformant. ``resp=`` also feeds the
spec.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CoverConfirmBody(BaseModel):
    """JSON body for ``POST /api/album/<id>/cover/confirm``.

    An optional precondition on the fetch stash. When ``expected_generation`` is
    sent, the handler compares it against the ``stash_generation`` of the
    currently stashed candidate inside ``state.identify_lock`` and rejects a
    mismatch with ``409`` — that is what stops a confirm from committing a cover
    somebody else fetched into the same slot. Omitting it keeps the pre-existing
    behaviour (the SPA does not send it).
    """

    model_config = ConfigDict(extra="forbid")

    expected_generation: int | None = Field(
        default=None,
        description=(
            "Precondition: the ``stash_generation`` returned by the fetch whose "
            "candidate this confirm intends to write. A mismatch yields 409."
        ),
    )


class CoverFetchResponse(BaseModel):
    """``POST /api/album/<id>/cover/fetch`` — result of a cover-art lookup.

    When nothing is found the body is ``{"status": "ok", "found": false}``; on a
    hit it additionally carries the ``source``, a ``preview_url``, the
    candidate's pixel dimensions and the stash generation to confirm against.
    """

    status: str = Field(description="Always ``ok`` on a successful lookup.")
    found: bool = Field(description="Whether a cover-art candidate was found.")
    source: str | None = Field(
        default=None, description="Art source name (present only when found)."
    )
    preview_url: str | None = Field(
        default=None,
        description="URL of the candidate preview image (present only when found).",
    )
    width: int | None = Field(
        default=None,
        description="Pixel width of the candidate; null when it cannot be read as an "
        "image. Compare against ``cover_width`` from ``GET /api/album/<id>`` to decide "
        "whether the candidate is an upgrade.",
    )
    height: int | None = Field(
        default=None, description="Pixel height of the candidate (see width)."
    )
    stash_generation: int | None = Field(
        default=None,
        description="Generation of the stash slot this candidate was written into. "
        "Pass it back as ``expected_generation`` on confirm to refuse a candidate "
        "that a competing fetch replaced in the meantime.",
    )


class CoverStatusResponse(BaseModel):
    """``{"status": "ok"}`` returned by ``cover/confirm`` and ``cover/upload``."""

    status: str = Field(description="Always ``ok`` on success.")


class CoverUploadForm(BaseModel):
    """Multipart body for ``POST /api/album/<id>/cover/upload``.

    A single ``file`` part carrying the image. Kept optional so the handler's own
    file validation (missing / empty filename / unsupported extension → ``400``)
    remains the source of truth; this model only documents the multipart shape.
    """

    file: Any = Field(
        default=None,
        description="Cover image file (jpg/jpeg/png/webp).",
        json_schema_extra={"type": "string", "format": "binary"},
    )
