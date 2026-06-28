"""Tests for beetdeck/scan_progress.py — pure beets 2.12.0 log parser.

Tests use fixed sample strings that reproduce the exact format produced by
``beet -v import -A -C [-i|-I] /music`` (and ``beet -v update -M``).  They
are intentionally independent of any I/O or threading.
"""

import pytest

from beetdeck.scan_progress import (
    BEETS_VERSION,
    LOG_LEVEL_ADDED,
    LOG_LEVEL_INFO,
    LOG_LEVEL_REMOVED,
    LOG_LEVEL_SKIP,
    LOG_LEVEL_SUMMARY,
    LOG_LEVEL_WARN,
    ParseResult,
    ProgressUpdate,
    parse_beets_line,
)

# --------------------------------------------------------------------------- #
# Sanity: version pin                                                           #
# --------------------------------------------------------------------------- #


def test_beets_version_pinned():
    assert BEETS_VERSION == "2.12.0"


# --------------------------------------------------------------------------- #
# Empty / whitespace → None                                                     #
# --------------------------------------------------------------------------- #


def test_empty_string_returns_none():
    assert parse_beets_line("") is None


def test_whitespace_only_returns_none():
    assert parse_beets_line("   ") is None


def test_newline_only_returns_none():
    assert parse_beets_line("\n") is None


def test_crlf_returns_none():
    assert parse_beets_line("\r\n") is None


# --------------------------------------------------------------------------- #
# Album start — primary progress marker (import_task_created + album path)       #
# --------------------------------------------------------------------------- #


class TestImportTaskCreated:
    """'Sending event: import_task_created' — the per-album progress marker.

    beets emits this DEBUG event once per import task (= one album in album
    mode), regardless of autotag. It is the reliable counter under the
    production -A (--noautotag) command, where 'Looking up:' never appears.
    """

    def test_increments_processed(self):
        result = parse_beets_line("Sending event: import_task_created")
        assert result is not None
        assert result.update is not None
        assert result.update.increment_processed is True

    def test_does_not_set_current_item(self):
        # The album path is on the following line, not this event.
        result = parse_beets_line("Sending event: import_task_created")
        assert result.update.current_item is None

    def test_log_level_is_info(self):
        assert parse_beets_line("Sending event: import_task_created").log_level == LOG_LEVEL_INFO

    def test_other_sending_event_lines_do_not_increment(self):
        """Only import_task_created counts; the many other 'Sending event:' lines do not."""
        for line in (
            "Sending event: import_begin",
            "Sending event: database_change",
            "Sending event: album_imported",
            "Sending event: cli_exit",
        ):
            result = parse_beets_line(line)
            assert result.update is None, line

    def test_looking_up_is_no_longer_a_marker(self):
        """'Looking up:' is autotag-only and never emitted under -A — no progress."""
        result = parse_beets_line("Looking up: /music/Artist/Album")
        assert result.update is None


class TestAlbumPath:
    """A bare absolute path (import_asis stage) sets current_item only."""

    def test_sets_current_item(self):
        result = parse_beets_line("/music/Artist/Album")
        assert result.update is not None
        assert result.update.current_item == "/music/Artist/Album"

    def test_does_not_increment(self):
        # The increment is owned by import_task_created; the path must not double-count.
        result = parse_beets_line("/music/Artist/Album")
        assert result.update.increment_processed is False

    def test_log_level_is_info(self):
        assert parse_beets_line("/music/Artist/Album").log_level == LOG_LEVEL_INFO

    def test_path_with_special_chars(self):
        result = parse_beets_line("/music/Björk/Selma Songs")
        assert result.update.current_item == "/music/Björk/Selma Songs"

    def test_path_with_year_prefix(self):
        result = parse_beets_line("/music/Artist/2024 - Album Title")
        assert result.update.current_item == "/music/Artist/2024 - Album Title"

    def test_trailing_newline_stripped(self):
        result = parse_beets_line("/music/Artist/Album\n")
        assert result.update.current_item == "/music/Artist/Album"

    def test_indented_path_is_not_an_album_path(self):
        """Leading-whitespace paths are not bare album paths → no progress update."""
        result = parse_beets_line("  /music/Artist/Album/01 - Track.mp3")
        assert result.update is None
        assert result.log_level == LOG_LEVEL_INFO


