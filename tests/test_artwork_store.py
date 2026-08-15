"""Artwork disk store — roots, the per-release index, and the lock protocol.

Everything here runs against real directories under `tmp_path`; there is nothing
to mock, and mocking the filesystem would hide exactly the failures this store
exists to survive (an unwritable root, a truncated index, two threads on one
file).

Two of these tests assert on the *protocol* rather than on a result:

* `test_public_index_operations_never_reenter_the_lock` — a re-entrant public
  call deadlocks on a non-reentrant `threading.Lock`, and a deadlock that hangs
  the suite is indistinguishable from a slow machine. Every such call is made
  from a worker thread with a join timeout, so the failure mode is a red test.
* `test_replace_metadata_reads_the_dims_it_merges_under_the_lock` — the merge is
  the whole reason `replace_metadata` re-reads under the lock, and *when* it
  reads is the only observable part of it; a read taken before the acquire
  passes every other test in this file.
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from beetdeck import artwork_store as store_mod
from beetdeck.artwork_store import (
    CACHE,
    INDEX_FILENAME,
    STORAGE,
    ArtworkStore,
    content_type_for,
    extension_for,
    sanitize_image_id,
    sanitize_mbid,
    sanitize_size,
    utc_now_iso,
)

MBID = "11111111-2222-3333-4444-555555555555"
OTHER_MBID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def record(image_id="1234567890", *, size="250"):
    """One internal record — metadata plus the `urls` map the store must keep."""
    return {
        "image_id": image_id,
        "types": ["Front"],
        "front": True,
        "back": False,
        "approved": True,
        "comment": "",
        "urls": {
            size: f"https://coverartarchive.org/release/{MBID}/{image_id}-{size}.jpg",
            "full": f"https://coverartarchive.org/release/{MBID}/{image_id}.jpg",
        },
    }


def make_store(tmp_path, *, storage=True, **kwargs):
    return ArtworkStore(
        storage_dir=str(tmp_path / "storage") if storage else None,
        cache_dir=str(tmp_path / "cache"),
        **kwargs,
    )


def index_file(root, mbid=MBID):
    return os.path.join(str(root), mbid, INDEX_FILENAME)


def write_index(root, payload, mbid=MBID):
    """Plant an index directly, bypassing the store's own write path."""
    path = index_file(root, mbid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def iso_ago(seconds):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def image_bytes(width=250, height=250, fmt="JPEG"):
    """Real encoded image bytes — `_image_size` parses a header, not a mock."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format=fmt)
    return buffer.getvalue()


def plant_image(root, name, mbid=MBID, data=b"planted"):
    """Drop a file directly into a root, bypassing the store's write path."""
    path = os.path.join(str(root), mbid, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def run_with_timeout(func, timeout=5.0):
    """Run `func` in a thread; fail (never hang) if it does not come back."""
    box = {}

    def target():
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"{func} did not return within {timeout}s — deadlock"
    if "error" in box:
        raise box["error"]
    return box.get("value")


# --------------------------------------------------------------------------- #
# sanitize_mbid
# --------------------------------------------------------------------------- #


def test_sanitize_mbid_lowercases_a_valid_release_id():
    assert sanitize_mbid(MBID.upper()) == MBID


@pytest.mark.parametrize(
    "value",
    ["", None, "   ", "../../etc", "not-a-uuid", f"{MBID}/../..", f"{MBID}x", f"{MBID}/x"],
)
def test_sanitize_mbid_rejects_anything_that_is_not_a_release_id(value):
    with pytest.raises(ValueError):
        sanitize_mbid(value)


@pytest.mark.parametrize("value", [f" {MBID} ", f"{MBID}\n", f"\t{MBID}"])
def test_sanitize_mbid_tolerates_surrounding_whitespace(value):
    """Padding is normalised away, not rejected.

    What comes back is still the release id and nothing else, so there is no
    filesystem surface in it — and refusing a row over a stray newline would take
    the gallery down for a value that is unambiguous.
    """
    assert sanitize_mbid(value) == MBID


def test_store_refuses_to_build_a_path_from_a_bad_mbid(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.read_index("../../etc/passwd")


# --------------------------------------------------------------------------- #
# roots
# --------------------------------------------------------------------------- #


def test_roots_are_created_on_construction(tmp_path):
    store = make_store(tmp_path)
    assert os.path.isdir(tmp_path / "storage")
    assert os.path.isdir(tmp_path / "cache")
    assert store.storage_error is None
    assert store.cache_error is None


def test_storage_is_the_write_root_when_configured(tmp_path):
    store = make_store(tmp_path)
    assert store.storage_enabled is True
    assert store.write_root == (str(tmp_path / "storage"), STORAGE)
    assert [kind for _path, kind in store.read_roots] == [STORAGE, CACHE]


def test_cache_is_the_write_root_when_storage_is_off(tmp_path):
    store = make_store(tmp_path, storage=False)
    assert store.storage_enabled is False
    assert store.storage_error is None
    assert store.write_root == (str(tmp_path / "cache"), CACHE)
    assert [kind for _path, kind in store.read_roots] == [CACHE]


def test_an_empty_storage_dir_string_leaves_storage_disabled(tmp_path):
    store = ArtworkStore(storage_dir="   ", cache_dir=str(tmp_path / "cache"))
    assert store.storage_enabled is False


def test_an_unusable_storage_root_falls_back_and_reports_itself(tmp_path, caplog):
    # A regular file where the root should be: `makedirs` cannot turn it into a
    # directory for any uid, so this holds even when the suite runs as root.
    blocker = tmp_path / "storage"
    blocker.write_text("not a directory")

    with caplog.at_level("ERROR", logger="beetdeck"):
        store = make_store(tmp_path)

    assert store.storage_enabled is True
    assert store.storage_error is not None
    assert store.storage_usable is False
    assert store.write_root == (str(tmp_path / "cache"), CACHE)
    # Dropped from reads too: a root we cannot write can only serve a stale index.
    assert [kind for _path, kind in store.read_roots] == [CACHE]
    assert any("unusable" in message for message in caplog.messages)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores mode bits")
def test_a_read_only_storage_root_is_detected_by_the_write_probe(tmp_path):
    root = tmp_path / "storage"
    root.mkdir(mode=0o500)
    try:
        store = ArtworkStore(storage_dir=str(root), cache_dir=str(tmp_path / "cache"))
        assert store.storage_error is not None
        assert store.write_root[1] == CACHE
    finally:
        root.chmod(0o700)


def test_the_write_probe_leaves_nothing_behind(tmp_path):
    make_store(tmp_path)
    assert os.listdir(tmp_path / "storage") == []


# --------------------------------------------------------------------------- #
# read_index
# --------------------------------------------------------------------------- #


def test_read_index_returns_none_when_no_root_has_one(tmp_path):
    assert make_store(tmp_path).read_index(MBID) is None


def test_read_index_returns_the_internal_records_with_their_urls(tmp_path):
    store = make_store(tmp_path)
    store.replace_metadata(MBID, [record()])

    index = store.read_index(MBID)
    assert index is not None
    assert index.source == STORAGE
    assert index.fresh is True
    assert index.images[0]["urls"]["full"].endswith("1234567890.jpg")
    assert index.images[0]["image_id"] == "1234567890"


def test_read_index_prefers_storage_over_cache(tmp_path):
    store = make_store(tmp_path)
    write_index(tmp_path / "cache", {"fetched_at": utc_now_iso(), "images": [record("111")]})
    write_index(tmp_path / "storage", {"fetched_at": utc_now_iso(), "images": [record("222")]})

    index = store.read_index(MBID)
    assert index.source == STORAGE
    assert [image["image_id"] for image in index.images] == ["222"]


def test_read_index_still_finds_what_the_other_mode_wrote(tmp_path):
    """Storage turned on later must not orphan the cache's index."""
    write_index(tmp_path / "cache", {"fetched_at": utc_now_iso(), "images": [record("111")]})
    store = make_store(tmp_path)

    index = store.read_index(MBID)
    assert index.source == CACHE
    assert [image["image_id"] for image in index.images] == ["111"]
    # …and it is not copied into storage behind the caller's back.
    assert not os.path.exists(index_file(tmp_path / "storage"))


def test_read_index_reports_stale_after_the_meta_ttl(tmp_path):
    store = make_store(tmp_path, meta_ttl=60)
    write_index(
        tmp_path / "storage",
        {"fetched_at": iso_ago(3600), "images": [record()], "dims": {}},
    )

    index = store.read_index(MBID)
    # Stale, but still returned — the binary route needs the urls at any age.
    assert index.fresh is False
    assert index.images[0]["urls"]


def test_read_index_reports_fresh_inside_the_meta_ttl(tmp_path):
    store = make_store(tmp_path, meta_ttl=3600)
    write_index(tmp_path / "storage", {"fetched_at": iso_ago(60), "images": [record()]})
    assert store.read_index(MBID).fresh is True


@pytest.mark.parametrize("stamp", [None, "", "yesterday", 12345])
def test_an_unusable_timestamp_reads_as_stale(tmp_path, stamp):
    store = make_store(tmp_path)
    write_index(tmp_path / "storage", {"fetched_at": stamp, "images": [record()]})
    index = store.read_index(MBID)
    assert index.fresh is False
    assert index.fetched_at == (stamp if isinstance(stamp, str) else None)


def test_a_corrupt_index_reads_as_a_miss_and_is_logged(tmp_path, caplog):
    store = make_store(tmp_path)
    path = index_file(tmp_path / "storage")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ truncated")

    with caplog.at_level("WARNING", logger="beetdeck"):
        assert store.read_index(MBID) is None
    assert any("unreadable" in message for message in caplog.messages)


def test_a_corrupt_storage_index_falls_through_to_the_cache(tmp_path):
    store = make_store(tmp_path)
    path = index_file(tmp_path / "storage")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("nonsense")
    write_index(tmp_path / "cache", {"fetched_at": utc_now_iso(), "images": [record("777")]})

    index = store.read_index(MBID)
    assert index.source == CACHE
    assert [image["image_id"] for image in index.images] == ["777"]


# --------------------------------------------------------------------------- #
# replace_metadata
# --------------------------------------------------------------------------- #


def test_replace_metadata_writes_into_the_write_root_only(tmp_path):
    store = make_store(tmp_path)
    index = store.replace_metadata(MBID, [record()])

    assert index.source == STORAGE
    assert os.path.exists(index_file(tmp_path / "storage"))
    assert not os.path.exists(index_file(tmp_path / "cache"))


def test_replace_metadata_writes_to_the_cache_when_storage_is_off(tmp_path):
    store = make_store(tmp_path, storage=False)
    index = store.replace_metadata(MBID, [record()])

    assert index.source == CACHE
    assert os.path.exists(index_file(tmp_path / "cache"))


def test_replace_metadata_stamps_fetched_at_and_reports_fresh(tmp_path):
    store = make_store(tmp_path)
    index = store.replace_metadata(MBID, [record()])

    assert index.fresh is True
    assert index.fetched_at.endswith("Z")
    assert store.read_index(MBID).fetched_at == index.fetched_at


def test_replace_metadata_accepts_an_explicit_timestamp(tmp_path):
    store = make_store(tmp_path, meta_ttl=60)
    index = store.replace_metadata(MBID, [record()], fetched_at=iso_ago(3600))
    assert index.fresh is False


def test_replace_metadata_keeps_the_dims_already_on_disk(tmp_path):
    store = make_store(tmp_path)
    write_index(
        tmp_path / "storage",
        {
            "fetched_at": iso_ago(3600),
            "images": [record("111")],
            "dims": {"111": {"full": [1425, 1425]}},
        },
    )

    index = store.replace_metadata(MBID, [record("222")])
    assert [image["image_id"] for image in index.images] == ["222"]
    assert index.dims == {"111": {"full": [1425, 1425]}}
    assert store.read_index(MBID).dims == {"111": {"full": [1425, 1425]}}


def test_replace_metadata_reads_the_dims_it_merges_under_the_lock(tmp_path):
    """The merge re-reads *inside* the lock, not from a copy taken before it.

    Racing an actual concurrent writer is impossible here and would prove
    nothing: every writer takes the same lock, so nothing can land between this
    operation's read and its write. What *is* observable — and what the merge
    depends on — is that the read happens after the lock is granted. The lock is
    held on the main thread while a worker calls `replace_metadata`; the worker
    must not have touched the index yet, and the dims planted before the release
    must still survive the write that follows.

    The genuine `store_image` barrier test belongs to Task 4, where there is a
    second lock-taking writer to race against.
    """
    store = make_store(tmp_path)
    store.replace_metadata(MBID, [record("111")])

    reads: list[str] = []
    original_read = store._read_index_unlocked

    def read_with_a_witness(mbid):
        reads.append(threading.current_thread().name)
        return original_read(mbid)

    store._read_index_unlocked = read_with_a_witness

    worker = threading.Thread(
        target=lambda: store.replace_metadata(MBID, [record("222")]),
        name="racer",
        daemon=True,
    )

    with store_mod._index_lock:
        worker.start()
        # Bounded wait: had the read been outside the lock, it would have run.
        worker.join(0.2)
        assert reads == [], "replace_metadata read the index before taking the lock"
        planted = json.loads(open(index_file(tmp_path / "storage"), encoding="utf-8").read())
        planted["dims"] = {"111": {"250": [250, 250]}}
        write_index(tmp_path / "storage", planted)

    worker.join(5)
    assert not worker.is_alive(), "replace_metadata never acquired the lock"

    # The metadata refresh landed…
    assert reads == ["racer"]
    index = store.read_index(MBID)
    assert [image["image_id"] for image in index.images] == ["222"]
    # …and the dims it never saw at call time survived, because it re-read them.
    assert index.dims == {"111": {"250": [250, 250]}}


def test_replace_metadata_drops_unrecognisable_dims(tmp_path):
    store = make_store(tmp_path)
    write_index(
        tmp_path / "storage",
        {
            "fetched_at": utc_now_iso(),
            "images": [],
            "dims": {"111": {"full": ["a", "b"], "250": [250, 250]}, "222": "nonsense"},
        },
    )
    index = store.replace_metadata(MBID, [record()])
    assert index.dims == {"111": {"250": [250, 250]}}


def test_the_index_write_is_atomic_and_leaves_no_tmp_files(tmp_path):
    store = make_store(tmp_path)
    store.replace_metadata(MBID, [record()])
    entries = os.listdir(os.path.join(str(tmp_path / "storage"), MBID))
    assert entries == [INDEX_FILENAME]


def test_releases_do_not_share_an_index(tmp_path):
    store = make_store(tmp_path)
    store.replace_metadata(MBID, [record("111")])
    store.replace_metadata(OTHER_MBID, [record("222")])

    assert [i["image_id"] for i in store.read_index(MBID).images] == ["111"]
    assert [i["image_id"] for i in store.read_index(OTHER_MBID).images] == ["222"]


# --------------------------------------------------------------------------- #
# the lock protocol
# --------------------------------------------------------------------------- #


def test_public_index_operations_never_reenter_the_lock(tmp_path):
    """Each public operation takes the lock once and gives it back.

    Run off the main thread with a join timeout, so a self-deadlock fails the
    suite instead of hanging it.
    """
    store = make_store(tmp_path)

    for operation in (
        lambda: store.replace_metadata(MBID, [record()]),
        lambda: store.read_index(MBID),
        lambda: store.replace_metadata(MBID, [record("999")]),
    ):
        run_with_timeout(operation)
        acquired = store_mod._index_lock.acquire(blocking=False)
        assert acquired, "the index lock was still held after the operation returned"
        store_mod._index_lock.release()


def test_the_index_lock_is_not_reentrant(tmp_path):
    """Pins the assumption the protocol is built on, so a swap to `RLock` — which
    would make the "one owner per compound operation" rule unenforceable — is a
    deliberate change and not an accident."""
    assert store_mod._index_lock.acquire(blocking=False)
    try:
        assert store_mod._index_lock.acquire(blocking=False) is False
    finally:
        store_mod._index_lock.release()


def test_concurrent_metadata_replacements_leave_a_parseable_index(tmp_path):
    store = make_store(tmp_path)
    errors = []
    barrier = threading.Barrier(8)

    def worker(n):
        try:
            barrier.wait(timeout=5)
            store.replace_metadata(MBID, [record(str(n))])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()

    assert errors == []
    index = store.read_index(MBID)
    assert len(index.images) == 1
    assert index.images[0]["image_id"] in {str(n) for n in range(8)}


# --------------------------------------------------------------------------- #
# sanitize_image_id / sanitize_size / extensions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1234567890", "1234567890"),
        (1234567890, "1234567890"),
        (" 1234567890 ", "1234567890"),
        ("0001234", "1234"),
        (0, "0"),
    ],
)
def test_sanitize_image_id_returns_a_decimal_string(value, expected):
    """One canonical spelling on both sides of the boundary.

    CAA sends a number and beets flex attrs are TEXT; anything less than a single
    representation makes `current_image_id === image_id` false in the browser
    after a reload and the *Current cover* marker vanish.
    """
    result = sanitize_image_id(value)
    assert result == expected
    assert isinstance(result, str)


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "../../etc", "12a", "12.0", "-12", "1234/../..", True, "٣", "1_2"],
)
def test_sanitize_image_id_rejects_anything_that_is_not_a_number(value):
    """The id becomes a filename, so the digits check is also the path guard."""
    with pytest.raises(ValueError):
        sanitize_image_id(value)


