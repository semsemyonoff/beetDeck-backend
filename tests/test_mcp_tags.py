"""MCP tag editing, driven against the real Flask app in-process.

Nothing is mocked here except the file write (`_try_write_item` — the test
library has no audio files): the field catalog, the album-scoped read endpoint,
the beets `Library` that performs the write and the SQLite rows it lands in are
all real. That is the point — the whole guarantee of this tool is that the
values it previewed are still the values in the database when it writes, and a
mocked backend could not disprove it.
"""

import sqlite3

import httpx
import pytest

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.preview import DRIFT, PreviewStore
from beetdeck_mcp.tools.apply import TOOL_NAMES as APPLY_TOOL_NAMES
from beetdeck_mcp.tools.apply import build_tools as build_apply_tools
from beetdeck_mcp.tools.tags import TOOL_NAMES, build_tools
from tests.conftest import insert_album, insert_item


@pytest.fixture()
def store():
    """A preview store per test — tokens must not leak between them."""
    return PreviewStore()


@pytest.fixture()
def make_tools(beetdeck_client, store):
    """The tag + apply tools, sharing one store and one REST client."""

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


@pytest.fixture()
def track(db_path):
    """One album with one track carrying a title, an artist and a comment."""
    album_id = insert_album(db_path, album="Test Album", albumartist="Test Artist")
    item_id = insert_item(
        db_path,
        album_id,
        title="Old Title",
        artist="Old Artist",
        comments="an old note",
    )
    return album_id, item_id


@pytest.fixture()
def writable(mocker):
    """File tag writes succeed; the database half is what these tests assert."""
    return mocker.patch("beetdeck.routes.items._try_write_item", return_value=True)


def add_flex_attr(db_path, item_id, key, value):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)",
            (item_id, key, value),
        )
        conn.commit()
    finally:
        conn.close()


def current_tags(client, album_id, item_id):
    return client.get(f"/api/album/{album_id}/track/{item_id}/tags").data


def dead_client(mcp_config):
    """A client whose backend refuses every connection."""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    return BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))


class TestBuildTools:
    def test_exposes_exactly_the_declared_tool_names(self, tools):
        assert set(tools) == set(TOOL_NAMES) | set(APPLY_TOOL_NAMES)


# ---------------------------------------------------------------------------
# preview_tags_edit
# ---------------------------------------------------------------------------


class TestPreviewTagsEdit:
    def test_diffs_old_to_new_and_mints_a_token(self, tools, track, store):
        album_id, item_id = track
        result = tools["preview_tags_edit"](album_id, item_id, {"title": "New Title"})

        assert result["preview_token"].startswith("tags-")
        assert result["kind"] == "tags"
        assert result["target"] == f"item {item_id}"
        assert result["changes"]["fields"] == {"title": {"old": "Old Title", "new": "New Title"}}
        assert result["changes"]["item_id"] == item_id
        assert "re-read" in result["guarantee"]
        assert len(store) == 1

    def test_the_token_carries_what_apply_change_re_reads(self, tools, track, store):
        album_id, item_id = track
        token = tools["preview_tags_edit"](album_id, item_id, {"title": "New"})["preview_token"]

        preview = store.peek(token)
        assert preview.old_values == {"title": "Old Title"}
        assert preview.params == {"fields": {"title": "New"}}
        assert preview.endpoint == f"/api/items/{item_id}/tags"
        assert preview.method == "PATCH"
        assert preview.reread_endpoint == f"/api/album/{album_id}/track/{item_id}/tags"

    def test_an_empty_value_is_shown_as_clearing_the_field(self, tools, track):
        album_id, item_id = track
        result = tools["preview_tags_edit"](album_id, item_id, {"comments": ""})
        assert result["changes"]["fields"]["comments"] == {
            "old": "an old note",
            "new": "",
            "clears": True,
        }

    def test_batches_several_fields_in_one_token(self, tools, track, store):
        album_id, item_id = track
        result = tools["preview_tags_edit"](
            album_id,
            item_id,
            {"title": "New Title", "artist": "New Artist", "comments": ""},
        )
        assert set(result["changes"]["fields"]) == {"title", "artist", "comments"}
        assert store.peek(result["preview_token"]).params["fields"] == {
            "title": "New Title",
            "artist": "New Artist",
            "comments": "",
        }
        assert len(store) == 1

    def test_a_flexible_attr_the_catalog_does_not_list_is_previewed(self, tools, track, db_path):
        """The catalog only knows beets' fixed fields — a flex attr must pass."""
        album_id, item_id = track
        add_flex_attr(db_path, item_id, "mood", "calm")

        result = tools["preview_tags_edit"](album_id, item_id, {"mood": "excited"})
        assert result["changes"]["fields"] == {"mood": {"old": "calm", "new": "excited"}}
        assert "notes" not in result

    def test_a_field_neither_known_nor_present_is_previewed_with_a_note(self, tools, track):
        """Not a refusal: only the write handler can see the item's flex attrs."""
        album_id, item_id = track
        result = tools["preview_tags_edit"](album_id, item_id, {"totally_new_key": "x"})

        assert result["preview_token"]
        assert any("totally_new_key" in note for note in result["notes"])

    def test_fields_already_at_the_requested_value_are_left_out(self, tools, track, store):
        album_id, item_id = track
        result = tools["preview_tags_edit"](
            album_id, item_id, {"title": "Old Title", "artist": "New Artist"}
        )
        assert set(result["changes"]["fields"]) == {"artist"}
        assert store.peek(result["preview_token"]).old_values == {"artist": "Old Artist"}
        assert any("title" in note for note in result["notes"])

    def test_clearing_an_already_absent_field_is_not_a_change(self, tools, track):
        """An empty value and an absent one are the same state throughout."""
        album_id, item_id = track
        result = tools["preview_tags_edit"](album_id, item_id, {"lyrics": ""})
        assert "nothing to change" in result["error"]

    def test_a_no_op_edit_mints_nothing(self, tools, track, store):
        album_id, item_id = track
        result = tools["preview_tags_edit"](album_id, item_id, {"title": "Old Title"})
        assert "title" in result["error"]
        assert len(store) == 0


