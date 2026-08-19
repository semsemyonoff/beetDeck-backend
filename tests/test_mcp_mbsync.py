"""MCP `preview_mbsync` and its `apply_change` handler, driven against the real
Flask app.

Only the MusicBrainz plugin lookup is faked — the route, the stash in
`state.identify_tasks` and the confirm's writes are real, the same shape
`test_mcp_media.py` uses for cover/lyrics. `Album`/`Item` are built directly
(not through the SQLite fixture) for the same reason `tests/test_mbsync.py`
does: `mb_releasetrackid` and item-level `label` are real beets `Item` fields
the test schema's minimal DDL carves no column for, so a DB-backed item can
never round-trip them. `_init_beets` is monkeypatched per test to hand back the
prepared album, the seam `tests/test_mbsync.py` uses.
"""

import httpx
import pytest
from beets.autotag.hooks import AlbumInfo, TrackInfo

from beetdeck import state
from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.preview import PreviewStore
from beetdeck_mcp.tools.apply import TOOL_NAMES as APPLY_TOOL_NAMES
from beetdeck_mcp.tools.apply import build_tools as build_apply_tools
from beetdeck_mcp.tools.mbsync import TOOL_NAMES, build_tools
from tests.conftest import insert_album, insert_item
from tests.test_mbsync import install_lib, make_album, make_item

TOOL_NAME = "preview_mbsync"


@pytest.fixture()
def store():
    """A preview store per test — tokens must not leak between them."""
    return PreviewStore()


@pytest.fixture()
def make_tools(beetdeck_client, store):
    """The mbsync + apply tools, sharing one store and one REST client."""

    def _make(client=None, *, readonly=False):
        rest = beetdeck_client if client is None else client
        tools = {tool.__name__: tool for tool in build_tools(rest, store)}
        tools.update(
            {tool.__name__: tool for tool in build_apply_tools(rest, store, readonly=readonly)}
        )
        return tools

    return _make


@pytest.fixture()
def tools(make_tools):
    return make_tools()


def make_track_info(**fields):
    return TrackInfo(**fields)


def make_album_info(tracks, **fields):
    return AlbumInfo(tracks=tracks, **fields)


def patch_mb(mocker, *, album_info=None, exc=None):
    """A stand-in for `metadata_plugins.get_metadata_source("MusicBrainz")`."""
    stub = mocker.MagicMock()
    if exc is not None:
        stub.album_for_id.side_effect = exc
    else:
        stub.album_for_id.return_value = album_info
    return mocker.patch(
        "beetdeck.routes.mbsync.metadata_plugins.get_metadata_source", return_value=stub
    )


class TestBuildTools:
    def test_exposes_exactly_the_declared_tool_names(self, tools):
        assert set(tools) == set(TOOL_NAMES) | set(APPLY_TOOL_NAMES)


