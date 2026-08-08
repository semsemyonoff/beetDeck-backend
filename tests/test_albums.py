"""Tests for albums blueprint routes: /api/album/<id>, /api/album/<id>/track/<id>/tags."""

import sqlite3

from tests.conftest import insert_album, insert_item

# ---------------------------------------------------------------------------
# GET /api/album/<id>
# ---------------------------------------------------------------------------


class TestAlbumDetail:
    def test_returns_404_for_missing_album(self, client):
        resp = client.get("/api/album/9999")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_returns_album_fields(self, client, db_path):
        album_id = insert_album(
            db_path,
            album="OK Computer",
            albumartist="Radiohead",
            year=1997,
            original_year=1997,
            label="Parlophone",
            mb_albumid="abc-123",
            genre="Alternative Rock",
        )
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == album_id
        assert data["album"] == "OK Computer"
        assert data["albumartist"] == "Radiohead"
        assert data["year"] == 1997
        assert data["label"] == "Parlophone"
        assert data["mb_albumid"] == "abc-123"
        assert data["genre"] == "Alternative Rock"

    def test_tracks_list_present(self, client, db_path):
        album_id = insert_album(db_path, album="The Wall", albumartist="Pink Floyd")
        insert_item(db_path, album_id, title="Another Brick", track=1, disc=1)
        insert_item(db_path, album_id, title="Comfortably Numb", track=2, disc=2)
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["tracks"]) == 2

    def test_tracks_sorted_by_disc_then_track(self, client, db_path):
        album_id = insert_album(db_path, album="Multi Disc", albumartist="Artist A")
        insert_item(db_path, album_id, title="D2T1", track=1, disc=2)
        insert_item(db_path, album_id, title="D1T1", track=1, disc=1)
        insert_item(db_path, album_id, title="D1T2", track=2, disc=1)
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        titles = [t["title"] for t in resp.get_json()["tracks"]]
        assert titles == ["D1T1", "D1T2", "D2T1"]

    def test_track_fields_present(self, client, db_path):
        album_id = insert_album(db_path, album="Abbey Road", albumartist="The Beatles")
        insert_item(
            db_path,
            album_id,
            title="Come Together",
            artist="The Beatles",
            track=1,
            disc=1,
            length=259.0,
            format="FLAC",
            bitrate=1000,
            samplerate=44100,
        )
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        track = resp.get_json()["tracks"][0]
        assert track["title"] == "Come Together"
        assert track["artist"] == "The Beatles"
        assert track["track"] == 1
        assert track["disc"] == 1
        assert track["length"] == "4:19"
        assert track["format"] == "FLAC"
        assert track["bitrate"] == 1000
        assert track["samplerate"] == 44100
        assert "has_lrc" in track

    def test_has_lrc_false_for_nonexistent_file(self, client, db_path):
        album_id = insert_album(db_path, album="Album", albumartist="Artist")
        insert_item(
            db_path,
            album_id,
            title="Track",
            path=b"/nonexistent/path/track.mp3",
        )
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        track = resp.get_json()["tracks"][0]
        assert track["has_lrc"] is False

    def test_tagged_false_by_default(self, client, db_path):
        album_id = insert_album(db_path, album="Untagged", albumartist="Artist B")
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["tagged"] is False

    def test_tagged_true_when_attribute_set(self, client, db_path):
        album_id = insert_album(db_path, album="Tagged", albumartist="Artist C")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO album_attributes (entity_id, key, value) VALUES (?, 'beetdeck_tagged', '1')",
            (album_id,),
        )
        conn.commit()
        conn.close()
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["tagged"] is True

    def test_has_cover_false_when_no_artpath(self, client, db_path):
        album_id = insert_album(db_path, album="No Cover", albumartist="Artist D", artpath=None)
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["has_cover"] is False

    def test_genre_formatted(self, client, db_path):
        album_id = insert_album(db_path, album="Genred", albumartist="Artist E", genre="Rock")
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["genre"] == "Rock"

    def test_genres_field_takes_precedence_over_genre(self, client, db_path):
        """beets 2.10.0 stores genres in a list field; genres column takes precedence."""
        album_id = insert_album(
            db_path,
            album="Multi Genre",
            albumartist="Artist X",
            genre="Old Genre",
            genres="Electronic, Ambient",
        )
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["genre"] == "Electronic, Ambient"

    def test_empty_album_has_no_tracks(self, client, db_path):
        album_id = insert_album(db_path, album="Empty", albumartist="Artist F")
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["tracks"] == []

    def test_length_formatted_as_mm_ss(self, client, db_path):
        album_id = insert_album(db_path, album="Timed", albumartist="Artist G")
        insert_item(db_path, album_id, title="Short", length=65.0)
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["tracks"][0]["length"] == "1:05"

    def test_length_zero_returns_empty_string(self, client, db_path):
        album_id = insert_album(db_path, album="Quiet", albumartist="Artist H")
        insert_item(db_path, album_id, title="Silence", length=None)
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["tracks"][0]["length"] == ""

    def test_length_sec_and_filename_present(self, client, db_path):
        album_id = insert_album(db_path, album="Raw", albumartist="Artist R")
        insert_item(
            db_path,
            album_id,
            title="Come Together",
            length=259.0,
            path=b"/music/beatles/abbey road/01 Come Together.flac",
        )
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        track = resp.get_json()["tracks"][0]
        assert track["length_sec"] == 259.0
        assert track["length"] == "4:19"
        assert track["filename"] == "01 Come Together.flac"

    def test_length_sec_null_when_length_missing(self, client, db_path):
        album_id = insert_album(db_path, album="Untimed", albumartist="Artist T")
        insert_item(db_path, album_id, title="Unknown", length=None)
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["tracks"][0]["length_sec"] is None

    def test_length_sec_null_when_length_zero(self, client, db_path):
        album_id = insert_album(db_path, album="Zeroed", albumartist="Artist U")
        insert_item(db_path, album_id, title="Silence", length=0.0)
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["tracks"][0]["length_sec"] is None

    def test_filename_empty_when_no_path(self, client, db_path):
        album_id = insert_album(db_path, album="Pathless", albumartist="Artist V")
        insert_item(db_path, album_id, title="Orphan", path=None)
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["tracks"][0]["filename"] == ""

    def test_filename_from_relative_path(self, client, db_path):
        album_id = insert_album(db_path, album="Relative", albumartist="Artist W")
        insert_item(db_path, album_id, title="Track", path=b"artist/album/02 Track.mp3")
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["tracks"][0]["filename"] == "02 Track.mp3"

    def test_filename_decodes_non_utf8_path_bytes(self, client, db_path):
        album_id = insert_album(db_path, album="Broken", albumartist="Artist X")
        insert_item(db_path, album_id, title="Mojibake", path=b"/music/a/b/03 Caf\xe9.mp3")
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        filename = resp.get_json()["tracks"][0]["filename"]
        assert filename.startswith("03 Caf")
        assert filename.endswith(".mp3")
        assert "�" in filename

    def test_path_field_present(self, client, db_path):
        album_id = insert_album(db_path, album="Pathed", albumartist="Artist I")
        insert_item(db_path, album_id, title="Track", path=b"/music/artist/album/track.mp3")
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["path"] == "/music/artist/album"

    def test_single_disc_multi_disc_false(self, client, db_path):
        album_id = insert_album(db_path, album="Solo", albumartist="Artist S")
        insert_item(
            db_path,
            album_id,
            title="T1",
            track=1,
            disc=1,
            length=120.0,
            path=b"/music/artist/solo/01.mp3",
        )
        insert_item(
            db_path,
            album_id,
            title="T2",
            track=2,
            disc=1,
            length=180.0,
            path=b"/music/artist/solo/02.mp3",
        )
        data = client.get(f"/api/album/{album_id}").get_json()
        assert data["multi_disc"] is False
        assert len(data["discs"]) == 1
        assert data["discs"][0]["disc"] == 1
        assert data["discs"][0]["dir"] == "/music/artist/solo"
        assert data["discs"][0]["track_count"] == 2
        assert data["discs"][0]["duration_sec"] == 300
        assert data["path"] == "/music/artist/solo"

    def test_multi_disc_true_with_two_discs(self, client, db_path):
        album_id = insert_album(db_path, album="The Wall", albumartist="Pink Floyd")
        insert_item(
            db_path,
            album_id,
            title="D1T1",
            track=1,
            disc=1,
            length=200.0,
            path=b"/music/pink floyd/the wall/CD1/01.mp3",
        )
        insert_item(
            db_path,
            album_id,
            title="D1T2",
            track=2,
            disc=1,
            length=220.0,
            path=b"/music/pink floyd/the wall/CD1/02.mp3",
        )
        insert_item(
            db_path,
            album_id,
            title="D2T1",
            track=1,
            disc=2,
            length=300.0,
            path=b"/music/pink floyd/the wall/CD2/01.mp3",
        )
        data = client.get(f"/api/album/{album_id}").get_json()
        assert data["multi_disc"] is True
        assert data["path"] == "/music/pink floyd/the wall"
        assert len(data["discs"]) == 2
        d1, d2 = data["discs"]
        assert d1["disc"] == 1
        assert d1["dir"] == "/music/pink floyd/the wall/CD1"
        assert d1["track_count"] == 2
        assert d1["duration_sec"] == 420
        assert d2["disc"] == 2
        assert d2["dir"] == "/music/pink floyd/the wall/CD2"
        assert d2["track_count"] == 1
        assert d2["duration_sec"] == 300

    def test_track_disc_coerced_to_int_when_null(self, client, db_path):
        album_id = insert_album(db_path, album="NullDisc", albumartist="Artist Z")
        insert_item(db_path, album_id, title="T1", track=1, disc=None)
        data = client.get(f"/api/album/{album_id}").get_json()
        assert data["tracks"][0]["disc"] == 1

    def test_original_year_preferred_over_year(self, client, db_path):
        album_id = insert_album(
            db_path, album="Reissue", albumartist="Artist J", year=2005, original_year=1975
        )
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        assert resp.get_json()["year"] == 1975

    def test_has_lyrics_true_when_embedded_lyrics(self, client, db_path):
        album_id = insert_album(db_path, album="Lyrics Album", albumartist="Artist L")
        insert_item(db_path, album_id, title="Lyric Track", lyrics="Some lyrics text")
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        track = resp.get_json()["tracks"][0]
        assert track["has_lyrics"] is True

    def test_has_lyrics_true_when_readable_lrc(self, client, db_path, tmp_path):
        lrc_file = tmp_path / "song.lrc"
        lrc_file.write_text("[00:01.00] Hello world\n", encoding="utf-8")
        audio_path = str(tmp_path / "song.mp3")
        album_id = insert_album(db_path, album="LRC Album", albumartist="Artist LRC")
        insert_item(
            db_path,
            album_id,
            title="LRC Track",
            path=audio_path.encode(),
        )
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        track = resp.get_json()["tracks"][0]
        assert track["has_lrc"] is True
        assert track["has_lyrics"] is True

    def test_has_lyrics_false_when_lrc_empty(self, client, db_path, tmp_path):
        lrc_file = tmp_path / "empty.lrc"
        lrc_file.write_text("", encoding="utf-8")
        audio_path = str(tmp_path / "empty.mp3")
        album_id = insert_album(db_path, album="Empty LRC Album", albumartist="Artist EL")
        insert_item(
            db_path,
            album_id,
            title="Empty LRC Track",
            path=audio_path.encode(),
        )
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        track = resp.get_json()["tracks"][0]
        assert track["has_lrc"] is True
        assert track["has_lyrics"] is False

    def test_has_lyrics_false_when_no_lyrics_and_no_lrc(self, client, db_path):
        album_id = insert_album(db_path, album="Plain Album", albumartist="Artist PL")
        insert_item(
            db_path,
            album_id,
            title="Plain Track",
            path=b"/nonexistent/path/plain.mp3",
        )
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        track = resp.get_json()["tracks"][0]
        assert track["has_lrc"] is False
        assert track["has_lyrics"] is False
        assert "has_lyrics" in track

    def test_has_bpm_true_when_bpm_set(self, client, db_path):
        album_id = insert_album(db_path, album="BPM Album", albumartist="Artist BPM")
        insert_item(db_path, album_id, title="BPM Track", bpm=128)
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        track = resp.get_json()["tracks"][0]
        assert track["has_bpm"] is True
        assert track["bpm"] == 128

    def test_has_bpm_false_when_bpm_null(self, client, db_path):
        album_id = insert_album(db_path, album="No BPM Album", albumartist="Artist NB")
        insert_item(db_path, album_id, title="No BPM Track")
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        track = resp.get_json()["tracks"][0]
        assert track["has_bpm"] is False
        assert "has_bpm" in track
        assert track["bpm"] is None

    def test_has_bpm_false_when_bpm_zero(self, client, db_path):
        album_id = insert_album(db_path, album="Zero BPM Album", albumartist="Artist ZB")
        insert_item(db_path, album_id, title="Zero BPM Track", bpm=0)
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        track = resp.get_json()["tracks"][0]
        assert track["has_bpm"] is False
        assert track["bpm"] is None


