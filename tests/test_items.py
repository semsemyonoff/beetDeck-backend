"""Tests for items blueprint routes."""

import beetdeck.state as state
from tests.conftest import insert_album, insert_item

# ---------------------------------------------------------------------------
# GET /api/items/fields
# ---------------------------------------------------------------------------


class TestItemsFields:
    def test_returns_200_with_list(self, client):
        resp = client.get("/api/items/fields")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_each_entry_has_required_shape(self, client):
        data = client.get("/api/items/fields").get_json()
        for entry in data:
            assert "name" in entry
            assert "type" in entry
            assert "editable" in entry
            assert "album_level" in entry
            assert isinstance(entry["editable"], bool)
            assert isinstance(entry["album_level"], bool)

    def test_known_editable_field_present_and_editable(self, client):
        data = client.get("/api/items/fields").get_json()
        by_name = {e["name"]: e for e in data}
        assert "title" in by_name
        assert by_name["title"]["editable"] is True

    def test_length_is_readonly(self, client):
        data = client.get("/api/items/fields").get_json()
        by_name = {e["name"]: e for e in data}
        assert "length" in by_name
        assert by_name["length"]["editable"] is False

    def test_mb_trackid_is_readonly(self, client):
        data = client.get("/api/items/fields").get_json()
        by_name = {e["name"]: e for e in data}
        assert "mb_trackid" in by_name
        assert by_name["mb_trackid"]["editable"] is False

    def test_album_has_album_level_true(self, client):
        data = client.get("/api/items/fields").get_json()
        by_name = {e["name"]: e for e in data}
        assert "album" in by_name
        assert by_name["album"]["album_level"] is True

    def test_title_has_album_level_false(self, client):
        data = client.get("/api/items/fields").get_json()
        by_name = {e["name"]: e for e in data}
        assert by_name["title"]["album_level"] is False


# ---------------------------------------------------------------------------
# POST /api/items/metadata-batch
# ---------------------------------------------------------------------------


