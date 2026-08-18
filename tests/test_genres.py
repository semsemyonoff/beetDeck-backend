"""Tests for genres blueprint routes."""

import beetdeck.state as state

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

    def test_drops_the_mbsync_stash(self, client, mocker):
        """The confirmed genre becomes part of the album's item_keys, which the
        mbsync confirm's album-level copy reads — a pending stash for this
        album is stale once this write commits."""
        with state.identify_lock:
            state.identify_tasks["mbsync_1"] = {
                "_mbsync_obj": object(),
                "stash_generation": state.next_stash_generation(),
            }

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
        assert "mbsync_1" not in state.identify_tasks

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

    def test_drops_the_mbsync_stash(self, client, mocker):
        """Same reasoning as the confirm route: genres are part of
        `Album.item_keys`, which the mbsync confirm's album-level copy reads."""
        with state.identify_lock:
            state.identify_tasks["mbsync_1"] = {
                "_mbsync_obj": object(),
                "stash_generation": state.next_stash_generation(),
            }

        mock_item = mocker.MagicMock()
        mock_album = mocker.MagicMock()
        mock_album.items.return_value = [mock_item]
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/genre/save", json={"genre": "Jazz"})
        assert resp.status_code == 200
        assert "mbsync_1" not in state.identify_tasks


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


# ---------------------------------------------------------------------------
# preview modes and a failed lookup
# ---------------------------------------------------------------------------


class TestGenrePreviewModes:
    """`replace` vs `merge`, and the difference between "nothing" and "no answer"."""

    class _Album:
        """A real object, not a MagicMock: the route *writes* `genres` before the
        lookup and reads it back afterwards, and a mock would keep answering the
        old value through `get()` no matter what was assigned."""

        albumartist = "Artist"
        album = "Album"

        def __init__(self, genres):
            self.genres = list(genres)

        def get(self, key, default=""):
            return self.genres if key == "genres" else default

        def store(self):
            pass

    def _lastgenre(self, mocker, *, current, fetched, error=None):
        """A library album carrying `current`, and a lookup returning `fetched`."""
        album = self._Album(current)
        lib = mocker.MagicMock()
        lib.get_album.return_value = album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=lib)

        plugin = mocker.MagicMock()
        plugin.name = "lastgenre"

        def process(target, write):
            if error is not None:
                # What lastgenre does with an upstream refusal: log it, write nothing.
                import logging

                logging.getLogger("beets.lastgenre").debug("last.fm error: {}", error)
                return
            target.genres = list(fetched)

        plugin._process.side_effect = process
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])
        return album

    def test_replace_is_the_default_and_proposes_only_what_was_fetched(self, client, mocker):
        self._lastgenre(mocker, current=["Rock"], fetched=["Electronic"])
        data = client.post("/api/album/1/genre").get_json()
        assert data["mode"] == "replace"
        assert data["old_genre"] == "Rock"
        assert data["fetched_genre"] == "Electronic"
        assert data["new_genre"] == "Electronic"

    def test_merge_keeps_the_albums_genres_and_appends_the_new_ones(self, client, mocker):
        self._lastgenre(mocker, current=["Rock", "Psychedelic Rock"], fetched=["Electronic"])
        data = client.post("/api/album/1/genre?mode=merge").get_json()
        assert data["mode"] == "merge"
        assert data["new_genre"] == "Rock, Psychedelic Rock, Electronic"

    def test_merge_does_not_duplicate_a_genre_that_differs_only_in_case(self, client, mocker):
        self._lastgenre(mocker, current=["Rock"], fetched=["rock", "Electronic"])
        data = client.post("/api/album/1/genre?mode=merge").get_json()
        assert data["new_genre"] == "Rock, Electronic"

    def test_merge_on_an_album_with_no_genres_equals_replace(self, client, mocker):
        self._lastgenre(mocker, current=[], fetched=["Electronic"])
        data = client.post("/api/album/1/genre?mode=merge").get_json()
        assert data["new_genre"] == "Electronic"

    def test_an_unknown_mode_is_rejected_rather_than_silently_replacing(self, client, mocker):
        self._lastgenre(mocker, current=["Rock"], fetched=["Electronic"])
        assert client.post("/api/album/1/genre?mode=append").status_code == 422

    def test_a_preview_on_a_legacy_album_leaves_its_genre_alone(self, client, db_path, mocker):
        """A pre-2.10 album reads its genre through the model's `genres` (beets
        maps the singular column), and merge must show it — while the preview
        itself leaves the album's genre exactly as it found it."""
        import sqlite3

        from beetdeck.utils import _genre_names
        from tests.conftest import insert_album

        album_id = insert_album(db_path, album="Old", albumartist="A", genre="Rock")
        plugin = mocker.MagicMock()
        plugin.name = "lastgenre"

        def process(target, write):
            target.genres = ["Electronic"]

        plugin._process.side_effect = process
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])

        data = client.post(f"/api/album/{album_id}/genre?mode=merge").get_json()
        assert data["old_genre"] == "Rock"
        assert data["new_genre"] == "Rock, Electronic"

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT genre, genres FROM albums WHERE id = ?", (album_id,)
            ).fetchone()
        finally:
            conn.close()
        # The genre is untouched: the previewed Electronic was never written and
        # the album still reads as Rock.
        assert row[0] == "Rock"
        assert _genre_names(row[1]) == ["Rock"]
        # The legacy column now also appears in `genres` — that copy is beets'
        # own migration, run by `_init_beets` on every route that opens the
        # library, not a write this preview performed.

    def test_a_failed_lookup_is_a_502_not_an_empty_genre(self, client, mocker):
        """The old shape claimed the album had no genre when Last.fm had refused."""
        self._lastgenre(
            mocker, current=["Rock"], fetched=[], error="Access Denied - You cannot access"
        )
        resp = client.post("/api/album/1/genre")
        assert resp.status_code == 502
        error = resp.get_json()["error"]
        assert "Access Denied" in error
        assert "says nothing about the album" in error

    def test_a_successful_lookup_with_no_tags_stays_a_200(self, client, mocker):
        """Last.fm answering "I have nothing" is a fact; it is not an error."""
        self._lastgenre(mocker, current=["Rock"], fetched=[])
        resp = client.post("/api/album/1/genre")
        assert resp.status_code == 200
        assert resp.get_json()["fetched_genre"] == ""
        assert resp.get_json()["new_genre"] == ""

    def test_the_capture_handler_is_removed_afterwards(self, client, mocker):
        import logging

        self._lastgenre(mocker, current=["Rock"], fetched=["Electronic"])
        client.post("/api/album/1/genre")
        assert logging.getLogger("beets.lastgenre").handlers == []
