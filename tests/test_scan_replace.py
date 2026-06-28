"""Integration tests for the format-replace bug fix (duplicate_action: remove).

These tests run a real ``beet import`` subprocess — nothing is mocked.

Requirements
------------
- ffmpeg must be available in PATH (satisfied in the backend container).
- beets 2.12.0 (pinned in pyproject.toml).

Test design
-----------
Each test builds its own isolated beets config in a tmp directory so it never
touches the running dev library.  The config uses::

    duplicate_action: remove
    incremental: no
    plugins: []          (no network, no MusicBrainz)
    copy: no
    move: no

Audio files are generated on the fly with ffmpeg (1-second sine wave, tagged).

Duplicate-detection mechanism
------------------------------
With ``-A`` (``--noautotag``), beets imports files using their existing ID3/Vorbis
tags without any MusicBrainz lookup.  An album is considered a duplicate when
the library already contains an album with the same albumartist + album name.
``duplicate_action: remove`` removes the existing album items and imports the
new ones in their place, so a format-replace (mp3→flac) lands in one pass.

Why this matters
----------------
With the old config (``duplicate_action: skip``):
  1. Import mp3 → 1 item in library.
  2. Replace mp3 with flac (same track/album).
  3. Rescan: beets sees flac as a duplicate, skips it.  mp3 item stays but the
     file no longer exists.
  4. ``beet update -M`` removes the dangling mp3 item → 0 items (track
     "disappeared").
  5. Next rescan: flac is now importable (no duplicate) → 1 item ("reappears").

With ``duplicate_action: remove`` the flac replaces the mp3 in a single import
pass → no disappear/reappear.
"""

import os
import shutil
import sqlite3
import subprocess
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_audio_file(dest_path):
    """Generate a 1-second silent audio file tagged for beets.

    Uses ffmpeg (required in PATH).  The format is inferred from dest_path's
    extension (mp3 or flac).
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-metadata",
            "title=Test Track",
            "-metadata",
            "artist=Test Artist",
            "-metadata",
            "album=Test Album",
            "-metadata",
            "albumartist=Test Artist",
            "-metadata",
            "date=2020",
            "-metadata",
            "tracknumber=1",
            dest_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr}")
    return dest_path


def _write_beets_config(config_path, db_path, music_dir, duplicate_action="remove"):
    """Write a minimal isolated beets config for testing."""
    config = textwrap.dedent(f"""\
        directory: {music_dir}
        library: {db_path}
        statefile: {os.path.dirname(db_path)}/state.pickle
        import:
          copy: no
          move: no
          write: yes
          incremental: no
          duplicate_action: {duplicate_action}
        plugins: []
    """)
    with open(config_path, "w") as f:
        f.write(config)


def _run_import(config_path, import_dir):
    """Run ``beet import -A -C -I`` and return the completed process."""
    return subprocess.run(
        ["beet", "-c", config_path, "import", "-A", "-C", "-I", import_dir],
        capture_output=True,
        text=True,
    )


def _count_items(db_path):
    """Return the number of rows in the items table (0 if DB doesn't exist yet)."""
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM items")
        return cur.fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _item_formats(db_path):
    """Return a list of format strings for all items in the library."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT format FROM items").fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def audio_files(tmp_path_factory):
    """Pre-generated mp3 and flac files (module-scoped to generate once)."""
    d = tmp_path_factory.mktemp("audio")
    mp3 = _make_audio_file(str(d / "track.mp3"))
    flac = _make_audio_file(str(d / "track.flac"))
    return {"mp3": mp3, "flac": flac}


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestFormatReplace:
    """format-replace (mp3→flac) resolves in one import pass with duplicate_action: remove."""

    def test_format_replace_single_pass(self, tmp_path, audio_files):
        """Replacing an mp3 with flac imports correctly in one beet import call."""
        import_dir = tmp_path / "music"
        import_dir.mkdir()
        db_path = str(tmp_path / "library.db")
        config_path = str(tmp_path / "config.yaml")
        _write_beets_config(config_path, db_path, str(import_dir))

        # Step 1: import the mp3.
        shutil.copy2(audio_files["mp3"], str(import_dir / "track.mp3"))
        result = _run_import(config_path, str(import_dir))
        assert result.returncode == 0, f"First import failed:\n{result.stderr}"
        assert _count_items(db_path) == 1, "Expected 1 item after first import"

        # Step 2: replace mp3 with flac.
        os.remove(str(import_dir / "track.mp3"))
        shutil.copy2(audio_files["flac"], str(import_dir / "track.flac"))

        # Step 3: second import — with duplicate_action: remove the flac replaces the
        # mp3 item in one pass; no "disappear" phase.
        result = _run_import(config_path, str(import_dir))
        assert result.returncode == 0, f"Second import failed:\n{result.stderr}"
        assert _count_items(db_path) == 1, (
            "Item count must stay at 1 after format replace (format-replace bug: track disappeared)"
        )

        # Verify the library now references the flac, not the mp3.
        formats = _item_formats(db_path)
        assert any(f.upper() == "FLAC" for f in formats), (
            f"Expected FLAC item after replace; got formats={formats}"
        )

    def test_reimport_no_change_loses_nothing(self, tmp_path, audio_files):
        """Re-importing an unchanged library preserves all items."""
        import_dir = tmp_path / "music"
        import_dir.mkdir()
        db_path = str(tmp_path / "library.db")
        config_path = str(tmp_path / "config.yaml")
        _write_beets_config(config_path, db_path, str(import_dir))

        shutil.copy2(audio_files["mp3"], str(import_dir / "track.mp3"))

        # First import.
        result = _run_import(config_path, str(import_dir))
        assert result.returncode == 0, f"First import failed:\n{result.stderr}"
        assert _count_items(db_path) == 1

        # Re-import with no file changes — library must not lose items.
        result = _run_import(config_path, str(import_dir))
        assert result.returncode == 0, f"Re-import failed:\n{result.stderr}"
        assert _count_items(db_path) == 1, "Re-import with no changes should preserve existing item"
