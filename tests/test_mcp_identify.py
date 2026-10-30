"""MCP identification tools, driven against the real Flask app in-process.

Only beets itself is mocked (autotag would hit MusicBrainz); the identification
task, its background thread, the generation counter and the confirm precondition
are all the real ones, so the tool↔REST contract is exercised end to end.
"""

import httpx
import pytest

from beetdeck import state
from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.preview import PreviewStore
from beetdeck_mcp.tools.apply import READONLY_REFUSAL
from beetdeck_mcp.tools.apply import TOOL_NAMES as APPLY_TOOL_NAMES
from beetdeck_mcp.tools.apply import build_tools as build_apply_tools
from beetdeck_mcp.tools.identify import TOOL_NAMES, build_tools
from tests.conftest import insert_album, insert_item


@pytest.fixture()
def store():
    """A preview store per test — tokens must not leak between them."""
    return PreviewStore()


@pytest.fixture()
def make_tools(beetdeck_client, store):
    """Build the identify + apply tools, sharing one store and one client."""

    def _make(client=None, *, readonly=False, **poll_kwargs):
        rest = beetdeck_client if client is None else client
        poll_kwargs.setdefault("poll_interval", 0.01)
        poll_kwargs.setdefault("poll_timeout", 10.0)
        tools = {tool.__name__: tool for tool in build_tools(rest, store, **poll_kwargs)}
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
    """An album row with one track, as every identification needs."""
    album_id = insert_album(db_path, album="Untitled", albumartist="Unknown")
    insert_item(db_path, album_id, title="Old Title", artist="Old Artist")
    return album_id


def dead_client(mcp_config):
    """A client whose backend refuses every connection."""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    return BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))


def make_match(mocker, *, distance=0.1, write_ok=True, album_title="Real Album"):
    """An AlbumMatch stand-in that survives serialize → apply → confirm."""
    item = mocker.MagicMock()
    item.id = 1
    item.track = 1
    item.title = "Old Title"
    item.artist = "Old Artist"
    item.path = b"/music/track.mp3"
    item.try_write.return_value = write_ok

    track = mocker.MagicMock()
    track.title = "Real Title"
    track.index = 1
    track.artist = "Real Artist"
    track.length = 217

    match = mocker.MagicMock()
    match.distance = distance
    match.info.artist = "Real Artist"
    match.info.album = album_title
    match.info.year = 1997
    match.info.label = "Real Label"
    match.info.media = "CD"
    match.info.data_source = "MusicBrainz"
    match.info.album_id = "mbid-real"
    match.info.tracks = [track]
    match.info.item_data = {}
    match.mapping = {item: track}
    return match


def mock_beets(mocker, match, *, album_found=True):
    """Patch the two beets entry points the identify thread reaches for."""
    mock_album = mocker.MagicMock()
    mock_album.albumartist = "Unknown"
    mock_album.album = "Untitled"
    mock_album.items.return_value = list(match.mapping) if match else [mocker.MagicMock()]

    mock_lib = mocker.MagicMock()
    mock_lib.get_album.return_value = mock_album if album_found else None
    mocker.patch("beetdeck.routes.identify._init_beets", return_value=mock_lib)

    proposal = mocker.MagicMock()
    proposal.candidates = [match] if match else []
    tag_album = mocker.patch(
        "beets.autotag.tag_album",
        return_value=proposal,
    )
    return mock_album, tag_album


# ---------------------------------------------------------------------------
# identify_album
# ---------------------------------------------------------------------------


class TestBuildTools:
    def test_exposes_exactly_the_declared_tool_names(self, tools):
        assert set(tools) == set(TOOL_NAMES) | set(APPLY_TOOL_NAMES)


