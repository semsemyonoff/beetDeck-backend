"""MCP genre previews, driven against the real Flask app in-process.

Only two things are faked: the `lastgenre` plugin (it would call Last.fm) and
the audio-file write (the test library has no files). The album endpoint, the
`…/genre/save` handler, the beets `Library` and the SQLite rows are real —
which is what lets `test_apply_writes_exactly_the_previewed_value` prove the
central claim of this tool: the value that lands is the one the agent was shown,
not whatever a second Last.fm lookup would have answered.
"""

import sqlite3

import httpx
import pytest

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.preview import PreviewStore
from beetdeck_mcp.tools.apply import TOOL_NAMES as APPLY_TOOL_NAMES
from beetdeck_mcp.tools.apply import build_tools as build_apply_tools
from beetdeck_mcp.tools.genres import TOOL_NAMES, build_tools
from tests.conftest import insert_album, insert_item


@pytest.fixture()
def store():
    """A preview store per test — tokens must not leak between them."""
    return PreviewStore()


@pytest.fixture()
def make_tools(beetdeck_client, store):
    """The genre + apply tools, sharing one store and one REST client."""

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
def album(db_path):
    """One album with one track, carrying the legacy singular genre `Rock`."""
    album_id = insert_album(db_path, album="Test Album", albumartist="Test Artist", genre="Rock")
    insert_item(db_path, album_id, title="Track One", path=b"/music/test/01.mp3")
    return album_id


@pytest.fixture()
def suggestion(mocker):
    """The `lastgenre` plugin, answering with a fixed genre in pretend mode."""

    def _install(genre="Electronic"):
        plugin = mocker.MagicMock()
        plugin.name = "lastgenre"

        def process(album, write):
            album.genres = [] if not genre else genre.split(", ")

        plugin._process.side_effect = process
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])
        return plugin

    return _install


@pytest.fixture()
def writable(mocker):
    """File tag writes succeed; the database half is what these tests assert."""
    return mocker.patch("beetdeck.routes.genres._try_write_item", return_value=True)


def current_genre(client, album_id):
    return client.get(f"/api/album/{album_id}").data.get("genre", "")


def dead_client(mcp_config):
    """A client whose backend refuses every connection."""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    return BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))


class TestBuildTools:
    def test_exposes_exactly_the_declared_tool_names(self, tools):
        assert set(tools) == set(TOOL_NAMES) | set(APPLY_TOOL_NAMES)


# ---------------------------------------------------------------------------
# preview_genres
# ---------------------------------------------------------------------------


class TestPreviewGenres:
    def test_looks_the_genre_up_and_mints_a_token(self, tools, album, suggestion, store):
        suggestion("Electronic")
        result = tools["preview_genres"](album)

        assert result["preview_token"].startswith("genres-")
        assert result["kind"] == "genres"
        assert result["target"] == f"album {album}"
        assert result["changes"]["genre"] == {"old": "Rock", "new": "Electronic"}
        assert result["changes"]["source"] == "lastgenre"
        assert "re-read" in result["guarantee"]
        assert len(store) == 1

    def test_the_token_commits_through_save_not_confirm(self, tools, album, suggestion, store):
        """`…/genre/confirm` looks Last.fm up a second time and can write a
        different value than the one previewed — see the module docstring."""
        suggestion("Electronic")
        token = tools["preview_genres"](album)["preview_token"]

        preview = store.peek(token)
        assert preview.endpoint == f"/api/album/{album}/genre/save"
        assert preview.method == "POST"
        assert preview.params == {"genre": "Electronic"}
        assert preview.old_values == {"genre": "Rock"}
        assert preview.reread_endpoint == f"/api/album/{album}"

    def test_an_override_is_carried_verbatim_and_skips_the_lookup(
        self, tools, album, mocker, store
    ):
        find_plugins = mocker.patch("beets.plugins.find_plugins", return_value=[])
        result = tools["preview_genres"](album, "Post-Punk, New Wave")

        assert result["changes"]["genre"] == {"old": "Rock", "new": "Post-Punk, New Wave"}
        assert result["changes"]["source"] == "override"
        assert store.peek(result["preview_token"]).params == {"genre": "Post-Punk, New Wave"}
        assert not find_plugins.called

    def test_an_override_is_normalized_the_way_the_handler_splits_it(self, tools, album, store):
        result = tools["preview_genres"](album, " Jazz ,, Funk , ")
        assert result["changes"]["genre"]["new"] == "Jazz, Funk"
        assert store.peek(result["preview_token"]).params == {"genre": "Jazz, Funk"}

    def test_the_old_value_comes_from_the_album_endpoint_not_the_lookup(
        self, tools, album, suggestion, store
    ):
        """The lookup reads only the native `genres` list; this album carries
        the legacy singular `genre`, so the lookup's `old_genre` is empty and
        using it would make every apply refuse on false drift."""
        suggestion("Electronic")
        token = tools["preview_genres"](album)["preview_token"]
        assert store.peek(token).old_values == {"genre": "Rock"}


