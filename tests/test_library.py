"""Tests for library blueprint routes: /, /api/library, /api/search, /api/artist."""

from tests.conftest import insert_album, insert_item


def _make_multidisc_album(db_path, tmp_path, artist="Pink Floyd", album="The Wall"):
    """Seed a multi-disc album with cover.jpg in the parent dir (not CD1/)."""
    album_root = tmp_path / artist / album
    cd1 = album_root / "CD1"
    cd2 = album_root / "CD2"
    cd1.mkdir(parents=True)
    cd2.mkdir(parents=True)
    cover = album_root / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    track1 = cd1 / "01.mp3"
    track2 = cd2 / "01.mp3"
    track1.write_bytes(b"")
    track2.write_bytes(b"")
    album_id = insert_album(db_path, albumartist=artist, album=album, artpath=None)
    insert_item(db_path, album_id, path=str(track1).encode(), disc=1)
    insert_item(db_path, album_id, path=str(track2).encode(), disc=2)
    return album_id


def _make_multidisc_album_disc_subdir_cover(
    db_path, tmp_path, artist="Pink Floyd", album="The Wall"
):
    """Seed a multi-disc album with cover.jpg only inside CD1/ (not the parent)."""
    album_root = tmp_path / artist / album
    cd1 = album_root / "CD1"
    cd2 = album_root / "CD2"
    cd1.mkdir(parents=True)
    cd2.mkdir(parents=True)
    cover = cd1 / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    track1 = cd1 / "01.mp3"
    track2 = cd2 / "01.mp3"
    track1.write_bytes(b"")
    track2.write_bytes(b"")
    album_id = insert_album(db_path, albumartist=artist, album=album, artpath=None)
    insert_item(db_path, album_id, path=str(track1).encode(), disc=1)
    insert_item(db_path, album_id, path=str(track2).encode(), disc=2)
    return album_id


# ---------------------------------------------------------------------------
# GET /api/library
# ---------------------------------------------------------------------------


class TestLibraryEndpoint:
    def test_returns_empty_list_when_no_albums(self, client):
        resp = client.get("/api/library")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == []

    def test_returns_artist_with_albums(self, client, db_path):
        insert_album(db_path, album="OK Computer", albumartist="Radiohead", year=1997)
        resp = client.get("/api/library")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["artist"] == "Radiohead"
        assert len(data[0]["albums"]) == 1
        assert data[0]["albums"][0]["album"] == "OK Computer"
        assert data[0]["albums"][0]["year"] == 1997

    def test_artists_sorted_case_insensitive(self, client, db_path):
        insert_album(db_path, albumartist="Zappa", album="Hot Rats")
        insert_album(db_path, albumartist="aphex twin", album="Selected Ambient")
        insert_album(db_path, albumartist="Boards of Canada", album="Music Has the Right")
        resp = client.get("/api/library")
        assert resp.status_code == 200
        data = resp.get_json()
        artists = [entry["artist"] for entry in data]
        assert artists == sorted(artists, key=str.lower)

    def test_tagged_field_present(self, client, db_path):
        album_id = insert_album(db_path, album="Tagged Album", albumartist="Artist A")
        # Insert beetdeck_tagged attribute
        import sqlite3

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO album_attributes (entity_id, key, value) VALUES (?, 'beetdeck_tagged', '1')",
            (album_id,),
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/library")
        assert resp.status_code == 200
        data = resp.get_json()
        album = data[0]["albums"][0]
        assert album["tagged"] is True

    def test_has_cover_false_when_no_art(self, client, db_path):
        insert_album(db_path, album="No Cover", albumartist="Artist B", artpath=None)
        resp = client.get("/api/library")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data[0]["albums"][0]["has_cover"] is False

    def test_not_initialized_when_db_missing(self, app):
        app.config["LIBRARY_DB"] = "/nonexistent/path/library.db"
        client = app.test_client()
        resp = client.get("/api/library")
        assert resp.status_code == 503
        assert resp.get_json()["error"] == "not_initialized"

    def test_multiple_albums_per_artist(self, client, db_path):
        insert_album(db_path, albumartist="Artist C", album="Album One", year=2000)
        insert_album(db_path, albumartist="Artist C", album="Album Two", year=2005)
        resp = client.get("/api/library")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert len(data[0]["albums"]) == 2

    def test_unknown_artist_fallback(self, client, db_path):
        insert_album(db_path, albumartist=None, album="Mystery Album")
        resp = client.get("/api/library")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data[0]["artist"] == "Unknown Artist"