class TestMetadataBatch:
    def _make_mock_item(self, mocker, item_id, album_id):
        item = mocker.MagicMock()
        item.id = item_id
        item.album_id = album_id
        item.path = f"/music/track{item_id}.mp3".encode()
        return item

    def _make_mock_album(self, mocker, album_id, members=None):
        al = mocker.MagicMock()
        al.id = album_id
        # Album.items() returns the album's member items; default to none so
        # existing tests only flush the request items.
        al.items.return_value = list(members) if members else []
        return al

    def test_returns_400_for_empty_body(self, client):
        resp = client.post("/api/items/metadata-batch", json={})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_returns_400_when_only_bare_ids_and_no_album_fields(self, client):
        # items with only ids, no album fields, no per-track overrides
        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 1}, {"id": 2}]},
        )
        assert resp.status_code == 400

    def test_returns_400_when_album_fields_present_but_no_items(self, client):
        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": {"albumartist": "Artist"}, "items": []},
        )
        assert resp.status_code == 400

    def test_returns_400_when_album_fields_present_and_items_absent(self, client):
        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": {"album": "My Album"}},
        )
        assert resp.status_code == 400

    def test_album_only_save_sets_album_fields_and_calls_store(self, client, mocker):
        """Album-level fields are written via the beets Album object; al.store() called."""
        mock_item = self._make_mock_item(mocker, 1, 10)
        mock_al = self._make_mock_album(mocker, 10)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mock_lib.get_album.return_value = mock_al
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        write = mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.post(
            "/api/items/metadata-batch",
            json={
                "album": {"album": "Tagged Album", "albumartist": "Great Artist", "year": 2023},
                "items": [{"id": 1}],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert 10 in data["albums_updated"]
        assert mock_al.album == "Tagged Album"
        assert mock_al.albumartist == "Great Artist"
        assert mock_al.year == 2023
        mock_al.store.assert_called_once()
        # Album-level fields must also be flushed to the member item's file,
        # otherwise on-disk tags go stale (Album.store() only touches the DB).
        write.assert_called_once_with(mock_item)
        assert mock_item.album == "Tagged Album"
        assert mock_item.albumartist == "Great Artist"
        assert mock_item.year == 2023
        mock_item.store.assert_called_once()

    def test_genres_stored_as_list_not_scalar(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 1, 10)
        mock_al = self._make_mock_album(mocker, 10)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mock_lib.get_album.return_value = mock_al
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.post(
            "/api/items/metadata-batch",
            json={
                "album": {"albumartist": "A", "genre": "Rock, Electronic"},
                "items": [{"id": 1}],
            },
        )
        assert resp.status_code == 200
        # genres must be a list, not a string — on both the album and the file-
        # flushed item.
        assert mock_al.genres == ["Rock", "Electronic"]
        assert mock_item.genres == ["Rock", "Electronic"]

    def test_per_track_title_and_artist_override_persists(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 2, 20)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 2, "title": "New Title", "artist": "New Artist"}]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert mock_item.title == "New Title"
        assert mock_item.artist == "New Artist"
        mock_item.store.assert_called()

    def test_per_track_track_and_disc_coerced_to_int(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 3, 30)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 3, "track": "5", "disc": "2"}]},
        )
        assert resp.status_code == 200
        assert mock_item.track == 5
        assert mock_item.disc == 2

    def test_per_track_numeric_float_reaches_handler_not_422(self, client, mocker):
        """A fractional JSON float for track/disc/year must reach the handler's
        int() coerce-and-warn path (200), not be rejected at the schema as 422.

        The lenient numeric fields are typed ``int | float | str | None`` so the
        documented "any JSON scalar passes; the handler decides" contract holds
        for JSON numbers as well as strings (matches pre-spectree behavior:
        int(1.5) -> 1)."""
        mock_item = self._make_mock_item(mocker, 3, 30)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 3, "track": 1.5, "disc": 2.0}]},
        )
        assert resp.status_code == 200
        assert mock_item.track == 1
        assert mock_item.disc == 2

    def test_unknown_item_id_produces_warning_not_500(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        # Need at least one item override since no album fields
        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 999, "title": "Ghost"}]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert any("999" in w for w in data["warnings"])

    def test_file_write_failure_produces_warning_not_500(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 4, 40)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=False)

        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 4, "title": "Titled Track"}]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert any("file write failed" in w for w in data["warnings"])

    def test_invalid_year_produces_warning_not_500(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 1, 10)
        mock_al = self._make_mock_album(mocker, 10)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mock_lib.get_album.return_value = mock_al
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.post(
            "/api/items/metadata-batch",
            json={
                "album": {"albumartist": "A", "year": "not-a-year"},
                "items": [{"id": 1}],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert any("invalid year" in w for w in data["warnings"])

    def test_invalid_track_and_disc_produce_warnings_not_500(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 3, 30)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 3, "track": "x", "disc": "y"}]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert any("invalid track" in w for w in data["warnings"])
        assert any("invalid disc" in w for w in data["warnings"])

    def test_album_only_save_sends_all_item_ids(self, client, mocker):
        """Album-only save (no per-track edits) still collects album_id from items."""
        items = [self._make_mock_item(mocker, i, 50) for i in range(1, 4)]
        mock_al = self._make_mock_album(mocker, 50)

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.side_effect = lambda iid: next((x for x in items if x.id == iid), None)
        mock_lib.get_album.return_value = mock_al

        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        write = mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.post(
            "/api/items/metadata-batch",
            json={
                "album": {"albumartist": "Bulk Artist"},
                "items": [{"id": 1}, {"id": 2}, {"id": 3}],
            },
        )
        assert resp.status_code == 200
        # All three items were loaded to resolve album_id
        assert mock_lib.get_item.call_count == 3
        # Album store called once (single album_id=50)
        mock_al.store.assert_called_once()
        # Every member file is rewritten with the propagated album field.
        assert write.call_count == 3
        for it in items:
            assert it.albumartist == "Bulk Artist"

    def test_multiple_album_ids_in_items_all_updated(self, client, mocker):
        item1 = self._make_mock_item(mocker, 1, 10)
        item2 = self._make_mock_item(mocker, 2, 20)
        al10 = self._make_mock_album(mocker, 10)
        al20 = self._make_mock_album(mocker, 20)

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.side_effect = lambda iid: {1: item1, 2: item2}.get(iid)
        mock_lib.get_album.side_effect = lambda aid: {10: al10, 20: al20}.get(aid)
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        write = mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.post(
            "/api/items/metadata-batch",
            json={
                "album": {"albumartist": "Shared Artist"},
                "items": [{"id": 1}, {"id": 2}],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert set(data["albums_updated"]) == {10, 20}
        al10.store.assert_called_once()
        al20.store.assert_called_once()
        # Both members' files get the album field flushed.
        assert write.call_count == 2
        assert item1.albumartist == "Shared Artist"
        assert item2.albumartist == "Shared Artist"

    def test_album_members_not_in_request_get_files_flushed(self, client, mocker):
        """A member of an updated album that is absent from the request still
        gets its file rewritten, so multi-disc / shared-album on-disk tags do
        not go stale (mirrors genres.py save_genre)."""
        req_item = self._make_mock_item(mocker, 1, 70)
        # A second track shares album_id 70 but is not listed in items_data.
        other_member = self._make_mock_item(mocker, 2, 70)
        mock_al = self._make_mock_album(mocker, 70, members=[req_item, other_member])

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.side_effect = lambda iid: {1: req_item}.get(iid)
        mock_lib.get_album.return_value = mock_al
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        write = mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": {"albumartist": "Bulk Artist"}, "items": [{"id": 1}]},
        )
        assert resp.status_code == 200
        # Both the request item and the absent member files are written once each.
        written = {c.args[0].id for c in write.call_args_list}
        assert written == {1, 2}
        assert write.call_count == 2

    def test_absent_album_member_file_write_failure_surfaces_warning(self, client, mocker):
        req_item = self._make_mock_item(mocker, 1, 71)
        other_member = self._make_mock_item(mocker, 2, 71)
        mock_al = self._make_mock_album(mocker, 71, members=[req_item, other_member])

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.side_effect = lambda iid: {1: req_item}.get(iid)
        mock_lib.get_album.return_value = mock_al
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        # Only the absent member (id 2) fails to write.
        mocker.patch(
            "beetdeck.routes.items._try_write_item",
            side_effect=lambda it: it.id != 2,
        )

        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": {"albumartist": "Bulk Artist"}, "items": [{"id": 1}]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert any("file write failed" in w for w in data["warnings"])

    # --- Structural / strict-type validation now enforced by pydantic → 422 ---
    # (Task 9: these were the handler's hand-rolled 400 type checks; spectree's
    # MetadataBatchBody now rejects them uniformly with 422 before the handler.)

    def test_non_object_body_returns_422(self, client):
        resp = client.post("/api/items/metadata-batch", json=[1, 2, 3])
        assert resp.status_code == 422

    def test_album_is_not_object_returns_422(self, client):
        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": "x", "items": [{"id": 1}]},
        )
        assert resp.status_code == 422

    def test_items_is_not_list_returns_422(self, client):
        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": {"albumartist": "A"}, "items": {"id": 1}},
        )
        assert resp.status_code == 422

    def test_item_entry_not_object_returns_422(self, client):
        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": {"albumartist": "A"}, "items": [1]},
        )
        assert resp.status_code == 422

    def test_album_is_falsy_non_object_returns_422(self, client):
        # A falsy malformed value (e.g. []) must not be normalized to {} and
        # silently accepted as an item-only write.
        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": [], "items": [{"id": 1, "title": "X"}]},
        )
        assert resp.status_code == 422

    def test_items_is_falsy_non_list_returns_422(self, client):
        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": {"albumartist": "A"}, "items": 0},
        )
        assert resp.status_code == 422

    def test_album_scalar_wrong_type_returns_422(self, client):
        # A non-string album scalar is a type error → 422 (was a hand-rolled 400).
        for bad in ([1], 123, True, {"a": 1}):
            resp = client.post(
                "/api/items/metadata-batch",
                json={"album": {"album": bad}, "items": [{"id": 1}]},
            )
            assert resp.status_code == 422, bad

    def test_album_falsy_scalar_wrong_type_returns_422(self, client):
        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": {"album": []}, "items": [{"id": 1}]},
        )
        assert resp.status_code == 422

    def test_item_scalar_wrong_type_returns_422(self, client):
        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 1, "title": ["x"]}]},
        )
        assert resp.status_code == 422

    def test_item_id_is_bool_returns_422(self, client):
        # `bool` is an `int` subclass; MetadataBatchItem.id is strict, so a JSON
        # boolean must not alias to item 0/1 — it is now a 422.
        for bad in (True, False):
            resp = client.post(
                "/api/items/metadata-batch",
                json={"items": [{"id": bad, "title": "X"}]},
            )
            assert resp.status_code == 422, bad

    def test_item_id_missing_returns_422(self, client):
        # id is required on every item entry.
        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"title": "X"}]},
        )
        assert resp.status_code == 422

    def test_unknown_field_returns_422(self, client):
        # extra="forbid" on both the body and item entries.
        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 1, "bogus": 1}]},
        )
        assert resp.status_code == 422


