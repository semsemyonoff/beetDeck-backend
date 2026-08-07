"""Blocking REST client over the beetDeck API, and the one place write results
are normalized.

Two things live here on purpose:

* **Nothing raises on a failed round trip.** A refused connection, a timeout or a
  non-JSON body come back as an `ApiResponse` carrying `error`, so a tool can
  report `failed` instead of an empty result that reads like "nothing to do".
* **Normalization is per endpoint, not per status code.** The backend's
  partial-success shapes disagree with each other — tags use `warnings`, genres a
  `status: "partial"` body, lyrics three separate lists — so a single rule would
  report success for a write that half happened. Every shape is mapped in
  `_NORMALIZERS` below, keyed by the preview-token kind that produced it.

The client is synchronous throughout: that is what lets the tests drive the real
Flask app through `httpx.WSGITransport` instead of hand-written mocks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from beetdeck_mcp.config import Config

#: Comfortable for library reads and tag writes. Long jobs (BPM ~9 s/track,
#: identification polling) pass their own `timeout=`.
DEFAULT_TIMEOUT = 30.0

ApplyStatus = Literal["ok", "partial", "failed"]

#: Preview-token kinds `normalize()` understands, i.e. every write the MCP
#: server can commit. Adding a kind without a normalizer is a `ValueError`, not a
#: silent pass-through.
WRITE_KINDS = ("identify", "items_identify", "tags", "genres", "cover", "lyrics")


@dataclass(frozen=True)
class ApiResponse:
    """One REST round trip — including the ones that never reached the API.

    `status_code` is `None` exactly when the request failed in transport; in that
    case `error` says why.
    """

    status_code: int | None
    payload: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None and self.status_code < 300

    @property
    def stale(self) -> bool:
        """The backend refused an `expected_generation` precondition (Task 3)."""
        return self.status_code == 409

    @property
    def data(self) -> dict[str, Any]:
        """The JSON body when it is an object, `{}` otherwise."""
        return self.payload if isinstance(self.payload, dict) else {}

    @property
    def message(self) -> str:
        """A one-line reason, suitable for an agent-visible `detail` entry."""
        if self.error is not None:
            return self.error
        parts = []
        error = self.data.get("error")
        if isinstance(error, str) and error:
            parts.append(error)
        detail = self.data.get("detail")
        if isinstance(detail, list) and detail:
            parts.append("; ".join(str(entry) for entry in detail))
        body = " ".join(parts)
        return f"HTTP {self.status_code}: {body}" if body else f"HTTP {self.status_code}"


@dataclass(frozen=True)
class ApplyResult:
    """The normalized outcome of a committing request."""

    status: ApplyStatus
    detail: tuple[str, ...] = ()
    #: True only for a backend precondition failure, so callers can tell "the
    #: library moved under you" apart from "the write itself failed".
    stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": list(self.detail)}


@dataclass(frozen=True)
class _Context:
    """Extra facts a normalizer needs that the response does not carry."""

    requested_item_ids: tuple[int, ...] | None = None


def _ok(*detail: str) -> ApplyResult:
    return ApplyResult("ok", tuple(detail))


def _partial(detail: Iterable[str]) -> ApplyResult:
    return ApplyResult("partial", tuple(detail))


def _as_strings(values: Any) -> list[str]:
    """A JSON list of ids or messages as strings; anything else as empty."""
    return [str(value) for value in values] if isinstance(values, list) else []


def _normalize_identify(_response: ApiResponse, _ctx: _Context) -> ApplyResult:
    """`POST …/confirm`: all-or-nothing. A write failure is a 500, handled above."""
    return _ok()


def _normalize_items_identify(response: ApiResponse, _ctx: _Context) -> ApplyResult:
    """`POST /api/items/identify/<task_id>/confirm`: 200 + `warnings` on a
    partial file write (the DB side rolled back or succeeded as a whole)."""
    warnings = _as_strings(response.data.get("warnings"))
    return _partial(warnings) if warnings else _ok()


def _normalize_tags(response: ApiResponse, _ctx: _Context) -> ApplyResult:
    """`PATCH /api/items/<id>/tags`: 200 + non-empty `warnings` ⇒ the DB was
    written but the file was not."""
    warnings = _as_strings(response.data.get("warnings"))
    return _partial(warnings) if warnings else _ok()


def _normalize_genres(response: ApiResponse, _ctx: _Context) -> ApplyResult:
    """`POST …/genre/save`: 200 + `status: "partial"` with the item ids whose
    file could not be written."""
    data = response.data
    if data.get("status") != "partial":
        return _ok()
    failures = _as_strings(data.get("write_failures"))
    detail = [f"genre tag write failed for item(s) {', '.join(failures)}"] if failures else []
    return _partial(detail or ["genre written to the database but not to every file"])


def _normalize_cover(_response: ApiResponse, _ctx: _Context) -> ApplyResult:
    """`POST …/cover/confirm`: embeds all tracks or errors — the handler returns a
    bare `{"status": "ok"}` with no partial signal, so there is nothing to map."""
    return _ok()


def _normalize_lyrics(response: ApiResponse, ctx: _Context) -> ApplyResult:
    """`POST …/lyrics/confirm`: three separate partial signals, one of them silent.

    Ids with no stashed fetch (or belonging to another album) are skipped without
    any marker, so `written_item_ids` shorter than the request is itself a
    partial result — see `CLAUDE.md`.
    """
    data = response.data
    written = _as_strings(data.get("written_item_ids"))
    failed = _as_strings(data.get("failed"))
    retained = _as_strings(data.get("retained_lrc_item_ids"))

    detail: list[str] = []
    if failed:
        detail.append(f"lyrics write failed for item(s) {', '.join(failed)}")
    if retained:
        detail.append(f"sidecar .lrc could not be removed for item(s) {', '.join(retained)}")
    if ctx.requested_item_ids is not None:
        accounted = set(written) | set(failed)
        skipped = [
            str(item_id) for item_id in ctx.requested_item_ids if str(item_id) not in accounted
        ]
        if skipped:
            detail.append(
                f"no stashed lyrics fetch for item(s) {', '.join(skipped)} — nothing was written "
                "for them; re-run the preview"
            )
    return _partial(detail) if detail else _ok()


_NORMALIZERS: Mapping[str, Callable[[ApiResponse, _Context], ApplyResult]] = {
    "identify": _normalize_identify,
    "items_identify": _normalize_items_identify,
    "tags": _normalize_tags,
    "genres": _normalize_genres,
    "cover": _normalize_cover,
    "lyrics": _normalize_lyrics,
}


def normalize(
    kind: str,
    response: ApiResponse,
    *,
    requested_item_ids: Sequence[int] | None = None,
) -> ApplyResult:
    """Map one endpoint's response onto `ok` / `partial` / `failed`."""
    normalizer = _NORMALIZERS.get(kind)
    if normalizer is None:
        raise ValueError(f"no result normalizer for kind {kind!r}; known kinds: {WRITE_KINDS}")

    if response.error is not None:
        return ApplyResult("failed", (response.error,))
    if response.stale:
        return ApplyResult("failed", (response.message,), stale=True)
    if not response.ok:
        return ApplyResult("failed", (response.message,))

    ctx = _Context(
        requested_item_ids=None if requested_item_ids is None else tuple(requested_item_ids)
    )
    return normalizer(response, ctx)


