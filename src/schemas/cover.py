"""Pydantic models for the ``cover`` blueprint's OpenAPI spec.

Covers the five cover-art routes in ``routes/cover.py``:

- ``GET  /api/album/<id>/cover``          — serve current cover (binary)
- ``POST /api/album/<id>/cover/fetch``    — fetch a cover candidate (preview)
- ``GET  /api/album/<id>/cover/preview``  — serve the fetched candidate (binary)
- ``POST /api/album/<id>/cover/confirm``  — save the fetched candidate
- ``POST /api/album/<id>/cover/upload``   — upload a cover image (multipart)

Request shapes (verified against the handlers):

- ``fetch`` and ``confirm`` take **no request body** — they act purely on the
  ``<int:album_id>`` path param plus server-side ``state.identify_tasks``. So no
  ``json=`` model is added (forcing one would 422 the SPA's body-less POSTs);
  these are documented response-only.
- ``upload`` is **multipart/form-data** with a single ``file`` field, modeled via
  ``form=CoverUploadForm`` (NOT ``json=``). The field is kept optional so
  spectree does not pre-empt the handler's own ``400`` checks (missing file /
  empty filename / unsupported extension) — only the multipart shape is
  documented, the bespoke validation stays in the handler.
- The two ``GET`` routes stream binary image data, so they only document their
  ``404`` error response (no success body model).

Response validation stays OFF (``resp=`` only feeds the spec), so handlers keep
returning their raw ``jsonify(...)`` output unchanged.
"""

from typing import Any

from pydantic import BaseModel, Field


class CoverFetchResponse(BaseModel):
    """``POST /api/album/<id>/cover/fetch`` — result of a cover-art lookup.

    When nothing is found the body is ``{"status": "ok", "found": false}``; on a
    hit it additionally carries the ``source`` and a ``preview_url``.
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