@pytest.mark.parametrize(("value", "expected"), [("250", "250"), (500, "500"), ("FULL", "full")])
def test_sanitize_size_accepts_the_known_sizes(value, expected):
    assert sanitize_size(value) == expected


@pytest.mark.parametrize("value", ["", None, "small", "large", "1000", "../250", "full.jpg"])
def test_sanitize_size_rejects_everything_else(value):
    with pytest.raises(ValueError):
        sanitize_size(value)


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [
        ("image/jpeg", ".jpg"),
        ("image/jpeg; charset=binary", ".jpg"),
        ("IMAGE/JPEG", ".jpg"),
        ("image/jpg", ".jpg"),
        ("image/png", ".png"),
        ("image/gif", ".gif"),
        ("image/webp", ".webp"),
        ("application/octet-stream", ".bin"),
        ("", ".bin"),
        (None, ".bin"),
    ],
)
def test_extension_for_maps_the_content_type(content_type, extension):
    assert extension_for(content_type) == extension


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/x/1-250.jpg", "image/jpeg"),
        ("/x/1-full.PNG", "image/png"),
        ("/x/1-full.gif", "image/gif"),
        ("/x/1-full.bin", None),
        ("/x/1-full", None),
    ],
)
def test_content_type_for_reads_the_extension_back(path, content_type):
    assert content_type_for(path) == content_type


