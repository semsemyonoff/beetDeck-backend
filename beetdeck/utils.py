"""Shared helpers and constants for beetDeek routes."""

import logging
import os
import re
import sqlite3
import sys
import threading

from flask import current_app

from beetdeck import state

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("beetdeck")


class _BeetsLogAdapter(logging.LoggerAdapter):
    """Adapter that converts beets-style {} format strings to %s for stdlib."""

    def process(self, msg, kwargs):
        if "{}" in str(msg):
            msg = str(msg).replace("{}", "%s")
        return msg, kwargs


_beets_log = _BeetsLogAdapter(log, {})

# ---------------------------------------------------------------------------
# Cover art constants
# ---------------------------------------------------------------------------
COVER_NAMES = [
    "cover.jpg",
    "cover.png",
    "cover.jpeg",
    "folder.jpg",
    "folder.png",
    "folder.jpeg",
    "front.jpg",
    "front.png",
    "front.jpeg",
]

_COVER_STEMS = {"cover", "folder", "front"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

COVER_HIRES_MAX = 1200
COVER_EMBED_MAX = 500
COVER_EMBED_QUALITY = 70

# Allowed max-side sizes for the on-the-fly cover thumbnails served by
# ``GET /api/album/<id>/cover?size=`` — bounded so the disk cache cannot be
# inflated with arbitrary dimensions.
COVER_THUMB_SIZES = (64, 128, 256, 400)
COVER_THUMB_QUALITY = 80


def _default_file_mode():
    """The mode a plain ``open()`` would produce here, i.e. 0666 minus the umask.

    Read once at import: querying the umask means temporarily clearing it, which
    would race with file creation in other threads under the threaded WSGI server.
    """
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


_FILE_MODE = _default_file_mode()

# ---------------------------------------------------------------------------
# Beets initialization state
# ---------------------------------------------------------------------------
_beets_initialized = False
_beets_init_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _decode_path(val):
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val or ""


def _resolve_path(path):
    """Decode path bytes and resolve relative paths against LIBRARY_ROOT.

    Beets 2.10.0 stores item/artpath values relative to the library root
    directory in the database. Absolute paths (pre-migration DBs) pass through
    unchanged for backward compatibility.
    """
    p = _decode_path(path)
    if not p or os.path.isabs(p):
        return p
    library_root = current_app.config.get("LIBRARY_ROOT", "")
    if library_root:
        return os.path.join(library_root, p)
    return p


def _try_write_item(item):
    """Write tags to an item's file with the path resolved against LIBRARY_ROOT.

    Beets' ``Item.try_write`` passes ``self.path`` straight to mediafile, which
    fails when paths are stored relative (the rescan preflight rewrites absolute
    item paths to relative to avoid duplicate imports on re-scan). Resolving the
    path here keeps the on-disk write working without persisting an absolute
    path back to the DB.
    """
    return item.try_write(path=_resolve_path(item.path))


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _get_ro_conn():
    library_db = current_app.config["LIBRARY_DB"]
    conn = sqlite3.connect(f"file:{library_db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.create_function("ULOWER", 1, lambda s: s.lower() if s else s)
    return conn


# ---------------------------------------------------------------------------
# Cover helpers
# ---------------------------------------------------------------------------


def _find_lrc_file(item_path):
    """Find a .lrc file next to the audio file."""
    if not item_path:
        return None
    base = os.path.splitext(item_path)[0]
    lrc = base + ".lrc"
    if os.path.isfile(lrc):
        return lrc
    return None


def _read_lrc_file(lrc_path):
    """Read and return contents of a .lrc file."""
    try:
        with open(lrc_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _find_cover(album_dir, fallback_dirs=None):
    """Find a cover image in album_dir; optionally fall back to per-disc subdirs.

    For multi-disc albums the cover sometimes lives only inside ``CD1/`` rather
    than the parent album dir. ``fallback_dirs`` (typically the ``dir`` field
    from ``_detect_disc_subdirs``) is probed in order if the primary dir has
    no cover.
    """

    def _probe(d):
        if not d or not os.path.isdir(d):
            return None
        for name in COVER_NAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
        return None

    found = _probe(album_dir)
    if found:
        return found
    if fallback_dirs:
        for d in fallback_dirs:
            if d == album_dir:
                continue
            found = _probe(d)
            if found:
                return found
    return None


def _image_size(path):
    """``(width, height)`` of the image at ``path``, or ``None`` if unreadable.

    ``Image.open`` parses only the header, so this costs one small read — cheap
    enough to answer on every album detail request. Dimensions are the one fact
    that makes "is this candidate better than the cover we have" answerable
    without downloading the image and running a tool on it.
    """
    from PIL import Image, UnidentifiedImageError

    if not path:
        return None
    try:
        with Image.open(path) as img:
            return img.size
    # DecompressionBombError needs its own arm: it derives from Exception, not
    # OSError, and Pillow raises it straight from the header parse — a corrupt
    # file whose declared dimensions exceed the bomb threshold never gets
    # decoded, so it reaches here on a path that must answer None, not 500.
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None


def _is_instrumental(value):
    """Whether a stored ``lyrics_instrumental`` attribute means "instrumental".

    beets 2.13.0's migration inserts the literal ``'1'`` while a write through
    ``types.BOOLEAN`` lands as ``1`` in a TEXT-affinity column, so the raw value
    has more than one truthy spelling. One rule, shared by every reader.
    """
    return str(value or "") in ("1", "True")


def _remove_cover_files(album_dir):
    """Remove all cover/folder/front image files from album directory."""
    if not album_dir or not os.path.isdir(album_dir):
        return
    for fname in os.listdir(album_dir):
        name_lower = fname.lower()
        stem, ext = os.path.splitext(name_lower)
        if ext not in _IMAGE_EXTS:
            continue
        base = stem.split(".")[0]
        if base in _COVER_STEMS:
            path = os.path.join(album_dir, fname)
            log.info("Removing old cover file: %s", path)
            os.remove(path)


def _resize_image(src_path, max_size, quality=95):
    """Resize image to fit max_size, return path to temp JPEG file."""
    import tempfile

    from PIL import Image

    with Image.open(src_path) as img:
        img.thumbnail((max_size, max_size), Image.LANCZOS)

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        try:
            img.save(tmp, format="JPEG", quality=quality, optimize=True)
            tmp.close()
            # NamedTemporaryFile always creates 0600 regardless of umask, and the
            # caller *moves* this file into the library as cover.jpg — where other
            # users (media servers, other containers) have to read it.
            os.chmod(tmp.name, _FILE_MODE)
        except Exception:
            tmp.close()
            os.unlink(tmp.name)
            raise
    return tmp.name


def _cover_thumbnail(src_path, size, cache_dir):
    """Return the path to a cached WebP thumbnail of ``src_path``.

    The thumbnail fits within a ``size``×``size`` box (aspect preserved) and is
    generated on first request, then cached on disk under ``cache_dir`` keyed by
    the source path, requested size and source mtime. A changed cover (new
    mtime) yields a fresh key, so stale thumbnails are simply orphaned rather
    than served. Returns ``None`` if the source cannot be read as an image.
    """
    import hashlib

    from PIL import Image, UnidentifiedImageError

    try:
        mtime = int(os.path.getmtime(src_path))
    except OSError:
        return None

    key = hashlib.sha1(f"{src_path}|{size}|{mtime}".encode()).hexdigest()
    out_path = os.path.join(cache_dir, f"{key}.webp")
    if os.path.isfile(out_path):
        return out_path

    try:
        os.makedirs(cache_dir, exist_ok=True)
        with Image.open(src_path) as img:
            img.thumbnail((size, size), Image.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")
            tmp = f"{out_path}.{threading.get_ident()}.tmp"
            img.save(tmp, format="WEBP", quality=COVER_THUMB_QUALITY, method=6)
        os.replace(tmp, out_path)
    except (OSError, UnidentifiedImageError):
        return None
    return out_path


def _save_cover_to_album(album, src_path):
    """Save hi-res cover to album dir, embed smaller version into tracks.

    1. Resize source to 1200px max, best quality -> save to album dir
    2. Embed via beets with maxwidth=500, quality=70

    For multi-disc albums (``<album>/CD1/…`` + ``<album>/CD2/…``) the cover
    is written to the common parent dir so a single ``cover.jpg`` lives at
    ``<album>/`` rather than inside one disc subdir. beets' ``set_art`` uses
    ``item_dir()`` which returns only the first item's directory, so we call
    ``art_destination`` directly with the correct common parent.
    """
    from beets import plugins
    from beets.util import bytestring_path, move
    from beets.util import unique_path as beets_unique_path
    from beetsplug._utils import art

    items = list(album.items())
    album_dir = _common_parent_dir([_resolve_path(it.path) for it in items]) if items else None
    _remove_cover_files(album_dir)

    hires_path = _resize_image(src_path, COVER_HIRES_MAX, quality=95)
    try:
        hires_bytes = bytestring_path(hires_path)
        if album_dir:
            artdest = album.art_destination(hires_bytes, item_dir=bytestring_path(album_dir))
        else:
            artdest = album.art_destination(hires_bytes)
        artdest = beets_unique_path(artdest)
        move(hires_bytes, artdest)
        album.artpath = artdest
        album.store()
    finally:
        if os.path.exists(hires_path):
            os.unlink(hires_path)

    art.embed_album(_beets_log, album, maxwidth=COVER_EMBED_MAX, quality=COVER_EMBED_QUALITY)

    # ``set_art`` is bypassed (see docstring), so emit the event it would have
    # sent — plugins such as ``permissions`` only see cover art through it. It
    # goes *after* the embed on purpose: beets' own ``embedart`` listens on
    # ``art_set`` and, when the operator enabled its ``auto``, re-embeds the
    # album and may delete the art file — which from inside our own embed would
    # be re-entrant, and could remove the cover we just wrote before we use it.
    plugins.send("art_set", album=album)


# ---------------------------------------------------------------------------
# Album directory helper
# ---------------------------------------------------------------------------


def _common_parent_dir(paths):
    """Return the common parent directory of a list of item file paths.

    Empty/None paths are filtered out. For a single path returns its dirname.
    Falls back to the first item's dirname when ``os.path.commonpath`` rejects
    mixed-root inputs (e.g. one absolute + one relative).
    """
    dirs = [os.path.dirname(p) for p in paths if p]
    dirs = [d for d in dirs if d]
    if not dirs:
        return None
    if len(dirs) == 1:
        return dirs[0]
    try:
        return os.path.commonpath(dirs)
    except ValueError:
        return dirs[0]


def _album_dirs_for_ids(conn, album_ids):
    """Return ``{album_id: (common_parent_dir, disc_subdirs)}`` for the given album ids.

    ``common_parent_dir`` is the album root (the common ancestor of all item
    paths).  ``disc_subdirs`` is a sorted list of unique per-item parent
    directories — for single-disc albums it contains only the one directory
    that equals ``common_parent_dir``; for multi-disc layouts it includes each
    per-disc dir (e.g. ``["/…/CD1", "/…/CD2"]``).  Pass ``disc_subdirs`` as
    ``fallback_dirs`` to ``_find_cover`` so that a cover stored only inside a
    disc subdir is still discovered when ``artpath`` is NULL.
    """
    if not album_ids:
        return {}
    ph = ",".join("?" * len(album_ids))
    rows = conn.execute(
        f"SELECT album_id, path FROM items WHERE album_id IN ({ph}) AND path IS NOT NULL",
        list(album_ids),
    ).fetchall()
    by_album = {}
    for r in rows:
        by_album.setdefault(r["album_id"], []).append(_resolve_path(r["path"]))
    result = {}
    for aid, paths in by_album.items():
        parent = _common_parent_dir(paths)
        disc_dirs = sorted({os.path.dirname(p) for p in paths if p})
        result[aid] = (parent, disc_dirs)
    return result


def _album_dir_from_items(conn, album_id):
    """Return the common parent directory of all items in an album.

    For single-disc albums every item shares one parent and this collapses to
    that dir. For multi-disc layouts (``<album>/CD1/…``, ``<album>/CD2/…``)
    the common parent is the album root, not one disc subdir.
    """
    rows = conn.execute(
        "SELECT path FROM items WHERE album_id = ? AND path IS NOT NULL",
        (album_id,),
    ).fetchall()
    return _common_parent_dir([_resolve_path(r["path"]) for r in rows])


def _detect_disc_subdirs(items):
    """Group album items by disc number and describe each disc's on-disk dir.

    ``items`` is any iterable of mapping-like rows exposing ``disc``, ``path``
    and (optionally) ``length`` keys — sqlite3.Row or plain dicts both work.
    Disc values of ``None`` are coerced to ``1`` here so callers never need to
    handle nulls; mixing ``None`` and ``int`` would otherwise raise a
    ``TypeError`` from ``sorted()``.

    Returns a list sorted by disc number::

        [{"disc": 1, "dir": "/…/CD1", "track_count": 13, "duration_sec": 2440},
         {"disc": 2, "dir": "/…/CD2", "track_count": 13, "duration_sec": 2467}]
    """
    by_disc = {}
    for it in items:
        disc = it["disc"] or 1
        bucket = by_disc.setdefault(disc, {"dirs": [], "count": 0, "duration": 0.0})
        bucket["count"] += 1
        try:
            bucket["duration"] += float(it["length"] or 0)
        except (KeyError, IndexError, TypeError):
            pass
        path = _resolve_path(it["path"])
        d = os.path.dirname(path) if path else ""
        if d:
            bucket["dirs"].append(d)

    out = []
    for disc in sorted(by_disc.keys()):
        b = by_disc[disc]
        if not b["dirs"]:
            disc_dir = ""
        elif len(set(b["dirs"])) == 1:
            disc_dir = b["dirs"][0]
        else:
            try:
                disc_dir = os.path.commonpath(b["dirs"])
            except ValueError:
                disc_dir = b["dirs"][0]
        out.append(
            {
                "disc": disc,
                "dir": disc_dir,
                "track_count": b["count"],
                "duration_sec": int(round(b["duration"])),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Genre formatting
# ---------------------------------------------------------------------------


def _format_genre(val):
    """Normalize a genre value (str with null-bytes or list) to a comma-separated string."""
    if isinstance(val, (list, tuple)):
        return ", ".join(str(v) for v in val)
    if isinstance(val, bytes):
        val = val.decode("utf-8", errors="replace")
    if not val:
        return ""
    val = str(val)
    parts = re.split(r"\\?\u2400|\x00", val)
    return ", ".join(p.strip() for p in parts if p.strip())


# ---------------------------------------------------------------------------
# Beets initialization
# ---------------------------------------------------------------------------


def _init_beets(library_db, library_dir=None):
    """Initialize beets config, load plugins, and return a Library instance.

    Accepts explicit library_db/library_dir paths because this function may be
    called from background threads that have no Flask app context.

    library_dir overrides beets' config `directory` — required so beets writes
    cover art and tag files to the same root that the app reads from
    (LIBRARY_ROOT / IMPORT_DIR). Without it, beets falls back to whatever is
    in the user's beets config or its built-in default, which typically does
    not match the container's mount point.
    """
    global _beets_initialized
    import beets
    import beets.library
    from beets import plugins

    with _beets_init_lock:
        if not _beets_initialized:
            beets.config.read(user=True, defaults=True)
            plugins.load_plugins()
            plugins.send("pluginload")
            _beets_initialized = True

            beets_log = logging.getLogger("beets")
            beets_log.setLevel(logging.DEBUG)

        if library_dir:
            beets.config["directory"].set(library_dir)

    return (
        beets.library.Library(library_db, directory=library_dir)
        if library_dir
        else (beets.library.Library(library_db))
    )


def _serialize_candidate(idx, album_match):
    """Serialize an AlbumMatch to a JSON-safe dict for the frontend."""
    info = album_match.info
    track_info = []
    for item, track in album_match.mapping.items():
        track_info.append(
            {
                "title": track.title,
                "track": track.index,
                "artist": track.artist or info.artist,
                "length": f"{int(track.length) // 60}:{int(track.length) % 60:02d}"
                if track.length
                else "",
                "current_title": item.title,
            }
        )

    return {
        "index": idx,
        "artist": info.artist,
        "album": info.album,
        "year": info.year,
        "label": getattr(info, "label", "") or "",
        "media": getattr(info, "media", "") or "",
        "data_source": info.data_source,
        "mb_albumid": info.album_id or "",
        "distance": round(float(album_match.distance), 4),
        "track_count": len(info.tracks),
        "tracks": sorted(track_info, key=lambda t: t.get("track", 0)),
    }


def _get_task_json(task):
    """Return JSON-serializable copy of task (without internal _-prefixed keys).

    ``matches_available`` is derived from the private ``_matches`` list because a
    consumed task is otherwise indistinguishable from a confirmable one over
    HTTP: confirming pops ``_matches`` but restores ``status: "done"`` under the
    same ``task_id``. Caller must hold ``state.identify_lock``.
    """
    data = {k: v for k, v in task.items() if not k.startswith("_")}
    data["matches_available"] = bool(task.get("_matches"))
    return data


# Body of the 409 returned when a confirm's ``expected_generation`` does not
# match the task's current one — the task was re-identified, already confirmed,
# or (for the UUID-keyed items tasks) purged.
STALE_GENERATION_ERROR = (
    "Identification results changed since the preview (re-identified or already confirmed)"
)

# Body of the 409 returned when a cover/lyrics confirm's ``expected_generation``
# does not match the stash it was fetched into — a competing fetch (the web UI,
# another agent) replaced the result, or it was already written and cleared.
STALE_STASH_ERROR = "The fetched result changed since the preview (re-fetched or already written)"


def _drop_cover_stash(album_id):
    """Discard a fetched cover candidate because its album was written directly.

    **Every** route that writes an album's cover must call this. A stash left
    behind keeps the generation it was fetched with, so a confirm holding that
    generation passes its precondition and silently reverts the write that
    overruled it — the precondition only ever sees what goes *through* the stash.

    Caller must not hold ``state.identify_lock``.
    """
    with state.identify_lock:
        state.identify_tasks.pop(f"cover_{album_id}", None)


def _drop_lyrics_stash(item_id):
    """Discard a fetched-lyrics stash because its track was written directly.

    Same contract as ``_drop_cover_stash`` — and the same obligation on every
    route that writes ``lyrics`` or a ``lyrics_*`` attribute, including the
    generic per-item tag patch.
    """
    with state.identify_lock:
        state.identify_tasks.pop(f"lyrics_{item_id}", None)


def _consume_matches(task):
    """Drop a task's candidate list and bump its ``match_generation``.

    Caller must hold ``state.identify_lock``. The bump is what makes a second
    confirm carrying the previous ``expected_generation`` fail the precondition
    instead of falling through to "Invalid candidate index".
    """
    task.pop("_matches", None)
    task["match_generation"] = state.next_match_generation()