# --------------------------------------------------------------------------- #
# beet update -M item-deleted line                                             #
# --------------------------------------------------------------------------- #


class TestUpdateDeleted:
    def test_indented_deleted_is_removed(self):
        """'  deleted' (beet update -M) is classified as removed, not an item path."""
        result = parse_beets_line("  deleted")
        assert result.log_level == LOG_LEVEL_REMOVED


# --------------------------------------------------------------------------- #
# Added                                                                         #
# --------------------------------------------------------------------------- #


class TestAdded:
    def test_replacing_item_is_added(self):
        result = parse_beets_line("Replacing item 42: /music/Artist/Album/track.flac")
        assert result.log_level == LOG_LEVEL_ADDED

    def test_replacing_item_no_update(self):
        result = parse_beets_line("Replacing item 42: /music/Artist/Album/track.flac")
        assert result.update is None

    def test_session_duplicate_replace_is_added(self):
        result = parse_beets_line("importer.session: duplicate-replace /music/Artist/Album")
        assert result.log_level == LOG_LEVEL_ADDED


# --------------------------------------------------------------------------- #
# Removed                                                                       #
# --------------------------------------------------------------------------- #


class TestRemoved:
    def test_deleting_duplicate_is_removed(self):
        result = parse_beets_line("deleting duplicate /music/Artist/Album/old.mp3")
        assert result.log_level == LOG_LEVEL_REMOVED

    def test_deleting_duplicate_no_update(self):
        result = parse_beets_line("deleting duplicate /music/Artist/Album/old.mp3")
        assert result.update is None

    def test_removing_duplicate_albums_is_removed(self):
        result = parse_beets_line("removing 2 old duplicate albums")
        assert result.log_level == LOG_LEVEL_REMOVED

    def test_removing_one_duplicate_album_is_removed(self):
        result = parse_beets_line("removing 1 old duplicate albums")
        assert result.log_level == LOG_LEVEL_REMOVED

    def test_removing_without_duplicate_is_info(self):
        """'removing' without 'duplicate' does not trigger removed level."""
        result = parse_beets_line("removing /some/path")
        assert result.log_level == LOG_LEVEL_INFO

    def test_deleted_line_is_removed(self):
        """beet update -M: '  deleted' (via ui.print_ + colorize)."""
        result = parse_beets_line("  deleted")
        assert result.log_level == LOG_LEVEL_REMOVED

    def test_deleted_with_ansi_codes_is_removed(self):
        """ANSI colour codes stripped before matching."""
        result = parse_beets_line("\x1b[31m  deleted\x1b[0m")
        assert result.log_level == LOG_LEVEL_REMOVED

    def test_deleting_art_is_removed(self):
        result = parse_beets_line("deleting duplicate album art /music/cover.jpg")
        assert result.log_level == LOG_LEVEL_REMOVED


# --------------------------------------------------------------------------- #
# Skip                                                                          #
# --------------------------------------------------------------------------- #


class TestSkip:
    def test_session_skip_is_skip(self):
        result = parse_beets_line("importer.session: skip /music/Artist/OldAlbum")
        assert result.log_level == LOG_LEVEL_SKIP

    def test_session_skip_no_update(self):
        result = parse_beets_line("importer.session: skip /music/Artist/OldAlbum")
        assert result.update is None

    def test_session_duplicate_skip_is_skip(self):
        result = parse_beets_line("importer.session: duplicate-skip /music/Artist/Album")
        assert result.log_level == LOG_LEVEL_SKIP


# --------------------------------------------------------------------------- #
# Summary                                                                       #
# --------------------------------------------------------------------------- #


class TestSummary:
    def test_skipped_paths_is_summary(self):
        result = parse_beets_line("Skipped 3 paths.")
        assert result.log_level == LOG_LEVEL_SUMMARY

    def test_skipped_one_path_is_summary(self):
        result = parse_beets_line("Skipped 1 paths.")
        assert result.log_level == LOG_LEVEL_SUMMARY

    def test_skipped_summary_no_update(self):
        result = parse_beets_line("Skipped 3 paths.")
        assert result.update is None

    def test_skipped_without_paths_is_info(self):
        """'Skipped' without 'paths' keyword is not a summary line."""
        result = parse_beets_line("Skipped something else")
        assert result.log_level == LOG_LEVEL_INFO


