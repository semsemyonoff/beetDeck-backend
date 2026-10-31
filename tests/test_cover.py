"""Tests for cover art blueprint routes."""

import io

import pytest

from beetdeck import state
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


def _write_jpeg_bytes(handle, size=(300, 300), color="red"):
    """A real JPEG into an open file object, for multipart upload bodies."""
    from PIL import Image

    Image.new("RGB", size, color=color).save(handle, format="JPEG")
    handle.seek(0)


def _write_jpeg(path, size=(300, 300), color="red"):
    """Write a real JPEG (Pillow-readable) so thumbnailing can run."""
    from PIL import Image

    Image.new("RGB", size, color=color).save(str(path), format="JPEG")


class TestAlbumCoverThumbnail:
    def test_invalid_size_returns_400(self, client, db_path, tmp_path):
        img_file = tmp_path / "cover.jpg"
        _write_jpeg(img_file)
        album_id = insert_album(db_path, album="Thumb Album", artpath=str(img_file).encode())
        resp = client.get(f"/api/album/{album_id}/cover?size=999")
        assert resp.status_code == 400
        assert "size must be one of" in resp.get_json()["error"]

    def test_size_returns_webp(self, client, db_path, tmp_path):
        img_file = tmp_path / "cover.jpg"
        _write_jpeg(img_file, size=(300, 300))
        album_id = insert_album(db_path, album="Thumb Album", artpath=str(img_file).encode())
        resp = client.get(f"/api/album/{album_id}/cover?size=64")
        assert resp.status_code == 200
        assert resp.mimetype == "image/webp"
        assert "max-age" in resp.headers.get("Cache-Control", "")
        # Body is a valid WebP downscaled to fit the 64px box.
        from PIL import Image

        img = Image.open(io.BytesIO(resp.data))
        assert img.format == "WEBP"
        assert max(img.size) <= 64

    def test_thumbnail_is_cached(self, client, app, db_path, tmp_path):
        img_file = tmp_path / "cover.jpg"
        _write_jpeg(img_file)
        album_id = insert_album(db_path, album="Cached Album", artpath=str(img_file).encode())
        cache_dir = app.config["COVER_CACHE_DIR"]
        client.get(f"/api/album/{album_id}/cover?size=128")
        import os

        cached = [f for f in os.listdir(cache_dir) if f.endswith(".webp")]
        assert len(cached) == 1
        # Second request reuses the cache (no new file).
        client.get(f"/api/album/{album_id}/cover?size=128")
        assert os.listdir(cache_dir) == cached

    def test_no_size_serves_original(self, client, db_path, tmp_path):
        img_file = tmp_path / "cover.jpg"
        _write_jpeg(img_file)
        album_id = insert_album(db_path, album="Orig Album", artpath=str(img_file).encode())
        resp = client.get(f"/api/album/{album_id}/cover")
        assert resp.status_code == 200
        assert resp.mimetype != "image/webp"

    def test_unreadable_source_falls_back_to_original(self, client, db_path, tmp_path):
        # Not a real image: thumbnailing fails, route streams the original bytes.
        img_file = tmp_path / "cover.jpg"
        img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        album_id = insert_album(db_path, album="Bad Img", artpath=str(img_file).encode())
        resp = client.get(f"/api/album/{album_id}/cover?size=64")
        assert resp.status_code == 200
        assert resp.mimetype != "image/webp"


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
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
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
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
        mock_save = mocker.patch("beetdeck.routes.cover._save_cover_to_album")
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
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
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
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
        mock_save = mocker.patch("beetdeck.routes.cover._save_cover_to_album")
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
        from beetdeck.utils import _save_cover_to_album

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

        mocker.patch("beetsplug._utils.art.embed_album")

        with app.app_context():
            _save_cover_to_album(album, str(src))

        # art_destination must receive the common parent, not CD1
        call_kwargs = album.art_destination.call_args.kwargs
        assert call_kwargs["item_dir"] == str(album_root).encode()
        assert cd1_old.exists()
        assert album.artpath == new_art
        assert album.store.called

    def test_single_disc_cleanup_targets_album_dir(self, app, tmp_path, mocker):
        from beetdeck.utils import _save_cover_to_album

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
        mocker.patch("beetsplug._utils.art.embed_album")

        with app.app_context():
            _save_cover_to_album(album, str(src))

        # art_destination must receive the album dir as item_dir
        call_kwargs = album.art_destination.call_args.kwargs
        assert call_kwargs["item_dir"] == str(album_dir).encode()
        assert album.store.called

    def test_written_cover_is_world_readable(self, app, tmp_path, mocker):
        """The cover lands in the library via ``move``, so it must not keep the
        0600 mode ``NamedTemporaryFile`` gives the intermediate resize output.
        """
        import os
        import stat

        from beetdeck.utils import _save_cover_to_album

        album_dir = tmp_path / "Artist" / "Album"
        album_dir.mkdir(parents=True)
        dest = album_dir / "cover.jpg"

        item = mocker.MagicMock()
        item.path = str(album_dir / "01.mp3").encode()
        album = mocker.MagicMock()
        album.items.return_value = [item]
        album.art_destination.return_value = str(dest).encode()

        src = tmp_path / "new.jpg"
        self._make_jpeg(src)
        mocker.patch("beetsplug._utils.art.embed_album")

        with app.app_context():
            _save_cover_to_album(album, str(src))

        umask = os.umask(0)
        os.umask(umask)
        assert stat.S_IMODE(dest.stat().st_mode) == 0o666 & ~umask

    def test_emits_art_set_event(self, app, tmp_path, mocker):
        from beetdeck.utils import _save_cover_to_album

        album_dir = tmp_path / "Artist" / "Album"
        album_dir.mkdir(parents=True)

        item = mocker.MagicMock()
        item.path = str(album_dir / "01.mp3").encode()
        album = mocker.MagicMock()
        album.items.return_value = [item]
        album.art_destination.return_value = str(album_dir / "cover.jpg").encode()

        src = tmp_path / "new.jpg"
        self._make_jpeg(src)
        mocker.patch("beetsplug._utils.art.embed_album")
        send = mocker.patch("beets.plugins.send")

        with app.app_context():
            _save_cover_to_album(album, str(src))

        send.assert_called_once_with("art_set", album=album)

    def test_art_set_is_emitted_after_the_embed(self, app, tmp_path, mocker):
        """beets' own ``embedart`` listens on ``art_set`` and, with its ``auto``
        enabled, re-embeds the album and may delete the art file. Emitting before
        our embed makes that re-entrant on the cover we just wrote."""
        from beetdeck.utils import _save_cover_to_album

        album_dir = tmp_path / "Artist" / "Album"
        album_dir.mkdir(parents=True)

        item = mocker.MagicMock()
        item.path = str(album_dir / "01.mp3").encode()
        album = mocker.MagicMock()
        album.items.return_value = [item]
        album.art_destination.return_value = str(album_dir / "cover.jpg").encode()

        src = tmp_path / "new.jpg"
        self._make_jpeg(src)

        order = mocker.MagicMock()
        order.attach_mock(mocker.patch("beetsplug._utils.art.embed_album"), "embed")
        order.attach_mock(mocker.patch("beets.plugins.send"), "send")

        with app.app_context():
            _save_cover_to_album(album, str(src))

        assert [call[0] for call in order.mock_calls] == ["embed", "send"]