def _reset_state():
    state.identify_tasks.clear()


# ---------------------------------------------------------------------------
# GET /api/items/untagged
# ---------------------------------------------------------------------------


class TestUntaggedItems:
    def test_returns_empty_when_no_items(self, client):
        resp = client.get("/api/items/untagged")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_items_for_null_albumartist(self, client, db_path):
        album_id = insert_album(db_path, albumartist=None, album="Mystery Album")
        item_id = insert_item(db_path, album_id, title="Track 1", artist="", path=b"/music/t1.mp3")

        resp = client.get("/api/items/untagged")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["id"] == item_id
        assert data[0]["title"] == "Track 1"
        assert data[0]["album_id"] == album_id

    def test_returns_items_for_empty_albumartist(self, client, db_path):
        album_id = insert_album(db_path, albumartist="", album="Empty Artist Album")
        insert_item(db_path, album_id, title="Track A", path=b"/music/ta.mp3")

        resp = client.get("/api/items/untagged")
        assert resp.status_code == 200
        data = resp.get_json()
        assert any(d["title"] == "Track A" for d in data)

    def test_excludes_items_with_known_albumartist(self, client, db_path):
        known_album_id = insert_album(db_path, albumartist="Known Artist", album="Known Album")
        insert_item(db_path, known_album_id, title="Known Track", path=b"/music/k.mp3")
        unknown_album_id = insert_album(db_path, albumartist=None, album="Unknown Album")
        insert_item(db_path, unknown_album_id, title="Unknown Track", path=b"/music/u.mp3")

        resp = client.get("/api/items/untagged")
        assert resp.status_code == 200
        data = resp.get_json()
        titles = [d["title"] for d in data]
        assert "Unknown Track" in titles
        assert "Known Track" not in titles

    def test_path_decoded_from_bytes(self, client, db_path):
        album_id = insert_album(db_path, albumartist=None)
        insert_item(db_path, album_id, title="T", path=b"/music/track.mp3")

        resp = client.get("/api/items/untagged")
        data = resp.get_json()
        assert isinstance(data[0]["path"], str)
        assert data[0]["path"] == "/music/track.mp3"

    def test_path_resolved_against_library_root(self, db_path):
        from beetdeck import create_app
        from tests.conftest import insert_album, insert_item

        app = create_app(
            test_config={
                "LIBRARY_DB": db_path,
                "IMPORT_DIR": "/tmp/music_import",
                "LIBRARY_ROOT": "/data/music",
                "TESTING": True,
            }
        )
        album_id = insert_album(db_path, albumartist=None)
        insert_item(db_path, album_id, title="Rel", path="Artist/Album/track.mp3")

        with app.test_client() as c:
            resp = c.get("/api/items/untagged")
        data = resp.get_json()
        assert data[0]["path"] == "/data/music/Artist/Album/track.mp3"