# --------------------------------------------------------------------------- #
# find_image
# --------------------------------------------------------------------------- #


def test_find_image_returns_none_when_no_root_has_the_file(tmp_path):
    store = make_store(tmp_path)
    assert store.find_image(MBID, "111", "250") is None


def test_find_image_matches_whatever_extension_was_stored(tmp_path):
    store = make_store(tmp_path)
    plant_image(tmp_path / "storage", "111-full.png")
    found = store.find_image(MBID, "111", "full")
    assert found.path.endswith("111-full.png")
    assert found.content_type == "image/png"
    assert found.source == STORAGE


def test_find_image_does_not_confuse_sizes_or_ids(tmp_path):
    store = make_store(tmp_path)
    plant_image(tmp_path / "storage", "111-250.jpg")
    plant_image(tmp_path / "storage", "1112-full.jpg")
    assert store.find_image(MBID, "111", "full") is None
    assert store.find_image(MBID, "1112", "250") is None
    assert store.find_image(OTHER_MBID, "111", "250") is None


def test_find_image_serves_storage_without_consulting_the_cache(tmp_path, monkeypatch):
    """Storage wins, and the cache root is not even globbed."""
    store = make_store(tmp_path)
    plant_image(tmp_path / "storage", "111-250.jpg", data=b"from-storage")
    plant_image(tmp_path / "cache", "111-250.png", data=b"from-cache")

    patterns = []
    real_glob = store_mod.glob.glob

    def glob_with_a_witness(pattern, *args, **kwargs):
        patterns.append(pattern)
        return real_glob(pattern, *args, **kwargs)

    monkeypatch.setattr(store_mod.glob, "glob", glob_with_a_witness)

    found = store.find_image(MBID, "111", "250")
    assert found.source == STORAGE
    assert found.path == os.path.join(str(tmp_path / "storage"), MBID, "111-250.jpg")
    assert all(str(tmp_path / "cache") not in pattern for pattern in patterns)


