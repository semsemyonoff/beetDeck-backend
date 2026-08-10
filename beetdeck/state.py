"""Shared in-memory state for beetDeek.

These module-level globals are shared across all request threads.
The application requires a single gunicorn worker to keep this state coherent.
"""

import threading

# Identification + cover preview state
# Cover previews are stored here too, keyed as f"cover_{album_id}"
identify_tasks: dict = {}
identify_lock = threading.Lock()

# Monotonic counter behind every "did this move under me" precondition: the
# ``match_generation`` of identify tasks and the ``stash_generation`` of the
# cover/lyrics fetch stashes. Process-wide (not per task) so a value is never
# reused after a task dict is replaced by a re-identify or a re-fetch.
_match_generation = 0


def next_match_generation():
    """Return a fresh match generation. Caller must hold ``identify_lock``."""
    global _match_generation
    _match_generation += 1
    return _match_generation


#: The same counter under the name the cover/lyrics stashes use for it. One
#: sequence, so a generation seen on a fetch can never collide with one seen on
#: an identification.
next_stash_generation = next_match_generation

# Per-album serialization for the routes that write cover art or lyrics.
# ``identify_lock`` guards the task dict for microseconds, but these handlers
# check a precondition under it and then spend *seconds* embedding into every
# track — so on their own the check and the write are not one step, and a direct
# upload/save landing in that gap gets silently reverted by a confirm that
# already passed. Keyed by album rather than by item so no handler ever has to
# hold two of them, which is what would make lock ordering a problem.
_album_write_locks: dict = {}
_album_write_locks_guard = threading.Lock()


def album_write_lock(album_id):
    """Return the write lock for ``album_id``, creating it on first use.

    Locks are never reaped: one per album ever written is a few hundred bytes
    against a library of bounded size, and reaping would need refcounting to
    avoid handing out a lock that is being dropped.

    Always acquired *outside* ``identify_lock`` — every caller does, so the two
    can never be taken in opposite orders.
    """
    with _album_write_locks_guard:
        lock = _album_write_locks.get(album_id)
        if lock is None:
            lock = threading.Lock()
            _album_write_locks[album_id] = lock
        return lock


# Rescan state
rescan_lock = threading.Lock()
rescan_proc = None  # subprocess.Popen | None — current subprocess (internal, used by controller)
rescan_snapshot = None  # {item_id: (title, artist, album_id, path)} | None — pre-import snapshot
rescan_run_id = (
    None  # str | None — run identifier (timestamp w/ microseconds: YYYYMMDD-HHMMSS-ffffff)
)
rescan_log_path = None  # str | None — path to the current run log file
rescan_phase = "idle"  # idle | importing | updating | done | error
rescan_result = None  # dict | None — persisted finish-state (status, returncode, added, removed)
rescan_thread = None  # threading.Thread | None — controller thread (used in tests)
rescan_processed = 0  # int — albums/singletons processed so far in current run
rescan_total = None  # int | None — total expected albums (None for quick/indeterminate)
rescan_current_item = None  # str | None — most recent item path being processed

# Metadata batch write state
metadata_write_lock = threading.Lock()
