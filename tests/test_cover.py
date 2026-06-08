"""Tests for cover art blueprint routes."""

import io

from src import state
from tests.conftest import insert_album, insert_item

# ---------------------------------------------------------------------------
# GET /api/album/<id>/cover
# ---------------------------------------------------------------------------


class TestAlbumCover:
    def test_returns_404_when_no_album_rows(self, client):
        resp = client.get("/api/album/9999/cover")
        assert resp.status_code == 404

    def test_returns_404_when_artpath_none_and_no_cover_file(self, client, db_path):
        album_id = insert_album(db_path, album="No Cover Album", artpath=None)
        insert_item(db_path, album_id, path=b"/nonexistent/path/track.mp3")
        resp = client.get(f"/api/album/{album_id}/cover")
        assert resp.status_code == 404

    def test_returns_404_when_artpath_points_to_missing_file(self, client, db_path):
        album_id = insert_album(db_path, album="Bad Artpath", artpath=b"/nonexistent/cover.jpg")
        insert_item(db_path, album_id, path=b"/nonexistent/path/track.mp3")
        resp = client.get(f"/api/album/{album_id}/cover")
        assert resp.status_code == 404

    def test_serves_file_from_artpath(self, client, db_path, tmp_path):
        img_file = tmp_path / "cover.jpg"
        img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # minimal JPEG header
        album_id = insert_album(db_path, album="Has Artpath", artpath=str(img_file).encode())
        resp = client.get(f"/api/album/{album_id}/cover")
        assert resp.status_code == 200

    def test_serves_cover_file_from_album_dir(self, client, db_path, tmp_path):
        album_dir = tmp_path / "album"
        album_dir.mkdir()
        cover_file = album_dir / "cover.jpg"
        cover_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        track_path = album_dir / "track.mp3"
        track_path.write_bytes(b"")
        album_id = insert_album(db_path, album="Dir Cover", artpath=None)
        insert_item(db_path, album_id, path=str(track_path).encode())
        resp = client.get(f"/api/album/{album_id}/cover")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/album/<id>/cover/preview
# ---------------------------------------------------------------------------


class TestCoverPreview:
    def setup_method(self):
        # Clean up state before each test
        keys = [k for k in state.identify_tasks if k.startswith("cover_")]
        for k in keys:
            del state.identify_tasks[k]

    def test_returns_404_when_no_task(self, client):
        resp = client.get("/api/album/1/cover/preview")
        assert resp.status_code == 404

    def test_returns_404_when_candidate_path_missing(self, client):
        state.identify_tasks["cover_42"] = {"candidate_path": None}
        resp = client.get("/api/album/42/cover/preview")
        assert resp.status_code == 404

    def test_returns_404_when_file_does_not_exist(self, client):
        state.identify_tasks["cover_43"] = {"candidate_path": "/nonexistent/preview.jpg"}
        resp = client.get("/api/album/43/cover/preview")
        assert resp.status_code == 404

    def test_serves_file_when_path_exists(self, client, tmp_path):
        img_file = tmp_path / "preview.jpg"
        img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        state.identify_tasks["cover_44"] = {"candidate_path": str(img_file)}
        resp = client.get("/api/album/44/cover/preview")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/album/<id>/cover/confirm
# ---------------------------------------------------------------------------


