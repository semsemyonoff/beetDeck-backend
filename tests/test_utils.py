"""Tests for beetdeck/utils.py shared helpers."""

import sqlite3

import pytest

from beetdeck import create_app
from beetdeck.utils import (
    _album_dir_from_items,
    _decode_path,
    _detect_disc_subdirs,
    _find_cover,
    _format_genre,
    _remove_cover_files,
    _resolve_path,
)

# ---------------------------------------------------------------------------
# _decode_path
# ---------------------------------------------------------------------------


def test_decode_path_bytes():
    assert _decode_path(b"/music/track.mp3") == "/music/track.mp3"


def test_decode_path_str():
    assert _decode_path("/music/track.mp3") == "/music/track.mp3"


def test_decode_path_none():
    assert _decode_path(None) == ""


def test_decode_path_empty_string():
    assert _decode_path("") == ""


def test_decode_path_invalid_utf8():
    result = _decode_path(b"\xff\xfe/music")
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _format_genre
# ---------------------------------------------------------------------------


def test_format_genre_plain_string():
    assert _format_genre("Rock") == "Rock"


def test_format_genre_list():
    assert _format_genre(["Rock", "Pop"]) == "Rock, Pop"


def test_format_genre_tuple():
    assert _format_genre(("Jazz", "Blues")) == "Jazz, Blues"


def test_format_genre_null_byte_separator():
    assert _format_genre("Rock\x00Pop") == "Rock, Pop"


def test_format_genre_unicode_null_separator():
    assert _format_genre("Rock\u2400Pop") == "Rock, Pop"


def test_format_genre_empty():
    assert _format_genre("") == ""


def test_format_genre_none():
    assert _format_genre(None) == ""


def test_format_genre_bytes():
    assert _format_genre(b"Rock") == "Rock"


def test_format_genre_strips_whitespace():
    assert _format_genre("  Rock  \x00  Pop  ") == "Rock, Pop"


# ---------------------------------------------------------------------------
# _resolve_path (requires app context for current_app.config)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app_for_resolve(tmp_path):
    db = tmp_path / "library.db"
    db.write_bytes(b"")
    return create_app(test_config={"LIBRARY_DB": str(db), "LIBRARY_ROOT": "", "TESTING": True})


def test_resolve_path_absolute_passthrough(_app_for_resolve):
    with _app_for_resolve.app_context():
        assert _resolve_path(b"/music/track.mp3") == "/music/track.mp3"


def test_resolve_path_str_absolute_passthrough(_app_for_resolve):
    with _app_for_resolve.app_context():
        assert _resolve_path("/data/library/track.mp3") == "/data/library/track.mp3"


def test_resolve_path_none_returns_empty(_app_for_resolve):
    with _app_for_resolve.app_context():
        assert _resolve_path(None) == ""


def test_resolve_path_empty_returns_empty(_app_for_resolve):
    with _app_for_resolve.app_context():
        assert _resolve_path("") == ""


def test_resolve_path_relative_without_library_root(_app_for_resolve):
    """Relative path with no LIBRARY_ROOT passes through unchanged."""
    with _app_for_resolve.app_context():
        assert _resolve_path("artist/album/track.mp3") == "artist/album/track.mp3"


def test_resolve_path_relative_with_library_root(tmp_path):
    """Relative path joined to LIBRARY_ROOT when set."""
    db = tmp_path / "library.db"
    db.write_bytes(b"")
    app = create_app(
        test_config={
            "LIBRARY_DB": str(db),
            "LIBRARY_ROOT": "/data/music",
            "TESTING": True,
        }
    )
    with app.app_context():
        result = _resolve_path("artist/album/track.mp3")
    assert result == "/data/music/artist/album/track.mp3"


def test_resolve_path_bytes_relative_with_library_root(tmp_path):
    db = tmp_path / "library.db"
    db.write_bytes(b"")
    app = create_app(
        test_config={
            "LIBRARY_DB": str(db),
            "LIBRARY_ROOT": "/data/music",
            "TESTING": True,
        }
    )
    with app.app_context():
        result = _resolve_path(b"artist/album/track.mp3")
    assert result == "/data/music/artist/album/track.mp3"