class TestFetchCover:
    def setup_method(self):
        keys = [k for k in state.identify_tasks if k.startswith("cover_")]
        for k in keys:
            del state.identify_tasks[k]

    def test_returns_404_when_album_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/9999/cover/fetch")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_returns_500_when_fetchart_plugin_not_loaded(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
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
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
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
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
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

    def _install_candidate(self, client, mocker, tmp_path, *, album_id, filename, size):
        from PIL import Image

        img_file = tmp_path / filename
        Image.new("RGB", size, color="blue").save(str(img_file), format="JPEG")
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mocker.MagicMock()
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
        candidate = mocker.MagicMock()
        candidate.path = str(img_file).encode()
        candidate.source_name = "coverart"
        plugin = mocker.MagicMock()
        plugin.name = "fetchart"
        plugin.art_for_album.return_value = candidate
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])
        return client.post(f"/api/album/{album_id}/cover/fetch").get_json()

    def test_reports_the_candidate_dimensions(self, client, tmp_path, mocker):
        data = self._install_candidate(
            client, mocker, tmp_path, album_id=78, filename="c.jpg", size=(600, 587)
        )
        assert (data["width"], data["height"]) == (600, 587)

    def test_reports_the_current_cover_size_next_to_the_candidate(self, client, tmp_path, mocker):
        """One request answers "is this an upgrade", not two."""
        from PIL import Image

        current = tmp_path / "cover.jpg"
        Image.new("RGB", (500, 500), color="green").save(str(current), format="JPEG")
        candidate_file = tmp_path / "candidate.jpg"
        Image.new("RGB", (1200, 1200), color="blue").save(str(candidate_file), format="JPEG")

        album = mocker.MagicMock()
        album.artpath = str(current).encode()
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = album
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
        candidate = mocker.MagicMock()
        candidate.path = str(candidate_file).encode()
        candidate.source_name = "coverart"
        plugin = mocker.MagicMock()
        plugin.name = "fetchart"
        plugin.art_for_album.return_value = candidate
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])

        data = client.post("/api/album/80/cover/fetch").get_json()
        assert (data["current_width"], data["current_height"]) == (500, 500)
        assert (data["width"], data["height"]) == (1200, 1200)
        assert "relaxed" not in data

    def test_an_unreadable_candidate_reports_null_dimensions_not_an_error(
        self, client, tmp_path, mocker
    ):
        img_file = tmp_path / "broken.jpg"
        img_file.write_bytes(b"\xff\xd8\xff\xe0not-an-image")
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mocker.MagicMock()
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
        candidate = mocker.MagicMock()
        candidate.path = str(img_file).encode()
        candidate.source_name = "coverart"
        plugin = mocker.MagicMock()
        plugin.name = "fetchart"
        plugin.art_for_album.return_value = candidate
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])

        data = client.post("/api/album/79/cover/fetch").get_json()
        assert data["found"] is True
        assert data["width"] is None
        assert data["height"] is None

    def test_each_fetch_stamps_a_fresh_stash_generation(self, client, tmp_path, mocker):
        first = self._install_candidate(
            client, mocker, tmp_path, album_id=80, filename="a.jpg", size=(10, 10)
        )
        second = self._install_candidate(
            client, mocker, tmp_path, album_id=80, filename="b.jpg", size=(10, 10)
        )
        assert second["stash_generation"] > first["stash_generation"]
        assert state.identify_tasks["cover_80"]["stash_generation"] == second["stash_generation"]


