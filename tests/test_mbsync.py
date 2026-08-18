"""Tests for the mbsync blueprint's preview route (`POST /api/album/<id>/mbsync`).

Albums and items are built directly (`library.Album(...)`/`library.Item(...)`),
not through the SQLite fixture: several fields this route reads and writes
(`mb_releasetrackid`, item-level `label`) are real beets `Item` fields the
test schema's minimal DDL does not carve a column for, so a DB-backed item
can never round-trip them. `Album.items` is monkeypatched per test, the same
seam `tests/test_mbsync_core.py` uses. `_init_beets` is patched to hand back
a lib whose `get_album` returns the prepared album, so the route's own DB
lookup is the only thing stubbed.
"""

from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.library import Album, Item

from beetdeck import state
from beetdeck.routes import mbsync as mbsync_routes
from beetdeck.utils import _drop_mbsync_stash


def make_item(**fields):
    return Item(**fields)


def make_album(**fields):
    return Album(**fields)


def make_track_info(**fields):
    return TrackInfo(**fields)


def make_album_info(tracks, **fields):
    return AlbumInfo(tracks=tracks, **fields)


def install_lib(mocker, album, items):
    """Patch `_init_beets` so the route sees `album` (with `items`) and nothing else."""
    Album_items = type(album)
    mocker.patch.object(Album_items, "items", lambda self: list(items))
    lib = mocker.MagicMock()
    lib.get_album.return_value = album
    mocker.patch("beetdeck.routes.mbsync._init_beets", return_value=lib)
    return lib


def make_mb_stub(mocker, album_info=None, exc=None):
    """A stand-in for `metadata_plugins.get_metadata_source("MusicBrainz")`.

    Exactly one of `album_info`/`exc` should be given: `album_info=None` with no
    `exc` reproduces the plugin's own not-found path (`album_for_id` -> `None`);
    `exc` reproduces an upstream failure the plugin does not catch.
    """
    stub = mocker.MagicMock()
    if exc is not None:
        stub.album_for_id.side_effect = exc
    else:
        stub.album_for_id.return_value = album_info
    return stub


def patch_mb(mocker, **kwargs):
    return mocker.patch(
        "beetdeck.routes.mbsync.metadata_plugins.get_metadata_source",
        return_value=make_mb_stub(mocker, **kwargs),
    )


