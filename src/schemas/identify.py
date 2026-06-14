"""Pydantic models for the ``identify`` blueprint's OpenAPI spec.

Covers the five routes in ``routes/identify.py`` (note: every URL lives under the
``/api/album/...`` prefix, but they are *defined* in ``identify.py``, not in the
``albums`` blueprint):

- ``POST /api/album/<id>/ignore``           — toggle the ``beetdeck_ignored`` flag
- ``POST /api/album/<id>/identify``         — start a background autotag task
- ``GET  /api/album/<id>/identify/status``  — poll the background task
- ``POST /api/album/<id>/apply``            — preview the diff for a candidate
- ``POST /api/album/<id>/confirm``          — write the chosen candidate to files/DB

Request shapes (verified against the handlers):

- ``ignore`` and ``identify/status`` take **no request body** — they act purely on
  the ``<int:album_id>`` path param plus server-side state. No ``json=`` model is
  added (forcing one would 422 the SPA's body-less requests); documented
  response-only.
- ``identify`` takes an **optional** JSON body of free-text search hints
  (``{"artist"?, "album"?, "search_id"?}``). The handler reads it with
  ``request.get_json(silent=True) or {}`` and every field defaults to ``""``, so a
  body-less POST is valid. ``extra="forbid"`` makes an unknown field a ``422``.
- ``apply`` and ``confirm`` take ``{"candidate_index": int}``. ``candidate_index``
  is kept **optional** (default ``0``, matching the handler's
  ``data.get("candidate_index", 0)``) with ``extra="forbid"``. A wrong-typed value
  now raises pydantic → ``422`` (this replaces the handlers' manual
  "candidate_index must be an integer" ``400`` check); an out-of-range index still
  reaches the handler and yields its own ``400``.

Response validation stays OFF (``resp=`` only feeds the spec), so handlers keep
returning their raw ``jsonify(...)`` output unchanged.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class IdentifyStartBody(BaseModel):
    """Optional JSON body for ``POST /api/album/<id>/identify``.

    Free-text search hints forwarded to beets autotag. All optional; a body-less
    POST is valid. ``extra="forbid"`` makes unknown fields a ``422``.
    """

    model_config = ConfigDict(extra="forbid")

    artist: str = Field(default="", description="Search hint: album artist.")
    album: str = Field(default="", description="Search hint: album title.")
    search_id: str = Field(
        default="",
        description="Search hint: an explicit MusicBrainz/source release id.",
    )


class CandidateBody(BaseModel):
    """JSON body for ``POST /api/album/<id>/apply`` and ``.../confirm``.

    Selects which previously-identified candidate to act on. Optional (defaults to
    the top candidate, index ``0``); ``extra="forbid"`` makes unknown fields a
    ``422``, and a non-integer value raises pydantic → ``422``.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_index: int = Field(
        default=0,
        description="Index into the task's candidate list (0 = best match).",
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class IgnoreResponse(BaseModel):
    """``POST /api/album/<id>/ignore`` — the new state of the ignored flag."""

    ignored: bool = Field(description="Whether the album is now flagged as ignored.")


class IdentifyStartResponse(BaseModel):
    """``POST /api/album/<id>/identify`` — background task acknowledgement.

    ``status`` is ``started`` for a fresh task, or ``running`` (HTTP 409) when a
    task for the album is already in flight.
    """

    status: str = Field(description="``started`` (new task) or ``running`` (409).")
    task_id: str = Field(description="Identifier of the background identify task.")


class CandidateTrack(BaseModel):
    """A proposed track within an identification candidate.

    All fields optional so response validation (which is active) never rejects a
    partial candidate shape — this model is documentation-only.
    """

    title: str | None = Field(default=None, description="Proposed track title.")
    track: int | None = Field(default=None, description="Proposed track number.")
    artist: str | None = Field(default=None, description="Proposed track artist.")
    length: str | None = Field(
        default=None, description="Track length formatted as ``M:SS`` ('' if unknown)."
    )
    current_title: str | None = Field(
        default=None, description="The track's current title, for comparison."
    )


class Candidate(BaseModel):
    """One autotag candidate (a possible MusicBrainz/source match).

    All fields optional so response validation (which is active) never rejects a
    partial candidate shape — this model is documentation-only.
    """

    index: int | None = Field(default=None, description="Candidate index (0 = best match).")
    artist: str | None = Field(default=None, description="Proposed album artist.")
    album: str | None = Field(default=None, description="Proposed album title.")
    year: int | None = Field(default=None, description="Proposed release year.")
    label: str | None = Field(default=None, description="Release label ('' if unknown).")
    media: str | None = Field(default=None, description="Release media ('' if unknown).")
    data_source: str | None = Field(default=None, description="Source, e.g. 'MusicBrainz'.")
    mb_albumid: str | None = Field(default=None, description="Source release id ('' if unknown).")
    distance: float | None = Field(default=None, description="Match distance (0 = exact).")
    track_count: int | None = Field(
        default=None, description="Number of tracks in the proposed release."
    )
    tracks: list[CandidateTrack] = Field(
        default_factory=list, description="Per-track proposals."
    )


class IdentifyStatusResponse(BaseModel):
    """``GET /api/album/<id>/identify/status`` — background task snapshot.

    When no task exists the body is ``{"status": "idle"}``. Otherwise it mirrors
    the in-memory task (private ``_``-prefixed keys stripped): ``running`` while in
    flight (``candidates`` empty), ``done`` with populated ``candidates``, or
    ``error`` with an ``error`` message.
    """

    status: str = Field(description="``idle``, ``running``, ``confirming``, ``done`` or ``error``.")
    task_id: str | None = Field(default=None, description="Background task id (absent when idle).")
    album_id: int | None = Field(default=None, description="Album id the task is for.")
    candidates: list[Candidate] = Field(
        default_factory=list, description="Identified candidates (populated when done)."
    )
    current_artist: str | None = Field(default=None, description="Album artist being processed.")
    current_album: str | None = Field(default=None, description="Album title being processed.")
    error: str | None = Field(default=None, description="Error message when status is 'error'.")


class FieldDiff(BaseModel):
    """An old/new value pair for a single field in an apply preview."""

    old: str = Field(description="Current value.")
    new: str = Field(description="Proposed value.")


class TrackDiff(BaseModel):
    """Per-track diff in an apply preview."""

    track: int | None = Field(default=None, description="Track number.")
    title: FieldDiff = Field(description="Title old/new.")
    artist: FieldDiff = Field(description="Artist old/new.")


class ApplyResponse(BaseModel):
    """``POST /api/album/<id>/apply`` — preview of what a candidate would change."""

    candidate_index: int = Field(description="The previewed candidate's index.")
    album: dict[str, Any] = Field(
        description="Album-level field diffs keyed by field name (each {old, new})."
    )
    tracks: list[TrackDiff] = Field(
        default_factory=list, description="Per-track field diffs."
    )


class ConfirmResponse(BaseModel):
    """``POST /api/album/<id>/confirm`` — ``{"status": "ok"}`` on a clean write."""

    status: str = Field(description="Always 'ok' on success.")