# ---------------------------------------------------------------------------
# GET /api/search
# ---------------------------------------------------------------------------


class TestSearchEndpoint:
    def test_empty_query_returns_empty_results(self, client):
        resp = client.get("/api/search")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"artists": [], "albums": [], "tracks": []}

    def test_whitespace_query_returns_empty_results(self, client):
        resp = client.get("/api/search?q=   ")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"artists": [], "albums": [], "tracks": []}

    def test_artist_search(self, client, db_path):
        insert_album(db_path, albumartist="Pink Floyd", album="The Wall")
        resp = client.get("/api/search?q=pink")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "Pink Floyd" in data["artists"]

    def test_album_search(self, client, db_path):
        insert_album(db_path, albumartist="Pink Floyd", album="The Wall")
        resp = client.get("/api/search?q=wall")
        assert resp.status_code == 200
        data = resp.get_json()
        album_names = [a["album"] for a in data["albums"]]
        assert "The Wall" in album_names

    def test_track_search(self, client, db_path):
        album_id = insert_album(db_path, albumartist="Artist D", album="Album D")
        insert_item(db_path, album_id, title="Comfortably Numb", artist="Artist D")
        resp = client.get("/api/search?q=comfortably")
        assert resp.status_code == 200
        data = resp.get_json()
        track_titles = [t["title"] for t in data["tracks"]]
        assert "Comfortably Numb" in track_titles

    def test_search_is_case_insensitive(self, client, db_path):
        insert_album(db_path, albumartist="The Beatles", album="Abbey Road")
        resp = client.get("/api/search?q=ABBEY")
        assert resp.status_code == 200
        data = resp.get_json()
        album_names = [a["album"] for a in data["albums"]]
        assert "Abbey Road" in album_names

    def test_no_match_returns_empty_lists(self, client, db_path):
        insert_album(db_path, albumartist="Artist E", album="Album E")
        resp = client.get("/api/search?q=xyznotfound")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["artists"] == []
        assert data["albums"] == []
        assert data["tracks"] == []

    def test_track_search_includes_album_info(self, client, db_path):
        album_id = insert_album(db_path, albumartist="Led Zeppelin", album="IV")
        insert_item(db_path, album_id, title="Stairway to Heaven", artist="Led Zeppelin")
        resp = client.get("/api/search?q=stairway")
        assert resp.status_code == 200
        data = resp.get_json()
        track = data["tracks"][0]
        assert track["album"] == "IV"
        assert track["albumartist"] == "Led Zeppelin"
        assert track["album_id"] == album_id


# ---------------------------------------------------------------------------
# GET /api/artist
# ---------------------------------------------------------------------------


