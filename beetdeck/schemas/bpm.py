"""Pydantic models for the ``bpm`` blueprint's OpenAPI spec.

Covers the single BPM route in ``routes/bpm.py``:

- ``POST /api/album/<id>/track/<id>/bpm/compute`` — compute and write BPM tag

Request shape: **no request body** — the route acts on path params only.  No
``json=`` model is added (forcing one would 422 the SPA's body-less POSTs);
this is documented response-only.

Response validation is active (spectree's default), but with
``force_resp_serialize`` left ``False`` the handlers' raw ``jsonify(...)``
bytes pass through unchanged; the models' all-optional fields keep real
payloads conformant.  ``resp=`` also feeds the spec.
"""

from pydantic import BaseModel, Field


class TrackBpmComputeResponse(BaseModel):
    """``POST /api/album/<id>/track/<id>/bpm/compute`` — computed BPM result.

    On success ``status`` is ``'ok'`` and ``bpm`` holds the rounded integer
    tempo.  On compute failure the handler returns a handler-owned ``400``
    error body; this model is only reached on the success path.
    """

    status: str | None = Field(default=None, description="Always 'ok' on success.")
    bpm: int | None = Field(
        default=None,
        description="Computed BPM value (rounded integer tempo).",
    )