# ---------------------------------------------------------------------------
# _find_cover
# ---------------------------------------------------------------------------


def test_find_cover_finds_cover_jpg(tmp_path):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"fake")
    result = _find_cover(str(tmp_path))
    assert result == str(cover)


def test_find_cover_finds_folder_jpg(tmp_path):
    cover = tmp_path / "folder.jpg"
    cover.write_bytes(b"fake")
    result = _find_cover(str(tmp_path))
    assert result == str(cover)


def test_find_cover_returns_none_when_no_cover(tmp_path):
    result = _find_cover(str(tmp_path))
    assert result is None


def test_find_cover_returns_none_for_nonexistent_dir():
    result = _find_cover("/nonexistent/path")
    assert result is None


def test_find_cover_returns_none_for_none():
    result = _find_cover(None)
    assert result is None


def test_find_cover_prefers_cover_jpg_over_folder_jpg(tmp_path):
    (tmp_path / "folder.jpg").write_bytes(b"folder")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"cover")
    result = _find_cover(str(tmp_path))
    assert result == str(cover)


# ---------------------------------------------------------------------------
# _remove_cover_files
# ---------------------------------------------------------------------------


def test_remove_cover_files_removes_cover_jpg(tmp_path):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"fake")
    _remove_cover_files(str(tmp_path))
    assert not cover.exists()


def test_remove_cover_files_removes_folder_png(tmp_path):
    cover = tmp_path / "folder.png"
    cover.write_bytes(b"fake")
    _remove_cover_files(str(tmp_path))
    assert not cover.exists()


def test_remove_cover_files_leaves_non_cover_images(tmp_path):
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"fake")
    _remove_cover_files(str(tmp_path))
    assert img.exists()


def test_remove_cover_files_removes_numbered_variants(tmp_path):
    cover = tmp_path / "cover.1.jpg"
    cover.write_bytes(b"fake")
    _remove_cover_files(str(tmp_path))
    assert not cover.exists()


def test_remove_cover_files_noop_on_nonexistent_dir():
    _remove_cover_files("/nonexistent/path")


def test_remove_cover_files_noop_on_none():
    _remove_cover_files(None)