def test_find_image_still_finds_what_the_other_mode_wrote_and_never_promotes_it(tmp_path):
    """A cache-only file is served from the cache with storage enabled.

    Flipping storage on must not orphan what the cache already holds — and must
    not copy it over either: that is a duplicate nobody asked for.
    """
    store = make_store(tmp_path)
    plant_image(tmp_path / "cache", "111-250.jpg")

    found = store.find_image(MBID, "111", "250")
    assert found.source == CACHE
    assert found.path == os.path.join(str(tmp_path / "cache"), MBID, "111-250.jpg")
    assert not os.path.exists(os.path.join(str(tmp_path / "storage"), MBID, "111-250.jpg"))


def test_find_image_ignores_a_concurrent_writers_tmp_file(tmp_path):
    """`<id>-<size>.*` matches `…-250.jpg.1234.tmp` too; serving one would hand
    the browser a half-written file."""
    store = make_store(tmp_path)
    plant_image(tmp_path / "storage", "111-250.jpg.999.888.tmp")
    assert store.find_image(MBID, "111", "250") is None


def test_find_image_sanitizes_its_inputs(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.find_image(MBID, "../../etc/passwd", "250")
    with pytest.raises(ValueError):
        store.find_image(MBID, "111", "../../etc")


# --------------------------------------------------------------------------- #
# store_image
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("content_type", "fmt", "extension"),
    [("image/jpeg", "JPEG", ".jpg"), ("image/png", "PNG", ".png"), ("image/gif", "GIF", ".gif")],
)
def test_store_image_picks_the_extension_from_the_content_type(
    tmp_path, content_type, fmt, extension
):
    store = make_store(tmp_path)
    stored = store.store_image(MBID, "111", "full", image_bytes(fmt=fmt), content_type)
    assert stored.path.endswith(f"111-full{extension}")
    assert os.path.isfile(stored.path)
    assert store.find_image(MBID, "111", "full").path == stored.path