class TestConfirmCoverPrecondition:
    """``expected_generation`` is what stops a confirm from committing a candidate
    a competing fetch put in the album's single stash slot."""

    def setup_method(self):
        for key in [k for k in state.identify_tasks if k.startswith("cover_")]:
            del state.identify_tasks[key]

    @pytest.fixture()
    def stashed(self, mocker):
        """A stashed candidate for album 5, and the generation it carries."""
        mocker.patch("beetdeck.routes.cover._init_beets")
        mocker.patch("beetdeck.routes.cover._save_cover_to_album")
        with state.identify_lock:
            generation = state.next_stash_generation()
        state.identify_tasks["cover_5"] = {
            "candidate_path": "/tmp/candidate.jpg",
            "source": "coverart",
            "stash_generation": generation,
        }
        return generation

    def test_a_matching_generation_writes(self, client, stashed, mocker):
        mocker.patch("beetdeck.routes.cover.os.path.isfile", return_value=True)
        resp = client.post("/api/album/5/cover/confirm", json={"expected_generation": stashed})
        assert resp.status_code == 200

    def test_a_replaced_stash_is_refused_with_409(self, client, stashed, mocker):
        mocker.patch("beetdeck.routes.cover.os.path.isfile", return_value=True)
        save = mocker.patch("beetdeck.routes.cover._save_cover_to_album")
        with state.identify_lock:
            state.identify_tasks["cover_5"]["stash_generation"] = state.next_stash_generation()

        resp = client.post("/api/album/5/cover/confirm", json={"expected_generation": stashed})
        assert resp.status_code == 409
        assert "changed since the preview" in resp.get_json()["error"]
        assert not save.called

    def test_a_vanished_stash_is_refused_with_409_not_400(self, client, stashed):
        state.identify_tasks.pop("cover_5")
        resp = client.post("/api/album/5/cover/confirm", json={"expected_generation": stashed})
        assert resp.status_code == 409

    def test_a_bodyless_confirm_still_works(self, client, stashed, mocker):
        """The SPA sends no body; omitting the precondition must keep the old
        behaviour rather than becoming a 422."""
        mocker.patch("beetdeck.routes.cover.os.path.isfile", return_value=True)
        assert client.post("/api/album/5/cover/confirm").status_code == 200

    def test_a_confirm_and_an_upload_cannot_interleave(self, client, stashed, mocker, app):
        """The precondition is checked under `identify_lock`, which is released
        long before the write finishes. Without a per-album write lock an upload
        landing in that window is reverted by the confirm that already passed —
        and the confirm still answers 200."""
        import threading
        import time

        from werkzeug.test import EnvironBuilder

        # Build the multipart request *here*, on the main thread, and let the
        # thread below send the finished bytes. Werkzeug's builder reaches into
        # `mimetypes`, whose lazy global initialization is not thread-safe: under
        # load the parse could come back without the file part and the upload
        # answered 400, failing this test for a reason that has nothing to do
        # with the lock it exists to measure.
        builder = EnvironBuilder(
            path="/api/album/5/cover/upload",
            method="POST",
            data={"file": (io.BytesIO(b"\xff\xd8\xff\xe0image"), "art.jpg")},
            content_type="multipart/form-data",
        )
        environ = builder.get_environ()
        upload_body = environ["wsgi.input"].read()
        upload_content_type = environ["CONTENT_TYPE"]

        mocker.patch("beetdeck.routes.cover.os.path.isfile", return_value=True)
        order = []
        order_guard = threading.Lock()
        # The upload must start while the confirm is inside its write. Started
        # unsynchronized, a busy runner can let the upload finish first — it
        # drops the stash, and the confirm answers 400 with nothing to confirm.
        confirm_writing = threading.Event()

        def slow_save(album, src_path):
            with order_guard:
                order.append("enter")
            confirm_writing.set()
            # Long enough that an unserialized second write would start here.
            time.sleep(0.2)
            with order_guard:
                order.append("exit")

        mocker.patch("beetdeck.routes.cover._save_cover_to_album", side_effect=slow_save)

        upload_status = {}

        def upload():
            if not confirm_writing.wait(timeout=5):
                return
            with app.test_client() as other:
                resp = other.post(
                    "/api/album/5/cover/upload",
                    data=upload_body,
                    content_type=upload_content_type,
                )
                upload_status["code"] = resp.status_code
                # Kept for the assertion below: a bare status code turns any
                # failure here into a bisect, and this one runs in a thread.
                upload_status["body"] = resp.get_json()

        thread = threading.Thread(target=upload)
        thread.start()
        assert client.post("/api/album/5/cover/confirm").status_code == 200
        thread.join(timeout=5)

        assert not thread.is_alive(), "the upload never finished — the lock is held"
        assert upload_status.get("code") == 200, upload_status.get("body")
        # Both routes write through _save_cover_to_album; mutual exclusion means
        # the record never shows one entering while the other is inside.
        assert order == ["enter", "exit", "enter", "exit"]

    def test_an_upload_drops_the_stash_so_a_pending_token_cannot_revert_it(
        self, client, stashed, mocker, db_path
    ):
        """An upload is the user overruling the fetched candidate. The stash has
        to go with it: left in place it keeps its original generation, so a
        confirm holding that generation passes its precondition and silently
        writes the fetched cover over what was just uploaded."""
        album_id = insert_album(db_path, album="Uploaded", albumartist="Artist")
        with state.identify_lock:
            state.identify_tasks[f"cover_{album_id}"] = {
                "candidate_path": "/tmp/candidate.jpg",
                "source": "coverart",
                "stash_generation": state.next_stash_generation(),
            }
        generation = state.identify_tasks[f"cover_{album_id}"]["stash_generation"]
        mocker.patch("beetdeck.routes.cover._init_beets")
        save = mocker.patch("beetdeck.routes.cover._save_cover_to_album")

        resp = client.post(
            f"/api/album/{album_id}/cover/upload",
            data={"file": (io.BytesIO(b"\xff\xd8\xff\xe0image"), "art.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert save.called
        assert f"cover_{album_id}" not in state.identify_tasks

        # The pending token is now refused instead of reverting the upload.
        refused = client.post(
            f"/api/album/{album_id}/cover/confirm", json={"expected_generation": generation}
        )
        assert refused.status_code == 409


# ---------------------------------------------------------------------------
# OpenAPI spec coverage (spectree decoration)
# ---------------------------------------------------------------------------


class TestCoverOpenAPISpec:
    """The five cover routes are decorated with ``@api.validate`` (tags + response
    docs; multipart ``form`` on upload, no JSON body validation). Assert they
    appear in the generated spec, tagged ``cover``, without altering behavior."""

    def test_fetch_in_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/album/{album_id}/cover/fetch"]["post"]
        assert "cover" in op["tags"]
        assert "200" in op["responses"]
        assert "404" in op["responses"]

    def test_confirm_in_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/album/{album_id}/cover/confirm"]["post"]
        assert "cover" in op["tags"]
        assert "200" in op["responses"]
        assert "400" in op["responses"]

    def test_upload_documented_as_multipart(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/album/{album_id}/cover/upload"]["post"]
        assert "cover" in op["tags"]
        # Upload body is documented as multipart/form-data, not JSON.
        content = op["requestBody"]["content"]
        assert "multipart/form-data" in content
        assert "application/json" not in content

    def test_binary_gets_undecorated(self, client):
        # The two send_file binary routes are intentionally left undecorated
        # (spectree's response post-processing breaks Flask passthrough mode).
        # spectree still lists every Flask route, so they appear in the spec, but
        # without our ``cover`` tag or documented responses.
        spec = client.get("/apidoc/openapi.json").get_json()
        cover = spec["paths"]["/api/album/{album_id}/cover"]["get"]
        preview = spec["paths"]["/api/album/{album_id}/cover/preview"]["get"]
        assert cover.get("tags", []) == []
        assert cover.get("responses", {}) == {}
        assert preview.get("tags", []) == []
        assert preview.get("responses", {}) == {}


class TestRelaxedCoverLookup:
    """The retry that separates "there is no art" from "you told us not to take it".

    `minwidth` rejects on *width alone*, so a portrait cover that the Cover Art
    Archive stores at its original 495×500 fails a 500 px floor by five pixels
    and the album is reported as having no art at all.
    """

    def _plugin(self, mocker, *, first, second, minwidth=500):
        plugin = mocker.MagicMock()
        plugin.name = "fetchart"
        plugin.minwidth = minwidth
        plugin.enforce_ratio = False
        seen = []

        def art_for_album(album, paths, local_only=False):
            seen.append((plugin.minwidth, plugin.enforce_ratio))
            return first if len(seen) == 1 else second

        plugin.art_for_album.side_effect = art_for_album
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])
        return plugin, seen

    def _album(self, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mocker.MagicMock()
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)

    def test_a_candidate_below_minwidth_is_returned_with_a_warning(self, client, tmp_path, mocker):
        from PIL import Image

        art = tmp_path / "small.jpg"
        Image.new("RGB", (495, 500), color="blue").save(str(art), format="JPEG")
        candidate = mocker.MagicMock()
        candidate.path = str(art).encode()
        candidate.source_name = "coverart"
        self._album(mocker)
        self._plugin(mocker, first=None, second=candidate)

        data = client.post("/api/album/81/cover/fetch").get_json()
        assert data["found"] is True
        assert data["relaxed"] is True
        assert data["min_width"] == 500
        assert (data["width"], data["height"]) == (495, 500)
        assert "495×500" in data["warning"]
        assert "minimum width of 500 px" in data["warning"]
        assert "aspect-ratio" not in data["warning"]

    def test_the_retry_runs_with_the_filters_off_and_puts_them_back(self, client, tmp_path, mocker):
        """The plugin is a process-wide singleton — a leaked 0 would silently
        accept undersized art for every later fetch."""
        from PIL import Image

        art = tmp_path / "small.jpg"
        Image.new("RGB", (495, 500), color="blue").save(str(art), format="JPEG")
        candidate = mocker.MagicMock()
        candidate.path = str(art).encode()
        candidate.source_name = "coverart"
        self._album(mocker)
        plugin, seen = self._plugin(mocker, first=None, second=candidate)

        client.post("/api/album/82/cover/fetch")
        assert seen == [(500, False), (0, False)]
        assert plugin.minwidth == 500
        assert plugin.enforce_ratio is False

    def test_a_retry_hit_that_meets_the_filters_is_not_called_a_rejection(
        self, client, tmp_path, mocker
    ):
        """A first pass can also come back empty from a timed-out source; calling
        that a policy rejection would invent a reason."""
        import logging

        from PIL import Image

        art = tmp_path / "big.jpg"
        Image.new("RGB", (1000, 1000), color="blue").save(str(art), format="JPEG")
        candidate = mocker.MagicMock()
        candidate.path = str(art).encode()
        candidate.source_name = "coverart"
        self._album(mocker)
        plugin = mocker.MagicMock()
        plugin.name = "fetchart"
        plugin.minwidth = 500
        plugin.enforce_ratio = False
        calls = []

        def art_for_album(album, paths, local_only=False):
            calls.append(1)
            if len(calls) == 1:
                logging.getLogger("beets.fetchart").debug(
                    "{}: error receiving response", "coverart"
                )
                return None
            return candidate

        plugin.art_for_album.side_effect = art_for_album
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])

        data = client.post("/api/album/86/cover/fetch").get_json()
        assert data["found"] is True
        assert "relaxed" not in data
        assert data["retried"] is True
        assert "error receiving response" in data["warning"]

    def test_no_active_filter_means_no_second_sweep(self, client, mocker):
        """With nothing to lift, the retry is a byte-identical duplicate of the
        sweep that just failed — and costs a second round of source requests."""
        self._album(mocker)
        plugin, seen = self._plugin(mocker, first=None, second=None, minwidth=0)
        plugin.enforce_ratio = False

        data = client.post("/api/album/87/cover/fetch").get_json()
        assert data["found"] is False
        assert len(seen) == 1

    def test_a_first_pass_hit_never_pays_for_the_retry(self, client, tmp_path, mocker):
        from PIL import Image

        art = tmp_path / "big.jpg"
        Image.new("RGB", (1000, 1000), color="blue").save(str(art), format="JPEG")
        candidate = mocker.MagicMock()
        candidate.path = str(art).encode()
        candidate.source_name = "coverart"
        self._album(mocker)
        _, seen = self._plugin(mocker, first=candidate, second=candidate)

        data = client.post("/api/album/83/cover/fetch").get_json()
        assert len(seen) == 1
        assert "relaxed" not in data
        assert "warning" not in data

    def test_nothing_anywhere_reports_the_plugins_own_reasons(self, client, mocker):
        """`found: false` used to be silent about whether art existed at all."""
        import logging

        self._album(mocker)
        plugin = mocker.MagicMock()
        plugin.name = "fetchart"
        plugin.minwidth = 500
        plugin.enforce_ratio = False

        def art_for_album(album, paths, local_only=False):
            logging.getLogger("beets.fetchart").debug("image too small ({} < {})", 495, 500)
            return None

        plugin.art_for_album.side_effect = art_for_album
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])

        data = client.post("/api/album/84/cover/fetch").get_json()
        assert data["found"] is False
        # Once, not twice: the retry walks the same sources and re-reports every
        # rejection the first pass already made.
        assert data["reasons"] == ["image too small (495 < 500)"]
        assert logging.getLogger("beets.fetchart").handlers == []

    def test_no_source_offering_anything_reports_an_empty_reason_list(self, client, mocker):
        self._album(mocker)
        self._plugin(mocker, first=None, second=None)
        data = client.post("/api/album/85/cover/fetch").get_json()
        assert data["found"] is False
        assert data["reasons"] == []