# ---------------------------------------------------------------------------
# POST /api/items/<item_id>/metadata
# ---------------------------------------------------------------------------


class TestUpdateMetadata:
    def test_returns_404_for_missing_item(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.post("/api/items/99/metadata", json={"artist": "A", "album": "B"})
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"].lower()

    def test_returns_400_when_no_fields(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.post("/api/items/1/metadata", json={})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_updates_artist_and_album(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.try_write.return_value = True
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.post(
            "/api/items/1/metadata", json={"artist": "New Artist", "album": "New Album"}
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert mock_item.artist == "New Artist"
        assert mock_item.album == "New Album"
        mock_item.store.assert_called()

    def test_updates_only_artist(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.try_write.return_value = True
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.post("/api/items/1/metadata", json={"artist": "Only Artist"})
        assert resp.status_code == 200
        assert mock_item.artist == "Only Artist"

    def test_returns_warning_when_tag_write_fails(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.try_write.return_value = False
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.post("/api/items/1/metadata", json={"artist": "X"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "warnings" in data

    def test_returns_ok_with_item_id(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.try_write.return_value = True
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.post("/api/items/42/metadata", json={"album": "My Album"})
        assert resp.status_code == 200
        assert resp.get_json()["item_id"] == 42

    def test_updates_title(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.try_write.return_value = True
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.post("/api/items/1/metadata", json={"title": "New Title"})
        assert resp.status_code == 200
        assert mock_item.title == "New Title"

    def test_returns_400_when_all_fields_empty(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.post("/api/items/1/metadata", json={"title": "", "artist": "", "album": ""})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/items/identify
# ---------------------------------------------------------------------------


class TestItemsIdentify:
    def setup_method(self):
        _reset_state()

    def test_returns_400_for_empty_list(self, client):
        resp = client.post("/api/items/identify", json={"item_ids": []})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_returns_400_for_missing_item_ids(self, client):
        resp = client.post("/api/items/identify", json={})
        assert resp.status_code == 400

    def test_non_integer_ids_returns_422(self, client):
        # item_ids is typed list[int]; non-int elements now fail pydantic → 422.
        resp = client.post("/api/items/identify", json={"item_ids": ["a", "b"]})
        assert resp.status_code == 422

    def test_unknown_field_returns_422(self, client):
        resp = client.post("/api/items/identify", json={"item_ids": [1], "bogus": 1})
        assert resp.status_code == 422

    def test_returns_started_and_task_id(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_item = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item

        mock_proposal = mocker.MagicMock()
        mock_proposal.candidates = []

        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch(
            "beets.autotag.tag_album",
            return_value=("Artist", "Album", mock_proposal),
        )

        resp = client.post("/api/items/identify", json={"item_ids": [1, 2]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "started"
        assert "task_id" in data

    def test_invalid_item_id_in_background_sets_error(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.post("/api/items/identify", json={"item_ids": [999]})
        assert resp.status_code == 200
        data = resp.get_json()
        task_id = data["task_id"]

        # Wait for background thread to complete
        import time

        time.sleep(0.5)

        status_resp = client.get(f"/api/items/identify/{task_id}/status")
        status_data = status_resp.get_json()
        assert status_data["status"] == "error"
        assert "999" in status_data["error"]


# ---------------------------------------------------------------------------
# GET /api/items/identify/<task_id>/status
# ---------------------------------------------------------------------------


class TestItemsIdentifyStatus:
    def setup_method(self):
        _reset_state()

    def test_returns_idle_when_no_task(self, client):
        resp = client.get("/api/items/identify/doesnotexist/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "idle"

    def test_returns_task_without_private_keys(self, client):
        state.identify_tasks["items_abc"] = {
            "task_id": "abc",
            "status": "done",
            "candidates": [],
            "_matches": ["private"],
            "_lib": object(),
        }
        resp = client.get("/api/items/identify/abc/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "done"
        assert "_matches" not in data
        assert "_lib" not in data

    def test_returns_running_status(self, client):
        state.identify_tasks["items_xyz"] = {
            "task_id": "xyz",
            "status": "running",
            "candidates": [],
        }
        resp = client.get("/api/items/identify/xyz/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"


# ---------------------------------------------------------------------------
# POST /api/items/identify/<task_id>/apply
# ---------------------------------------------------------------------------


class TestItemsApply:
    def setup_method(self):
        _reset_state()

    def test_returns_400_when_no_task(self, client):
        resp = client.post("/api/items/identify/notexist/apply", json={"candidate_index": 0})
        assert resp.status_code == 400

    def test_returns_400_when_task_not_done(self, client):
        state.identify_tasks["items_t1"] = {"status": "running", "_matches": []}
        resp = client.post("/api/items/identify/t1/apply", json={"candidate_index": 0})
        assert resp.status_code == 400

    def test_returns_400_for_invalid_candidate_index(self, client, mocker):
        mock_match = mocker.MagicMock()
        state.identify_tasks["items_t2"] = {
            "status": "done",
            "_matches": [mock_match],
        }
        resp = client.post("/api/items/identify/t2/apply", json={"candidate_index": 5})
        assert resp.status_code == 400

    def test_returns_diff_with_album_and_tracks(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.track = 1
        mock_item.title = "Old Title"
        mock_item.artist = "Old Artist"

        mock_track_info = mocker.MagicMock()
        mock_track_info.title = "New Title"
        mock_track_info.artist = "New Artist"

        mock_info = mocker.MagicMock()
        mock_info.artist = "Match Artist"
        mock_info.album = "Match Album"
        mock_info.year = 2023
        mock_info.album_id = "mb-123"
        mock_info.item_data = {}

        mock_match = mocker.MagicMock()
        mock_match.info = mock_info
        mock_match.mapping = {mock_item: mock_track_info}

        state.identify_tasks["items_t3"] = {
            "status": "done",
            "_matches": [mock_match],
        }

        resp = client.post("/api/items/identify/t3/apply", json={"candidate_index": 0})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "album" in data
        assert "tracks" in data
        assert data["album"]["albumartist"]["new"] == "Match Artist"
        assert data["tracks"][0]["title"]["old"] == "Old Title"
        assert data["tracks"][0]["title"]["new"] == "New Title"
        assert data["candidate_index"] == 0


# ---------------------------------------------------------------------------
# POST /api/items/identify/<task_id>/confirm
# ---------------------------------------------------------------------------


class TestRollbackFieldsCoverage:
    """Pins _ROLLBACK_FIELDS to the beets field set it is supposed to mirror.

    The list drifted silently once already: beets renamed arranger/composer/
    lyricist/remixer to their plural multi-valued forms and the snapshot kept
    the dead singulars, so an identify rollback could not restore those four
    fields at all. Checked against the installed beets rather than a literal, so
    the next upstream rename fails here instead of rotting.
    """

    # Written by apply_metadata() as flexible attributes — they are deliberately
    # snapshotted even though beets does not declare them as Item fields.
    FLEX_ONLY = {"data_source", "data_url", "track_alt"}

    def test_covers_every_media_tag_field(self):
        from beets.library import Item

        from beetdeck.routes.items import _ROLLBACK_FIELDS

        # from_scratch imports call item.clear(), which zeroes every media tag
        # field before the update — anything missing here cannot be rolled back.
        assert set(Item._media_tag_fields) <= set(_ROLLBACK_FIELDS)

    def test_has_no_entries_beets_no_longer_knows(self):
        from beets.library import Item

        from beetdeck.routes.items import _ROLLBACK_FIELDS

        # Rollback does setattr() over the whole snapshot, so a name beets has
        # dropped is not merely useless: it stamps a NULL flex attribute onto
        # every rolled-back item.
        assert set(_ROLLBACK_FIELDS) - set(Item._fields) == self.FLEX_ONLY

    def test_has_no_duplicates(self):
        from beetdeck.routes.items import _ROLLBACK_FIELDS

        assert len(_ROLLBACK_FIELDS) == len(set(_ROLLBACK_FIELDS))


class TestItemsConfirm:
    def setup_method(self):
        _reset_state()

    def test_returns_400_when_no_task(self, client):
        resp = client.post("/api/items/identify/notexist/confirm", json={"candidate_index": 0})
        assert resp.status_code == 400

    def test_returns_400_when_task_not_done(self, client):
        state.identify_tasks["items_c1"] = {"status": "running", "_matches": [], "item_ids": [1]}
        resp = client.post("/api/items/identify/c1/confirm", json={"candidate_index": 0})
        assert resp.status_code == 400

    def test_returns_ok_with_album_id_on_success(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.id = 1
        mock_item.album_id = 10
        mock_item.track = 1
        mock_item.try_write.return_value = True

        mock_album = mocker.MagicMock()
        mock_album.id = 99

        mock_match = mocker.MagicMock()
        mock_match.mapping = {mock_item: mocker.MagicMock()}

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mock_lib.add_album.return_value = mock_album

        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        state.identify_tasks["items_c2"] = {
            "task_id": "c2",
            "status": "done",
            "_matches": [mock_match],
            "item_ids": [1],
        }

        resp = client.post("/api/items/identify/c2/confirm", json={"candidate_index": 0})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["album_id"] == 99
        assert "warnings" not in data
        mock_album.store.assert_called()
        mock_match.apply_metadata.assert_called()
        mock_match.apply_album_metadata.assert_called_with(mock_album)

    def test_returns_ok_with_warnings_on_partial_file_write_failure(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.id = 1
        mock_item.album_id = 10
        mock_item.track = 1
        mock_item.title = "Track"
        mock_item.path = b"/music/track.mp3"
        mock_item.try_write.return_value = False  # simulate write failure

        mock_album = mocker.MagicMock()
        mock_album.id = 55

        mock_match = mocker.MagicMock()
        mock_match.mapping = {mock_item: mocker.MagicMock()}

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mock_lib.add_album.return_value = mock_album

        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        state.identify_tasks["items_c3"] = {
            "task_id": "c3",
            "status": "done",
            "_matches": [mock_match],
            "item_ids": [1],
        }

        resp = client.post("/api/items/identify/c3/confirm", json={"candidate_index": 0})
        assert resp.status_code == 200
        data = resp.get_json()
        # DB album was still created
        assert data["status"] == "ok"
        assert data["album_id"] == 55
        # warnings list includes the failed file path
        assert "warnings" in data
        assert len(data["warnings"]) == 1
        assert "/music/track.mp3" in data["warnings"][0]

    def test_rolls_back_on_add_album_failure(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.id = 1
        mock_item.album_id = 10

        mock_match = mocker.MagicMock()
        mock_match.mapping = {mock_item: mocker.MagicMock()}

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mock_lib.add_album.side_effect = RuntimeError("DB error")

        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        state.identify_tasks["items_c4"] = {
            "task_id": "c4",
            "status": "done",
            "_matches": [mock_match],
            "item_ids": [1],
        }

        resp = client.post("/api/items/identify/c4/confirm", json={"candidate_index": 0})
        assert resp.status_code == 500
        assert "error" in resp.get_json()
        # album_id restored to original value and store() called
        assert mock_item.album_id == 10
        assert mock_item.store.called
        # apply_metadata not called since add_album failed first
        mock_match.apply_metadata.assert_not_called()

    def test_rolls_back_metadata_on_apply_metadata_failure(self, client, mocker):
        """Rollback reloads fresh items from DB so partially-written metadata is reverted."""
        mock_item = mocker.MagicMock()
        mock_item.id = 1
        mock_item.album_id = 10
        mock_item.title = "Original Title"

        # fresh_item simulates what lib.get_item returns during rollback —
        # it would have the partially-written new value from apply_metadata.
        fresh_item = mocker.MagicMock()
        fresh_item.id = 1

        mock_album = mocker.MagicMock()
        mock_album.id = 42

        mock_match = mocker.MagicMock()
        mock_match.mapping = {mock_item: mocker.MagicMock()}
        mock_match.apply_metadata.side_effect = RuntimeError("metadata error")

        # get_item is called twice: once for initial load, once for rollback reload
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.side_effect = [mock_item, fresh_item]
        mock_lib.add_album.return_value = mock_album

        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        state.identify_tasks["items_c8"] = {
            "task_id": "c8",
            "status": "done",
            "_matches": [mock_match],
            "item_ids": [1],
        }

        resp = client.post("/api/items/identify/c8/confirm", json={"candidate_index": 0})
        assert resp.status_code == 500
        assert "error" in resp.get_json()
        # album record removed on failure
        mock_album.remove.assert_called_once_with(with_items=False)
        # rollback reloaded a fresh item and called store() on it
        assert fresh_item.store.called
        # original title was assigned to the fresh item
        assert fresh_item.title == "Original Title"
        # _matches cleared on rollback to prevent retry with stale mapping items
        assert "_matches" not in state.identify_tasks["items_c8"]

    def test_task_status_set_to_done_after_success(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.id = 1
        mock_item.album_id = 10
        mock_item.track = 1
        mock_item.try_write.return_value = True

        mock_album = mocker.MagicMock()
        mock_album.id = 77

        mock_match = mocker.MagicMock()
        mock_match.mapping = {mock_item: mocker.MagicMock()}

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mock_lib.add_album.return_value = mock_album

        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        state.identify_tasks["items_c7"] = {
            "task_id": "c7",
            "status": "done",
            "_matches": [mock_match],
            "item_ids": [1],
        }

        resp = client.post("/api/items/identify/c7/confirm", json={"candidate_index": 0})
        assert resp.status_code == 200
        assert state.identify_tasks["items_c7"]["status"] == "done"
        assert "_matches" not in state.identify_tasks["items_c7"]

    def test_returns_404_when_item_not_found(self, client, mocker):
        mock_match = mocker.MagicMock()
        mock_match.mapping = {}

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None

        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        state.identify_tasks["items_c5"] = {
            "task_id": "c5",
            "status": "done",
            "_matches": [mock_match],
            "item_ids": [999],
        }

        resp = client.post("/api/items/identify/c5/confirm", json={"candidate_index": 0})
        assert resp.status_code == 404

    def test_returns_400_for_invalid_candidate_index(self, client, mocker):
        mock_match = mocker.MagicMock()
        mock_lib = mocker.MagicMock()
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        state.identify_tasks["items_c6"] = {
            "task_id": "c6",
            "status": "done",
            "_matches": [mock_match],
            "item_ids": [1],
        }

        resp = client.post("/api/items/identify/c6/confirm", json={"candidate_index": 10})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# OpenAPI spec coverage (Task 9)
# ---------------------------------------------------------------------------


class TestItemsOpenAPISpec:
    """All seven items routes are decorated with ``@api.validate`` (tagged
    ``items``). ``untagged`` and ``identify/status`` are body-less GETs
    (response-only); the POSTs that take a JSON body document a requestBody."""

    def _spec(self, client):
        return client.get("/apidoc/openapi.json").get_json()

    def test_untagged_in_spec(self, client):
        op = self._spec(client)["paths"]["/api/items/untagged"]["get"]
        assert "items" in op["tags"]
        assert "200" in op["responses"]
        assert "requestBody" not in op

    def test_update_metadata_documents_json_body(self, client):
        op = self._spec(client)["paths"]["/api/items/{item_id}/metadata"]["post"]
        assert "items" in op["tags"]
        assert "200" in op["responses"]
        assert "application/json" in op["requestBody"]["content"]

    def test_metadata_batch_documents_json_body(self, client):
        op = self._spec(client)["paths"]["/api/items/metadata-batch"]["post"]
        assert "items" in op["tags"]
        assert "200" in op["responses"]
        assert "application/json" in op["requestBody"]["content"]

    def test_identify_documents_json_body(self, client):
        op = self._spec(client)["paths"]["/api/items/identify"]["post"]
        assert "items" in op["tags"]
        assert "200" in op["responses"]
        assert "application/json" in op["requestBody"]["content"]

    def test_identify_status_in_spec(self, client):
        op = self._spec(client)["paths"]["/api/items/identify/{task_id}/status"]["get"]
        assert "items" in op["tags"]
        assert "200" in op["responses"]
        assert "requestBody" not in op

    def test_apply_documents_json_body(self, client):
        op = self._spec(client)["paths"]["/api/items/identify/{task_id}/apply"]["post"]
        assert "items" in op["tags"]
        assert "200" in op["responses"]
        assert "application/json" in op["requestBody"]["content"]

    def test_confirm_documents_json_body(self, client):
        op = self._spec(client)["paths"]["/api/items/identify/{task_id}/confirm"]["post"]
        assert "items" in op["tags"]
        assert "200" in op["responses"]
        assert "application/json" in op["requestBody"]["content"]


# ---------------------------------------------------------------------------
# Behavior unchanged after decoration (Task 9)
# ---------------------------------------------------------------------------


class TestItemsBehaviorUnchanged:
    """Decoration changes only the contract for malformed input: strict-type /
    unknown-field errors now yield 422, while semantic rules and lenient numeric
    handling keep their previous 400 / warning behavior."""

    def setup_method(self):
        _reset_state()

    # --- single-item metadata ---

    def test_metadata_unknown_field_returns_422(self, client):
        resp = client.post("/api/items/1/metadata", json={"bogus": "x"})
        assert resp.status_code == 422

    def test_metadata_wrong_type_returns_422(self, client):
        resp = client.post("/api/items/1/metadata", json={"title": ["x"]})
        assert resp.status_code == 422

    def test_metadata_empty_body_reaches_handler(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        # Empty body → all fields default to "" → handler's own "at least one
        # field" 400 (NOT 422).
        resp = client.post("/api/items/1/metadata", json={})
        assert resp.status_code == 400

    # --- metadata-batch: semantic rules stay 400, numerics still warn ---

    def test_batch_empty_body_reaches_handler(self, client):
        # {} validates structurally (album={}, items=[]) → handler's "at least
        # one field" 400.
        resp = client.post("/api/items/metadata-batch", json={})
        assert resp.status_code == 400

    def test_batch_album_fields_without_items_reaches_handler(self, client):
        resp = client.post(
            "/api/items/metadata-batch",
            json={"album": {"albumartist": "Artist"}, "items": []},
        )
        assert resp.status_code == 400

    def test_batch_string_numeric_still_coerced(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.id = 3
        mock_item.album_id = 30
        mock_item.path = b"/music/t.mp3"
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)
        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 3, "track": "5", "disc": "2"}]},
        )
        assert resp.status_code == 200
        assert mock_item.track == 5
        assert mock_item.disc == 2

    def test_batch_bad_numeric_still_warns_not_422(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.id = 3
        mock_item.album_id = 30
        mock_item.path = b"/music/t.mp3"
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)
        resp = client.post(
            "/api/items/metadata-batch",
            json={"items": [{"id": 3, "track": "x"}]},
        )
        assert resp.status_code == 200
        assert any("invalid track" in w for w in resp.get_json()["warnings"])

    # --- items identify empty list stays 400 ---

    def test_identify_empty_list_reaches_handler(self, client):
        resp = client.post("/api/items/identify", json={"item_ids": []})
        assert resp.status_code == 400

    # --- apply / confirm: wrong-typed candidate_index now 422 ---

    def test_apply_wrong_type_candidate_returns_422(self, client):
        resp = client.post("/api/items/identify/t/apply", json={"candidate_index": "nope"})
        assert resp.status_code == 422

    def test_apply_unknown_field_returns_422(self, client):
        resp = client.post("/api/items/identify/t/apply", json={"bogus": 1})
        assert resp.status_code == 422

    def test_apply_empty_body_reaches_handler(self, client):
        # Empty body → candidate_index defaults to 0 → handler's own "no task" 400.
        resp = client.post("/api/items/identify/t/apply", json={})
        assert resp.status_code == 400

    def test_confirm_wrong_type_candidate_returns_422(self, client):
        resp = client.post("/api/items/identify/t/confirm", json={"candidate_index": 1.5})
        assert resp.status_code == 422

    def test_confirm_empty_body_reaches_handler(self, client):
        resp = client.post("/api/items/identify/t/confirm", json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PATCH /api/items/<item_id>/tags
# ---------------------------------------------------------------------------


class TestItemTagsPatch:
    def _make_mock_item(self, mocker, item_id, album_id=10):
        item = mocker.MagicMock()
        item.id = item_id
        item.album_id = album_id
        item.path = f"/music/track{item_id}.mp3".encode()
        return item

    def test_returns_200_and_writes_field(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 1)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        write = mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.patch("/api/items/1/tags", json={"fields": {"title": "New Title"}})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["warnings"] == []
        mock_item.__setitem__.assert_called_with("title", "New Title")
        mock_item.store.assert_called_once()
        write.assert_called_once_with(mock_item)

    def test_empty_value_clears_field_via_delitem(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 2)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.patch("/api/items/2/tags", json={"fields": {"comments": ""}})
        assert resp.status_code == 200
        mock_item.__delitem__.assert_called_once_with("comments")
        mock_item.__setitem__.assert_not_called()

    def test_existing_flexible_attr_set_via_setitem(self, client, mocker):
        # An existing flexible attr (present on the item) stays editable.
        mock_item = self._make_mock_item(mocker, 3)
        mock_item.__contains__.return_value = True
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.patch("/api/items/3/tags", json={"fields": {"my_flex_attr": "custom"}})
        assert resp.status_code == 200
        mock_item.__setitem__.assert_called_with("my_flex_attr", "custom")

    def test_existing_flexible_attr_cleared_via_empty_string(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 4)
        mock_item.__contains__.return_value = True
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.patch("/api/items/4/tags", json={"fields": {"my_flex_attr": ""}})
        assert resp.status_code == 200
        mock_item.__delitem__.assert_called_once_with("my_flex_attr")

    def test_unknown_field_rejected_with_400(self, client, mocker):
        # A field that is neither a known beets field nor an existing flexible
        # attr on the item must be rejected — direct API callers cannot create
        # arbitrary new flexible attrs.
        mock_item = self._make_mock_item(mocker, 9)
        mock_item.__contains__.return_value = False
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        write = mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.patch("/api/items/9/tags", json={"fields": {"totally_new_key": "x"}})
        assert resp.status_code == 400
        assert "not editable" in resp.get_json()["error"]
        assert "totally_new_key" in resp.get_json()["error"]
        mock_item.__setitem__.assert_not_called()
        mock_item.store.assert_not_called()
        write.assert_not_called()

    def test_genres_comma_string_becomes_list(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 5)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.patch("/api/items/5/tags", json={"fields": {"genres": "Rock, Electronic"}})
        assert resp.status_code == 200
        mock_item.__setitem__.assert_called_with("genres", ["Rock", "Electronic"])

    def test_readonly_length_returns_400(self, client):
        resp = client.patch("/api/items/1/tags", json={"fields": {"length": "200"}})
        assert resp.status_code == 400
        assert "not editable" in resp.get_json()["error"]
        assert "length" in resp.get_json()["error"]

    def test_readonly_mb_trackid_returns_400(self, client):
        resp = client.patch("/api/items/1/tags", json={"fields": {"mb_trackid": "x"}})
        assert resp.status_code == 400
        assert "not editable" in resp.get_json()["error"]

    def test_readonly_rg_track_gain_returns_400(self, client):
        resp = client.patch("/api/items/1/tags", json={"fields": {"rg_track_gain": "1.0"}})
        assert resp.status_code == 400
        assert "not editable" in resp.get_json()["error"]

    def test_bad_int_value_returns_400(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 6)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.patch("/api/items/6/tags", json={"fields": {"track": "notanumber"}})
        assert resp.status_code == 400
        assert "track" in resp.get_json()["error"]

    def test_bad_bool_value_returns_400(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 7)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.patch("/api/items/7/tags", json={"fields": {"comp": "maybe"}})
        assert resp.status_code == 400
        assert "comp" in resp.get_json()["error"]

    def test_unknown_item_returns_404(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.patch("/api/items/999/tags", json={"fields": {"title": "X"}})
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"].lower()

    def test_file_write_failure_returns_warning_and_200(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 8)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=False)

        resp = client.patch("/api/items/8/tags", json={"fields": {"title": "T"}})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert any("file write failed" in w for w in data["warnings"])

    def test_clearing_absent_field_is_noop_not_500(self, client, mocker):
        # del item[name] for a known field that was never present raises
        # KeyError in beets; the handler must treat that as a no-op (200), not
        # surface a 500. (comments is a known beets field, so it passes the
        # unknown-field guard even when absent on the item.)
        mock_item = self._make_mock_item(mocker, 11)
        mock_item.__delitem__.side_effect = KeyError("no such field 'comments'")
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.patch("/api/items/11/tags", json={"fields": {"comments": ""}})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        mock_item.store.assert_called_once()

    def test_multiple_fields_all_written_with_single_store(self, client, mocker):
        mock_item = self._make_mock_item(mocker, 12)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)

        resp = client.patch(
            "/api/items/12/tags",
            json={"fields": {"title": "T", "artist": "A"}},
        )
        assert resp.status_code == 200
        mock_item.__setitem__.assert_any_call("title", "T")
        mock_item.__setitem__.assert_any_call("artist", "A")
        mock_item.store.assert_called_once()

    def test_bad_value_in_later_field_aborts_before_store(self, client, mocker):
        # A bad value on any field must 400 without persisting the in-memory
        # mutations applied by the earlier valid fields.
        mock_item = self._make_mock_item(mocker, 13)
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=mock_lib)

        resp = client.patch(
            "/api/items/13/tags",
            json={"fields": {"title": "T", "track": "notanumber"}},
        )
        assert resp.status_code == 400
        assert "track" in resp.get_json()["error"]
        mock_item.store.assert_not_called()

    def test_unknown_toplevel_key_returns_422(self, client):
        resp = client.patch("/api/items/1/tags", json={"fields": {"title": "X"}, "bogus": 1})
        assert resp.status_code == 422

    def test_patch_tags_in_openapi_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/items/{item_id}/tags"]["patch"]
        assert "items" in op["tags"]
        assert "200" in op["responses"]
        assert "400" in op["responses"]
        assert "404" in op["responses"]
        assert "application/json" in op["requestBody"]["content"]