# ---------------------------------------------------------------------------
# _album_dir_from_items (requires app context for _get_ro_conn)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app_with_db(tmp_path):
    """App fixture for utils tests that need an app context."""
    db = tmp_path / "library.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT,
            genre TEXT, year INTEGER, artpath BLOB
        );
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, path BLOB, album_id INTEGER,
            title TEXT, artist TEXT, track INTEGER, length REAL, format TEXT
        );
        CREATE TABLE album_attributes (entity_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (entity_id, key));
        CREATE TABLE item_attributes (entity_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (entity_id, key));
        """
    )
    conn.commit()
    conn.close()
    return create_app(
        test_config={"LIBRARY_DB": str(db), "LIBRARY_ROOT": "", "TESTING": True}
    ), str(db)


def test_album_dir_from_items_returns_dir(_app_with_db, tmp_path):
    app, db_path = _app_with_db
    music_dir = tmp_path / "music" / "album"
    music_dir.mkdir(parents=True)
    track_path = str(music_dir / "track.mp3").encode()

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO albums (id, album, albumartist) VALUES (1, 'A', 'B')")
    conn.execute(
        "INSERT INTO items (id, path, album_id, title) VALUES (1, ?, 1, 'T')",
        (track_path,),
    )
    conn.commit()
    conn.close()

    with app.app_context():
        ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        ro_conn.row_factory = sqlite3.Row
        result = _album_dir_from_items(ro_conn, 1)
        ro_conn.close()

    assert result == str(music_dir)


def test_album_dir_from_items_multi_disc_returns_common_parent(_app_with_db, tmp_path):
    """For <album>/CD1/.. + <album>/CD2/.. layout, return <album>."""
    app, db_path = _app_with_db
    album_root = tmp_path / "music" / "Pink Floyd" / "The Wall"
    cd1 = album_root / "CD1"
    cd2 = album_root / "CD2"
    cd1.mkdir(parents=True)
    cd2.mkdir(parents=True)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO albums (id, album, albumartist) VALUES (1, 'The Wall', 'Pink Floyd')")
    conn.execute(
        "INSERT INTO items (id, path, album_id, title, track) VALUES (1, ?, 1, 'T1', 1)",
        (str(cd1 / "01.mp3").encode(),),
    )
    conn.execute(
        "INSERT INTO items (id, path, album_id, title, track) VALUES (2, ?, 1, 'T2', 1)",
        (str(cd2 / "01.mp3").encode(),),
    )
    conn.commit()
    conn.close()

    with app.app_context():
        ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        ro_conn.row_factory = sqlite3.Row
        result = _album_dir_from_items(ro_conn, 1)
        ro_conn.close()
    assert result == str(album_root)


def test_album_dir_from_items_returns_none_for_missing(_app_with_db):
    app, db_path = _app_with_db
    with app.app_context():
        ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        ro_conn.row_factory = sqlite3.Row
        result = _album_dir_from_items(ro_conn, 999)
        ro_conn.close()
    assert result is None


# ---------------------------------------------------------------------------
# _detect_disc_subdirs
# ---------------------------------------------------------------------------


def _row(disc, path, length=200.0):
    return {
        "disc": disc,
        "path": path.encode() if isinstance(path, str) else path,
        "length": length,
    }


def test_detect_disc_subdirs_single_disc(_app_for_resolve):
    items = [
        _row(1, "/m/A/01.mp3", 180.0),
        _row(1, "/m/A/02.mp3", 220.0),
    ]
    with _app_for_resolve.app_context():
        result = _detect_disc_subdirs(items)
    assert result == [{"disc": 1, "dir": "/m/A", "track_count": 2, "duration_sec": 400}]


def test_detect_disc_subdirs_cd1_cd2(_app_for_resolve):
    items = [
        _row(1, "/m/Wall/CD1/01.mp3", 100.0),
        _row(1, "/m/Wall/CD1/02.mp3", 100.0),
        _row(2, "/m/Wall/CD2/01.mp3", 150.0),
    ]
    with _app_for_resolve.app_context():
        result = _detect_disc_subdirs(items)
    assert result == [
        {"disc": 1, "dir": "/m/Wall/CD1", "track_count": 2, "duration_sec": 200},
        {"disc": 2, "dir": "/m/Wall/CD2", "track_count": 1, "duration_sec": 150},
    ]


def test_detect_disc_subdirs_mixed_naming(_app_for_resolve):
    items = [
        _row(1, "/m/Album/Disc 1/01.mp3", 60.0),
        _row(2, "/m/Album/Disc-2/01.mp3", 60.0),
    ]
    with _app_for_resolve.app_context():
        result = _detect_disc_subdirs(items)
    assert [r["dir"] for r in result] == ["/m/Album/Disc 1", "/m/Album/Disc-2"]
    assert [r["disc"] for r in result] == [1, 2]


def test_detect_disc_subdirs_null_disc_coerced_to_one(_app_for_resolve):
    """NULL disc values must be coerced to 1 inside the helper, not by callers.

    Otherwise sorted() raises TypeError comparing None vs int.
    """
    items = [
        _row(None, "/m/A/01.mp3", 100.0),
        _row(1, "/m/A/02.mp3", 100.0),
        _row(2, "/m/A/CD2/01.mp3", 100.0),
    ]
    with _app_for_resolve.app_context():
        result = _detect_disc_subdirs(items)
    discs = [r["disc"] for r in result]
    assert discs == [1, 2]
    disc1 = next(r for r in result if r["disc"] == 1)
    assert disc1["track_count"] == 2


def test_detect_disc_subdirs_empty():
    assert _detect_disc_subdirs([]) == []


# ---------------------------------------------------------------------------
# _find_cover — multi-disc fallback
# ---------------------------------------------------------------------------


def test_find_cover_parent_dir_takes_priority(tmp_path):
    album = tmp_path / "Album"
    cd1 = album / "CD1"
    cd1.mkdir(parents=True)
    parent_cover = album / "cover.jpg"
    parent_cover.write_bytes(b"parent")
    (cd1 / "cover.jpg").write_bytes(b"disc")
    result = _find_cover(str(album), fallback_dirs=[str(cd1)])
    assert result == str(parent_cover)


def test_find_cover_falls_back_to_disc_subdir(tmp_path):
    album = tmp_path / "Album"
    cd1 = album / "CD1"
    cd2 = album / "CD2"
    cd1.mkdir(parents=True)
    cd2.mkdir(parents=True)
    disc_cover = cd1 / "cover.jpg"
    disc_cover.write_bytes(b"disc")
    result = _find_cover(str(album), fallback_dirs=[str(cd1), str(cd2)])
    assert result == str(disc_cover)


def test_find_cover_returns_none_when_no_cover_anywhere(tmp_path):
    album = tmp_path / "Album"
    cd1 = album / "CD1"
    cd1.mkdir(parents=True)
    result = _find_cover(str(album), fallback_dirs=[str(cd1)])
    assert result is None


def test_find_cover_no_fallback_back_compat(tmp_path):
    """Existing single-arg callers keep working unchanged."""
    (tmp_path / "cover.jpg").write_bytes(b"x")
    assert _find_cover(str(tmp_path)) == str(tmp_path / "cover.jpg")


# ---------------------------------------------------------------------------
# create_app — factory smoke tests
# ---------------------------------------------------------------------------


def test_create_app_default_config():
    app = create_app()
    assert app.config["LIBRARY_DB"] == "/data/beets/library.db"
    assert app.config["IMPORT_DIR"] == "/music"
    assert "LIBRARY_ROOT" in app.config


def test_create_app_test_config(tmp_path):
    db = str(tmp_path / "test.db")
    app = create_app(test_config={"LIBRARY_DB": db, "IMPORT_DIR": "/tmp/music"})
    assert app.config["LIBRARY_DB"] == db
    assert app.config["IMPORT_DIR"] == "/tmp/music"


def test_create_app_returns_flask_app():
    from flask import Flask

    app = create_app()
    assert isinstance(app, Flask)


def test_library_root_defaults_to_import_dir(monkeypatch):
    """LIBRARY_ROOT defaults to IMPORT_DIR.

    Covers the standard deployment where the music library and import directory
    are the same volume. Without this default, artpath/item paths stored by
    beets 2.10.0 as relative can't be resolved at request time and cover art
    silently breaks.
    """
    monkeypatch.delenv("BEETS_LIBRARY_ROOT", raising=False)
    monkeypatch.setenv("BEETS_IMPORT_DIR", "/music")
    app = create_app()
    assert app.config["LIBRARY_ROOT"] == "/music"


def test_library_root_env_var_takes_precedence(monkeypatch):
    monkeypatch.setenv("BEETS_LIBRARY_ROOT", "/custom/root")
    monkeypatch.setenv("BEETS_IMPORT_DIR", "/music")
    app = create_app()
    assert app.config["LIBRARY_ROOT"] == "/custom/root"


def test_init_beets_overrides_beets_directory(tmp_path):
    """_init_beets must propagate library_dir into beets.config['directory'].

    beets resolves relative item/artpath stored in the DB against its configured
    directory when writing (cover art, tag writes). Without this override beets
    reads the directory from the user's beets config (or its built-in default),
    which typically does not match the container's mount point and causes
    'No such file or directory' on every write.
    """
    import beets

    from beetdeck.utils import _init_beets

    db = tmp_path / "library.db"
    # The only test that builds a real Library. Since beets 2.13.0 every
    # migration copies the DB first, so a fresh file would drop one .bak per
    # migration into tmp_path before it has a single row worth saving.
    beets.config["create_backup_before_migrations"] = False
    lib = _init_beets(str(db), "/music/custom")
    try:
        configured = beets.config["directory"].as_str()
        assert configured == "/music/custom"
        lib_dir = lib.directory
        if isinstance(lib_dir, bytes):
            lib_dir = lib_dir.decode("utf-8")
        assert lib_dir == "/music/custom"
    finally:
        lib._close()
