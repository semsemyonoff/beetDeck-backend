"""Artwork disk store — the roots and the per-release index.

Two roots, one shape. `<root>/<mb_albumid>/index.json` plus the image files that
belong to that release, laid out identically under the storage root and under the
cache root:

* the **write root** is the storage dir when `ARTWORK_STORAGE_DIR` is configured
  and usable, otherwise the cache dir — every write goes to exactly one root, so
  nothing is ever duplicated;
* the **read roots** are both, storage first — flipping storage on or off never
  orphans what the other mode already wrote, but nothing is ever *promoted*
  between them either, which would be a copy nobody asked for.

A configured storage root that cannot be written to is neither a silent fallback
nor a `500` per tile: it is logged once at ERROR, dropped from both the write and
the read side (a root we cannot write is not one whose stale index should win a
read), and surfaced as :attr:`ArtworkStore.storage_error` for the listing
response.

**The index holds the internal records** — metadata *plus* the `urls` map, which
is the only thing that makes a cold image fetch possible at all. The public
projection that strips `urls` happens in the route, never here.

Locking. `_index_lock` is module-level, following `_fetchart_plugin_lock`
(`routes/cover.py`). The protocol around it is the part worth reading twice:

* the primitives (`_read_index_unlocked` / `_write_index_unlocked`) take **no**
  lock and are private;
* every public operation acquires the lock **exactly once** and owns it for the
  whole compound read-modify-write;
* therefore **a public operation must never call another public operation** —
  `threading.Lock` is not reentrant and that is a self-deadlock, not a slow path.

`replace_metadata` merges rather than overwrites for the same reason the lock is
held across the whole operation: a metadata refresh runs while a tile download is
recording the dimensions it just measured, and an overwrite would drop those
permanently — the image file already exists, so it is never measured again.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from beetdeck.utils import log

INDEX_FILENAME = "index.json"

#: How long a stored listing is served without asking CAA again.
DEFAULT_META_TTL = 86400
#: How long a cached *file* survives; the storage root ignores this entirely.
DEFAULT_CACHE_TTL = 604800
DEFAULT_CACHE_MAX_BYTES = 512 * 1024 * 1024

STORAGE = "storage"
CACHE = "cache"

#: A release id is turned into a directory name, so it is checked rather than
#: trusted. The value comes from the beets DB, which is not a trust boundary the
#: filesystem knows about.
_MBID_RE = re.compile(r"\A[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")

#: One lock for every index operation in the process (precedent:
#: `_fetchart_plugin_lock`, `routes/cover.py:45`). Non-reentrant on purpose — see
#: the module docstring.
_index_lock = threading.Lock()


def sanitize_mbid(value: Any) -> str:
    """The release id as a lowercase UUID string, or `ValueError`.

    Raising is the right answer here: every caller has already established that
    the album *has* an `mb_albumid`, so a value that is not a release id means the
    row is corrupt, not that the gallery is empty.
    """
    text = str(value or "").strip()
    if not _MBID_RE.match(text):
        raise ValueError(f"{value!r} is not a MusicBrainz release id")
    return text.lower()


def utc_now_iso() -> str:
    """`2026-08-14T12:00:00Z` — the spelling `index.json` stores."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class ArtworkIndex:
    """One release's `index.json`, as read from disk.

    `images` are the **internal** records (`urls` included). `fresh` answers the
    TTL question at read time so the caller does not have to re-derive it, but the
    record is returned either way — a stale index still carries the URLs a binary
    fetch needs.
    """

    mbid: str
    source: str
    fetched_at: str | None = None
    images: list[dict[str, Any]] = field(default_factory=list)
    dims: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    fresh: bool = False