def test_store_image_writes_into_the_storage_root_when_enabled(tmp_path):
    store = make_store(tmp_path)
    stored = store.store_image(MBID, "111", "250", image_bytes(), "image/jpeg")
    assert stored.source == STORAGE
    assert stored.path.startswith(str(tmp_path / "storage"))
    assert not os.path.exists(os.path.join(str(tmp_path / "cache"), MBID))


def test_store_image_writes_into_the_cache_when_storage_is_off(tmp_path):
    store = make_store(tmp_path, storage=False)
    stored = store.store_image(MBID, "111", "250", image_bytes(), "image/jpeg")
    assert stored.source == CACHE
    assert stored.path.startswith(str(tmp_path / "cache"))


def test_store_image_records_the_measured_dims(tmp_path):
    store = make_store(tmp_path)
    store.replace_metadata(MBID, [record("111")])

    stored = store.store_image(MBID, "111", "full", image_bytes(1425, 1200), "image/jpeg")
    assert stored.size == (1425, 1200)

    index = store.read_index(MBID)
    assert index.dims == {"111": {"full": [1425, 1200]}}
    # …and the listing it merged into is untouched.
    assert [image["image_id"] for image in index.images] == ["111"]


def test_store_image_keeps_dims_for_other_sizes_and_other_images(tmp_path):
    store = make_store(tmp_path)
    store.replace_metadata(MBID, [record("111")])
    store.store_image(MBID, "111", "250", image_bytes(250, 250), "image/jpeg")
    store.store_image(MBID, "111", "full", image_bytes(1000, 500), "image/jpeg")
    store.store_image(MBID, "222", "250", image_bytes(250, 100), "image/jpeg")

    assert store.read_index(MBID).dims == {
        "111": {"250": [250, 250], "full": [1000, 500]},
        "222": {"250": [250, 100]},
    }


