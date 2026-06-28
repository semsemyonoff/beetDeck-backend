"""Pydantic models for the ``scan`` blueprint's OpenAPI spec.

Covers ``POST /api/rescan`` and ``GET /api/rescan/status``.

``RescanQuery.mode`` is a closed ``Literal`` (``quick``/``full``) with a default
of ``quick``, so a missing param keeps the existing behavior (defaults to a quick
incremental import) while an **invalid** value is now rejected by spectree with a
**422** — replacing the handler's old ad-hoc ``400 Invalid mode`` check.
``extra="forbid"`` makes an unknown query param a 422 too.

Response models are validated against the handler output (spectree's default), but
with ``force_resp_serialize`` left ``False`` the raw ``jsonify(...)`` bytes pass
through unchanged; ``resp=`` also feeds the spec. The status endpoint returns a
union of shapes (idle / running / done [+ diff]); all fields beyond ``status`` are
optional so every variant validates.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RescanQuery(BaseModel):
    """Query params for ``POST /api/rescan``.

    ``mode`` selects the beets import strategy: ``quick`` (incremental, ``-i``) or
    ``full`` (re-import everything, ``-I``). Invalid values → 422.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["quick", "full"] = Field(
        default="quick",
        description="Rescan strategy: 'quick' (incremental) or 'full' (re-import).",
    )


class RescanStartResponse(BaseModel):
    """``POST /api/rescan`` success — a rescan was started."""

    status: str = Field(description="Always 'started' on success.")
    mode: str = Field(description="The effective rescan mode ('quick' or 'full').")


class RescanRunningResponse(BaseModel):
    """``POST /api/rescan`` 409 — a rescan is already in progress."""

    status: str = Field(description="Always 'running' when a scan is already active.")


class ScanChange(BaseModel):
    """A single added/removed item in the rescan diff."""

    id: int = Field(description="Item (track) id.")
    title: str | None = Field(default=None, description="Track title.")
    artist: str | None = Field(default=None, description="Track artist.")


class RescanStatusResponse(BaseModel):
    """``GET /api/rescan/status`` — current rescan state.

    ``status`` is one of ``idle`` / ``running`` / ``done`` / ``error``. ``returncode``
    and the ``added`` / ``removed`` diff lists are only present once a scan has
    finished.  Progress fields (``phase``, ``processed``, ``total``,
    ``current_item``, ``run_id``) are present in running and finished responses.
    """

    status: str = Field(description="One of 'idle', 'running', 'done', 'error'.")
    phase: str | None = Field(
        default=None,
        description="Lifecycle phase: 'importing', 'updating', 'done', 'error', or 'idle'.",
    )
    processed: int | None = Field(
        default=None,
        description="Albums/singletons processed so far in the current run.",
    )
    total: int | None = Field(
        default=None,
        description="Total albums expected (null for quick/indeterminate scans).",
    )
    current_item: str | None = Field(
        default=None,
        description="Most recent item path being processed.",
    )
    run_id: str | None = Field(
        default=None,
        description="Unique identifier for the current or last run.",
    )
    returncode: int | None = Field(
        default=None, description="Subprocess exit code (present when status='done' or 'error')."
    )
    added: list[ScanChange] | None = Field(
        default=None, description="Items newly added by the rescan."
    )
    removed: list[ScanChange] | None = Field(
        default=None, description="Items removed by the rescan."
    )


class RescanDismissResponse(BaseModel):
    """``POST /api/rescan/dismiss`` success."""

    status: str = Field(description="Always 'ok'.")
