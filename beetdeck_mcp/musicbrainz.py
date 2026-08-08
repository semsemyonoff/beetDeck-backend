"""MusicBrainz `ws/2` client — the agent's escape route from a wrong autotag match.

Plain `httpx` against the public web service, deliberately not `musicbrainzngs`:
`httpx` is already required for the REST client, and the two calls this server
makes are a search and a lookup.

Three things this module owns:

* **The rate gate.** MusicBrainz allows ~1 request/second per source IP. `RateGate`
  is process-wide (a `threading.Lock` plus a monotonic timestamp), so the MCP
  SDK's worker threads queue behind each other instead of bursting.

  ⚠️ Known limitation: the backend's own autotag queries MusicBrainz too, and the
  budget is shared per IP. This gate bounds the MCP process only, so an agent
  session running alongside a rescan can still exceed it.
* **The user agent.** MusicBrainz rejects anonymous clients, so an empty one is a
  `ValueError` at construction rather than a puzzling `403` at the first call.
* **Never raising, and never retrying forever.** Like `client.py`, a failed round
  trip comes back as data. A `503`/`429` is surfaced as a rate-limit message the
  agent can act on — retrying it in a loop here would just spend the shared
  budget faster.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

MUSICBRAINZ_BASE_URL = "https://musicbrainz.org/ws/2"

#: MusicBrainz' documented budget for anonymous clients: one request per second.
MIN_REQUEST_INTERVAL = 1.0

#: A search is a single indexed query; the gate is what makes calls slow, not the
#: service.
DEFAULT_TIMEOUT = 15.0

#: What a release lookup needs to answer "is this the same album?" — the credited
#: artist, the label/catalogue number, and the tracklist with lengths.
RELEASE_INCLUDES = "artist-credits+labels+recordings+release-groups"


class RateGate:
    """Process-wide "no more than one request per `min_interval`" gate.

    The lock is held across the sleep on purpose: each waiter blocks the next, so
    N threads arriving at once are spaced out instead of all reading the same
    free slot.
    """

    def __init__(
        self,
        min_interval: float = MIN_REQUEST_INTERVAL,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = float("-inf")

    def wait(self) -> float:
        """Block until the next request is allowed; return how long that took."""
        with self._lock:
            now = self._clock()
            delay = self._next_at - now
            if delay > 0:
                self._sleep(delay)
                now = max(now, self._next_at)
            else:
                delay = 0.0
            self._next_at = now + self._min_interval
            return delay


@dataclass(frozen=True)
class MbResponse:
    """One MusicBrainz round trip, including the ones that never got there.

    `status_code` is `None` exactly when the request failed in transport.
    """

    status_code: int | None
    payload: Any = None
    error: str | None = None
    #: True when MusicBrainz asked us to slow down (`503`/`429`), so a caller can
    #: say "wait and retry" rather than "no results".
    rate_limited: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None and self.status_code < 300

    @property
    def data(self) -> dict[str, Any]:
        return self.payload if isinstance(self.payload, dict) else {}

    @property
    def message(self) -> str:
        """A one-line reason, suitable for an agent-visible error."""
        if self.error is not None:
            return self.error
        return f"MusicBrainz returned HTTP {self.status_code}"


def phrase(value: str) -> str:
    """A Lucene phrase — only `\\` and `"` need escaping inside quotes."""
    escaped = value.strip().replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_release_query(
    artist: str = "",
    album: str = "",
    track_count: int | None = None,
    year: int | None = None,
) -> str:
    """The Lucene query for a release search, or `""` when nothing was given.

    `track_count` and `year` are the two cheap discriminators between different
    editions of the same album; `date:<year>*` matches any release date in that
    year, not only a bare `YYYY` value.
    """
    clauses: list[str] = []
    if artist.strip():
        clauses.append(f"artist:{phrase(artist)}")
    if album.strip():
        clauses.append(f"release:{phrase(album)}")
    if track_count:
        clauses.append(f"tracks:{int(track_count)}")
    if year:
        clauses.append(f"date:{int(year)}*")
    return " AND ".join(clauses)


@dataclass
class MusicBrainzClient:
    """Blocking `ws/2` client behind a shared rate gate.

    `transport` and `gate` are injectable so the tests can drive the real request
    construction without network access and without waiting out the gate.
    """

    user_agent: str
    base_url: str = MUSICBRAINZ_BASE_URL
    transport: httpx.BaseTransport | None = None
    timeout: float = DEFAULT_TIMEOUT
    gate: RateGate | None = None
    _client: httpx.Client = field(init=False, repr=False)
    _gate: RateGate = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.user_agent.strip():
            raise ValueError(
                "MusicBrainz requires a descriptive User-Agent ('app/version (contact)'); "
                "refusing to query it anonymously"
            )
        self._gate = self.gate if self.gate is not None else RateGate()
        self._client = httpx.Client(
            base_url=self.base_url,
            transport=self.transport,
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent.strip(), "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MusicBrainzClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get(self, path: str, params: Mapping[str, Any]) -> MbResponse:
        """One gated round trip. Transport failures come back as data."""
        self._gate.wait()
        query = {**params, "fmt": "json"}
        try:
            response = self._client.get(path, params=query)
        except httpx.TimeoutException as exc:
            return MbResponse(None, error=f"MusicBrainz request {path} timed out: {exc}")
        except httpx.InvalidURL as exc:
            # `InvalidURL` does not inherit from `HTTPError`; an mbid carrying a
            # control character would otherwise raise out of a never-raise method.
            return MbResponse(None, error=f"{path!r} is not a usable MusicBrainz path: {exc}")
        except httpx.HTTPError as exc:
            return MbResponse(None, error=f"MusicBrainz is unreachable ({path}): {exc}")

        if response.status_code in (429, 503):
            retry_after = response.headers.get("Retry-After")
            hint = f" Retry-After: {retry_after}." if retry_after else ""
            return MbResponse(
                response.status_code,
                error=(
                    f"MusicBrainz is rate limiting or temporarily unavailable "
                    f"(HTTP {response.status_code}).{hint} Wait a few seconds and call again — "
                    "the request was not retried, so nothing is queued."
                ),
                rate_limited=True,
            )

        payload: Any = None
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                return MbResponse(
                    response.status_code,
                    error=(
                        f"MusicBrainz returned a non-JSON body for {path} "
                        f"(HTTP {response.status_code})"
                    ),
                )

        if response.status_code >= 300:
            detail = payload.get("error") if isinstance(payload, dict) else None
            body = f": {detail}" if isinstance(detail, str) and detail else ""
            return MbResponse(
                response.status_code,
                payload=payload,
                error=f"MusicBrainz returned HTTP {response.status_code} for {path}{body}",
            )
        return MbResponse(response.status_code, payload=payload)

    def search_releases(
        self,
        *,
        artist: str = "",
        album: str = "",
        track_count: int | None = None,
        year: int | None = None,
        limit: int = 5,
    ) -> MbResponse:
        query = build_release_query(artist, album, track_count, year)
        if not query:
            return MbResponse(
                None,
                error="a MusicBrainz release search needs at least an artist or an album title",
            )
        return self.get("/release", {"query": query, "limit": limit})

    def get_release(self, mbid: str) -> MbResponse:
        release_id = mbid.strip()
        if not release_id:
            return MbResponse(None, error="a MusicBrainz release id is required")
        return self.get(f"/release/{release_id}", {"inc": RELEASE_INCLUDES})
