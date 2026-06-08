"""Smoke tests for the DB fixtures defined in conftest.py."""

import sqlite3

from tests.conftest import insert_album, insert_item


def test_db_fixture_creates_schema(db_path):
    """The db_path fixture produces a valid SQLite file with beets tables."""
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()

    assert "albums" in tables
    assert "items" in tables
    assert "album_attributes" in tables
    assert "item_attributes" in tables


def test_insert_album(db_path):
    """insert_album() adds a row and returns its id."""
    album_id = insert_album(db_path, album="My Album", albumartist="Some Artist", year=2021)
    assert isinstance(album_id, int)
    assert album_id > 0

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT album, albumartist, year FROM albums WHERE id = ?", (album_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == "My Album"
    assert row[1] == "Some Artist"
    assert row[2] == 2021


def test_insert_item(db_path):
    """insert_item() adds a track row linked to an album."""
    album_id = insert_album(db_path)
    item_id = insert_item(db_path, album_id, title="Track One", track=1)
    assert isinstance(item_id, int)
    assert item_id > 0

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT title, album_id, track FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == "Track One"
    assert row[1] == album_id
    assert row[2] == 1


def test_multiple_albums(db_path):
    """Multiple albums can be inserted with distinct ids."""
    id1 = insert_album(db_path, album="Alpha")
    id2 = insert_album(db_path, album="Beta")
    assert id1 != id2
