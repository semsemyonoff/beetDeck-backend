"""Pydantic models for the ``genres`` blueprint's OpenAPI spec.

Covers the three genre routes in ``routes/genres.py``:

- ``POST /api/album/<id>/genre``          — fetch a genre preview (no write)
- ``POST /api/album/<id>/genre/confirm``  — confirm & write the fetched genre
- ``POST /api/album/<id>/genre/save``     — manually set a genre

Request shapes (verified against the handlers):

- ``genre`` and ``genre/confirm`` take **no request body** — they act purely on
  the ``<int:album_id>`` path param plus the ``lastgenre`` plugin. No ``json=``
  model is added (forcing one would 422 the SPA's body-less POSTs); these are
  documented response-only.
- ``genre/save`` takes a JSON body ``{"genre": "..."}``. The ``genre`` field is
  kept **optional** and ``extra="forbid"`` so the handler's own
  "Genre cannot be empty" ``400`` check stays the source of truth for empty /
  missing values, while an unknown field or a wrong-typed ``genre`` raises
  pydantic → ``422``.

Response validation is active (spectree's default), but with ``force_resp_serialize``
left ``False`` the handlers' raw ``jsonify(...)`` bytes pass through unchanged; the
models' all-optional fields keep real payloads conformant. ``resp=`` also feeds the
spec.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GenrePreviewQuery(BaseModel):
    """Query string for ``POST /api/album/<id>/genre``.

    ``mode`` decides what the previewed value is made of, not what is fetched —
    the Last.fm lookup is identical either way and is returned separately as
    ``fetched_genre``. Extra query keys are tolerated (no ``extra="forbid"``):
    the SPA appends cache-busters, and a rejected preview over a stray parameter
    would be a worse failure than an ignored one.
    """

    mode: Literal["replace", "merge"] = Field(
        default="replace",
        description=(
            "``replace`` (default) previews the fetched genre on its own; ``merge`` "
            "previews the album's current genres followed by the fetched ones it does "
            "not already carry (case-insensitive, order preserved)."
        ),
    )


class GenreSaveBody(BaseModel):
    """JSON body for ``POST /api/album/<id>/genre/save``.

    A single comma-separated ``genre`` string. Kept optional so the handler's own
    empty-value ``400`` remains authoritative; ``extra="forbid"`` makes unknown
    fields a ``422``.
    """

    model_config = ConfigDict(extra="forbid")

    genre: str = Field(
        default="",
        description=(
            "Comma-separated genre(s) to write, e.g. 'Rock, Pop'. An empty or "
            "blank value is rejected by the handler with a 400 'Genre cannot be "
            "empty'."
        ),
    )


class GenrePreviewResponse(BaseModel):
    """``POST /api/album/<id>/genre`` — old vs proposed genre (no write).

    A failed lookup is **not** this shape: when Last.fm cannot be reached the
    route answers ``502`` with an ``error``, because an empty ``new_genre`` would
    otherwise read as "this album has no genre" — a statement about the library
    that the failed lookup never established.
    """

    status: str = Field(description="Always ``ok`` on success.")
    mode: str | None = Field(
        default=None, description="The mode the preview was built with: ``replace`` or ``merge``."
    )
    old_genre: str = Field(description="Current genre(s), comma-formatted.")
    fetched_genre: str | None = Field(
        default=None,
        description="What Last.fm returned, comma-formatted, before the mode was applied. "
        "Empty means the lookup succeeded and had nothing for this album.",
    )
    new_genre: str = Field(
        description="The genre(s) this preview proposes, comma-formatted: the fetched "
        "value in ``replace`` mode, current-plus-fetched in ``merge``."
    )


class GenreWriteResponse(BaseModel):
    """Result of ``genre/confirm`` and ``genre/save``.

    ``status`` is ``ok`` when every track was written, or ``partial`` when some
    audio files failed to write (the failing item ids are listed in
    ``write_failures``).
    """

    status: str = Field(description="``ok`` or ``partial``.")
    genre: str = Field(description="The genre(s) written, comma-formatted.")
    write_failures: list[int] | None = Field(
        default=None,
        description=(
            "Item ids whose audio-file write failed (present when status is ``partial``)."
        ),
    )
