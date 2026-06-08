"""Tests for lyrics blueprint routes."""

from src import state
from tests.conftest import insert_album, insert_item  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lyrics_obj(
    mocker,
    text="Some lyrics",
    full_text=None,
    synced=False,
    backend="genius",
    url=None,
    language=None,
    translation_language=None,
):
    """Create a mock lyrics result object."""
    obj = mocker.MagicMock()
    obj.text = text
    obj.full_text = full_text if full_text is not None else text
    obj.synced = synced
    obj.backend = backend
    obj.url = url
    obj.language = language
    obj.translation_language = translation_language
    return obj


# ---------------------------------------------------------------------------
# GET /api/album/<id>/track/<id>/lyrics
# ---------------------------------------------------------------------------


class TestTrackLyrics:
    def test_returns_404_when_track_not_found(self, client, db_path):
        insert_album(db_path)
        resp = client.get("/api/album/1/track/9999/lyrics")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Track not found"

    def test_returns_no_lyrics_when_empty(self, client, db_path):
        album_id = insert_album(db_path)
        item_id = insert_item(db_path, album_id, lyrics=None, path=b"/music/test/track.mp3")
        resp = client.get(f"/api/album/{album_id}/track/{item_id}/lyrics")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_lyrics"] is False
        assert data["lyrics"] == ""
        assert data["source"] is None

    def test_returns_embedded_lyrics(self, client, db_path):
        album_id = insert_album(db_path)
        item_id = insert_item(
            db_path, album_id, lyrics="Hello world", path=b"/music/test/track.mp3"
        )
        resp = client.get(f"/api/album/{album_id}/track/{item_id}/lyrics")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_lyrics"] is True
        assert data["lyrics"] == "Hello world"
        assert data["source"] == "embedded"

    def test_returns_lrc_file_when_no_embedded_lyrics(self, client, db_path, tmp_path, mocker):
        album_id = insert_album(db_path)
        lrc_file = tmp_path / "track.lrc"
        lrc_file.write_text("[00:00.00]Synced lyrics", encoding="utf-8")
        audio_path = str(tmp_path / "track.mp3")
        item_id = insert_item(db_path, album_id, lyrics=None, path=audio_path.encode())
        resp = client.get(f"/api/album/{album_id}/track/{item_id}/lyrics")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_lyrics"] is True
        assert data["source"] == "lrc_file"
        assert "[00:00.00]Synced lyrics" in data["lyrics"]

    def test_prefers_embedded_over_lrc(self, client, db_path, tmp_path):
        album_id = insert_album(db_path)
        lrc_file = tmp_path / "track.lrc"
        lrc_file.write_text("[00:00.00]Synced", encoding="utf-8")
        audio_path = str(tmp_path / "track.mp3")
        item_id = insert_item(db_path, album_id, lyrics="Embedded lyrics", path=audio_path.encode())
        resp = client.get(f"/api/album/{album_id}/track/{item_id}/lyrics")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["source"] == "embedded"
        assert data["lyrics"] == "Embedded lyrics"


# ---------------------------------------------------------------------------
# POST /api/album/<id>/track/<id>/lyrics/fetch
# ---------------------------------------------------------------------------