class TestPreviewTagsEditRefusals:
    @pytest.mark.parametrize("name", ["length", "bpm", "mb_trackid", "rg_track_gain"])
    def test_a_field_the_catalog_calls_read_only_is_refused(self, tools, track, store, name):
        album_id, item_id = track
        result = tools["preview_tags_edit"](album_id, item_id, {name: "1"})
        assert name in result["error"]
        assert "not editable" in result["error"]
        assert len(store) == 0

    def test_an_mb_name_outside_the_catalog_is_refused_too(self, tools, track, store):
        """`item_fields.is_editable` blocks the prefix, not a list of names."""
        album_id, item_id = track
        result = tools["preview_tags_edit"](album_id, item_id, {"mb_something_new": "x"})
        assert "mb_something_new" in result["error"]
        assert len(store) == 0

    def test_one_read_only_field_refuses_the_whole_batch(self, tools, track, store):
        album_id, item_id = track
        result = tools["preview_tags_edit"](album_id, item_id, {"title": "New", "length": "1"})
        assert "length" in result["error"]
        assert len(store) == 0

    def test_an_empty_field_set_is_refused(self, tools, track, store):
        album_id, item_id = track
        result = tools["preview_tags_edit"](album_id, item_id, {})
        assert "at least one field" in result["error"]
        assert len(store) == 0

    def test_an_item_from_another_album_is_not_previewable(self, tools, track, db_path, store):
        """The read endpoint is album-scoped and 404s on a mismatched pair."""
        album_id, item_id = track
        other_album = insert_album(db_path, album="Other")
        result = tools["preview_tags_edit"](other_album, item_id, {"title": "New"})
        assert "404" in result["error"]
        assert len(store) == 0

    def test_an_unreachable_backend_is_reported_not_swallowed(self, make_tools, mcp_config, store):
        result = make_tools(dead_client(mcp_config))["preview_tags_edit"](1, 1, {"title": "New"})
        assert "could not reach" in result["error"]
        assert len(store) == 0


# ---------------------------------------------------------------------------
# apply_change — the `tags` kind
# ---------------------------------------------------------------------------