class TestPreviewMbsync:
    def test_returns_404_when_album_not_found(self, client, mocker):
        lib = mocker.MagicMock()
        lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.mbsync._init_beets", return_value=lib)
        patch_mb(mocker)

        resp = client.post("/api/album/9999/mbsync")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_returns_400_without_mb_albumid(self, client, mocker):
        album = make_album(id=1, mb_albumid="")
        install_lib(mocker, album, [])

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 400
        assert "MusicBrainz release id" in resp.get_json()["error"]

    def test_returns_400_for_foreign_data_source(self, client, mocker):
        album = make_album(id=1, mb_albumid="mb-release-1", data_source="Deezer")
        install_lib(mocker, album, [])

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 400
        assert "Deezer" in resp.get_json()["error"]

    def test_returns_500_when_musicbrainz_plugin_not_loaded(self, client, mocker):
        album = make_album(id=1, mb_albumid="mb-release-1", data_source="MusicBrainz")
        install_lib(mocker, album, [])
        mocker.patch(
            "beetdeck.routes.mbsync.metadata_plugins.get_metadata_source", return_value=None
        )

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 500
        assert resp.get_json()["error"] == "musicbrainz plugin not loaded"

    def test_returns_400_when_release_not_found(self, client, mocker):
        album = make_album(id=1, mb_albumid="mb-release-1", data_source="MusicBrainz")
        install_lib(mocker, album, [])
        patch_mb(mocker, album_info=None)

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 400
        assert "not found" in resp.get_json()["error"]

    def test_returns_502_when_musicbrainz_unreachable(self, client, mocker):
        album = make_album(id=1, mb_albumid="mb-release-1", data_source="MusicBrainz")
        install_lib(mocker, album, [])
        # The real production path: album_for_id only catches HTTPNotFoundError,
        # so any other exception propagates out of the plugin call.
        patch_mb(mocker, exc=ConnectionError("timed out"))

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 502
        assert "timed out" in resp.get_json()["reasons"]

    def test_returns_409_during_an_importing_rescan(self, client, mocker):
        album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, album, [])
        patch_mb(mocker)
        state.rescan_phase = "importing"

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 409

    def test_returns_409_during_an_updating_rescan(self, client, mocker):
        album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, album, [])
        state.rescan_phase = "updating"

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 409

    def test_happy_path_reports_diff_and_stash_generation(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        install_lib(mocker, album, [item])

        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info(
            [track], album="New Album", artist="New Artist", album_id="mb-release-1", label="4AD"
        )
        patch_mb(mocker, album_info=info)

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 200
        body = resp.get_json()

        assert body["album_id"] == 1
        assert body["mb_albumid"] == "mb-release-1"
        assert body["data_source"] == "MusicBrainz"
        assert isinstance(body["stash_generation"], int)
        assert body["changed"] is True
        assert body["unmapped"] == []

        title_fields = [f for f in body["track_fields"] if f["field"] == "title"]
        assert len(title_fields) == 1
        change = title_fields[0]["changes"][0]
        assert change["item_id"] == 101
        assert change["old"] == "Old Title"
        assert change["new"] == "New Title"

        label_fields = [f for f in body["album_fields"] if f["field"] == "label"]
        assert len(label_fields) == 1
        assert label_fields[0]["old"] == "Old Label"
        assert label_fields[0]["new"] == "4AD"

    def test_changed_is_false_when_release_matches(self, client, mocker):
        item = make_item(
            id=101,
            title="Same Title",
            mb_trackid="rec-1",
            mb_releasetrackid="rt-1",
            mb_albumid="mb-release-1",
            track=1,
            disc=1,
            tracktotal=1,
            label="4AD",
        )
        album = make_album(id=1, mb_albumid="mb-release-1", label="4AD")
        install_lib(mocker, album, [item])

        track = make_track_info(
            title="Same Title",
            track_id="rec-1",
            release_track_id="rt-1",
            medium=1,
            medium_index=1,
        )
        info = make_album_info([track], album_id="mb-release-1", label="4AD")
        patch_mb(mocker, album_info=info)

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["changed"] is False
        assert body["album_fields"] == []
        assert body["track_fields"] == []

    def test_reports_unmapped_tracks_with_track_and_title(self, client, mocker):
        matched = make_item(id=101, title="Track One", mb_trackid="rec-1", track=1, disc=1)
        extra = make_item(
            id=102, title="Bonus Track", mb_trackid="not-on-release", track=13, disc=1
        )
        album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, album, [matched, extra])

        track = make_track_info(
            title="Track One", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        patch_mb(mocker, album_info=info)

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["unmapped"] == [{"item_id": 102, "track": 13, "title": "Bonus Track"}]

    def test_preview_writes_nothing(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        install_lib(mocker, album, [item])

        store_calls = []
        mocker.patch.object(Item, "store", lambda self, **kw: store_calls.append(("item", self.id)))
        mocker.patch.object(
            Album, "store", lambda self, **kw: store_calls.append(("album", self.id))
        )

        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1", label="4AD")
        patch_mb(mocker, album_info=info)

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 200
        assert resp.get_json()["changed"] is True
        assert store_calls == []

    def test_mints_a_stash_entry_with_a_generation(self, client, mocker):
        item = make_item(id=101, title="X", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, album, [item])

        track = make_track_info(track_id="rec-1", release_track_id="rt-1")
        info = make_album_info([track], album_id="mb-release-1")
        patch_mb(mocker, album_info=info)

        resp = client.post("/api/album/1/mbsync")
        body = resp.get_json()

        task = state.identify_tasks["mbsync_1"]
        assert task["stash_generation"] == body["stash_generation"]
        assert task["album_id"] == 1
        assert task["mb_albumid"] == "mb-release-1"


class TestPreviewMbsyncInSpec:
    def test_preview_in_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        assert "/api/album/{album_id}/mbsync" in spec["paths"]

    def test_preview_without_body_reaches_handler(self, client, mocker):
        # No JSON body is required by this route; a bare POST must reach the
        # handler rather than 422 on request validation.
        lib = mocker.MagicMock()
        lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.mbsync._init_beets", return_value=lib)
        patch_mb(mocker)

        resp = client.post("/api/album/9999/mbsync")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/album/<id>/mbsync/confirm
# ---------------------------------------------------------------------------
#
# Every test below runs a real preview first to mint a genuine stash entry,
# then re-points `_init_beets`/`Album.items` at *fresh* item/album instances
# before confirming — mirroring a real confirm's own DB re-read. Reusing the
# exact objects the preview mutated in place (it never reverts them; only
# `store()` is skipped) would make every field look drifted, since the
# "current" value would already equal the "new" one.


def _write_ok(mocker):
    mocker.patch("beetdeck.routes.mbsync._try_write_item", return_value=True)


def _no_op_store(mocker):
    mocker.patch.object(Item, "store", lambda self, **kw: None)
    mocker.patch.object(Album, "store", lambda self, **kw: None)


class TestConfirmMbsync:
    def _run_preview(self, client, mocker, item, album, info):
        install_lib(mocker, album, [item])
        patch_mb(mocker, album_info=info)
        resp = client.post(f"/api/album/{album.id}/mbsync")
        assert resp.status_code == 200
        return resp.get_json()

    def test_returns_400_with_no_stash_and_no_generation(self, client, mocker):
        album = make_album(id=1)
        install_lib(mocker, album, [])

        resp = client.post("/api/album/1/mbsync/confirm")
        assert resp.status_code == 400

    def test_returns_409_with_no_stash_when_generation_given(self, client, mocker):
        album = make_album(id=1)
        install_lib(mocker, album, [])

        resp = client.post("/api/album/1/mbsync/confirm", json={"expected_generation": 1})
        assert resp.status_code == 409

    def test_returns_409_with_stale_generation(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        self._run_preview(client, mocker, item, album, info)

        resp = client.post("/api/album/1/mbsync/confirm", json={"expected_generation": 999999})
        assert resp.status_code == 409

    def test_returns_409_during_an_importing_rescan(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        body = self._run_preview(client, mocker, item, album, info)

        state.rescan_phase = "importing"
        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 409

    def test_writes_included_fields_and_updates_album_row(self, client, mocker):
        item = make_item(
            id=101, title="Old Title", label="Old Label", mb_trackid="rec-1", track=1, disc=1
        )
        album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1", label="4AD", artist="New Artist")
        body = self._run_preview(client, mocker, item, album, info)

        fresh_item = make_item(
            id=101, title="Old Title", label="Old Label", mb_trackid="rec-1", track=1, disc=1
        )
        fresh_album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        install_lib(mocker, fresh_album, [fresh_item])
        _write_ok(mocker)
        stored_item_ids = []
        stored_album_ids = []
        mocker.patch.object(Item, "store", lambda self, **kw: stored_item_ids.append(self.id))
        mocker.patch.object(Album, "store", lambda self, **kw: stored_album_ids.append(self.id))

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert fresh_item.title == "New Title"
        assert fresh_album.label == "4AD"
        assert stored_item_ids == [101]
        assert stored_album_ids == [1]
        # The stash is gone once confirmed.
        assert "mbsync_1" not in state.identify_tasks

    def test_excluded_field_stays_unchanged_on_item_and_album(self, client, mocker):
        item = make_item(
            id=101, title="Old Title", label="Old Label", mb_trackid="rec-1", track=1, disc=1
        )
        album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1", label="4AD")
        body = self._run_preview(client, mocker, item, album, info)

        fresh_item = make_item(
            id=101, title="Old Title", label="Old Label", mb_trackid="rec-1", track=1, disc=1
        )
        fresh_album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        install_lib(mocker, fresh_album, [fresh_item])
        _write_ok(mocker)
        _no_op_store(mocker)

        resp = client.post(
            "/api/album/1/mbsync/confirm",
            json={"expected_generation": body["stash_generation"], "excluded_fields": ["label"]},
        )
        assert resp.status_code == 200
        assert fresh_item.title == "New Title"
        assert fresh_item.label == "Old Label"
        assert fresh_album.label == "Old Label"

    def test_unrelated_album_row_disagreement_survives_untouched(self, client, mocker):
        # The release never proposes a "genre" value (album-level, not stripped
        # like the dedicated genre fields), so a pre-existing mismatch between
        # the album row and item[0] on that field is not this sync's business.
        item = make_item(
            id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1, catalognum="ITEM-CAT"
        )
        album = make_album(id=1, mb_albumid="mb-release-1", catalognum="ALBUM-CAT")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        body = self._run_preview(client, mocker, item, album, info)

        fresh_item = make_item(
            id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1, catalognum="ITEM-CAT"
        )
        fresh_album = make_album(id=1, mb_albumid="mb-release-1", catalognum="ALBUM-CAT")
        install_lib(mocker, fresh_album, [fresh_item])
        _write_ok(mocker)
        _no_op_store(mocker)

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 200
        assert fresh_album.catalognum == "ALBUM-CAT"

    def test_unmapped_items_are_untouched(self, client, mocker):
        matched = make_item(id=101, title="Track One", mb_trackid="rec-1", track=1, disc=1)
        extra = make_item(
            id=102, title="Bonus Track", mb_trackid="not-on-release", track=13, disc=1
        )
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="Track One Fixed",
            track_id="rec-1",
            release_track_id="rt-1",
            medium=1,
            medium_index=1,
        )
        info = make_album_info([track], album_id="mb-release-1")
        install_lib(mocker, album, [matched, extra])
        patch_mb(mocker, album_info=info)
        preview_resp = client.post("/api/album/1/mbsync")
        assert preview_resp.status_code == 200
        body = preview_resp.get_json()

        fresh_matched = make_item(id=101, title="Track One", mb_trackid="rec-1", track=1, disc=1)
        fresh_extra = make_item(
            id=102, title="Bonus Track", mb_trackid="not-on-release", track=13, disc=1
        )
        fresh_album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, fresh_album, [fresh_matched, fresh_extra])
        _write_ok(mocker)
        _no_op_store(mocker)

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 200
        assert fresh_matched.title == "Track One Fixed"
        assert fresh_extra.title == "Bonus Track"

    # -- drift ---------------------------------------------------------

    def test_returns_409_when_an_item_field_drifted(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        body = self._run_preview(client, mocker, item, album, info)

        # Something else wrote a different title between preview and confirm.
        fresh_item = make_item(
            id=101, title="Someone Else's Title", mb_trackid="rec-1", track=1, disc=1
        )
        fresh_album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, fresh_album, [fresh_item])

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 409

    def test_returns_409_when_the_album_row_drifted(self, client, mocker):
        item = make_item(
            id=101, title="Old Title", label="Old Label", mb_trackid="rec-1", track=1, disc=1
        )
        album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1", label="4AD")
        body = self._run_preview(client, mocker, item, album, info)

        fresh_item = make_item(
            id=101, title="Old Title", label="Old Label", mb_trackid="rec-1", track=1, disc=1
        )
        # Someone else edited the album's label directly since the preview.
        fresh_album = make_album(id=1, mb_albumid="mb-release-1", label="Someone Else's Label")
        install_lib(mocker, fresh_album, [fresh_item])

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 409

    def test_returns_409_when_a_stashed_item_left_the_album(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        body = self._run_preview(client, mocker, item, album, info)

        # Item 101 has since been re-parented away from album 1.
        fresh_album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, fresh_album, [])

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 409

    def test_returns_409_when_the_album_was_removed(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        body = self._run_preview(client, mocker, item, album, info)

        lib = mocker.MagicMock()
        lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.mbsync._init_beets", return_value=lib)

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 409

    def test_reports_partial_status_on_a_write_failure(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        body = self._run_preview(client, mocker, item, album, info)

        fresh_item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        fresh_album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, fresh_album, [fresh_item])
        mocker.patch("beetdeck.routes.mbsync._try_write_item", return_value=False)
        _no_op_store(mocker)

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "partial"
        assert body["write_failures"] == [101]

    def test_returns_409_when_an_item_joined_the_album(self, client, mocker):
        # The mirror of "an item left": a newcomer has no snapshot row, so
        # `restore_fields` would delete an excluded field off it and
        # `diff_against` would read all of its fields as changed.
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        body = self._run_preview(client, mocker, item, album, info)

        fresh_item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        newcomer = make_item(id=102, title="Just Imported", mb_trackid="rec-9", track=2, disc=1)
        fresh_album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, fresh_album, [fresh_item, newcomer])
        _write_ok(mocker)
        _no_op_store(mocker)

        resp = client.post(
            "/api/album/1/mbsync/confirm",
            json={"expected_generation": body["stash_generation"], "excluded_fields": ["title"]},
        )
        assert resp.status_code == 409
        assert newcomer.title == "Just Imported"

    def test_album_row_write_does_not_inherit_onto_unmapped_items(self, client, mocker):
        # `album.store()` defaults to `inherit=True`, which pushes every dirty
        # `Album.item_keys` field onto *every* item's DB row — including the
        # unmapped ones, whose files this route never rewrites.
        matched = make_item(
            id=101, title="Track One", label="Old Label", mb_trackid="rec-1", track=1, disc=1
        )
        extra = make_item(
            id=102, title="Bonus", label="Old Label", mb_trackid="not-on-release", track=13, disc=1
        )
        album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        track = make_track_info(
            title="Track One", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1", label="4AD")
        install_lib(mocker, album, [matched, extra])
        patch_mb(mocker, album_info=info)
        body = client.post("/api/album/1/mbsync").get_json()

        fresh_matched = make_item(
            id=101, title="Track One", label="Old Label", mb_trackid="rec-1", track=1, disc=1
        )
        fresh_extra = make_item(
            id=102, title="Bonus", label="Old Label", mb_trackid="not-on-release", track=13, disc=1
        )
        fresh_album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        install_lib(mocker, fresh_album, [fresh_matched, fresh_extra])
        _write_ok(mocker)
        inherit_flags = []
        mocker.patch.object(Item, "store", lambda self, **kw: None)
        mocker.patch.object(
            Album, "store", lambda self, **kw: inherit_flags.append(kw.get("inherit", True))
        )

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 200
        assert fresh_album.label == "4AD"
        assert inherit_flags == [False]
        assert fresh_extra.label == "Old Label"

    def test_excluding_every_changed_field_writes_nothing_and_still_succeeds(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        body = self._run_preview(client, mocker, item, album, info)

        fresh_item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        fresh_album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, fresh_album, [fresh_item])
        writes = []
        mocker.patch("beetdeck.routes.mbsync._try_write_item", lambda item: writes.append(item.id))
        stored = []
        mocker.patch.object(Item, "store", lambda self, **kw: stored.append(self.id))
        mocker.patch.object(Album, "store", lambda self, **kw: stored.append("album"))

        resp = client.post(
            "/api/album/1/mbsync/confirm",
            json={
                "expected_generation": body["stash_generation"],
                "excluded_fields": [f["field"] for f in body["track_fields"]],
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert fresh_item.title == "Old Title"
        assert writes == []
        assert stored == []
        # The stash is consumed either way — a no-op confirm is still a confirm.
        assert "mbsync_1" not in state.identify_tasks

    def test_a_second_preview_invalidates_the_first_token(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        first = self._run_preview(client, mocker, item, album, info)
        second = self._run_preview(client, mocker, item, album, info)
        assert first["stash_generation"] != second["stash_generation"]

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": first["stash_generation"]}
        )
        assert resp.status_code == 409


class TestPreviewStashSlot:
    def test_a_write_during_the_snapshot_is_not_resurrected(self, client, mocker):
        """A `_drop_mbsync_stash` landing between the mint and the fill wins.

        Re-inserting the slot unconditionally would hand the confirm back the
        same generation it was minted with, so the competing write's
        invalidation would be silently undone.
        """
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        install_lib(mocker, album, [item])
        patch_mb(mocker, album_info=info)

        real_snapshot = mbsync_routes.snapshot_album

        def drop_then_snapshot(album_arg):
            _drop_mbsync_stash(1)
            return real_snapshot(album_arg)

        mocker.patch.object(mbsync_routes, "snapshot_album", drop_then_snapshot)

        resp = client.post("/api/album/1/mbsync")
        assert resp.status_code == 409
        assert "mbsync_1" not in state.identify_tasks

    def test_a_preview_that_fails_after_minting_leaves_no_stash(self, client, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        track = make_track_info(
            title="New Title", track_id="rec-1", release_track_id="rt-1", medium=1, medium_index=1
        )
        info = make_album_info([track], album_id="mb-release-1")
        install_lib(mocker, album, [item])
        patch_mb(mocker, album_info=info)
        mocker.patch.object(
            mbsync_routes, "snapshot_album", mocker.Mock(side_effect=RuntimeError("boom"))
        )

        assert client.post("/api/album/1/mbsync").status_code == 500
        # A slot carrying only a generation would make a bodyless confirm raise
        # KeyError on the missing snapshot and answer 500 instead of 400.
        assert "mbsync_1" not in state.identify_tasks
        assert client.post("/api/album/1/mbsync/confirm").status_code == 400


class TestPreviewStripsGenres:
    def test_release_genres_never_reach_the_diff_or_the_write(self, client, mocker):
        """`strip_genre_fields` is wired in, not merely unit-tested.

        With `musicbrainz.genres: yes` in a deploy's beets config the release
        carries genres, and `genres` is in `Album.item_keys` — without the strip
        they would ride into this diff, which is exactly what the dedicated
        genre flow exists to prevent.
        """
        item = make_item(
            id=101, title="Old Title", genres=["Shoegaze"], mb_trackid="rec-1", track=1, disc=1
        )
        album = make_album(id=1, mb_albumid="mb-release-1", genres=["Shoegaze"])
        track = make_track_info(
            title="New Title",
            track_id="rec-1",
            release_track_id="rt-1",
            medium=1,
            medium_index=1,
            genres=["Dream Pop"],
        )
        info = make_album_info([track], album_id="mb-release-1", genres=["Dream Pop"])
        install_lib(mocker, album, [item])
        patch_mb(mocker, album_info=info)

        body = client.post("/api/album/1/mbsync").get_json()
        assert [f["field"] for f in body["album_fields"]] == []
        assert "genres" not in {f["field"] for f in body["track_fields"]}
        assert "genre" not in {f["field"] for f in body["track_fields"]}

        fresh_item = make_item(
            id=101, title="Old Title", genres=["Shoegaze"], mb_trackid="rec-1", track=1, disc=1
        )
        fresh_album = make_album(id=1, mb_albumid="mb-release-1", genres=["Shoegaze"])
        install_lib(mocker, fresh_album, [fresh_item])
        _write_ok(mocker)
        _no_op_store(mocker)

        resp = client.post(
            "/api/album/1/mbsync/confirm", json={"expected_generation": body["stash_generation"]}
        )
        assert resp.status_code == 200
        assert fresh_item.title == "New Title"
        assert fresh_item.genres == ["Shoegaze"]
        assert fresh_album.genres == ["Shoegaze"]


class TestConfirmMbsyncInSpec:
    def test_confirm_in_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        assert "/api/album/{album_id}/mbsync/confirm" in spec["paths"]

    def test_confirm_without_body_reaches_handler(self, client, mocker):
        album = make_album(id=1)
        install_lib(mocker, album, [])

        resp = client.post("/api/album/1/mbsync/confirm")
        assert resp.status_code == 400