class TestIdentifyAlbum:
    def test_polls_to_completion_and_returns_candidates(self, tools, album, mocker):
        mock_beets(mocker, make_match(mocker))

        result = tools["identify_album"](album)

        assert result["status"] == "done"
        assert result["matches_available"] is True
        assert isinstance(result["match_generation"], int)
        assert result["candidates"] == [
            {
                "index": 0,
                "artist": "Real Artist",
                "album": "Real Album",
                "year": 1997,
                "label": "Real Label",
                "media": "CD",
                "data_source": "MusicBrainz",
                "mb_albumid": "mbid-real",
                "distance": 0.1,
                "track_count": 1,
            }
        ]

    def test_omits_the_per_candidate_tracklist(self, tools, album, mocker):
        mock_beets(mocker, make_match(mocker))
        assert "tracks" not in tools["identify_album"](album)["candidates"][0]

    def test_forwards_the_search_hints(self, tools, album, mocker):
        _album, tag_album = mock_beets(mocker, make_match(mocker))

        tools["identify_album"](album, artist="Daft Punk", album="Homework")

        kwargs = tag_album.call_args.kwargs
        assert kwargs["search_artist"] == "Daft Punk"
        assert kwargs["search_name"] == "Homework"

    def test_forced_release_id_yields_a_candidate_despite_a_high_distance(
        self, tools, album, mocker
    ):
        """The override path: an explicit id is routed straight to the source,
        so no distance filter can drop it."""
        _album, tag_album = mock_beets(mocker, make_match(mocker, distance=0.94))

        result = tools["identify_album"](album, mb_release_id="mbid-forced")

        assert tag_album.call_args.kwargs["search_ids"] == ["mbid-forced"]
        assert result["candidates"][0]["distance"] == 0.94

    def test_adopts_a_running_task_instead_of_restarting_it(self, make_tools, album, mocker):
        state.identify_tasks[f"album_{album}"] = {
            "task_id": "already-running",
            "album_id": album,
            "status": "running",
            "candidates": [],
            "match_generation": None,
        }

        def finish(_delay):
            task = state.identify_tasks[f"album_{album}"]
            task["status"] = "done"
            task["candidates"] = [{"index": 0, "artist": "Real Artist", "distance": 0.2}]
            task["_matches"] = ["a match"]
            task["match_generation"] = 4242

        tools = make_tools(sleep=finish)
        result = tools["identify_album"](album, artist="ignored hint")

        assert result["task_id"] == "already-running"
        assert result["status"] == "done"
        assert result["match_generation"] == 4242
        assert "adopted" in result["notes"][0]
        # The running task was joined, not replaced.
        assert state.identify_tasks[f"album_{album}"]["task_id"] == "already-running"

    def test_a_poll_timeout_reports_running_and_invites_a_re_call(self, make_tools, album):
        state.identify_tasks[f"album_{album}"] = {
            "task_id": "slow",
            "album_id": album,
            "status": "running",
            "candidates": [],
            "match_generation": None,
        }

        result = make_tools(poll_timeout=0.0)["identify_album"](album)

        assert result["status"] == "running"
        assert result["task_id"] == "slow"
        assert "candidates" not in result
        # Adopted *and* timed out — both facts survive, neither overwrites the other.
        assert [note.split(" ")[0] for note in result["notes"]] == ["an", "still"]
        assert "identify_album again" in result["notes"][1]

    def test_reports_a_failed_identification(self, tools, album, mocker):
        mock_beets(mocker, make_match(mocker), album_found=False)

        result = tools["identify_album"](album)

        assert result["status"] == "error"
        assert result["error"] == "Album not found"
        assert "candidates" not in result

    def test_reports_an_unreachable_backend(self, make_tools, mcp_config, album):
        result = make_tools(dead_client(mcp_config))["identify_album"](album)
        assert "could not reach the beetDeck API" in result["error"]
        assert "candidates" not in result

    def test_a_backend_that_dies_mid_poll_is_an_error_not_an_empty_result(
        self, make_tools, mcp_config, album
    ):
        """The start request succeeds and the status read does not.

        `dead_client` fails on the very first call, so it never reaches the poll
        loop's own failure branch — the realistic case is the container
        restarting while the tool is already waiting on a long identification.
        """

        def handler(request):
            if request.url.path.endswith("/identify"):
                return httpx.Response(200, json={"status": "started"})
            raise httpx.ConnectError("connection refused", request=request)

        flaky = BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))

        result = make_tools(flaky)["identify_album"](album)

        assert "could not reach the beetDeck API" in result["error"]
        assert "candidates" not in result
        assert "status" not in result