class TestPreviewMbsync:
    def test_reports_the_diff_and_mints_a_token(self, tools, mocker, store):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        install_lib(mocker, album, [item])
        info = make_album_info(
            [make_track_info(title="New Title", track_id="rec-1", medium=1, medium_index=1)],
            album_id="mb-release-1",
            label="4AD",
        )
        patch_mb(mocker, album_info=info)

        result = tools[TOOL_NAME](1)

        assert result["preview_token"].startswith("mbsync-")
        assert result["kind"] == "mbsync"
        assert result["target"] == "album 1"
        assert result["changes"]["mb_albumid"] == "mb-release-1"
        assert result["changes"]["data_source"] == "MusicBrainz"
        assert "label" in [f["field"] for f in result["changes"]["album_fields"]]
        assert "title" in [f["field"] for f in result["changes"]["track_fields"]]
        assert "unmapped" not in result["changes"]
        assert any("cannot be excluded" in note for note in result["notes"])
        assert len(store) == 1

    def test_unmapped_tracks_are_named_in_notes(self, tools, mocker, store):
        matched = make_item(id=101, title="Track One", mb_trackid="rec-1", track=1, disc=1)
        extra = make_item(
            id=102, title="Bonus Track", mb_trackid="not-on-release", track=13, disc=1
        )
        album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, album, [matched, extra])
        info = make_album_info(
            [make_track_info(title="Track One", track_id="rec-1", medium=1, medium_index=1)],
            album_id="mb-release-1",
        )
        patch_mb(mocker, album_info=info)

        result = tools[TOOL_NAME](1)
        assert result["changes"]["unmapped"] == [
            {"item_id": 102, "track": 13, "title": "Bonus Track"}
        ]
        assert any(
            "1 local track(s)" in note and "Bonus Track (item 102)" in note
            for note in result["notes"]
        )

    def test_no_change_reports_nothing_to_sync_and_mints_no_token(self, tools, mocker, store):
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
        info = make_album_info(
            [
                make_track_info(
                    title="Same Title",
                    track_id="rec-1",
                    release_track_id="rt-1",
                    medium=1,
                    medium_index=1,
                )
            ],
            album_id="mb-release-1",
            label="4AD",
        )
        patch_mb(mocker, album_info=info)

        result = tools[TOOL_NAME](1)
        assert "no change" in result["error"]
        assert len(store) == 0

    def test_a_release_not_found_is_reported(self, tools, mocker, store):
        album = make_album(id=1, mb_albumid="mb-release-1", data_source="MusicBrainz")
        install_lib(mocker, album, [])
        patch_mb(mocker, album_info=None)

        result = tools[TOOL_NAME](1)
        assert "not found" in result["error"]
        assert len(store) == 0

    def test_an_unreachable_musicbrainz_is_reported(self, tools, mocker, store):
        album = make_album(id=1, mb_albumid="mb-release-1", data_source="MusicBrainz")
        install_lib(mocker, album, [])
        patch_mb(mocker, exc=ConnectionError("timed out"))

        result = tools[TOOL_NAME](1)
        assert "could not be reached" in result["error"]
        assert len(store) == 0

    def test_an_unknown_album_is_reported(self, tools, mocker):
        lib = mocker.MagicMock()
        lib.get_album.return_value = None
        mocker.patch("beetdeck.routes.mbsync._init_beets", return_value=lib)

        result = tools[TOOL_NAME](9999)
        assert "Album not found" in result["error"]

    def test_the_token_commits_the_confirm_with_its_precondition(self, tools, mocker, store):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, album, [item])
        info = make_album_info(
            [make_track_info(title="New Title", track_id="rec-1", medium=1, medium_index=1)],
            album_id="mb-release-1",
        )
        patch_mb(mocker, album_info=info)

        token = tools[TOOL_NAME](1)["preview_token"]

        preview = store.peek(token)
        assert preview.method == "POST"
        assert preview.endpoint == "/api/album/1/mbsync/confirm"
        assert preview.request_json() == {
            "expected_generation": state.identify_tasks["mbsync_1"]["stash_generation"]
        }
        assert "Enforced by the backend" in store.describe(preview)["guarantee"]

    def test_an_unreachable_backend_is_reported(self, make_tools, mcp_config, store):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        dead = BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))
        result = make_tools(dead)[TOOL_NAME](1)
        assert "could not reach" in result["error"]
        assert len(store) == 0

    @pytest.mark.parametrize("generation", [None, True])
    def test_a_preview_without_a_usable_stash_generation_mints_nothing(
        self, make_tools, mcp_config, store, generation
    ):
        """`True` is not a generation, and `isinstance(True, int)` is `True`.

        A token described as backend-enforced that nothing enforces is worse
        than no token, so the preview refuses rather than weakening itself.
        """
        body = {
            "album_id": 1,
            "mb_albumid": "mb-release-1",
            "data_source": "MusicBrainz",
            "album_fields": [],
            "track_fields": [
                {
                    "field": "title",
                    "changes": [{"item_id": 101, "track": 1, "old": "Old", "new": "New"}],
                }
            ],
            "unmapped": [],
            "changed": True,
        }
        if generation is not None:
            body["stash_generation"] = generation

        def handler(request):
            return httpx.Response(200, json=body)

        odd = BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))
        result = make_tools(odd)[TOOL_NAME](1)
        assert "no stash generation" in result["error"]
        assert len(store) == 0