# --------------------------------------------------------------------------- #
# Warn                                                                          #
# --------------------------------------------------------------------------- #


class TestWarn:
    def test_no_files_imported_is_warn(self):
        result = parse_beets_line("No files imported from /music/Empty Dir")
        assert result.log_level == LOG_LEVEL_WARN

    def test_no_files_imported_no_update(self):
        result = parse_beets_line("No files imported from /music/Empty Dir")
        assert result.update is None

    def test_resuming_interrupted_is_warn(self):
        result = parse_beets_line("Resuming interrupted import of /music/Artist/Unfinished")
        assert result.log_level == LOG_LEVEL_WARN

    def test_error_reading_is_warn(self):
        """beet update -M: read error on a file."""
        result = parse_beets_line("error reading /music/Artist/Album/track.mp3: [Errno 5]")
        assert result.log_level == LOG_LEVEL_WARN


# --------------------------------------------------------------------------- #
# Info — default for unrecognized lines                                         #
# --------------------------------------------------------------------------- #


class TestInfo:
    def test_unrecognized_line_is_info(self):
        result = parse_beets_line("some random debug output")
        assert result is not None
        assert result.log_level == LOG_LEVEL_INFO
        assert result.update is None

    def test_musicbrainz_line_is_info(self):
        result = parse_beets_line("musicbrainz: Searching for: Artist - Album")
        assert result.log_level == LOG_LEVEL_INFO
        assert result.update is None

    def test_autotag_line_is_info(self):
        result = parse_beets_line("autotag: Best match (0.96):")
        assert result.log_level == LOG_LEVEL_INFO
        assert result.update is None

    def test_found_duplicates_is_info(self):
        result = parse_beets_line("found duplicates: [1, 2]")
        assert result.log_level == LOG_LEVEL_INFO
        assert result.update is None

    def test_session_import_started_is_info(self):
        result = parse_beets_line("importer.session: import started Mon Jan  1 12:00:00 2024")
        assert result.log_level == LOG_LEVEL_INFO

    def test_session_asis_is_info(self):
        result = parse_beets_line("importer.session: asis /music/Artist/Album")
        assert result.log_level == LOG_LEVEL_INFO

    def test_session_duplicate_keep_is_info(self):
        result = parse_beets_line("importer.session: duplicate-keep /music/Artist/Album")
        assert result.log_level == LOG_LEVEL_INFO

    def test_update_skipping_is_info(self):
        """beet update -M: items not modified since last update."""
        result = parse_beets_line(
            "skipping /music/Artist/Album/track.mp3 because mtime is up to date (1234567.0)"
        )
        assert result.log_level == LOG_LEVEL_INFO

    def test_update_moving_album_is_info(self):
        result = parse_beets_line("moving album 42")
        assert result.log_level == LOG_LEVEL_INFO

    def test_looking_up_debug_line(self):
        """DEBUG prefix on 'Looking up' is not used (beets omits level in format)."""
        result = parse_beets_line("DEBUG Looking up: /music/Artist/Album")
        # Does NOT match "Looking up: " prefix (because "DEBUG " is prepended)
        assert result.log_level == LOG_LEVEL_INFO
        assert result.update is None

    def test_bare_path_asis_sets_current_item(self):
        """import_asis stage logs bare paths for non-autotag imports.

        Classified as info but carries a current_item update (see TestAlbumPath).
        """
        result = parse_beets_line("/music/Artist/Album")
        assert result.log_level == LOG_LEVEL_INFO
        assert result.update is not None
        assert result.update.current_item == "/music/Artist/Album"
        assert result.update.increment_processed is False


# --------------------------------------------------------------------------- #
# NamedTuple structure                                                           #
# --------------------------------------------------------------------------- #