# ---------------------------------------------------------------------------
# preview_match
# ---------------------------------------------------------------------------


class TestPreviewMatch:
    def test_returns_the_diff_and_a_token(self, tools, album, mocker):
        mock_beets(mocker, make_match(mocker))
        tools["identify_album"](album)

        preview = tools["preview_match"](album)

        assert preview["kind"] == "identify"
        assert preview["target"] == f"album {album}"
        assert preview["preview_token"].startswith("identify-")
        assert preview["expires_in_seconds"] > 0
        assert "rejected" in preview["guarantee"]
        assert preview["changes"]["album"]["album"] == {"old": "Untitled", "new": "Real Album"}
        assert preview["changes"]["tracks"] == [
            {
                "track": 1,
                "title": {"old": "Old Title", "new": "Real Title"},
                "artist": {"old": "Old Artist", "new": "Real Artist"},
            }
        ]

    def test_token_carries_the_generation_from_the_status_response(
        self, tools, store, album, mocker
    ):
        mock_beets(mocker, make_match(mocker))
        generation = tools["identify_album"](album)["match_generation"]

        preview = store.peek(tools["preview_match"](album)["preview_token"])

        assert preview.expected_generation == generation
        assert preview.endpoint == f"/api/album/{album}/confirm"
        assert preview.request_json() == {
            "candidate_index": 0,
            "expected_generation": generation,
        }

    def test_without_an_identification_it_says_to_run_one(self, tools, album):
        result = tools["preview_match"](album)
        assert "run identify_album first" in result["error"]
        assert "preview_token" not in result

    def test_an_invalid_candidate_index_is_reported_not_minted(self, tools, store, album, mocker):
        mock_beets(mocker, make_match(mocker))
        tools["identify_album"](album)

        result = tools["preview_match"](album, candidate_index=7)

        assert "Invalid candidate index" in result["error"]
        assert len(store) == 0

    def test_a_consumed_task_is_no_longer_previewable(self, tools, album, mocker):
        mock_beets(mocker, make_match(mocker))
        tools["identify_album"](album)
        assert (
            tools["apply_change"](tools["preview_match"](album)["preview_token"])["status"] == "ok"
        )

        result = tools["preview_match"](album)

        assert "no confirmable identification results" in result["error"]

    def test_reports_an_unreachable_backend(self, make_tools, mcp_config, album):
        result = make_tools(dead_client(mcp_config))["preview_match"](album)
        assert "could not reach the beetDeck API" in result["error"]

    def test_a_confirmable_task_without_a_generation_mints_nothing(
        self, make_tools, mcp_config, store, album
    ):
        """The precondition is the whole guarantee of this kind.

        A status response that says `done` + `matches_available` but carries no
        usable `match_generation` must refuse rather than mint a token that
        would silently degrade to no defence at all.
        """

        def handler(request):
            return httpx.Response(
                200,
                json={"status": "done", "matches_available": True, "match_generation": None},
            )

        tools = make_tools(BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler)))

        result = tools["preview_match"](album)

        assert "carries no match_generation" in result["error"]
        assert "preview_token" not in result
        assert len(store) == 0


# ---------------------------------------------------------------------------
# apply_change
# ---------------------------------------------------------------------------