class TestConfirmCover:
    def setup_method(self):
        keys = [k for k in state.identify_tasks if k.startswith("cover_")]
        for k in keys:
            del state.identify_tasks[k]

    def test_returns_400_when_no_task(self, client):
        resp = client.post("/api/album/1/cover/confirm")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No cover art to confirm"

    def test_returns_400_when_task_has_no_candidate_path(self, client):
        state.identify_tasks["cover_50"] = {"candidate_path": None}
        resp = client.post("/api/album/50/cover/confirm")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No cover art to confirm"

    def test_task_preserved_in_state_on_missing_album(self, client, db_path, tmp_path):
        img_file = tmp_path / "cand.jpg"
        img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        state.identify_tasks["cover_9999"] = {"candidate_path": str(img_file)}
        # album 9999 doesn't exist in DB
        resp = client.post("/api/album/9999/cover/confirm")
        # Task should be preserved so the user can retry without refetching
        assert "cover_9999" in state.identify_tasks
        # Response depends on beets init — 404 album or 500 beets error both acceptable
        assert resp.status_code in (404, 500)

    def test_returns_404_when_candidate_file_missing(self, client, db_path, mocker):
        album_id = insert_album(db_path, album="Confirm Album")
        insert_item(db_path, album_id, path=b"/music/album/track.mp3")
        state.identify_tasks[f"cover_{album_id}"] = {"candidate_path": "/nonexistent/candidate.jpg"}
        # Mock _init_beets to avoid beets dependency in test
        mock_album = mocker.MagicMock()
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.cover._init_beets", return_value=mock_lib)
        resp = client.post(f"/api/album/{album_id}/cover/confirm")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Cover art file not found"

    def test_confirm_calls_save_cover(self, client, db_path, tmp_path, mocker):
        img_file = tmp_path / "cand.jpg"
        img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        album_id = insert_album(db_path, album="Save Album")
        insert_item(db_path, album_id, path=b"/music/album/track.mp3")
        state.identify_tasks[f"cover_{album_id}"] = {"candidate_path": str(img_file)}
        mock_album = mocker.MagicMock()
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.cover._init_beets", return_value=mock_lib)
        mock_save = mocker.patch("src.routes.cover._save_cover_to_album")
        resp = client.post(f"/api/album/{album_id}/cover/confirm")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        mock_save.assert_called_once_with(mock_album, str(img_file))
        assert f"cover_{album_id}" not in state.identify_tasks


# ---------------------------------------------------------------------------
# POST /api/album/<id>/cover/upload
# ---------------------------------------------------------------------------