@dataclass
class BeetdeckClient:
    """Blocking `httpx` wrapper over `BEETDECK_API_URL`.

    `transport` is injectable so tests can hand in
    `httpx.WSGITransport(app=create_app(...))` and exercise the real API without
    a socket.
    """

    config: Config
    transport: httpx.BaseTransport | None = None
    timeout: float = DEFAULT_TIMEOUT
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            base_url=self.config.api_url,
            transport=self.transport,
            timeout=self.timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BeetdeckClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> ApiResponse:
        """One round trip. Transport failures come back as data, never as a raise."""
        kwargs: dict[str, Any] = {"params": params, "json": json}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            return ApiResponse(
                None, error=f"{method} {path} timed out after {timeout or self.timeout}s: {exc}"
            )
        except httpx.InvalidURL as exc:
            # Not an `HTTPError` subclass, so it needs its own arm — a control
            # character in an agent-supplied path (a trailing newline is enough)
            # would otherwise escape as a raise and break the never-raise contract.
            return ApiResponse(None, error=f"{method} {path!r} is not a usable request path: {exc}")
        except httpx.HTTPError as exc:
            return ApiResponse(
                None, error=f"{method} {path} could not reach the beetDeck API: {exc}"
            )

        if not response.content:
            return ApiResponse(response.status_code)
        try:
            return ApiResponse(response.status_code, payload=response.json())
        except ValueError:
            return ApiResponse(
                response.status_code,
                error=(
                    f"{method} {path} returned a non-JSON body "
                    f"(HTTP {response.status_code}, {response.headers.get('content-type', '?')})"
                ),
            )

    def get(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("PATCH", path, **kwargs)

    def write(
        self,
        kind: str,
        method: str,
        path: str,
        *,
        json: Any = None,
        timeout: float | None = None,
        requested_item_ids: Sequence[int] | None = None,
    ) -> ApplyResult:
        """Commit through `path` and normalize the result for `kind`."""
        if kind not in _NORMALIZERS:
            raise ValueError(f"no result normalizer for kind {kind!r}; known kinds: {WRITE_KINDS}")
        response = self.request(method, path, json=json, timeout=timeout)
        return normalize(kind, response, requested_item_ids=requested_item_ids)
