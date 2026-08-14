"""Cover Art Archive client — every scan a MusicBrainz release has, not just one.

Plain `httpx` against `coverartarchive.org`, the same shape as
`beetdeck_mcp/musicbrainz.py`: a dataclass client with an **injectable
`transport` and `gate`**, so the route tests drive real request construction
without network access and without waiting out the gate.

Four things this module owns:

* **The rate gate.** `RateGate` here is a deliberate ~30-line duplicate of the one
  in `beetdeck_mcp/musicbrainz.py`. It is not imported: `beetdeck_mcp` must never
  couple to the Flask package, and importing it from here would make that coupling
  real in the other direction. The duplication is the cheaper of the two costs.

  What budget does it protect? `coverartarchive.org` is MusicBrainz
  infrastructure and asks callers to be polite, but — unlike `ws/2` — it does not
  publish the 1 req/s figure. `MIN_REQUEST_INTERVAL` is therefore a **courtesy
  default, not a documented limit**.
* **What the gate does *not* cover.** Only :meth:`CaaClient.fetch_release` — the
  JSON lookup — goes through it. Image bytes are served by the archive.org CDN,
  which is outside that budget entirely; gating them would serialize a 40-tile
  gallery to ≥40 s with a request thread parked for each, on an app that is
  architecturally `-w 1 --threads N`.
* **The user agent.** `ARTWORK_USER_AGENT` supplies it (the app factory owns the
  wiring). An empty one is **not** fatal: CAA/archive.org, unlike MusicBrainz
  `ws/2`, does not reject anonymous clients, and raising here would take
  `create_app()` down for a config default.
* **Normalization.** `fetch_release` calls :func:`normalize_images` itself, so
  `CaaResult.images` is always the **internal** record — metadata plus the `urls`
  map the binary route needs to fetch a cold image. No caller ever sees the raw
  CAA payload; leaving that projection to the route would put the same
  normalization in three places and let the index and the response drift apart.

The client instance is **app-scoped** (`app.extensions["beetdeck_caa"]`). Its gate
only spaces requests if every request thread shares one instance — a per-request
factory hands each thread a fresh gate with a clean timestamp, and the spacing
contract silently does nothing.

Like every other outbound client in this codebase, nothing here raises and
nothing retries in a loop: a failed round trip comes back as data.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from beetdeck.utils import log

CAA_BASE_URL = "https://coverartarchive.org"

#: Courtesy spacing for the JSON lookup — see the module docstring; CAA publishes
#: no rate limit of its own.
MIN_REQUEST_INTERVAL = 1.0

#: A release lookup is a single indexed read; an image is a file off a CDN.
DEFAULT_TIMEOUT = 20.0
DEFAULT_CONNECT_TIMEOUT = 5.0


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
class CaaResult:
    """One release lookup, including the ones that never got there.

    `status_code` is `None` exactly when the request failed in transport.
    `not_found` is a **fact about the release** — CAA answers `404` for "no art
    here", which is not an error and must never collapse into one.
    """

    status_code: int | None
    #: Internal records (see `normalize_images`), never the raw CAA payload.
    images: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    not_found: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def message(self) -> str:
        """A one-line reason, suitable for a `502` body."""
        if self.error is not None:
            return self.error
        if self.not_found:
            return "the Cover Art Archive has no images for this release"
        return f"the Cover Art Archive returned HTTP {self.status_code}"


@dataclass(frozen=True)
class CaaImageResult:
    """One image download. `data`/`content_type` are set exactly when `ok`."""

    status_code: int | None
    data: bytes | None = None
    content_type: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.data is not None

    @property
    def message(self) -> str:
        if self.error is not None:
            return self.error
        return f"the Cover Art Archive returned HTTP {self.status_code}"


def _coerce_image_id(value: Any) -> str | None:
    """A CAA image id as a **decimal string**, or `None` when it is not one.

    CAA sends a number and beets flex attrs are TEXT; one canonical spelling on
    both sides is what keeps `current_image_id === image_id` true in the browser
    after a reload.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip().isdigit():
        return str(int(value.strip()))
    return None