def test_store_image_records_dims_even_before_a_listing_exists(tmp_path):
    """No index yet is not a reason to lose a measurement — but the record it
    creates carries no timestamp, so it reads as stale and the next listing
    refreshes it instead of serving an empty gallery from disk."""
    store = make_store(tmp_path)
    store.store_image(MBID, "111", "250", image_bytes(), "image/jpeg")

    index = store.read_index(MBID)
    assert index.dims == {"111": {"250": [250, 250]}}
    assert index.images == []
    assert index.fresh is False


def test_store_image_stores_unreadable_bytes_but_records_no_dims(tmp_path):
    """Losing the bytes over a header we cannot parse is worse than an opaque
    file; inventing a size for them would be worse than both."""
    store = make_store(tmp_path)
    stored = store.store_image(MBID, "111", "full", b"not an image", "application/octet-stream")

    assert stored.path.endswith("111-full.bin")
    assert stored.size is None
    assert stored.content_type is None
    assert store.read_index(MBID) is None


def test_store_image_replaces_a_file_stored_under_another_content_type(tmp_path):
    store = make_store(tmp_path)
    store.store_image(MBID, "111", "full", image_bytes(fmt="PNG"), "image/png")
    store.store_image(MBID, "111", "full", image_bytes(fmt="JPEG"), "image/jpeg")

    entries = sorted(os.listdir(os.path.join(str(tmp_path / "storage"), MBID)))
    assert [name for name in entries if name.startswith("111-full")] == ["111-full.jpg"]