class TestCoverBehaviorUnchanged:
    """Decoration must not change request behavior: body-less fetch/confirm POSTs
    still work, and upload's bespoke 400 file validation still fires."""

    def setup_method(self):
        keys = [k for k in state.identify_tasks if k.startswith("cover_")]
        for k in keys:
            del state.identify_tasks[k]

    def test_confirm_without_body_still_400_no_task(self, client):
        # No JSON body, no task → handler's own 400, NOT a spectree 422.
        resp = client.post("/api/album/1/cover/confirm")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No cover art to confirm"

    def test_fetch_without_body_reaches_handler(self, client, mocker):
        # Body-less fetch must reach the handler (not 422); album missing → 404.
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/9999/cover/fetch")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_upload_missing_file_still_400(self, client):
        # form= model keeps the file optional, so the handler's 400 still fires.
        resp = client.post("/api/album/1/cover/upload", data={})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No file uploaded"

    def test_upload_valid_file_still_ok(self, client, db_path, mocker):
        album_id = insert_album(db_path, album="Spec Upload Album")
        insert_item(db_path, album_id, path=b"/music/album/track.mp3")
        mock_album = mocker.MagicMock()
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.cover._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.cover._save_cover_to_album")
        resp = client.post(
            f"/api/album/{album_id}/cover/upload",
            data={"file": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"\x00" * 100), "cover.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"


class TestUploadClearsTheArtworkMarker:
    """``beetdeck_cover_image_id`` names one Cover Art Archive scan as the
    album's cover, so it has to die with the cover it describes.

    Every cover write inside Flask funnels through ``_save_cover_to_album``,
    which clears it — this drives the upload route against a **real** beets
    library so the clearing is asserted where it matters, in the DB, rather than
    on a mock that records the assignment and stores nothing.

    ⚠️ Known limitation, out of scope here: a rescan (`beet import` /
    `beet update -M` with fetchart) rewrites ``artpath`` without going through
    this funnel, so the marker can survive a cover it no longer describes.
    """

    def _marker(self, db_path, album_id):
        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT value FROM album_attributes WHERE entity_id = ? AND key = ?",
                (album_id, "beetdeck_cover_image_id"),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else row[0]

    def _album_with_marker(self, db_path, tmp_path, value="1234567890"):
        import sqlite3

        album_dir = tmp_path / "Artist" / "Marked Album"
        album_dir.mkdir(parents=True)
        track = album_dir / "01.mp3"
        track.write_bytes(b"")
        album_id = insert_album(db_path, album="Marked Album")
        insert_item(db_path, album_id, path=str(track).encode())
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO album_attributes (entity_id, key, value) VALUES (?, ?, ?)",
                (album_id, "beetdeck_cover_image_id", value),
            )
            conn.commit()
        finally:
            conn.close()
        return album_id

    def test_an_upload_clears_the_marker(self, client, db_path, tmp_path, mocker):
        album_id = self._album_with_marker(db_path, tmp_path)
        assert self._marker(db_path, album_id) == "1234567890"
        mocker.patch("beetsplug._utils.art.embed_album")
        new_cover = io.BytesIO()
        _write_jpeg_bytes(new_cover)

        resp = client.post(
            f"/api/album/{album_id}/cover/upload",
            data={"file": (new_cover, "new.jpg")},
            content_type="multipart/form-data",
        )

        assert resp.status_code == 200
        assert self._marker(db_path, album_id) == ""

    def test_an_album_that_never_had_a_marker_uploads_fine(self, client, db_path, tmp_path, mocker):
        """The clear is an assignment, not a ``del`` — deleting an absent flex
        attr raises, which would 500 every upload in a library with no gallery
        history at all."""
        album_dir = tmp_path / "Artist" / "Unmarked"
        album_dir.mkdir(parents=True)
        track = album_dir / "01.mp3"
        track.write_bytes(b"")
        album_id = insert_album(db_path, album="Unmarked")
        insert_item(db_path, album_id, path=str(track).encode())
        mocker.patch("beetsplug._utils.art.embed_album")
        new_cover = io.BytesIO()
        _write_jpeg_bytes(new_cover)

        resp = client.post(
            f"/api/album/{album_id}/cover/upload",
            data={"file": (new_cover, "new.jpg")},
            content_type="multipart/form-data",
        )

        assert resp.status_code == 200