def normalize_images(payload: Any) -> list[dict[str, Any]]:
    """The CAA payload projected onto the **internal** record.

    Keeping the URLs is not optional: the binary route has no other way to fetch a
    cold image, and rebuilding CAA URLs by convention would have to guess the
    original's extension.

    `urls` is keyed by the **numeric** thumbnail sizes plus `"full"` (the image's
    own `image` URL). The `small` / `large` aliases CAA also ships are discarded —
    `thumb_sizes` is derived from these keys, and advertising an alias would make
    the binary route reject its own advertised size with a `400`.
    """
    images = payload.get("images") if isinstance(payload, dict) else None
    if not isinstance(images, list):
        return []

    records: list[dict[str, Any]] = []
    for entry in images:
        if not isinstance(entry, dict):
            continue
        image_id = _coerce_image_id(entry.get("id"))
        if image_id is None:
            continue

        urls: dict[str, str] = {}
        thumbnails = entry.get("thumbnails")
        if isinstance(thumbnails, dict):
            for key, url in thumbnails.items():
                if not isinstance(url, str) or not url:
                    continue
                size = str(key).strip()
                if not size.isdigit():
                    continue
                urls[str(int(size))] = url
        original = entry.get("image")
        if isinstance(original, str) and original:
            urls["full"] = original

        comment = entry.get("comment")
        types = entry.get("types")
        records.append(
            {
                "image_id": image_id,
                "types": [t for t in types if isinstance(t, str)]
                if isinstance(types, list)
                else [],
                "front": bool(entry.get("front")),
                "back": bool(entry.get("back")),
                "approved": bool(entry.get("approved")),
                "comment": comment if isinstance(comment, str) else "",
                "urls": urls,
            }
        )
    return records


@dataclass
class CaaClient:
    """Blocking Cover Art Archive client; the JSON lookup sits behind the gate.

    `transport` and `gate` are injectable so the tests can drive the real request
    construction without network access and without waiting out the gate.
    """

    user_agent: str = ""
    base_url: str = CAA_BASE_URL
    transport: httpx.BaseTransport | None = None
    timeout: float = DEFAULT_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    gate: RateGate | None = None
    _client: httpx.Client = field(init=False, repr=False)
    _gate: RateGate = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # No user-agent check on purpose — see the module docstring.
        self._gate = self.gate if self.gate is not None else RateGate()
        headers = {"Accept": "application/json"}
        if self.user_agent.strip():
            headers["User-Agent"] = self.user_agent.strip()
        self._client = httpx.Client(
            base_url=self.base_url,
            transport=self.transport,
            # CAA 307-redirects the JSON to archive.org and the images to a CDN
            # host; without this every call comes back as an empty redirect.
            follow_redirects=True,
            timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CaaClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def fetch_release(self, mbid: str) -> CaaResult:
        """The release's images, normalized. Gated. Never raises, never retries."""
        release_id = (mbid or "").strip()
        if not release_id:
            return CaaResult(None, error="a MusicBrainz release id is required")

        path = f"/release/{release_id}"
        self._gate.wait()
        # One line per outbound request: the only way to observe that a second
        # gallery open was served from disk without going to the network.
        log.info("CAA GET %s", f"{self.base_url}{path}")
        try:
            response = self._client.get(path)
        except httpx.TimeoutException as exc:
            return CaaResult(None, error=f"the Cover Art Archive timed out ({path}): {exc}")
        except httpx.InvalidURL as exc:
            # `InvalidURL` does not inherit from `HTTPError`; an mbid carrying a
            # control character would otherwise raise out of a never-raise method.
            return CaaResult(None, error=f"{release_id!r} is not a usable release id: {exc}")
        except httpx.HTTPError as exc:
            return CaaResult(None, error=f"the Cover Art Archive is unreachable ({path}): {exc}")

        if response.status_code == 404:
            # Data, not an error: this release simply has no art.
            return CaaResult(response.status_code, not_found=True)

        if response.status_code >= 300:
            return CaaResult(
                response.status_code,
                error=(f"the Cover Art Archive returned HTTP {response.status_code} for {path}"),
            )

        try:
            payload = response.json()
        except ValueError:
            return CaaResult(
                response.status_code,
                error=f"the Cover Art Archive returned a non-JSON body for {path}",
            )
        return CaaResult(response.status_code, images=normalize_images(payload))

    def fetch_image(self, url: str) -> CaaImageResult:
        """One image off the archive.org CDN. **Not** gated — see the docstring."""
        target = (url or "").strip()
        if not target:
            return CaaImageResult(None, error="an image URL is required")

        log.info("CAA GET %s", target)
        try:
            response = self._client.get(target, headers={"Accept": "image/*"})
        except httpx.TimeoutException as exc:
            return CaaImageResult(None, error=f"the image request timed out ({target}): {exc}")
        except httpx.InvalidURL as exc:
            return CaaImageResult(None, error=f"{target!r} is not a usable image URL: {exc}")
        except httpx.HTTPError as exc:
            return CaaImageResult(None, error=f"the image is unreachable ({target}): {exc}")

        if response.status_code >= 300:
            return CaaImageResult(
                response.status_code,
                error=f"the Cover Art Archive returned HTTP {response.status_code} for {target}",
            )
        if not response.content:
            return CaaImageResult(response.status_code, error=f"empty image body for {target}")

        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
        return CaaImageResult(
            response.status_code, data=response.content, content_type=content_type
        )
