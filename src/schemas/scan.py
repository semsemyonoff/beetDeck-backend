"""Pydantic models for the ``scan`` blueprint's OpenAPI spec.

Covers ``POST /api/rescan`` and ``GET /api/rescan/status``.

``RescanQuery.mode`` is a closed ``Literal`` (``quick``/``full``) with a default
of ``quick``, so a missing param keeps the existing behavior (defaults to a quick
incremental import) while an **invalid** value is now rejected by spectree with a
**422** — replacing the handler's old ad-hoc ``400 Invalid mode`` check.
``extra="forbid"`` makes an unknown query param a 422 too.

Response models document the success bodies only — response validation feeds the
spec but handlers keep returning their raw ``jsonify(...)`` output. The status
endpoint returns a union of shapes (idle / running / done [+ diff]); all fields
beyond ``status`` are optional so every variant validates.
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

    ``status`` is one of ``idle`` / ``running`` / ``done``. ``returncode`` and the
    ``added`` / ``removed`` diff lists are only present once a scan has finished.
    """

    status: str = Field(description="One of 'idle', 'running', 'done'.")
    returncode: int | None = Field(
        default=None, description="Subprocess exit code (present when status='done')."
    )
    added: list[ScanChange] | None = Field(
        default=None, description="Items newly added by the rescan."
    )
    removed: list[ScanChange] | None = Field(
        default=None, description="Items removed by the rescan."
    )