class TestArtistEndpoint:
    def test_missing_name_returns_400(self, client):
        resp = client.get("/api/artist")
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_unknown_artist_returns_empty_albums(self, client):
        resp = client.get("/api/artist?name=NoSuchArtist")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["artist"] == "NoSuchArtist"
        assert data["albums"] == []

    def test_artist_albums_returned(self, client, db_path):
        insert_album(db_path, albumartist="David Bowie", album="Heroes", year=1977)
        insert_album(db_path, albumartist="David Bowie", album="Ziggy Stardust", year=1972)
        resp = client.get("/api/artist?name=David+Bowie")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["artist"] == "David Bowie"
        assert len(data["albums"]) == 2

    def test_artist_albums_ordered_by_year(self, client, db_path):
        insert_album(db_path, albumartist="David Bowie", album="Heroes", year=1977)
        insert_album(db_path, albumartist="David Bowie", album="Ziggy Stardust", year=1972)
        resp = client.get("/api/artist?name=David+Bowie")
        assert resp.status_code == 200
        data = resp.get_json()
        years = [a["year"] for a in data["albums"]]
        assert years == sorted(years)

    def test_artist_album_has_expected_fields(self, client, db_path):
        insert_album(db_path, albumartist="Nick Cave", album="Murder Ballads", year=1996)
        resp = client.get("/api/artist?name=Nick+Cave")
        assert resp.status_code == 200
        data = resp.get_json()
        album = data["albums"][0]
        assert "id" in album
        assert "album" in album
        assert "year" in album
        assert "tagged" in album
        assert "has_cover" in album

    def test_other_artist_albums_not_included(self, client, db_path):
        insert_album(db_path, albumartist="Artist X", album="X Album")
        insert_album(db_path, albumartist="Artist Y", album="Y Album")
        resp = client.get("/api/artist?name=Artist+X")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["albums"]) == 1
        assert data["albums"][0]["album"] == "X Album"

    def test_unknown_artist_matches_null_albumartist(self, client, db_path):
        insert_album(db_path, albumartist=None, album="Null Artist Album")
        resp = client.get("/api/artist?name=Unknown+Artist")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["artist"] == "Unknown Artist"
        assert len(data["albums"]) == 1
        assert data["albums"][0]["album"] == "Null Artist Album"

    def test_unknown_artist_matches_empty_string_albumartist(self, client, db_path):
        insert_album(db_path, albumartist="", album="Empty Artist Album")
        resp = client.get("/api/artist?name=Unknown+Artist")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["artist"] == "Unknown Artist"
        assert len(data["albums"]) == 1
        assert data["albums"][0]["album"] == "Empty Artist Album"

    def test_unknown_artist_matches_both_null_and_empty(self, client, db_path):
        insert_album(db_path, albumartist=None, album="Null Album")
        insert_album(db_path, albumartist="", album="Empty Album")
        resp = client.get("/api/artist?name=Unknown+Artist")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["albums"]) == 2

    def test_normal_artist_not_matched_by_unknown_artist_query(self, client, db_path):
        insert_album(db_path, albumartist="Real Artist", album="Real Album")
        resp = client.get("/api/artist?name=Unknown+Artist")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["albums"] == []


# ---------------------------------------------------------------------------
# Multi-disc cover lookup regression — covers should be found at the album's
# common parent dir, not just the first item's disc subdir.
# ---------------------------------------------------------------------------


class TestMultiDiscCoverLookup:
    def test_library_finds_cover_in_parent_dir(self, client, db_path, tmp_path):
        _make_multidisc_album(db_path, tmp_path)
        resp = client.get("/api/library")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data[0]["albums"][0]["has_cover"] is True

    def test_search_finds_cover_in_parent_dir(self, client, db_path, tmp_path):
        _make_multidisc_album(db_path, tmp_path)
        resp = client.get("/api/search?q=wall")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["albums"][0]["has_cover"] is True

    def test_artist_finds_cover_in_parent_dir(self, client, db_path, tmp_path):
        _make_multidisc_album(db_path, tmp_path)
        resp = client.get("/api/artist?name=Pink+Floyd")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["albums"][0]["has_cover"] is True

    def test_library_finds_cover_in_disc_subdir(self, client, db_path, tmp_path):
        _make_multidisc_album_disc_subdir_cover(db_path, tmp_path)
        resp = client.get("/api/library")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data[0]["albums"][0]["has_cover"] is True

    def test_search_finds_cover_in_disc_subdir(self, client, db_path, tmp_path):
        _make_multidisc_album_disc_subdir_cover(db_path, tmp_path)
        resp = client.get("/api/search?q=wall")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["albums"][0]["has_cover"] is True

    def test_artist_finds_cover_in_disc_subdir(self, client, db_path, tmp_path):
        _make_multidisc_album_disc_subdir_cover(db_path, tmp_path)
        resp = client.get("/api/artist?name=Pink+Floyd")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["albums"][0]["has_cover"] is True
