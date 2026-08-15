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

`store_image` is the second lock owner and follows the same rule: it writes and
measures the file *outside* the lock, then takes it once to merge the measurement
into `dims`. Reaching for `read_index` / `replace_metadata` from inside it would
be a second acquisition of a non-reentrant lock the caller already holds.

Eviction. Only the **cache** root is bounded — TTL first, then oldest-`mtime`
first until it fits `max_bytes`; the storage root is a permanent mirror and the
sweep never looks at it. Two things about *when* it runs:

* it is **amortised behind a byte counter**, not run after every write. A cold
  40-tile gallery would otherwise stat and sort the whole cache root forty times,
  which is quadratic in cache size for no gain;
* it runs **inline**, on the storing thread. The app is `-w 1 --threads N` by
  architecture, so a background sweeper thread would buy nothing but a second
  place for the same failure to happen.

The sweep evicts image files and nothing else. An `index.json` is kilobytes, and
dropping one would discard the `dims` measured for files still on disk —
measurements that never come back, because an existing file is never measured
twice.
"""

from __future__ import annotations

import glob
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from beetdeck.utils import _image_size, log

INDEX_FILENAME = "index.json"

#: How long a stored listing is served without asking CAA again.
DEFAULT_META_TTL = 86400
#: How long a cached *file* survives; the storage root ignores this entirely.
DEFAULT_CACHE_TTL = 604800
DEFAULT_CACHE_MAX_BYTES = 512 * 1024 * 1024
#: Fraction of `max_bytes` that has to be written into the cache before a sweep
#: is worth its own cost. A sweep is O(files) in stats plus a sort, so running it
#: per tile makes a cold gallery quadratic in cache size.
_SWEEP_FRACTION = 16
#: …but never less than this, so a tiny `max_bytes` still sweeps promptly.
_MIN_SWEEP_BYTES = 1024 * 1024

STORAGE = "storage"
CACHE = "cache"

#: The sizes a stored file can be keyed by — the numeric CAA thumbnails plus the
#: original. `size` ends up in a filename, so it is checked here rather than
#: trusted; the route validates it again to turn a bad one into a `400`.
IMAGE_SIZES = ("250", "500", "1200", "full")

#: `Content-Type` → extension. Thumbnails are always JPEG; originals may be PNG
#: or GIF, so the extension cannot be assumed and the response has to say.
_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
#: Anything unrecognised is still stored — losing the bytes over a header we do
#: not know is worse than an opaque extension — but it is not given an image
#: extension it might not have.
_FALLBACK_EXTENSION = ".bin"
#: The way back: a stored file's extension is the only surviving record of the
#: `Content-Type` it arrived with. `image/jpg` is dropped from the reverse map —
#: it is an alias nobody should be served.
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
#: Extensions this store owns, so a re-store under a different `Content-Type`
#: can drop the file it replaces without ever touching a concurrent writer's
#: `.tmp` — which the `<id>-<size>.*` glob would otherwise match too.
_OWNED_EXTENSIONS = frozenset(_EXTENSIONS.values()) | {_FALLBACK_EXTENSION}

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


def sanitize_image_id(value: Any) -> str:
    """The CAA image id as a **decimal string**, or `ValueError`.

    Same canonical spelling as `beetdeck.coverart._coerce_image_id`: CAA sends a
    number, beets flex attrs are TEXT, and one representation on both sides is
    what keeps `current_image_id === image_id` true in the browser after a
    reload. Here it doubles as path-traversal protection — the id becomes a
    filename, and a value made of digits has no filesystem surface at all.
    """
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a Cover Art Archive image id")
    text = str(value if value is not None else "").strip()
    # `isdigit()` alone accepts Arabic-Indic and other non-ASCII digits, which
    # `int()` would silently transliterate into a different id than the one the
    # request asked for.
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"{value!r} is not a Cover Art Archive image id")
    return str(int(text))


def sanitize_size(value: Any) -> str:
    """One of :data:`IMAGE_SIZES` as a string, or `ValueError`."""
    text = str(value if value is not None else "").strip().lower()
    if text not in IMAGE_SIZES:
        raise ValueError(f"{value!r} is not an artwork size")
    return text


def extension_for(content_type: Any) -> str:
    """The file extension a response with this `Content-Type` is stored under."""
    text = str(content_type or "").split(";", 1)[0].strip().lower()
    return _EXTENSIONS.get(text, _FALLBACK_EXTENSION)


def content_type_for(path: str) -> str | None:
    """The `Content-Type` a stored file is served with, or `None`.

    The extension is the only record of the upstream header, so it is read back
    rather than re-guessed. `None` for a file stored under the fallback
    extension: the route lets Flask decide rather than asserting a type nobody
    reported.
    """
    return _CONTENT_TYPES.get(os.path.splitext(path)[1].lower())


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


@dataclass(frozen=True)
class StoredImage:
    """One image file on disk: where it is and which root it came from.

    `size` is `[width, height]` when the file could be measured and `None` when
    it could not — a size is only ever reported as a fact after `_image_size`
    actually read the header.
    """

    path: str
    source: str
    content_type: str | None = None
    size: tuple[int, int] | None = None


@dataclass(frozen=True)
class CacheSweep:
    """What one :meth:`ArtworkStore.sweep_cache` pass did.

    `total_bytes` is what the cache root holds *after* the pass, so a caller can
    assert the budget actually held rather than that the sweep ran.
    """

    expired: int = 0
    evicted: int = 0
    freed_bytes: int = 0
    total_bytes: int = 0


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
        sweep_every_bytes: int | None = None,
    ) -> None:
        self.storage_dir = (storage_dir or "").strip() or None
        self.cache_dir = (cache_dir or "").strip() or os.path.join(
            tempfile.gettempdir(), "beetdeck-artwork-cache"
        )
        self.meta_ttl = meta_ttl
        self.cache_ttl = cache_ttl
        self.max_bytes = max_bytes
        self.sweep_every_bytes = (
            sweep_every_bytes
            if sweep_every_bytes is not None
            else max(_MIN_SWEEP_BYTES, int(max_bytes) // _SWEEP_FRACTION)
        )

        #: Bytes written to the cache since the last sweep. Its own lock: it is
        #: touched on every cache write and has nothing to do with the index.
        self._sweep_lock = threading.Lock()
        self._unswept_bytes = 0

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

    def find_image(self, mbid: str, image_id: str, size: str) -> StoredImage | None:
        """The stored file for one image at one size, or `None` on a miss.

        Roots are consulted in :attr:`read_roots` order and the first hit wins,
        so a file the other mode wrote is still served — but it is **not**
        promoted into the write root. Copying it would be a duplicate nobody
        asked for, and every *cold* fetch already lands in the write root.
        """
        image_id = sanitize_image_id(image_id)
        size = sanitize_size(size)
        for root, kind in self.read_roots:
            pattern = os.path.join(self.release_dir(root, mbid), f"{image_id}-{size}.*")
            for path in sorted(glob.glob(pattern)):
                if os.path.splitext(path)[1].lower() not in _OWNED_EXTENSIONS:
                    continue
                if os.path.isfile(path):
                    return StoredImage(path=path, source=kind, content_type=content_type_for(path))
        return None

    def store_image(
        self,
        mbid: str,
        image_id: str,
        size: str,
        data: bytes,
        content_type: Any,
    ) -> StoredImage:
        """Write one fetched image into the write root and measure it.

        The bytes are written atomically and measured **outside** the index
        lock; the lock is then taken exactly once to merge the measurement into
        `dims`. Calling `read_index` / `replace_metadata` from here instead would
        be a second acquisition of a non-reentrant lock this method already
        holds — see the module docstring.

        CAA reports no pixel dimensions, so this is the only place they can come
        from. A file `_image_size` cannot read leaves no `dims` entry at all
        rather than a guess.
        """
        image_id = sanitize_image_id(image_id)
        size = sanitize_size(size)
        root, kind = self.write_root
        directory = self.release_dir(root, mbid)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{image_id}-{size}{extension_for(content_type)}")

        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        # A re-store whose Content-Type changed would otherwise leave the old
        # file beside the new one and let glob order decide which is served.
        for stale in glob.glob(os.path.join(directory, f"{image_id}-{size}.*")):
            if stale == path or os.path.splitext(stale)[1].lower() not in _OWNED_EXTENSIONS:
                continue
            try:
                os.unlink(stale)
            except OSError:
                pass

        measured = _image_size(path)
        if measured is not None:
            with _index_lock:
                payload, _kind = self._read_index_unlocked(mbid)
                if not isinstance(payload, dict):
                    # Dims measured before any listing was persisted: keep them
                    # anyway, with no timestamp, so the record reads as stale and
                    # the next listing refreshes it rather than serving an empty
                    # gallery from disk.
                    payload = {"fetched_at": None, "images": []}
                dims = _clean_dims(payload.get("dims"))
                dims.setdefault(image_id, {})[size] = [int(measured[0]), int(measured[1])]
                payload["dims"] = dims
                self._write_index_unlocked(mbid, payload)

        if kind == CACHE and self._note_cache_write(len(data)):
            self.sweep_cache()

        return StoredImage(
            path=path,
            source=kind,
            content_type=content_type_for(path),
            size=(int(measured[0]), int(measured[1])) if measured is not None else None,
        )

    def _note_cache_write(self, nbytes: int) -> bool:
        """Account `nbytes` written to the cache; `True` when a sweep is due.

        The counter is reset by whichever caller is handed the sweep, under the
        lock, so a burst of concurrent writers produces one sweep rather than
        one per thread that happened to cross the threshold.
        """
        with self._sweep_lock:
            self._unswept_bytes += max(0, int(nbytes))
            if self._unswept_bytes < self.sweep_every_bytes:
                return False
            self._unswept_bytes = 0
        return True

    def sweep_cache(self) -> CacheSweep:
        """Bring the **cache** root back inside its TTL and byte budget.

        TTL first (an expired file is gone regardless of pressure), then
        oldest-`mtime` first until the total fits `max_bytes`. The storage root
        is never read, let alone unlinked: it is a permanent mirror, and the
        whole point of `ARTWORK_STORAGE_DIR` is that nothing evicts from it.

        A non-positive `cache_ttl` or `max_bytes` disables that half of the
        sweep rather than expiring everything immediately — a zero budget would
        delete each tile as it landed and turn the proxy into a pass-through.
        """
        if self.cache_error is not None:
            return CacheSweep()

        entries = self._scan_cache_files()
        total = sum(size for _mtime, size, _path in entries)
        expired = 0
        freed = 0

        if self.cache_ttl and self.cache_ttl > 0:
            cutoff = time.time() - float(self.cache_ttl)
            survivors: list[tuple[float, int, str]] = []
            for mtime, size, path in entries:
                if mtime < cutoff and _unlink(path):
                    expired += 1
                    freed += size
                    total -= size
                else:
                    survivors.append((mtime, size, path))
            entries = survivors

        evicted = 0
        if self.max_bytes and self.max_bytes > 0 and total > self.max_bytes:
            for mtime, size, path in sorted(entries):
                if total <= self.max_bytes:
                    break
                if _unlink(path):
                    evicted += 1
                    freed += size
                    total -= size

        if expired or evicted:
            log.info(
                "artwork cache sweep: %d expired, %d evicted, %d bytes freed, %d bytes left",
                expired,
                evicted,
                freed,
                total,
            )
        return CacheSweep(expired=expired, evicted=evicted, freed_bytes=freed, total_bytes=total)

    def _scan_cache_files(self) -> list[tuple[float, int, str]]:
        """`(mtime, size, path)` for every image file under the cache root.

        Image files only. An `index.json` is kilobytes and carries the `dims`
        measured for files that are still here — dropping it would lose
        measurements permanently, since an existing file is never measured twice.
        """
        entries: list[tuple[float, int, str]] = []
        try:
            releases = list(os.scandir(self.cache_dir))
        except OSError as exc:
            log.warning("artwork cache dir %s is unreadable: %s", self.cache_dir, exc)
            return entries
        for release in releases:
            if not release.is_dir(follow_symlinks=False):
                continue
            try:
                files = list(os.scandir(release.path))
            except OSError:
                continue
            for entry in files:
                if os.path.splitext(entry.name)[1].lower() not in _OWNED_EXTENSIONS:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append((info.st_mtime, info.st_size, entry.path))
        return entries

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


def _unlink(path: str) -> bool:
    """Remove `path`; `False` (with a line in the log) when it could not go.

    A file that resists deletion is not a request failure — the sweep runs on
    whoever happened to store the tile that crossed the threshold — so it is
    counted as still present and the budget stays honest.
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("artwork cache entry %s could not be evicted: %s", path, exc)
        return False
    return True


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