class TestPreviewGenresRefusals:
    def test_a_blank_override_is_refused_before_the_handler_400s(self, tools, album, store):
        result = tools["preview_genres"](album, " , ")
        assert "no genre names" in result["error"]
        assert len(store) == 0

    def test_a_lookup_that_finds_nothing_mints_nothing(self, tools, album, suggestion, store):
        suggestion("")
        result = tools["preview_genres"](album)
        assert "no genre" in result["error"]
        assert len(store) == 0

    def test_lastgenre_unavailable_is_reported(self, tools, album, mocker, store):
        mocker.patch("beets.plugins.find_plugins", return_value=[])
        result = tools["preview_genres"](album)
        assert "lastgenre plugin not loaded" in result["error"]
        assert len(store) == 0

    def test_a_genre_the_album_already_carries_is_not_a_change(self, tools, album, store):
        result = tools["preview_genres"](album, "Rock")
        assert "nothing to change" in result["error"]
        assert len(store) == 0

    def test_an_unknown_album_is_refused_at_the_read(self, tools, store):
        result = tools["preview_genres"](9999, "Jazz")
        assert "404" in result["error"]
        assert len(store) == 0

    def test_an_unreachable_backend_is_reported_not_swallowed(self, make_tools, mcp_config, store):
        result = make_tools(dead_client(mcp_config))["preview_genres"](1, "Jazz")
        assert "could not reach" in result["error"]
        assert len(store) == 0


# ---------------------------------------------------------------------------
# apply_change — the `genres` kind
# ---------------------------------------------------------------------------


class TestApplyGenres:
    def test_writes_exactly_the_previewed_value(
        self, tools, album, suggestion, beetdeck_client, writable
    ):
        plugin = suggestion("Electronic")
        token = tools["preview_genres"](album)["preview_token"]

        # Last.fm changes its mind between preview and apply: a commit through
        # `…/genre/confirm` would write this instead of what was previewed.
        plugin._process.side_effect = lambda album, write: setattr(album, "genres", ["Ska"])

        result = tools["apply_change"](token)
        assert result["status"] == "ok"
        assert result["kind"] == "genres"
        assert result["target"] == f"album {album}"
        assert current_genre(beetdeck_client, album) == "Electronic"

    def test_an_overridden_value_is_written_verbatim(
        self, tools, album, beetdeck_client, writable, db_path
    ):
        token = tools["preview_genres"](album, "Post-Punk, New Wave")["preview_token"]
        assert tools["apply_change"](token)["status"] == "ok"

        assert current_genre(beetdeck_client, album) == "Post-Punk, New Wave"
        tracks = beetdeck_client.get(f"/api/album/{album}").data["tracks"]
        assert tracks, "the album must have a track for the per-item write to mean anything"

    def test_a_file_write_failure_is_partial_not_ok(self, tools, album, beetdeck_client, mocker):
        """The database is written and the file is not — a 200 carrying
        `status: "partial"` must not read as success."""
        mocker.patch("beetdeck.routes.genres._try_write_item", return_value=False)
        token = tools["preview_genres"](album, "Jazz")["preview_token"]

        result = tools["apply_change"](token)
        assert result["status"] == "partial"
        assert any("genre tag write failed" in entry for entry in result["detail"])
        assert current_genre(beetdeck_client, album) == "Jazz"

    def test_a_genre_changed_since_the_preview_is_refused(
        self, tools, album, beetdeck_client, writable
    ):
        token = tools["preview_genres"](album, "Jazz")["preview_token"]

        # Someone else sets the genre in the web UI between preview and apply.
        assert beetdeck_client.post(f"/api/album/{album}/genre/save", json={"genre": "Ambient"}).ok

        result = tools["apply_change"](token)
        assert result["status"] == "failed"
        assert "changed since the preview" in result["detail"][0]
        assert "Rock" in result["detail"][0] and "Ambient" in result["detail"][0]
        assert current_genre(beetdeck_client, album) == "Ambient"

    def test_a_failed_re_read_refuses_instead_of_writing_blind(
        self, tools, album, db_path, writable
    ):
        token = tools["preview_genres"](album, "Jazz")["preview_token"]

        # The album disappears (a rescan re-imported it under a new id): the
        # re-read 404s, so the previewed genre cannot be confirmed.
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM albums WHERE id = ?", (album,))
            conn.commit()
        finally:
            conn.close()

        result = tools["apply_change"](token)
        assert result["status"] == "failed"
        assert "could not be re-read" in result["detail"][0]
        assert "nothing was written" in result["detail"][0]

    def test_the_token_commits_exactly_once(self, tools, album, writable):
        token = tools["preview_genres"](album, "Jazz")["preview_token"]
        assert tools["apply_change"](token)["status"] == "ok"

        again = tools["apply_change"](token)
        assert again["status"] == "failed"
        assert "already been applied" in again["detail"][0]

    def test_a_readonly_server_writes_nothing_and_keeps_the_token(
        self, make_tools, album, beetdeck_client, writable, store
    ):
        readonly = make_tools(readonly=True)
        token = readonly["preview_genres"](album, "Jazz")["preview_token"]

        refused = readonly["apply_change"](token)
        assert refused["status"] == "failed"
        assert current_genre(beetdeck_client, album) == "Rock"
        assert len(store) == 1