class TestApplyMbsync:
    def _preview(self, tools, mocker, item, album, info):
        install_lib(mocker, album, [item])
        patch_mb(mocker, album_info=info)
        return tools[TOOL_NAME](album.id)["preview_token"]

    def test_writes_the_previewed_diff(self, tools, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        info = make_album_info(
            [make_track_info(title="New Title", track_id="rec-1", medium=1, medium_index=1)],
            album_id="mb-release-1",
        )
        token = self._preview(tools, mocker, item, album, info)

        fresh_item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        fresh_album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, fresh_album, [fresh_item])
        mocker.patch("beetdeck.routes.mbsync._try_write_item", return_value=True)
        mocker.patch("beets.library.Item.store", lambda self, **kw: None)
        mocker.patch("beets.library.Album.store", lambda self, **kw: None)

        result = tools["apply_change"](token)

        assert result["status"] == "ok"
        assert result["kind"] == "mbsync"
        assert result["target"] == "album 1"
        assert fresh_item.title == "New Title"
        assert "mbsync_1" not in state.identify_tasks

    def test_a_write_failure_reports_partial(self, tools, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        info = make_album_info(
            [make_track_info(title="New Title", track_id="rec-1", medium=1, medium_index=1)],
            album_id="mb-release-1",
        )
        token = self._preview(tools, mocker, item, album, info)

        fresh_item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        fresh_album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, fresh_album, [fresh_item])
        mocker.patch("beetdeck.routes.mbsync._try_write_item", return_value=False)
        mocker.patch("beets.library.Item.store", lambda self, **kw: None)
        mocker.patch("beets.library.Album.store", lambda self, **kw: None)

        result = tools["apply_change"](token)
        assert result["status"] == "partial"
        assert "file write failed for item(s) 101" in result["detail"][0]

    def test_a_stash_consumed_elsewhere_fails_loudly(self, tools, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        info = make_album_info(
            [make_track_info(title="New Title", track_id="rec-1", medium=1, medium_index=1)],
            album_id="mb-release-1",
        )
        token = self._preview(tools, mocker, item, album, info)
        state.identify_tasks.pop("mbsync_1")

        result = tools["apply_change"](token)
        assert result["status"] == "failed"
        assert "the MusicBrainz sync diff" in result["detail"][0]
        assert "re-run the preview" in result["detail"][0]

    def test_a_competing_tag_write_between_preview_and_apply_is_refused(
        self, tools, make_tools, store, mocker, db_path, beetdeck_client
    ):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        info = make_album_info(
            [make_track_info(title="New Title", track_id="rec-1", medium=1, medium_index=1)],
            album_id="mb-release-1",
        )
        token = self._preview(tools, mocker, item, album, info)

        # A real, DB-backed item sharing the same album id — the tag write's
        # own `_init_beets` is untouched by the mbsync mock above, so it goes
        # through the real app exactly as a competing agent or the web UI
        # would, and drops the mbsync stash purely by album id.
        other_album = insert_album(db_path, id=1)
        other_item = insert_item(db_path, other_album, id=555, title="Other")
        beetdeck_client.write(
            "tags", "PATCH", f"/api/items/{other_item}/tags", json={"fields": {"comments": "x"}}
        )
        assert "mbsync_1" not in state.identify_tasks

        result = tools["apply_change"](token)
        assert result["status"] == "failed"
        assert "the MusicBrainz sync diff" in result["detail"][0]

    def test_a_readonly_server_writes_nothing_and_keeps_the_token(self, make_tools, mocker, store):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, album, [item])
        info = make_album_info(
            [make_track_info(title="New Title", track_id="rec-1", medium=1, medium_index=1)],
            album_id="mb-release-1",
        )
        patch_mb(mocker, album_info=info)
        readonly = make_tools(readonly=True)

        token = readonly[TOOL_NAME](1)["preview_token"]
        refused = readonly["apply_change"](token)

        assert refused["status"] == "failed"
        assert "mbsync_1" in state.identify_tasks
        assert len(store) == 1

    def test_the_preview_alone_wrote_nothing(self, tools, mocker):
        """`apply_metadata` mutates the `Item`/`Album` objects in memory — that
        is how the diff is computed — but `store()` must never be called on
        the preview path; nothing reaches disk until `apply_change`."""
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1", label="Old Label")
        store_calls = []
        install_lib(mocker, album, [item])
        mocker.patch("beets.library.Item.store", lambda self, **kw: store_calls.append(self.id))
        mocker.patch("beets.library.Album.store", lambda self, **kw: store_calls.append("album"))
        info = make_album_info(
            [make_track_info(title="New Title", track_id="rec-1", medium=1, medium_index=1)],
            album_id="mb-release-1",
            label="4AD",
        )
        patch_mb(mocker, album_info=info)

        tools[TOOL_NAME](1)
        assert store_calls == []

    def test_the_token_commits_exactly_once(self, tools, mocker):
        item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        album = make_album(id=1, mb_albumid="mb-release-1")
        info = make_album_info(
            [make_track_info(title="New Title", track_id="rec-1", medium=1, medium_index=1)],
            album_id="mb-release-1",
        )
        token = self._preview(tools, mocker, item, album, info)

        fresh_item = make_item(id=101, title="Old Title", mb_trackid="rec-1", track=1, disc=1)
        fresh_album = make_album(id=1, mb_albumid="mb-release-1")
        install_lib(mocker, fresh_album, [fresh_item])
        mocker.patch("beetdeck.routes.mbsync._try_write_item", return_value=True)
        mocker.patch("beets.library.Item.store", lambda self, **kw: None)
        mocker.patch("beets.library.Album.store", lambda self, **kw: None)

        assert tools["apply_change"](token)["status"] == "ok"
        again = tools["apply_change"](token)
        assert again["status"] == "failed"
        assert "already been applied" in again["detail"][0]
