"""Pure parser for beets 2.13.1 verbose log output.

Parses a single beets log line into a UI log level and an optional progress
update.  This module is intentionally free of I/O and threading so that it
is fully unit-testable and keeps the tail-thread loop thin.

Log format
----------
``beet -v import -A -C ...`` writes to stderr using Python's logging with
the beets LegacyFormatter::

    %(legacy_prefix)s%(message)s

where ``legacy_prefix`` is empty for the root ``"beets"`` logger and
``"<subname>: "`` for child loggers, e.g. ``"musicbrainz: "`` for
``beets.musicbrainz``.

Progress marker (verified against real ``beet -v import -A -C -I`` output):
the production command imports with ``-A`` (``--noautotag``), so beets uses
the *asis* pipeline — the autotag ``Looking up: /path`` line is **never
emitted**.  Instead each album task produces, in order::

    ``Sending event: import_task_created``  — DEBUG; once per album task
    ``/path/to/album``                       — INFO; the album path (import_asis)

The ``import_task_created`` event is the reliable per-album increment (one
per album in album mode); the bare album-path line that follows it supplies
``current_item`` (best-effort).

Other key lines produced during import (root "beets" logger):

    ``found duplicates: [N, …]``       — DEBUG (info, no progress)
    ``removing N old duplicate albums``— DEBUG (tasks.py) → removed
    ``deleting duplicate /path``       — DEBUG (tasks.py) → removed
    ``Replacing item N: /path``        — DEBUG (tasks.py) → added
    ``Skipped N paths.``               — INFO; end-of-run summary (incremental)
    ``No files imported from /path``   — WARNING

Phase 2 — ``beet -v update -M`` (separate Popen, same log file)::

    ``skipping /path because mtime is up to date…`` — DEBUG (info)
    ``  deleted``                                    — stdout via ui.print_() → removed
    ``error reading /path: …``                       — ERROR (mapped to warn — no
                                                       distinct error level in the UI)

Version pinned to: beets 2.13.1
"""

from __future__ import annotations

import re
from typing import NamedTuple

# beets version this parser is pinned to
BEETS_VERSION = "2.13.1"

# UI log level constants (match the prototype's SCAN_LOG shape)
LOG_LEVEL_INFO = "info"
LOG_LEVEL_ADDED = "added"
LOG_LEVEL_REMOVED = "removed"
LOG_LEVEL_WARN = "warn"
LOG_LEVEL_SKIP = "skip"
LOG_LEVEL_SUMMARY = "summary"

# ANSI colour-escape pattern (beet update -M uses colorize(); stripped as a
# safety measure even though colorize() is a no-op when stdout is not a TTY).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class ProgressUpdate(NamedTuple):
    """Signals a change to scan progress counters."""

    increment_processed: bool = False
    current_item: str | None = None


class ParseResult(NamedTuple):
    """Result of parsing a single beets verbose log line."""

    log_level: str  # one of LOG_LEVEL_* constants
    update: ProgressUpdate | None = None


def parse_beets_line(line: str) -> ParseResult | None:
    """Parse a single beets 2.13.1 verbose log line.

    Returns ``None`` for empty or whitespace-only input.  The ``update``
    field is non-``None`` only when the line signals a progress change:
    an album/singleton start (``increment_processed=True``, ``current_item``
    set).

    Lines that carry no special meaning fall through to ``LOG_LEVEL_INFO``
    with no update.
    """
    clean = _ANSI_RE.sub("", line).rstrip("\r\n").rstrip()
    if not clean:
        return None

    # ------------------------------------------------------------------ #
    # Album start — increment processed counter once per album task        #
    # ------------------------------------------------------------------ #

    # Primary progress marker. beets emits this DEBUG event exactly once per
    # import task (= one album in album mode) regardless of autotag mode, so
    # it is reliable under the production -A (--noautotag) command where the
    # autotag-only "Looking up:" line never appears. The album path is set as
    # current_item by the bare-path branch below (the line that follows).
    if clean == "Sending event: import_task_created":
        return ParseResult(
            log_level=LOG_LEVEL_INFO,
            update=ProgressUpdate(increment_processed=True),
        )

    # ------------------------------------------------------------------ #
    # Added                                                                 #
    # ------------------------------------------------------------------ #

    # Duplicate-format replace: new file supersedes existing item in one pass.
    if clean.startswith("Replacing item "):
        return ParseResult(log_level=LOG_LEVEL_ADDED)

    # Session tag_log: "duplicate-replace" means new format replaced old item.
    if "importer.session: duplicate-replace " in clean:
        return ParseResult(log_level=LOG_LEVEL_ADDED)

    # ------------------------------------------------------------------ #
    # Removed                                                              #
    # ------------------------------------------------------------------ #

    # Duplicate item deleted from library.
    if clean.startswith("deleting duplicate "):
        return ParseResult(log_level=LOG_LEVEL_REMOVED)

    # Duplicate album(s) purged: "removing N old duplicate albums".
    if clean.startswith("removing ") and "duplicate" in clean:
        return ParseResult(log_level=LOG_LEVEL_REMOVED)

    # beet update -M: item not found on disk → printed via ui.print_ + colorize.
    # After ANSI stripping this is exactly "  deleted".
    if clean == "  deleted":
        return ParseResult(log_level=LOG_LEVEL_REMOVED)

    # ------------------------------------------------------------------ #
    # Skip                                                                  #
    # ------------------------------------------------------------------ #

    # Session tag_log for skipped albums (written to the importer log; may
    # appear if the session logger propagates to the main handler).
    if "importer.session: skip " in clean or "importer.session: duplicate-skip " in clean:
        return ParseResult(log_level=LOG_LEVEL_SKIP)

    # ------------------------------------------------------------------ #
    # Summary                                                              #
    # ------------------------------------------------------------------ #

    # End-of-run count of directories skipped by the incremental statefile.
    if clean.startswith("Skipped ") and "paths" in clean:
        return ParseResult(log_level=LOG_LEVEL_SUMMARY)

    # ------------------------------------------------------------------ #
    # Warn                                                                  #
    # ------------------------------------------------------------------ #

    if clean.startswith("No files imported from "):
        return ParseResult(log_level=LOG_LEVEL_WARN)

    # Incremental import resuming after an interrupted run.
    if "Resuming interrupted import" in clean:
        return ParseResult(log_level=LOG_LEVEL_WARN)

    # beet update -M: read error on a file.
    if clean.startswith("error reading "):
        return ParseResult(log_level=LOG_LEVEL_WARN)

    # ------------------------------------------------------------------ #
    # Album path (import_asis stage) — set current_item, do NOT increment  #
    # ------------------------------------------------------------------ #

    # A bare absolute path printed once per album by the asis pipeline. The
    # increment is owned by the import_task_created event above, so this only
    # updates current_item. No other production line starts with a bare "/"
    # (added/removed/skip/warn lines all start with a keyword; indented item
    # paths start with whitespace).
    if clean.startswith("/"):
        return ParseResult(
            log_level=LOG_LEVEL_INFO,
            update=ProgressUpdate(current_item=clean),
        )

    # ------------------------------------------------------------------ #
    # Default — unrecognized lines are shown but carry no progress data    #
    # ------------------------------------------------------------------ #

    return ParseResult(log_level=LOG_LEVEL_INFO)