class TestUploadCover:
    def test_returns_400_when_no_file_field(self, client):
        resp = client.post("/api/album/1/cover/upload", data={})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No file uploaded"

    def test_returns_400_when_empty_filename(self, client):
        resp = client.post(
            "/api/album/1/cover/upload",
            data={"file": (io.BytesIO(b""), "")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Empty filename"

    def test_returns_404_when_album_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("src.routes.cover._init_beets", return_value=mock_lib)
        resp = client.post(
            "/api/album/9999/cover/upload",
            data={"file": (io.BytesIO(b"\xff\xd8\xff\xe0"), "cover.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_upload_calls_save_cover_and_returns_ok(self, client, db_path, mocker):
        album_id = insert_album(db_path, album="Upload Album")
        insert_item(db_path, album_id, path=b"/music/album/track.mp3")
        mock_album = mocker.MagicMock()
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.cover._init_beets", return_value=mock_lib)
        mock_save = mocker.patch("src.routes.cover._save_cover_to_album")
        resp = client.post(
            f"/api/album/{album_id}/cover/upload",
            data={"file": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"\x00" * 100), "cover.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        mock_save.assert_called_once()
        # First arg is the album, second is a temp file path
        call_args = mock_save.call_args[0]
        assert call_args[0] is mock_album
        assert call_args[1].endswith(".jpg")


# ---------------------------------------------------------------------------
# POST /api/album/<id>/cover/fetch (state mutation only, no real beets call)
# ---------------------------------------------------------------------------


class TestSaveCoverToAlbumMultiDisc:
    """Regression tests: _save_cover_to_album must operate on the album's common
    parent dir, not the first item's disc subdir. For multi-disc layouts this
    means the cleanup wipes covers from <album>/ and album.set_art is called
    such that beets writes to <album>/ — never to <album>/CD1/.
    """

    def _make_jpeg(self, path):
        from PIL import Image

        Image.new("RGB", (16, 16), color="red").save(str(path), format="JPEG")

    def test_cleanup_targets_parent_not_cd1(self, app, tmp_path, mocker):
        from src.utils import _save_cover_to_album

        album_root = tmp_path / "Pink Floyd" / "The Wall"
        cd1 = album_root / "CD1"
        cd2 = album_root / "CD2"
        cd1.mkdir(parents=True)
        cd2.mkdir(parents=True)
        parent_old = album_root / "cover.jpg"
        cd1_old = cd1 / "cover.jpg"
        parent_old.write_bytes(b"old-parent")
        cd1_old.write_bytes(b"old-cd1")

        i1 = mocker.MagicMock()
        i1.path = str(cd1 / "01.mp3").encode()
        i2 = mocker.MagicMock()
        i2.path = str(cd2 / "01.mp3").encode()
        album = mocker.MagicMock()
        album.items.return_value = [i1, i2]
        new_art = str(album_root / "cover.jpg").encode()
        album.art_destination.return_value = new_art

        src = tmp_path / "new-cover.jpg"
        self._make_jpeg(src)

        mocker.patch("beets.art.embed_album")

        with app.app_context():
            _save_cover_to_album(album, str(src))

        # art_destination must receive the common parent, not CD1
        call_kwargs = album.art_destination.call_args.kwargs
        assert call_kwargs["item_dir"] == str(album_root).encode()
        assert cd1_old.exists()
        assert album.artpath == new_art
        assert album.store.called

    def test_single_disc_cleanup_targets_album_dir(self, app, tmp_path, mocker):
        from src.utils import _save_cover_to_album

        album_dir = tmp_path / "Artist" / "Album"
        album_dir.mkdir(parents=True)
        old = album_dir / "cover.jpg"
        old.write_bytes(b"old")

        item = mocker.MagicMock()
        item.path = str(album_dir / "01.mp3").encode()
        album = mocker.MagicMock()
        album.items.return_value = [item]
        album.art_destination.return_value = str(album_dir / "cover.jpg").encode()

        src = tmp_path / "new.jpg"
        self._make_jpeg(src)
        mocker.patch("beets.art.embed_album")

        with app.app_context():
            _save_cover_to_album(album, str(src))

        # art_destination must receive the album dir as item_dir
        call_kwargs = album.art_destination.call_args.kwargs
        assert call_kwargs["item_dir"] == str(album_dir).encode()
        assert album.store.called


class TestFetchCover:
    def setup_method(self):
        keys = [k for k in state.identify_tasks if k.startswith("cover_")]
        for k in keys:
            del state.identify_tasks[k]

    def test_returns_404_when_album_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("src.routes.cover._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/9999/cover/fetch")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_returns_500_when_fetchart_plugin_not_loaded(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.cover._init_beets", return_value=mock_lib)
        # find_plugins returns nothing
        mocker.patch("beets.plugins.find_plugins", return_value=[])
        resp = client.post("/api/album/1/cover/fetch")
        assert resp.status_code == 500
        assert resp.get_json()["error"] == "fetchart plugin not loaded"

    def test_returns_found_false_when_no_candidate(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.cover._init_beets", return_value=mock_lib)
        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "fetchart"
        mock_plugin.art_for_album.return_value = None
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])
        resp = client.post("/api/album/1/cover/fetch")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["found"] is False

    def test_stores_candidate_in_state_and_returns_preview_url(self, client, tmp_path, mocker):
        img_file = tmp_path / "candidate.jpg"
        img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("src.routes.cover._init_beets", return_value=mock_lib)
        mock_candidate = mocker.MagicMock()
        mock_candidate.path = str(img_file).encode()
        mock_candidate.source_name = "coverart"
        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "fetchart"
        mock_plugin.art_for_album.return_value = mock_candidate
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])
        resp = client.post("/api/album/77/cover/fetch")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["found"] is True
        assert data["source"] == "coverart"
        assert data["preview_url"] == "/api/album/77/cover/preview"
        assert "cover_77" in state.identify_tasks
        assert state.identify_tasks["cover_77"]["candidate_path"] == str(img_file)