def test_store_image_leaves_no_tmp_files(tmp_path):
    store = make_store(tmp_path)
    store.store_image(MBID, "111", "250", image_bytes(), "image/jpeg")
    entries = os.listdir(os.path.join(str(tmp_path / "storage"), MBID))
    assert sorted(entries) == ["111-250.jpg", INDEX_FILENAME]


def test_store_image_sanitizes_its_inputs(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.store_image(MBID, "../../etc/passwd", "250", image_bytes(), "image/jpeg")
    with pytest.raises(ValueError):
        store.store_image(MBID, "111", "small", image_bytes(), "image/jpeg")


def test_store_image_never_reenters_the_index_lock(tmp_path):
    """Run off the main thread: a second acquisition of the non-reentrant lock
    would hang the suite rather than fail it."""
    store = make_store(tmp_path)
    run_with_timeout(lambda: store.store_image(MBID, "111", "250", image_bytes(), "image/jpeg"))
    acquired = store_mod._index_lock.acquire(blocking=False)
    assert acquired, "the index lock was still held after store_image returned"
    store_mod._index_lock.release()


def test_concurrent_store_image_calls_keep_every_measurement(tmp_path):
    store = make_store(tmp_path)
    store.replace_metadata(MBID, [record("111")])
    errors = []
    barrier = threading.Barrier(8)

    def worker(n):
        try:
            barrier.wait(timeout=5)
            store.store_image(MBID, str(n), "250", image_bytes(250, 100 + n), "image/jpeg")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()

    assert errors == []
    # Parseable, and not one measurement lost to a read-modify-write race.
    index = store.read_index(MBID)
    assert index.dims == {str(n): {"250": [250, 100 + n]} for n in range(8)}
    assert [image["image_id"] for image in index.images] == ["111"]


def test_a_metadata_refresh_racing_a_store_image_loses_neither(tmp_path):
    """The barrier test the merge protocol exists for.

    Both writers are queued behind the lock held here, so they run back to back
    in an order the test does not control — and the invariant has to hold either
    way: `replace_metadata` merges the dims it re-reads, and `store_image` merges
    its measurement into whatever listing it finds.
    """
    store = make_store(tmp_path)
    store.replace_metadata(MBID, [record("111")])

    errors = []

    def guard(func):
        def run():
            try:
                func()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        return run

    refresher = threading.Thread(
        target=guard(lambda: store.replace_metadata(MBID, [record("222")])), daemon=True
    )
    storer = threading.Thread(
        target=guard(
            lambda: store.store_image(MBID, "222", "full", image_bytes(1425, 1425), "image/jpeg")
        ),
        daemon=True,
    )

    with store_mod._index_lock:
        storer.start()
        refresher.start()
        # Both must be parked on the lock before it is handed over; store_image
        # does its file write and measurement before reaching for it.
        storer.join(0.3)
        refresher.join(0.3)

    for thread in (storer, refresher):
        thread.join(10)
        assert not thread.is_alive()

    assert errors == []
    index = store.read_index(MBID)
    assert [image["image_id"] for image in index.images] == ["222"]
    assert index.dims == {"222": {"full": [1425, 1425]}}
