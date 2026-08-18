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