class TestApplyChange:
    def test_full_flow_writes_the_match(self, tools, album, mocker):
        match = make_match(mocker)
        mock_album, _ = mock_beets(mocker, match)
        tools["identify_album"](album)
        token = tools["preview_match"](album)["preview_token"]

        result = tools["apply_change"](token)

        assert result == {
            "status": "ok",
            "detail": [],
            "kind": "identify",
            "target": f"album {album}",
        }
        match.apply_metadata.assert_called_once()
        assert mock_album.beetdeck_tagged == "1"

    def test_a_token_commits_exactly_once(self, tools, album, mocker):
        mock_beets(mocker, make_match(mocker))
        tools["identify_album"](album)
        token = tools["preview_match"](album)["preview_token"]
        assert tools["apply_change"](token)["status"] == "ok"

        result = tools["apply_change"](token)

        assert result["status"] == "failed"
        assert "already been applied" in result["detail"][0]

    def test_readonly_refuses_and_keeps_the_token(self, make_tools, store, album, mocker):
        mock_beets(mocker, make_match(mocker))
        readonly = make_tools(readonly=True)
        readonly["identify_album"](album)
        token = readonly["preview_match"](album)["preview_token"]

        result = readonly["apply_change"](token)

        assert result["status"] == "failed"
        assert result["detail"] == [READONLY_REFUSAL]
        # Previews keep working and the token survives — a read-only server can
        # still report what it would change.
        assert store.peek(token).kind == "identify"

    def test_a_write_failure_is_reported_as_failed(self, tools, album, mocker):
        """`…/confirm` is all-or-nothing: a failed file write is a 500 and the
        album is deliberately left untagged."""
        mock_album, _ = mock_beets(mocker, make_match(mocker, write_ok=False))
        tools["identify_album"](album)
        token = tools["preview_match"](album)["preview_token"]

        result = tools["apply_change"](token)

        assert result["status"] == "failed"
        assert "Failed to write tags for 1 track(s)" in result["detail"][0]
        assert not hasattr(mock_album, "beetdeck_tagged") or mock_album.beetdeck_tagged != "1"

    def test_a_re_identify_between_preview_and_apply_is_refused(self, tools, album, mocker):
        mock_beets(mocker, make_match(mocker))
        tools["identify_album"](album)
        token = tools["preview_match"](album)["preview_token"]

        tools["identify_album"](album)  # the library moved under the preview

        result = tools["apply_change"](token)

        assert result["status"] == "failed"
        assert "re-run, replaced or already applied" in result["detail"][0]
        assert "identify again" in result["detail"][0]
        # The fresh candidates survive the refusal.
        assert state.identify_tasks[f"album_{album}"]["_matches"]

    def test_a_confirm_of_an_already_consumed_task_is_refused(self, tools, album, mocker):
        """Two previews, one apply each: the second carries a dead generation."""
        mock_beets(mocker, make_match(mocker))
        tools["identify_album"](album)
        first = tools["preview_match"](album)["preview_token"]
        second = tools["preview_match"](album)["preview_token"]

        assert tools["apply_change"](first)["status"] == "ok"
        result = tools["apply_change"](second)

        assert result["status"] == "failed"
        assert "re-run, replaced or already applied" in result["detail"][0]

    def test_an_unknown_token_says_so(self, tools):
        result = tools["apply_change"]("identify-nope")
        assert result["status"] == "failed"
        assert "no such preview token" in result["detail"][0]

    def test_a_kind_with_no_handler_is_refused_explicitly(self, tools, store, album, mocker):
        """Preview kinds arrive one task at a time; an unhandled one must say
        what it is rather than fail obscurely.

        The handler table is emptied rather than picking whichever kind happens
        to be unimplemented today — that choice would expire as the remaining
        kinds land, and the behaviour under test is the lookup, not the gap.
        """
        mocker.patch.dict("beetdeck_mcp.tools.apply._HANDLERS", {}, clear=True)
        token = store.mint(
            "cover",
            target=f"album {album}",
            method="POST",
            endpoint=f"/api/album/{album}/cover/confirm",
            expected_generation=1,
        ).token

        result = tools["apply_change"](token)

        assert result["status"] == "failed"
        assert "cannot commit a 'cover' preview" in result["detail"][0]

    def test_reports_an_unreachable_backend(self, make_tools, mcp_config, store, album, mocker):
        mock_beets(mocker, make_match(mocker))
        tools = make_tools()
        tools["identify_album"](album)
        token = tools["preview_match"](album)["preview_token"]

        result = make_tools(dead_client(mcp_config))["apply_change"](token)

        assert result["status"] == "failed"
        assert "could not reach the beetDeck API" in result["detail"][0]
