"""Tests for genres blueprint routes."""


# ---------------------------------------------------------------------------
# POST /api/album/<id>/genre  (fetch_genre_preview)
# ---------------------------------------------------------------------------


class TestFetchGenrePreview:
    def test_returns_404_when_album_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/9999/genre")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_returns_500_when_lastgenre_plugin_not_loaded(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[])
        resp = client.post("/api/album/1/genre")
        assert resp.status_code == 500
        assert resp.get_json()["error"] == "lastgenre plugin not loaded"

    def test_returns_old_and_new_genre(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.get.side_effect = lambda key, default="": {"genres": "Rock"}.get(key, default)
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lastgenre"

        def side_effect_process(album, write):
            # Simulate plugin writing a new genre to album
            mock_album.get.side_effect = lambda key, default="": {"genres": "Electronic"}.get(
                key, default
            )

        mock_plugin._process.side_effect = side_effect_process
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        resp = client.post("/api/album/1/genre")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "old_genre" in data
        assert "new_genre" in data

    def test_restores_genre_after_preview(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.get.return_value = "Rock"
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lastgenre"
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        client.post("/api/album/1/genre")

        # genres should have been set back to the original value and store() called
        assert mock_album.store.called

    def test_pretend_mode_is_reset_even_on_exception(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.get.return_value = "Rock"
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lastgenre"
        mock_plugin._process.side_effect = RuntimeError("plugin error")
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        resp = client.post("/api/album/1/genre")
        assert resp.status_code == 500
        # Verify pretend was reset (False) even after error
        mock_plugin.config.__getitem__.return_value.set.assert_called_with(False)


# ---------------------------------------------------------------------------
# POST /api/album/<id>/genre/confirm
# ---------------------------------------------------------------------------


class TestConfirmGenre:
    def test_returns_404_when_album_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/9999/genre/confirm")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_returns_500_when_lastgenre_plugin_not_loaded(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[])
        resp = client.post("/api/album/1/genre/confirm")
        assert resp.status_code == 500
        assert resp.get_json()["error"] == "lastgenre plugin not loaded"

    def test_calls_process_with_write_true(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.get.return_value = "Jazz"
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lastgenre"
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        resp = client.post("/api/album/1/genre/confirm")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "genre" in data
        mock_plugin._process.assert_called_once_with(mock_album, write=True)

    def test_returns_formatted_genre(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.get.return_value = ["Rock", "Pop"]
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lastgenre"
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        resp = client.post("/api/album/1/genre/confirm")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["genre"] == "Rock, Pop"

    def test_clears_genres_before_process_so_existing_genre_is_overwritten(self, client, mocker):
        """confirm_genre must clear fresh.genres before calling _process.

        Without clearing, lastgenre._process with force=False skips the Last.fm
        lookup when the album already has genres, making confirm a silent no-op.
        """
        genres_at_call_time = []

        def capture_genres_on_process(album, write):
            genres_at_call_time.append(list(album.genres))

        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.genres = ["OldGenre"]
        mock_album.get.return_value = "NewGenre"
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lastgenre"
        mock_plugin._process.side_effect = capture_genres_on_process
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        resp = client.post("/api/album/1/genre/confirm")
        assert resp.status_code == 200
        assert genres_at_call_time == [[]], "genres must be cleared before _process"

    def test_writes_genre_to_audio_files_after_process(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.track = 1
        mock_item.title = "Track"
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.get.return_value = "Rock"
        mock_album.items.return_value = [mock_item]
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)
        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lastgenre"
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])
        mock_write = mocker.patch("beetdeck.routes.genres._try_write_item", return_value=True)

        resp = client.post("/api/album/1/genre/confirm")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        mock_write.assert_called_once_with(mock_item)

    def test_returns_partial_status_when_file_write_fails(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.track = 1
        mock_item.title = "Track"
        mock_item.id = 42
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.get.return_value = "Rock"
        mock_album.items.return_value = [mock_item]
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)
        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "lastgenre"
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])
        mocker.patch("beetdeck.routes.genres._try_write_item", return_value=False)

        resp = client.post("/api/album/1/genre/confirm")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "partial"
        assert data["write_failures"] == [42]
        assert "genre" in data


# ---------------------------------------------------------------------------
# POST /api/album/<id>/genre/save
# ---------------------------------------------------------------------------


class TestSaveGenre:
    def test_returns_400_when_genre_is_empty(self, client):
        resp = client.post(
            "/api/album/1/genre/save",
            json={"genre": ""},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Genre cannot be empty"

    def test_returns_400_when_no_body(self, client):
        resp = client.post("/api/album/1/genre/save", json={})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Genre cannot be empty"

    def test_returns_404_when_album_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/9999/genre/save", json={"genre": "Rock"})
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_saves_single_genre_to_album_and_items(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_album = mocker.MagicMock()
        mock_album.items.return_value = [mock_item]
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/genre/save", json={"genre": "Jazz"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["genre"] == "Jazz"

        # beets 2.10.0: write only to the native list field genres
        assert mock_album.genres == ["Jazz"]
        assert mock_album.store.called
        assert mock_item.genres == ["Jazz"]
        assert mock_item.store.called
        assert mock_item.try_write.called

    def test_saves_multiple_genres_as_list(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_album = mocker.MagicMock()
        mock_album.items.return_value = [mock_item]
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/genre/save", json={"genre": "Rock, Pop, Electronic"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["genre"] == "Rock, Pop, Electronic"
        # beets 2.10.0: write genres list directly, not singular genre field
        assert mock_album.genres == ["Rock", "Pop", "Electronic"]
        assert mock_item.genres == ["Rock", "Pop", "Electronic"]

    def test_returns_partial_when_write_fails(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.id = 99
        mock_album = mocker.MagicMock()
        mock_album.items.return_value = [mock_item]
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.genres._try_write_item", return_value=False)

        resp = client.post("/api/album/1/genre/save", json={"genre": "Rock"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "partial"
        assert data["genre"] == "Rock"
        assert data["write_failures"] == [99]

    def test_strips_whitespace_from_genre(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_album.items.return_value = []
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/genre/save", json={"genre": "  Rock  "})
        assert resp.status_code == 200
        assert resp.get_json()["genre"] == "Rock"


# ---------------------------------------------------------------------------
# OpenAPI spec coverage (spectree decoration)
# ---------------------------------------------------------------------------


class TestGenresOpenAPISpec:
    """The three genre routes are decorated with ``@api.validate`` (tagged
    ``genres``). fetch/confirm are documented response-only (no body); save
    documents a JSON body. Assert they appear in the generated spec."""

    def test_preview_in_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/album/{album_id}/genre"]["post"]
        assert "genres" in op["tags"]
        assert "200" in op["responses"]
        assert "404" in op["responses"]
        # No JSON body documented (body-less POST).
        assert "requestBody" not in op

    def test_confirm_in_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/album/{album_id}/genre/confirm"]["post"]
        assert "genres" in op["tags"]
        assert "200" in op["responses"]
        assert "requestBody" not in op

    def test_save_documents_json_body(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/album/{album_id}/genre/save"]["post"]
        assert "genres" in op["tags"]
        assert "200" in op["responses"]
        assert "application/json" in op["requestBody"]["content"]


class TestGenresBehaviorUnchanged:
    """Decoration must not change behavior: body-less preview/confirm POSTs still
    reach the handler, and save's own empty-genre 400 still fires; only a
    malformed save body (unknown field / wrong type) now yields 422."""

    def test_save_unknown_field_returns_422(self, client):
        # extra="forbid" → unknown field rejected by pydantic before the handler.
        resp = client.post("/api/album/1/genre/save", json={"bogus": "x"})
        assert resp.status_code == 422

    def test_save_wrong_type_returns_422(self, client):
        # genre must be a string; an int is rejected → 422 (not the handler's 400).
        resp = client.post("/api/album/1/genre/save", json={"genre": 123})
        assert resp.status_code == 422

    def test_save_empty_body_still_400(self, client):
        # Empty body is valid against the optional model → handler's own 400 fires.
        resp = client.post("/api/album/1/genre/save", json={})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Genre cannot be empty"

    def test_preview_without_body_reaches_handler(self, client, mocker):
        # Body-less preview must reach the handler (not 422); album missing → 404.
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/9999/genre")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_confirm_without_body_reaches_handler(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/9999/genre/confirm")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"