class ArtworkStore:
    """The two roots and the index operations over them."""

    def __init__(
        self,
        storage_dir: str | None = None,
        cache_dir: str | None = None,
        *,
        meta_ttl: float = DEFAULT_META_TTL,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        max_bytes: int = DEFAULT_CACHE_MAX_BYTES,
    ) -> None:
        self.storage_dir = (storage_dir or "").strip() or None
        self.cache_dir = (cache_dir or "").strip() or os.path.join(
            tempfile.gettempdir(), "beetdeck-artwork-cache"
        )
        self.meta_ttl = meta_ttl
        self.cache_ttl = cache_ttl
        self.max_bytes = max_bytes

        self.storage_error: str | None = None
        self.cache_error: str | None = None

        if self.storage_dir is not None:
            self.storage_error = _prepare_root(self.storage_dir)
            if self.storage_error is not None:
                log.error(
                    "artwork storage dir %s is unusable, falling back to the cache: %s",
                    self.storage_dir,
                    self.storage_error,
                )
        self.cache_error = _prepare_root(self.cache_dir)
        if self.cache_error is not None:
            log.error("artwork cache dir %s is unusable: %s", self.cache_dir, self.cache_error)

    @property
    def storage_enabled(self) -> bool:
        """Whether `ARTWORK_STORAGE_DIR` is configured — not whether it works."""
        return self.storage_dir is not None

    @property
    def storage_usable(self) -> bool:
        return self.storage_dir is not None and self.storage_error is None

    @property
    def write_root(self) -> tuple[str, str]:
        """`(path, kind)` of the single root every write goes to."""
        if self.storage_usable:
            return self.storage_dir, STORAGE  # type: ignore[return-value]
        return self.cache_dir, CACHE

    @property
    def read_roots(self) -> list[tuple[str, str]]:
        """`(path, kind)` pairs in lookup order, storage first.

        An unusable storage root is left out of *reads* too: a root we cannot
        write is a root whose index can only go stale, and letting it win the
        lookup would pin the gallery to whatever it happened to contain.
        """
        roots: list[tuple[str, str]] = []
        if self.storage_usable:
            roots.append((self.storage_dir, STORAGE))  # type: ignore[arg-type]
        roots.append((self.cache_dir, CACHE))
        return roots

    def release_dir(self, root: str, mbid: str) -> str:
        return os.path.join(root, sanitize_mbid(mbid))

    def _index_path(self, root: str, mbid: str) -> str:
        return os.path.join(self.release_dir(root, mbid), INDEX_FILENAME)

    def _read_index_unlocked(self, mbid: str) -> tuple[dict[str, Any] | None, str | None]:
        """The raw index payload and the root it came from. **Takes no lock.**"""
        for root, kind in self.read_roots:
            path = self._index_path(root, mbid)
            try:
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                # A truncated or unreadable index is a cache miss, not a 500 —
                # the next refresh overwrites it.
                log.warning("artwork index %s is unreadable: %s", path, exc)
                continue
            if isinstance(payload, dict):
                return payload, kind
        return None, None

    def _write_index_unlocked(self, mbid: str, payload: dict[str, Any]) -> str:
        """Atomically replace the index in the write root. **Takes no lock.**"""
        root, _kind = self.write_root
        directory = self.release_dir(root, mbid)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, INDEX_FILENAME)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, path)
        except OSError:
            # Propagate the failure, but do not leave the partial tmp behind.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    def read_index(self, mbid: str) -> ArtworkIndex | None:
        """The stored listing, or `None` when no root has one.

        `fresh` is computed against `meta_ttl`; a stale index is still returned,
        because the binary route needs its `urls` map regardless of age.
        """
        with _index_lock:
            payload, kind = self._read_index_unlocked(mbid)
        if payload is None or kind is None:
            return None
        return self._to_index(mbid, kind, payload)

    def replace_metadata(
        self,
        mbid: str,
        images: list[dict[str, Any]],
        *,
        fetched_at: str | None = None,
    ) -> ArtworkIndex:
        """Persist a fresh listing, **keeping the dims already on disk**.

        The existing `dims` are re-read under the same lock that writes, so a
        `store_image` that measured a file a moment ago does not lose it. One
        acquisition, one owner — this method must not call `read_index`.
        """
        stamp = fetched_at or utc_now_iso()
        with _index_lock:
            existing, _kind = self._read_index_unlocked(mbid)
            dims = _clean_dims(existing.get("dims") if existing else None)
            payload = {"fetched_at": stamp, "images": list(images), "dims": dims}
            self._write_index_unlocked(mbid, payload)
        _root, write_kind = self.write_root
        return self._to_index(mbid, write_kind, payload)

    def _to_index(self, mbid: str, source: str, payload: dict[str, Any]) -> ArtworkIndex:
        fetched_at = payload.get("fetched_at")
        images = payload.get("images")
        return ArtworkIndex(
            mbid=sanitize_mbid(mbid),
            source=source,
            fetched_at=fetched_at if isinstance(fetched_at, str) else None,
            images=[entry for entry in images if isinstance(entry, dict)]
            if isinstance(images, list)
            else [],
            dims=_clean_dims(payload.get("dims")),
            fresh=self._is_fresh(fetched_at),
        )

    def _is_fresh(self, fetched_at: Any) -> bool:
        stamp = _parse_iso(fetched_at)
        if stamp is None:
            # No usable timestamp: treat it as expired rather than as eternal.
            return False
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if age < 0:
            # Clock skew, not expiry — a listing from the future is not stale.
            return True
        return age < self.meta_ttl


def _prepare_root(path: str) -> str | None:
    """Create `path` and prove it is writable; the error message, or `None`."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return str(exc)
    probe = os.path.join(path, f".write-probe.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("")
    except OSError as exc:
        return str(exc)
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass
    return None


def _clean_dims(value: Any) -> dict[str, dict[str, list[int]]]:
    """`{image_id: {size: [w, h]}}` with everything unrecognisable dropped."""
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, dict[str, list[int]]] = {}
    for image_id, sizes in value.items():
        if not isinstance(sizes, dict):
            continue
        per_image: dict[str, list[int]] = {}
        for size, pair in sizes.items():
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                try:
                    per_image[str(size)] = [int(pair[0]), int(pair[1])]
                except (TypeError, ValueError):
                    continue
        if per_image:
            cleaned[str(image_id)] = per_image
    return cleaned