class TestFetchTrackLyrics:
    def test_returns_404_when_track_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/1/track/9999/lyrics/fetch")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Track not found"

    def test_returns_404_when_item_belongs_to_different_album(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 99
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/1/track/1/lyrics/fetch")
        assert resp.status_code == 404

    def test_returns_500_when_lyrics_plugin_not_loaded(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[])
        resp = client.post("/api/album/1/track/1/lyrics/fetch")
        assert resp.status_code == 500
        assert resp.get_json()["error"] == "lyrics plugin not loaded"

    def test_returns_not_found_when_no_lyrics(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.lyrics = ""
        mock_item.path = b"/music/track.mp3"
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lyrics"
        mock_plugin.find_lyrics.return_value = None

        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        resp = client.post("/api/album/1/track/1/lyrics/fetch")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["found"] is False

    def test_returns_lyrics_preview_when_found(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.artist = "Artist"
        mock_item.title = "Song"
        mock_item.lyrics = ""
        mock_item.path = b"/music/track.mp3"
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item

        lyrics_obj = _make_lyrics_obj(mocker, text="Verse 1", full_text="Verse 1\nVerse 2")

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lyrics"
        mock_plugin.find_lyrics.return_value = lyrics_obj

        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        resp = client.post("/api/album/1/track/1/lyrics/fetch")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["found"] is True
        assert data["new_lyrics"] == "Verse 1\nVerse 2"
        assert data["new_backend"] == "genius"

    def test_stores_lyrics_obj_in_identify_tasks(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.artist = "Artist"
        mock_item.title = "Song"
        mock_item.lyrics = ""
        mock_item.path = b"/music/track.mp3"
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item

        lyrics_obj = _make_lyrics_obj(mocker)
        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lyrics"
        mock_plugin.find_lyrics.return_value = lyrics_obj

        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        state.identify_tasks.clear()
        client.post("/api/album/1/track/1/lyrics/fetch")
        assert "lyrics_1" in state.identify_tasks
        assert state.identify_tasks["lyrics_1"]["_lyrics_obj"] is lyrics_obj


# ---------------------------------------------------------------------------
# POST /api/album/<id>/track/<id>/lyrics/confirm
# ---------------------------------------------------------------------------


class TestConfirmTrackLyrics:
    def test_returns_400_when_no_task(self, client):
        state.identify_tasks.clear()
        resp = client.post("/api/album/1/track/1/lyrics/confirm")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No lyrics to confirm"

    def test_returns_404_when_track_not_found(self, client, mocker):
        state.identify_tasks["lyrics_1"] = {"_lyrics_obj": mocker.MagicMock()}
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/1/track/1/lyrics/confirm")
        assert resp.status_code == 404

    def test_writes_lyrics_to_item(self, client, mocker):
        lyrics_obj = _make_lyrics_obj(mocker, text="Lyrics", full_text="Full lyrics")
        state.identify_tasks["lyrics_1"] = {"_lyrics_obj": lyrics_obj}

        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = b"/music/track.mp3"
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/track/1/lyrics/confirm")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert mock_item.lyrics == "Full lyrics"
        mock_item.store.assert_called_once()
        mock_item.try_write.assert_called_once()

    def test_removes_task_from_identify_tasks(self, client, mocker):
        lyrics_obj = _make_lyrics_obj(mocker)
        state.identify_tasks["lyrics_1"] = {"_lyrics_obj": lyrics_obj}

        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = b"/music/track.mp3"
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        client.post("/api/album/1/track/1/lyrics/confirm")
        assert "lyrics_1" not in state.identify_tasks

    def test_removes_lrc_file_after_confirm(self, client, mocker, tmp_path):
        lyrics_obj = _make_lyrics_obj(mocker, text="L", full_text="L")
        state.identify_tasks["lyrics_1"] = {"_lyrics_obj": lyrics_obj}

        lrc_file = tmp_path / "track.lrc"
        lrc_file.write_text("[00:00]line", encoding="utf-8")
        audio_path = str(tmp_path / "track.mp3")

        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = audio_path.encode()
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/track/1/lyrics/confirm")
        assert resp.status_code == 200
        assert not lrc_file.exists()

    def test_returns_500_when_write_fails(self, client, mocker):
        lyrics_obj = _make_lyrics_obj(mocker)
        state.identify_tasks["lyrics_1"] = {"_lyrics_obj": lyrics_obj}

        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = b"/music/track.mp3"
        mock_item.try_write.return_value = False

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/track/1/lyrics/confirm")
        assert resp.status_code == 500
        assert "Failed to write tags" in resp.get_json()["error"]
        # task preserved for retry
        assert "lyrics_1" in state.identify_tasks


# ---------------------------------------------------------------------------
# POST /api/album/<id>/track/<id>/lyrics/embed
# ---------------------------------------------------------------------------


class TestEmbedLrcLyrics:
    def test_returns_404_when_track_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/1/track/9999/lyrics/embed")
        assert resp.status_code == 404

    def test_returns_404_when_no_lrc_file(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = b"/music/track.mp3"
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/1/track/1/lyrics/embed")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "No .lrc file found"

    def test_embeds_lrc_and_removes_file(self, client, mocker, tmp_path):
        lrc_file = tmp_path / "track.lrc"
        lrc_file.write_text("[00:00]verse", encoding="utf-8")
        audio_path = str(tmp_path / "track.mp3")

        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = audio_path.encode()
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/track/1/lyrics/embed")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert mock_item.lyrics == "[00:00]verse"
        assert not lrc_file.exists()


# ---------------------------------------------------------------------------
# POST /api/album/<id>/lyrics/embed
# ---------------------------------------------------------------------------


class TestEmbedAllLrc:
    def test_returns_404_when_album_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/9999/lyrics/embed")
        assert resp.status_code == 404

    def test_embeds_all_lrc_files(self, client, mocker, tmp_path):
        lrc1 = tmp_path / "t1.lrc"
        lrc1.write_text("[00:00]line1", encoding="utf-8")
        lrc2 = tmp_path / "t2.lrc"
        lrc2.write_text("[00:00]line2", encoding="utf-8")

        mock_item1 = mocker.MagicMock()
        mock_item1.id = 1
        mock_item1.title = "Track 1"
        mock_item1.path = str(tmp_path / "t1.mp3").encode()

        mock_item2 = mocker.MagicMock()
        mock_item2.id = 2
        mock_item2.title = "Track 2"
        mock_item2.path = str(tmp_path / "t2.mp3").encode()

        mock_album = mocker.MagicMock()
        mock_album.items.return_value = [mock_item1, mock_item2]
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/lyrics/embed")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert len(data["embedded"]) == 2
        assert mock_item1.lyrics == "[00:00]line1"
        assert mock_item2.lyrics == "[00:00]line2"
        assert not lrc1.exists()
        assert not lrc2.exists()

    def test_skips_items_without_lrc(self, client, mocker, tmp_path):
        mock_item = mocker.MagicMock()
        mock_item.id = 1
        mock_item.title = "Track"
        mock_item.path = str(tmp_path / "track.mp3").encode()

        mock_album = mocker.MagicMock()
        mock_album.items.return_value = [mock_item]
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/lyrics/embed")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["embedded"] == []


# ---------------------------------------------------------------------------
# POST /api/album/<id>/track/<id>/lyrics/save
# ---------------------------------------------------------------------------


class TestSaveTrackLyrics:
    def test_returns_404_when_track_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/1/track/9999/lyrics/save", json={"lyrics": "text"})
        assert resp.status_code == 404

    def test_saves_lyrics_to_item(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = b"/music/track.mp3"
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/track/1/lyrics/save", json={"lyrics": "My lyrics"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert mock_item.lyrics == "My lyrics"
        mock_item.store.assert_called_once()
        mock_item.try_write.assert_called_once()

    def test_saves_empty_lyrics(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = b"/music/track.mp3"
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/track/1/lyrics/save", json={})
        assert resp.status_code == 200
        assert mock_item.lyrics == ""

    def test_removes_lrc_file_when_saving_non_empty_lyrics(self, client, mocker, tmp_path):
        lrc_file = tmp_path / "track.lrc"
        lrc_file.write_text("[00:00]old", encoding="utf-8")
        audio_path = str(tmp_path / "track.mp3")

        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = audio_path.encode()
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/track/1/lyrics/save", json={"lyrics": "New lyrics"})
        assert resp.status_code == 200
        assert not lrc_file.exists()

    def test_keeps_lrc_file_when_saving_empty_lyrics(self, client, mocker, tmp_path):
        lrc_file = tmp_path / "track.lrc"
        lrc_file.write_text("[00:00]old", encoding="utf-8")
        audio_path = str(tmp_path / "track.mp3")

        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = audio_path.encode()
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/track/1/lyrics/save", json={"lyrics": ""})
        assert resp.status_code == 200
        assert lrc_file.exists()

    def test_returns_500_when_try_write_fails(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = b"/music/track.mp3"
        mock_item.try_write.return_value = False
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/track/1/lyrics/save", json={"lyrics": "My lyrics"})
        assert resp.status_code == 500
        assert "error" in resp.get_json()


# ---------------------------------------------------------------------------
# POST /api/album/<id>/lyrics/fetch
# ---------------------------------------------------------------------------


class TestFetchAlbumLyrics:
    def test_returns_404_when_album_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/9999/lyrics/fetch")
        assert resp.status_code == 404

    def test_returns_500_when_lyrics_plugin_not_loaded(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[])
        resp = client.post("/api/album/1/lyrics/fetch")
        assert resp.status_code == 500
        assert resp.get_json()["error"] == "lyrics plugin not loaded"

    def test_returns_tracks_with_found_lyrics(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.id = 1
        mock_item.track = 1
        mock_item.disc = 1
        mock_item.title = "Song"
        mock_item.artist = "Artist"
        mock_item.lyrics = ""
        mock_item.path = b"/music/track.mp3"

        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.items.return_value = [mock_item]

        lyrics_obj = _make_lyrics_obj(mocker, text="Lyrics", full_text="Full")
        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lyrics"
        mock_plugin.find_lyrics.return_value = lyrics_obj

        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        state.identify_tasks.clear()
        resp = client.post("/api/album/1/lyrics/fetch")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert len(data["tracks"]) == 1
        track = data["tracks"][0]
        assert track["found"] is True
        assert track["new_lyrics"] == "Full"
        assert "lyrics_1" in state.identify_tasks

    def test_returns_not_found_for_tracks_without_lyrics(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.id = 1
        mock_item.track = 1
        mock_item.disc = 1
        mock_item.title = "Song"
        mock_item.artist = "Artist"
        mock_item.lyrics = ""
        mock_item.path = b"/music/track.mp3"

        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.items.return_value = [mock_item]

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lyrics"
        mock_plugin.find_lyrics.return_value = None

        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        resp = client.post("/api/album/1/lyrics/fetch")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["tracks"][0]["found"] is False


# ---------------------------------------------------------------------------
# POST /api/album/<id>/lyrics/confirm
# ---------------------------------------------------------------------------


class TestConfirmAlbumLyrics:
    def test_returns_400_when_no_item_ids(self, client):
        resp = client.post("/api/album/1/lyrics/confirm", json={})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No tracks selected"

    def test_writes_lyrics_for_selected_tracks(self, client, mocker):
        lyrics_obj = _make_lyrics_obj(mocker, text="Verse", full_text="Verse 1")
        state.identify_tasks["lyrics_1"] = {"_lyrics_obj": lyrics_obj}
        state.identify_tasks["lyrics_2"] = {"_lyrics_obj": _make_lyrics_obj(mocker)}

        mock_item1 = mocker.MagicMock()
        mock_item1.album_id = 1
        mock_item1.path = b"/music/t1.mp3"

        mock_item2 = mocker.MagicMock()
        mock_item2.album_id = 1
        mock_item2.path = b"/music/t2.mp3"

        def get_item(item_id):
            return {1: mock_item1, 2: mock_item2}.get(item_id)

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.side_effect = get_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/lyrics/confirm", json={"item_ids": [1, 2]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["written"] == 2
        assert data["failed"] == []
        assert mock_item1.lyrics == "Verse 1"
        assert "lyrics_1" not in state.identify_tasks
        assert "lyrics_2" not in state.identify_tasks

    def test_skips_items_without_task(self, client, mocker):
        state.identify_tasks.clear()

        mock_lib = mocker.MagicMock()
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/lyrics/confirm", json={"item_ids": [1, 2]})
        assert resp.status_code == 200
        assert resp.get_json()["written"] == 0

    def test_skips_items_belonging_to_different_album(self, client, mocker):
        lyrics_obj = _make_lyrics_obj(mocker, text="L", full_text="L")
        state.identify_tasks["lyrics_1"] = {"_lyrics_obj": lyrics_obj}

        mock_item = mocker.MagicMock()
        mock_item.album_id = 99  # different album
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/lyrics/confirm", json={"item_ids": [1]})
        assert resp.status_code == 200
        assert resp.get_json()["written"] == 0

    def test_does_not_count_write_failures(self, client, mocker):
        lyrics_obj = _make_lyrics_obj(mocker, text="Verse", full_text="Verse 1")
        state.identify_tasks["lyrics_1"] = {"_lyrics_obj": lyrics_obj}

        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.path = b"/music/t1.mp3"
        mock_item.try_write.return_value = False

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("src.routes.lyrics._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/lyrics/confirm", json={"item_ids": [1]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["written"] == 0
        assert data["failed"] == [1]
        # task preserved for retry
        assert "lyrics_1" in state.identify_tasks