# ---------------------------------------------------------------------------
# GET /api/album/<id>/track/<id>/tags
# ---------------------------------------------------------------------------


class TestTrackTags:
    def test_returns_404_for_missing_track(self, client, db_path):
        album_id = insert_album(db_path, album="Album", albumartist="Artist")
        resp = client.get(f"/api/album/{album_id}/track/9999/tags")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Track not found"

    def test_returns_404_when_track_belongs_to_different_album(self, client, db_path):
        album_id_a = insert_album(db_path, album="Album A", albumartist="Artist A")
        album_id_b = insert_album(db_path, album="Album B", albumartist="Artist B")
        item_id = insert_item(db_path, album_id_b, title="Track B")
        resp = client.get(f"/api/album/{album_id_a}/track/{item_id}/tags")
        assert resp.status_code == 404

    def test_returns_track_tags(self, client, db_path):
        album_id = insert_album(db_path, album="Tagging Album", albumartist="Artist K")
        item_id = insert_item(
            db_path,
            album_id,
            title="Tagged Track",
            artist="Artist K",
            format="FLAC",
            year=2000,
        )
        resp = client.get(f"/api/album/{album_id}/track/{item_id}/tags")
        assert resp.status_code == 200
        tags = resp.get_json()
        assert tags["title"] == "Tagged Track"
        assert tags["artist"] == "Artist K"
        assert tags["format"] == "FLAC"

    def test_excludes_id_album_id_path_keys(self, client, db_path):
        album_id = insert_album(db_path, album="Private Album", albumartist="Artist L")
        item_id = insert_item(db_path, album_id, title="Track L")
        resp = client.get(f"/api/album/{album_id}/track/{item_id}/tags")
        assert resp.status_code == 200
        tags = resp.get_json()
        assert "id" not in tags
        assert "album_id" not in tags
        assert "path" not in tags

    def test_includes_item_attributes(self, client, db_path):
        album_id = insert_album(db_path, album="Attr Album", albumartist="Artist M")
        item_id = insert_item(db_path, album_id, title="Attr Track")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, 'custom_key', 'custom_value')",
            (item_id,),
        )
        conn.commit()
        conn.close()
        resp = client.get(f"/api/album/{album_id}/track/{item_id}/tags")
        assert resp.status_code == 200
        tags = resp.get_json()
        assert tags.get("custom_key") == "custom_value"

    def test_skips_empty_and_zero_values(self, client, db_path):
        album_id = insert_album(db_path, album="Sparse Album", albumartist="Artist N")
        item_id = insert_item(db_path, album_id, title="Sparse Track", bitrate=0, samplerate=0)
        resp = client.get(f"/api/album/{album_id}/track/{item_id}/tags")
        assert resp.status_code == 200
        tags = resp.get_json()
        assert "bitrate" not in tags
        assert "samplerate" not in tags


# ---------------------------------------------------------------------------
# OpenAPI spec coverage (spectree decoration — doc-only, response validation off)
# ---------------------------------------------------------------------------


class TestAlbumsOpenAPISpec:
    """The two albums GETs are decorated with ``@api.validate`` (path params +
    response docs only, no body/query validation). Assert they appear in the
    generated spec, tagged ``albums``, without changing the success bodies."""

    def test_album_detail_in_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/album/{album_id}"]["get"]
        assert "albums" in op["tags"]
        assert "200" in op["responses"]
        assert "404" in op["responses"]

    def test_track_tags_in_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/album/{album_id}/track/{item_id}/tags"]["get"]
        assert "albums" in op["tags"]
        assert "200" in op["responses"]
        assert "404" in op["responses"]

    def test_album_detail_success_body_unchanged(self, client, db_path):
        """Response validation is off → handler body is untouched by decoration."""
        album_id = insert_album(db_path, album="Spec Album", albumartist="Spec Artist")
        resp = client.get(f"/api/album/{album_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == album_id
        assert data["album"] == "Spec Album"
        assert data["tracks"] == []

    def test_album_detail_404_body_unchanged(self, client):
        resp = client.get("/api/album/123456")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"
