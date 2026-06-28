"""Pure parser for beets 2.12.0 verbose log output.

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

Key lines produced by ``beets/importer/stages.py`` (root "beets" logger,
written to stderr by the CLI handler):

    ``Looking up: /path``              — DEBUG; once per album in autotag mode
    ``found duplicates: [N, …]``       — DEBUG
    ``removing N old duplicate albums``— DEBUG (tasks.py)
    ``deleting duplicate /path``       — DEBUG (tasks.py)
    ``Replacing item N: /path``        — DEBUG (tasks.py, duplicate-replace)
    ``Skipped N paths.``               — INFO; end-of-run summary (incremental)
    ``No files imported from /path``   — WARNING

Pretend mode only (``-p`` flag; not used in production – handled for
completeness)::

    ``Album: /path``     — INFO; once per album
    ``  /path/to/track`` — INFO; per item within album

Phase 2 — ``beet -v update -M`` (separate Popen, same log file)::

    ``skipping /path because mtime is up to date…`` — DEBUG
    ``  deleted``                                    — stdout via ui.print_()
    ``error reading /path: …``                       — ERROR

Version pinned to: beets 2.12.0
"""

from __future__ import annotations

import re
from typing import NamedTuple

# beets version this parser is pinned to
BEETS_VERSION = "2.12.0"

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
    """Parse a single beets 2.12.0 verbose log line.

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
    # Album / singleton start — increment processed counter once per unit  #
    # ------------------------------------------------------------------ #

    # Primary progress marker (autotag mode, beet -v import -A).
    # lookup_candidates stage emits this once per album directory.
    if clean.startswith("Looking up: "):
        path = clean[len("Looking up: ") :]
        return ParseResult(
            log_level=LOG_LEVEL_INFO,
            update=ProgressUpdate(increment_processed=True, current_item=path),
        )

    # Pretend-mode markers from the log_files pipeline stage.
    # Not emitted during normal (non-pretend) imports; handled for completeness.
    if clean.startswith("Album: "):
        path = clean[len("Album: ") :]
        return ParseResult(
            log_level=LOG_LEVEL_INFO,
            update=ProgressUpdate(increment_processed=True, current_item=path),
        )

    if clean.startswith("Singleton: "):
        path = clean[len("Singleton: ") :]
        return ParseResult(
            log_level=LOG_LEVEL_INFO,
            update=ProgressUpdate(increment_processed=True, current_item=path),
        )

    # ------------------------------------------------------------------ #
    # Pretend-mode per-item lines — must NOT double-count                  #
    # ------------------------------------------------------------------ #

    # Indented file paths: "  /path/to/track.mp3"
    # Already counted as part of the album above; skip the progress update.
    if clean.startswith("  /"):
        return ParseResult(log_level=LOG_LEVEL_INFO)

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
    # Default — unrecognized lines are shown but carry no progress data    #
    # ------------------------------------------------------------------ #

    return ParseResult(log_level=LOG_LEVEL_INFO)