class TestApplyTags:
    def test_writes_the_previewed_value(self, tools, track, beetdeck_client, writable):
        album_id, item_id = track
        token = tools["preview_tags_edit"](album_id, item_id, {"title": "New Title"})
        result = tools["apply_change"](token["preview_token"])

        assert result["status"] == "ok"
        assert result["kind"] == "tags"
        assert result["target"] == f"item {item_id}"
        assert current_tags(beetdeck_client, album_id, item_id)["title"] == "New Title"
        assert writable.called

    def test_an_empty_value_clears_the_field(self, tools, track, beetdeck_client, writable):
        album_id, item_id = track
        token = tools["preview_tags_edit"](album_id, item_id, {"comments": ""})
        assert tools["apply_change"](token["preview_token"])["status"] == "ok"
        assert "comments" not in current_tags(beetdeck_client, album_id, item_id)

    def test_a_batch_lands_as_one_write(self, tools, track, beetdeck_client, writable):
        album_id, item_id = track
        token = tools["preview_tags_edit"](
            album_id, item_id, {"title": "New Title", "artist": "New Artist", "comments": ""}
        )
        assert tools["apply_change"](token["preview_token"])["status"] == "ok"

        tags = current_tags(beetdeck_client, album_id, item_id)
        assert tags["title"] == "New Title"
        assert tags["artist"] == "New Artist"
        assert "comments" not in tags

    def test_a_flexible_attr_edit_lands(self, tools, track, db_path, beetdeck_client, writable):
        album_id, item_id = track
        add_flex_attr(db_path, item_id, "mood", "calm")

        token = tools["preview_tags_edit"](album_id, item_id, {"mood": "excited"})
        assert tools["apply_change"](token["preview_token"])["status"] == "ok"
        assert current_tags(beetdeck_client, album_id, item_id)["mood"] == "excited"

    def test_a_file_write_failure_is_partial_not_ok(self, tools, track, beetdeck_client, mocker):
        """The database is written and the file is not — the backend says so
        with HTTP 200 plus warnings, which must not read as success."""
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=False)
        album_id, item_id = track

        token = tools["preview_tags_edit"](album_id, item_id, {"title": "New Title"})
        result = tools["apply_change"](token["preview_token"])

        assert result["status"] == "partial"
        assert any("file write failed" in entry for entry in result["detail"])
        assert current_tags(beetdeck_client, album_id, item_id)["title"] == "New Title"

    def test_a_value_changed_since_the_preview_is_refused(
        self, tools, track, beetdeck_client, writable
    ):
        album_id, item_id = track
        token = tools["preview_tags_edit"](album_id, item_id, {"title": "New Title"})

        # Someone else edits the same field between preview and apply.
        assert beetdeck_client.patch(
            f"/api/items/{item_id}/tags", json={"fields": {"title": "Someone Else"}}
        ).ok

        result = tools["apply_change"](token["preview_token"])
        assert result["status"] == "failed"
        assert "changed since the preview" in result["detail"][0]
        assert "Old Title" in result["detail"][0] and "Someone Else" in result["detail"][0]
        assert current_tags(beetdeck_client, album_id, item_id)["title"] == "Someone Else"

    def test_drift_on_another_field_of_the_same_item_does_not_block(
        self, tools, track, beetdeck_client, writable
    ):
        """Only the previewed fields are guarded — a token is not a lock on the
        whole row."""
        album_id, item_id = track
        token = tools["preview_tags_edit"](album_id, item_id, {"title": "New Title"})
        assert beetdeck_client.patch(
            f"/api/items/{item_id}/tags", json={"fields": {"artist": "Someone Else"}}
        ).ok

        assert tools["apply_change"](token["preview_token"])["status"] == "ok"
        assert current_tags(beetdeck_client, album_id, item_id)["title"] == "New Title"

    def test_the_drift_refusal_is_flagged_stale_on_the_store(self, tools, track, store, writable):
        album_id, item_id = track
        token = tools["preview_tags_edit"](album_id, item_id, {"title": "New Title"})[
            "preview_token"
        ]
        preview = store.peek(token)

        from beetdeck_mcp.preview import PreviewError, check_old_values

        with pytest.raises(PreviewError) as excinfo:
            check_old_values(preview, {"title": "Someone Else"})
        assert excinfo.value.reason == DRIFT

    def test_a_field_the_item_does_not_carry_fails_at_the_write(
        self, tools, track, beetdeck_client, writable
    ):
        album_id, item_id = track
        token = tools["preview_tags_edit"](album_id, item_id, {"totally_new_key": "x"})

        result = tools["apply_change"](token["preview_token"])
        assert result["status"] == "failed"
        assert "not editable" in result["detail"][0]
        assert "totally_new_key" not in current_tags(beetdeck_client, album_id, item_id)

    def test_a_failed_re_read_refuses_instead_of_writing_blind(
        self, tools, track, db_path, mocker, writable
    ):
        album_id, item_id = track
        token = tools["preview_tags_edit"](album_id, item_id, {"title": "New Title"})

        # The track disappears (a rescan re-imported it under a new id): the
        # re-read 404s, so the previewed values cannot be confirmed.
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            conn.commit()
        finally:
            conn.close()

        result = tools["apply_change"](token["preview_token"])
        assert result["status"] == "failed"
        assert "could not be re-read" in result["detail"][0]
        assert "nothing was written" in result["detail"][0]

    def test_the_token_commits_exactly_once(self, tools, track, writable):
        album_id, item_id = track
        token = tools["preview_tags_edit"](album_id, item_id, {"title": "New Title"})
        assert tools["apply_change"](token["preview_token"])["status"] == "ok"

        again = tools["apply_change"](token["preview_token"])
        assert again["status"] == "failed"
        assert "already been applied" in again["detail"][0]

    def test_a_readonly_server_writes_nothing_and_keeps_the_token(
        self, make_tools, track, beetdeck_client, writable, store
    ):
        album_id, item_id = track
        readonly = make_tools(readonly=True)
        token = readonly["preview_tags_edit"](album_id, item_id, {"title": "New Title"})

        refused = readonly["apply_change"](token["preview_token"])
        assert refused["status"] == "failed"
        assert current_tags(beetdeck_client, album_id, item_id)["title"] == "Old Title"
        assert len(store) == 1
