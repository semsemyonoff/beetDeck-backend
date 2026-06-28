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
# Album / singleton start — primary progress marker                             #
# --------------------------------------------------------------------------- #


class TestLookingUp:
    """'Looking up: /path' — the primary progress marker in autotag mode.

    Emitted by lookup_candidates stage (beets/importer/stages.py) once per
    album directory fed into the pipeline.
    """

    def test_increments_processed(self):
        result = parse_beets_line("Looking up: /music/Artist/Album")
        assert result is not None
        assert result.update is not None
        assert result.update.increment_processed is True

    def test_sets_current_item(self):
        result = parse_beets_line("Looking up: /music/Artist/Album")
        assert result.update.current_item == "/music/Artist/Album"

    def test_log_level_is_info(self):
        assert parse_beets_line("Looking up: /music/Artist/Album").log_level == LOG_LEVEL_INFO

    def test_path_with_special_chars(self):
        result = parse_beets_line("Looking up: /music/Björk/Selma Songs")
        assert result.update.current_item == "/music/Björk/Selma Songs"

    def test_path_with_year_prefix(self):
        result = parse_beets_line("Looking up: /music/Artist/2024 - Album Title")
        assert result.update.current_item == "/music/Artist/2024 - Album Title"

    def test_trailing_newline_stripped(self):
        result = parse_beets_line("Looking up: /music/Artist/Album\n")
        assert result.update.current_item == "/music/Artist/Album"

    def test_trailing_crlf_stripped(self):
        result = parse_beets_line("Looking up: /music/Artist/Album\r\n")
        assert result.update.current_item == "/music/Artist/Album"

    def test_does_not_match_mid_line(self):
        """'Looking up' appearing mid-line does not trigger progress update."""
        result = parse_beets_line("Not Looking up anything")
        assert result.log_level == LOG_LEVEL_INFO
        assert result.update is None


class TestAlbumPrefix:
    """'Album: /path' — pretend-mode progress marker (log_files stage)."""

    def test_increments_processed(self):
        result = parse_beets_line("Album: /music/Artist/Album")
        assert result.update.increment_processed is True

    def test_sets_current_item(self):
        result = parse_beets_line("Album: /music/Artist/Album")
        assert result.update.current_item == "/music/Artist/Album"

    def test_log_level_is_info(self):
        assert parse_beets_line("Album: /music/Artist/Album").log_level == LOG_LEVEL_INFO


class TestSingletonPrefix:
    """'Singleton: /path' — pretend-mode singleton progress marker."""

    def test_increments_processed(self):
        result = parse_beets_line("Singleton: /music/Various/track.mp3")
        assert result.update.increment_processed is True

    def test_sets_current_item(self):
        result = parse_beets_line("Singleton: /music/Various/track.mp3")
        assert result.update.current_item == "/music/Various/track.mp3"

    def test_log_level_is_info(self):
        assert parse_beets_line("Singleton: /music/V/t.mp3").log_level == LOG_LEVEL_INFO


# --------------------------------------------------------------------------- #
# Indented item lines — must NOT double-count                                   #
# --------------------------------------------------------------------------- #


class TestIndentedItems:
    """Per-item lines inside an album (pretend mode) must not increment processed."""

    def test_no_update_for_indented_path(self):
        result = parse_beets_line("  /music/Artist/Album/01 - Track.mp3")
        assert result is not None
        assert result.update is None

    def test_log_level_is_info(self):
        result = parse_beets_line("  /music/Artist/Album/01 - Track.mp3")
        assert result.log_level == LOG_LEVEL_INFO

    def test_no_update_for_deep_path(self):
        result = parse_beets_line("  /music/Artist/Album/CD1/01.flac")
        assert result.update is None

    def test_indented_deleted_is_not_info(self):
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

    def test_bare_path_asis_is_info(self):
        """import_asis stage logs bare paths for non-autotag imports."""
        result = parse_beets_line("/music/Artist/Album")
        assert result.log_level == LOG_LEVEL_INFO
        assert result.update is None


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
    """'Looking up' and 'Album:' are from mutually exclusive pipeline modes;
    neither should produce two increments for the same album."""

    def test_looking_up_increments_exactly_once(self):
        results = [parse_beets_line("Looking up: /music/Artist/Album")]
        increments = sum(
            1 for r in results if r is not None and r.update and r.update.increment_processed
        )
        assert increments == 1

    def test_album_prefix_increments_exactly_once(self):
        results = [parse_beets_line("Album: /music/Artist/Album")]
        increments = sum(
            1 for r in results if r is not None and r.update and r.update.increment_processed
        )
        assert increments == 1

    def test_indented_tracks_add_zero_increments(self):
        """Tracks listed under an album must not produce any increments."""
        lines = [
            "Album: /music/Artist/Album",
            "  /music/Artist/Album/01 - Track 1.mp3",
            "  /music/Artist/Album/02 - Track 2.mp3",
            "  /music/Artist/Album/03 - Track 3.mp3",
        ]
        results = [parse_beets_line(line) for line in lines]
        increments = sum(
            1 for r in results if r is not None and r.update and r.update.increment_processed
        )
        # Only the "Album:" line, not the 3 track lines
        assert increments == 1

    def test_full_session_sample_output(self):
        """Fixed sample of a complete verbose import run.

        Reproduce what ``beet -v import -A -C -I /music`` writes to the log
        file for a small library with two albums and one error case.
        """
        sample = [
            # Import phase
            "importer.session: import started Mon Jan  1 12:00:00 2024",
            "Looking up: /music/Artist1/Album1",
            "musicbrainz: Searching for: Artist1 - Album1",
            "autotag: Best match (0.95):",
            "Looking up: /music/Artist2/Album2",
            "musicbrainz: Searching for: Artist2 - Album2",
            "found duplicates: [3, 4]",
            "removing 1 old duplicate albums",
            "deleting duplicate /music/Artist2/Album2/old.mp3",
            "No files imported from /music/EmptyDir",
            "Skipped 5 paths.",
            # Update phase (separate Popen)
            "skipping /music/Artist1/Album1/01.mp3 because mtime is up to date (1234567.0)",
        ]
        results = [parse_beets_line(line) for line in sample]

        # Exactly 2 "Looking up:" → 2 increments
        increments = sum(1 for r in results if r and r.update and r.update.increment_processed)
        assert increments == 2

        # Level mapping
        levels = [r.log_level if r else None for r in results]
        assert levels[0] == LOG_LEVEL_INFO  # import started
        assert levels[1] == LOG_LEVEL_INFO  # Looking up → also info
        assert levels[2] == LOG_LEVEL_INFO  # musicbrainz:
        assert levels[3] == LOG_LEVEL_INFO  # autotag:
        assert levels[4] == LOG_LEVEL_INFO  # Looking up
        assert levels[5] == LOG_LEVEL_INFO  # musicbrainz:
        assert levels[6] == LOG_LEVEL_INFO  # found duplicates
        assert levels[7] == LOG_LEVEL_REMOVED  # removing duplicate albums
        assert levels[8] == LOG_LEVEL_REMOVED  # deleting duplicate
        assert levels[9] == LOG_LEVEL_WARN  # No files imported
        assert levels[10] == LOG_LEVEL_SUMMARY  # Skipped N paths
        assert levels[11] == LOG_LEVEL_INFO  # update skipping