class TestDataTypes:
    def test_parse_result_default_update_is_none(self):
        r = ParseResult(log_level="info")
        assert r.log_level == "info"
        assert r.update is None

    def test_parse_result_with_update(self):
        u = ProgressUpdate(increment_processed=True, current_item="/path")
        r = ParseResult(log_level="info", update=u)
        assert r.update.increment_processed is True
        assert r.update.current_item == "/path"

    def test_progress_update_defaults(self):
        u = ProgressUpdate()
        assert u.increment_processed is False
        assert u.current_item is None

    def test_progress_update_is_immutable(self):
        u = ProgressUpdate(increment_processed=True, current_item="/path")
        with pytest.raises(AttributeError):
            u.increment_processed = False  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# No-double-count invariants                                                    #
# --------------------------------------------------------------------------- #


class TestNoDoubleCount:
    """The per-album increment is owned solely by import_task_created; the album
    path line and per-track lines must not add extra increments."""

    def test_import_task_created_increments_exactly_once(self):
        results = [parse_beets_line("Sending event: import_task_created")]
        increments = sum(
            1 for r in results if r is not None and r.update and r.update.increment_processed
        )
        assert increments == 1

    def test_album_path_does_not_increment(self):
        """The bare album path that follows the event sets current_item only."""
        results = [parse_beets_line("/music/Artist/Album")]
        increments = sum(
            1 for r in results if r is not None and r.update and r.update.increment_processed
        )
        assert increments == 0

    def test_event_plus_path_increments_once(self):
        """The event + its trailing album path together count as a single album."""
        lines = [
            "Sending event: import_task_created",
            "/music/Artist/Album",
        ]
        results = [parse_beets_line(line) for line in lines]
        increments = sum(
            1 for r in results if r is not None and r.update and r.update.increment_processed
        )
        assert increments == 1

    def test_full_session_sample_output(self):
        """Fixed sample of a real verbose import run.

        Reproduces what ``beet -v import -A -C -I /music`` writes to the log
        file for a small two-album library with a duplicate replace — captured
        from real beets 2.12.0 output (note: no 'Looking up:' lines under -A).
        """
        sample = [
            # Import phase (two albums, one a duplicate replace)
            "Sending event: import_begin",
            "Sending event: import_task_created",
            "/music/Artist1/Album1",
            "Sending event: import_task_created",
            "found duplicates: [1]",
            "/music/Artist2/Album2",
            "Replacing item 2: /music/Artist2/Album2/track.mp3",
            "removing 1 old duplicate albums",
            "deleting duplicate /music/Artist1/Album1/old.mp3",
            "No files imported from /music/EmptyDir",
            "Skipped 5 paths.",
            # Update phase (separate Popen)
            "skipping /music/Artist1/Album1/01.mp3 because mtime is up to date (1234567.0)",
        ]
        results = [parse_beets_line(line) for line in sample]

        # Exactly 2 import_task_created events → 2 increments
        increments = sum(1 for r in results if r and r.update and r.update.increment_processed)
        assert increments == 2

        # current_item tracks the latest album path
        current_items = [
            r.update.current_item for r in results if r and r.update and r.update.current_item
        ]
        assert current_items == ["/music/Artist1/Album1", "/music/Artist2/Album2"]

        # Level mapping
        levels = [r.log_level if r else None for r in results]
        assert levels[0] == LOG_LEVEL_INFO  # import_begin event
        assert levels[1] == LOG_LEVEL_INFO  # import_task_created
        assert levels[2] == LOG_LEVEL_INFO  # album1 path
        assert levels[3] == LOG_LEVEL_INFO  # import_task_created
        assert levels[4] == LOG_LEVEL_INFO  # found duplicates
        assert levels[5] == LOG_LEVEL_INFO  # album2 path
        assert levels[6] == LOG_LEVEL_ADDED  # Replacing item
        assert levels[7] == LOG_LEVEL_REMOVED  # removing duplicate albums
        assert levels[8] == LOG_LEVEL_REMOVED  # deleting duplicate
        assert levels[9] == LOG_LEVEL_WARN  # No files imported
        assert levels[10] == LOG_LEVEL_SUMMARY  # Skipped N paths
        assert levels[11] == LOG_LEVEL_INFO  # update skipping
