"""MCP untagged-items identification, driven against the real Flask app in-process.

The album flow is covered in `test_mcp_identify.py`; this module covers what the
items flow does *differently*, because those differences are what an agent trips
over:

* it is addressed by a generated `task_id`, not by an album id;
* **starting a run purges every finished items task**, so a token minted against
  an earlier one is dead — and must be refused as *stale*, not as "not found";
* its confirm has a partial-success contract the album confirm does not have: a
  DB failure rolls back with `500`, a partial *file* write returns `200` with
  `warnings` (see the app repo's `CLAUDE.md`).

Only beets is mocked (autotag would hit MusicBrainz); the background thread, the
generation counter and the confirm precondition are the real ones.
"""

import threading

import httpx
import pytest

from beetdeck import state
from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.preview import PreviewStore
from beetdeck_mcp.tools.apply import READONLY_REFUSAL
from beetdeck_mcp.tools.apply import build_tools as build_apply_tools
from beetdeck_mcp.tools.identify import build_tools


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


def dead_client(mcp_config):
    """A client whose backend refuses every connection."""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    return BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))


def make_match(mocker, *, distance=0.1, write_ok=True, item_id=1):
    """An AlbumMatch stand-in that survives serialize → apply → confirm."""
    item = mocker.MagicMock()
    item.id = item_id
    item.album_id = None
    item.track = 1
    item.title = "loose track"
    item.artist = "Unknown"
    item.path = b"/music/loose/track.mp3"
    item.try_write.return_value = write_ok

    track = mocker.MagicMock()
    track.title = "Real Title"
    track.index = 1
    track.artist = "Real Artist"
    track.length = 217

    match = mocker.MagicMock()
    match.distance = distance
    match.info.artist = "Real Artist"
    match.info.album = "Real Album"
    match.info.year = 1997
    match.info.label = "Real Label"
    match.info.media = "CD"
    match.info.data_source = "MusicBrainz"
    match.info.album_id = "mbid-real"
    match.info.tracks = [track]
    match.info.item_data = {}
    match.mapping = {item: track}
    return match


def mock_beets(mocker, match, *, item_found=True, add_album=None):
    """Patch the beets entry points the items-identify thread and confirm reach for.

    `add_album` is the album the confirm creates; pass a `side_effect` mock to
    drive the DB-failure path.
    """
    item = next(iter(match.mapping)) if match else mocker.MagicMock()

    album = mocker.MagicMock()
    album.id = 99

    lib = mocker.MagicMock()
    lib.get_item.return_value = item if item_found else None
    lib.get_album.return_value = album
    if add_album is None:
        lib.add_album.return_value = album
    else:
        lib.add_album = add_album
    mocker.patch("beetdeck.routes.items._init_beets", return_value=lib)

    proposal = mocker.MagicMock()
    proposal.candidates = [match] if match else []
    tag_album = mocker.patch(
        "beets.autotag.tag_album",
        return_value=proposal,
    )
    return album, tag_album


def blocking_beets(mocker, gate):
    """Patch beets so the identify thread parks until `gate` is set.

    Lets a test observe a task that is still `running` — a poll timeout, or a
    task deleted underneath the poll.
    """
    lib = mocker.MagicMock()
    lib.get_item.return_value = mocker.MagicMock()
    mocker.patch("beetdeck.routes.items._init_beets", return_value=lib)

    proposal = mocker.MagicMock()
    proposal.candidates = []

    def wait(*_args, **_kwargs):
        gate.wait(5.0)
        return proposal

    mocker.patch("beets.autotag.tag_album", side_effect=wait)


# ---------------------------------------------------------------------------
# identify_untagged_items
# ---------------------------------------------------------------------------


class TestIdentifyUntaggedItems:
    def test_polls_to_completion_and_returns_candidates(self, tools, mocker):
        mock_beets(mocker, make_match(mocker))

        result = tools["identify_untagged_items"]([1])

        assert result["status"] == "done"
        assert result["item_ids"] == [1]
        assert isinstance(result["task_id"], str) and result["task_id"]
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

    def test_forwards_the_search_hints(self, tools, mocker):
        _album, tag_album = mock_beets(mocker, make_match(mocker))

        tools["identify_untagged_items"]([1], artist="Daft Punk", album="Homework")

        kwargs = tag_album.call_args.kwargs
        assert kwargs["search_artist"] == "Daft Punk"
        assert kwargs["search_name"] == "Homework"

    def test_reports_a_failed_identification(self, tools, mocker):
        mock_beets(mocker, make_match(mocker), item_found=False)

        result = tools["identify_untagged_items"]([999])

        assert result["status"] == "error"
        assert result["error"] == "Item 999 not found"
        assert "candidates" not in result

    def test_an_empty_item_list_is_rejected_by_the_backend(self, tools):
        result = tools["identify_untagged_items"]([])
        assert "item_ids must be a non-empty list" in result["error"]

    def test_a_poll_timeout_reports_running_and_names_the_task(self, make_tools, mocker):
        gate = threading.Event()
        blocking_beets(mocker, gate)
        try:
            result = make_tools(poll_timeout=0.0)["identify_untagged_items"]([1])

            assert result["status"] == "running"
            assert result["task_id"]
            assert "candidates" not in result
            # The retry hint must not send the agent back to a *second* run —
            # that would purge the task it is waiting on.
            assert "preview_items_match" in result["notes"][0]
            assert result["task_id"] in result["notes"][0]
        finally:
            gate.set()

    def test_a_task_that_disappears_mid_poll_is_terminal_not_an_endless_wait(
        self, make_tools, mocker
    ):
        """`idle` means the task is gone; polling it forever would never settle."""
        gate = threading.Event()
        blocking_beets(mocker, gate)

        def purge(_delay):
            state.identify_tasks.clear()

        try:
            result = make_tools(sleep=purge)["identify_untagged_items"]([1])

            assert result["status"] == "idle"
            assert "purged it" in result["error"]
            assert "candidates" not in result
        finally:
            gate.set()

    def test_a_start_without_a_task_id_is_reported_not_polled(self, make_tools, mcp_config):
        """Nothing can address the result, so saying "started" would be a lie."""

        def handler(request):
            return httpx.Response(200, json={"status": "started"})

        client = BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))

        result = make_tools(client)["identify_untagged_items"]([1])

        assert "no task_id" in result["error"]
        assert "task_id" not in result

    def test_reports_an_unreachable_backend(self, make_tools, mcp_config):
        result = make_tools(dead_client(mcp_config))["identify_untagged_items"]([1])
        assert "could not reach the beetDeck API" in result["error"]
        assert "candidates" not in result


# ---------------------------------------------------------------------------
# preview_items_match
# ---------------------------------------------------------------------------


class TestPreviewItemsMatch:
    def test_returns_the_diff_and_a_token(self, tools, mocker):
        mock_beets(mocker, make_match(mocker))
        task_id = tools["identify_untagged_items"]([1])["task_id"]

        preview = tools["preview_items_match"](task_id)

        assert preview["kind"] == "items_identify"
        assert preview["target"] == f"untagged items task {task_id}"
        assert preview["preview_token"].startswith("items_identify-")
        assert preview["expires_in_seconds"] > 0
        assert "rejected" in preview["guarantee"]
        # Loose items belong to no album, so every album-level old value is empty.
        assert preview["changes"]["album"]["album"] == {"old": "", "new": "Real Album"}
        assert preview["changes"]["tracks"] == [
            {
                "track": 1,
                "title": {"old": "loose track", "new": "Real Title"},
                "artist": {"old": "Unknown", "new": "Real Artist"},
            }
        ]

    def test_token_carries_the_generation_and_the_task_confirm_path(self, tools, store, mocker):
        mock_beets(mocker, make_match(mocker))
        started = tools["identify_untagged_items"]([1])
        task_id = started["task_id"]

        preview = store.peek(tools["preview_items_match"](task_id)["preview_token"])

        assert preview.expected_generation == started["match_generation"]
        assert preview.endpoint == f"/api/items/identify/{task_id}/confirm"
        assert preview.request_json() == {
            "candidate_index": 0,
            "expected_generation": started["match_generation"],
        }

    def test_an_unknown_task_says_to_run_an_identification(self, tools, store):
        result = tools["preview_items_match"]("no-such-task")

        assert "run identify_untagged_items first" in result["error"]
        assert "preview_token" not in result
        assert len(store) == 0

    def test_an_invalid_candidate_index_is_reported_not_minted(self, tools, store, mocker):
        mock_beets(mocker, make_match(mocker))
        task_id = tools["identify_untagged_items"]([1])["task_id"]

        result = tools["preview_items_match"](task_id, candidate_index=7)

        assert "Invalid candidate index" in result["error"]
        assert len(store) == 0

    def test_a_consumed_task_is_no_longer_previewable(self, tools, mocker):
        mock_beets(mocker, make_match(mocker))
        task_id = tools["identify_untagged_items"]([1])["task_id"]
        token = tools["preview_items_match"](task_id)["preview_token"]
        assert tools["apply_change"](token)["status"] == "ok"

        result = tools["preview_items_match"](task_id)

        assert "no confirmable identification results" in result["error"]

    def test_reports_an_unreachable_backend(self, make_tools, mcp_config):
        result = make_tools(dead_client(mcp_config))["preview_items_match"]("t1")
        assert "could not reach the beetDeck API" in result["error"]


# ---------------------------------------------------------------------------
# apply_change — the items_identify kind
# ---------------------------------------------------------------------------


class TestApplyItemsMatch:
    def test_full_flow_creates_the_album_and_writes_the_match(self, tools, mocker):
        match = make_match(mocker)
        album, _ = mock_beets(mocker, match)
        task_id = tools["identify_untagged_items"]([1])["task_id"]
        token = tools["preview_items_match"](task_id)["preview_token"]

        result = tools["apply_change"](token)

        assert result == {
            "status": "ok",
            "detail": [],
            "kind": "items_identify",
            "target": f"untagged items task {task_id}",
        }
        match.apply_metadata.assert_called_once()
        match.apply_album_metadata.assert_called_once_with(album)
        album.store.assert_called_once()

    def test_a_file_write_failure_is_partial_not_failed(self, tools, mocker):
        """Documented contract: the DB side landed, only the file did not."""
        mock_beets(mocker, make_match(mocker, write_ok=False))
        task_id = tools["identify_untagged_items"]([1])["task_id"]
        token = tools["preview_items_match"](task_id)["preview_token"]

        result = tools["apply_change"](token)

        assert result["status"] == "partial"
        assert "file write failed" in result["detail"][0]
        assert "/music/loose/track.mp3" in result["detail"][0]

    def test_a_db_failure_rolls_back_and_is_failed(self, tools, mocker):
        add_album = mocker.MagicMock(side_effect=RuntimeError("disk is on fire"))
        mock_beets(mocker, make_match(mocker), add_album=add_album)
        task_id = tools["identify_untagged_items"]([1])["task_id"]
        token = tools["preview_items_match"](task_id)["preview_token"]

        result = tools["apply_change"](token)

        assert result["status"] == "failed"
        assert "Failed to create album" in result["detail"][0]
        assert "disk is on fire" in result["detail"][0]

    def test_a_token_commits_exactly_once(self, tools, mocker):
        mock_beets(mocker, make_match(mocker))
        task_id = tools["identify_untagged_items"]([1])["task_id"]
        token = tools["preview_items_match"](task_id)["preview_token"]
        assert tools["apply_change"](token)["status"] == "ok"

        result = tools["apply_change"](token)

        assert result["status"] == "failed"
        assert "already been applied" in result["detail"][0]

    def test_readonly_refuses_and_keeps_the_token(self, make_tools, store, mocker):
        mock_beets(mocker, make_match(mocker))
        readonly = make_tools(readonly=True)
        task_id = readonly["identify_untagged_items"]([1])["task_id"]
        token = readonly["preview_items_match"](task_id)["preview_token"]

        result = readonly["apply_change"](token)

        assert result["status"] == "failed"
        assert result["detail"] == [READONLY_REFUSAL]
        assert store.peek(token).kind == "items_identify"

    def test_a_second_confirm_of_the_same_task_is_refused_as_stale(self, tools, mocker):
        mock_beets(mocker, make_match(mocker))
        task_id = tools["identify_untagged_items"]([1])["task_id"]
        first = tools["preview_items_match"](task_id)["preview_token"]
        second = tools["preview_items_match"](task_id)["preview_token"]

        assert tools["apply_change"](first)["status"] == "ok"
        result = tools["apply_change"](second)

        assert result["status"] == "failed"
        assert "re-run, replaced or already applied" in result["detail"][0]

    def test_a_purged_task_is_reported_as_stale_not_as_not_found(self, tools, mocker):
        """Starting a second run purges the first task; a token minted against it
        must say the library moved, not that the task vanished."""
        mock_beets(mocker, make_match(mocker))
        first_task = tools["identify_untagged_items"]([1])["task_id"]
        token = tools["preview_items_match"](first_task)["preview_token"]

        second_task = tools["identify_untagged_items"]([2])["task_id"]
        assert second_task != first_task
        assert f"items_{first_task}" not in state.identify_tasks

        result = tools["apply_change"](token)

        assert result["status"] == "failed"
        assert "re-run, replaced or already applied" in result["detail"][0]
        assert "identify again" in result["detail"][0]
        assert "not found" not in result["detail"][0]
        # The live task survives the rejected confirm, so the agent can preview it.
        assert state.identify_tasks[f"items_{second_task}"]["_matches"]

    def test_reports_an_unreachable_backend(self, make_tools, mcp_config, mocker):
        mock_beets(mocker, make_match(mocker))
        tools = make_tools()
        task_id = tools["identify_untagged_items"]([1])["task_id"]
        token = tools["preview_items_match"](task_id)["preview_token"]

        result = make_tools(dead_client(mcp_config))["apply_change"](token)

        assert result["status"] == "failed"
        assert "could not reach the beetDeck API" in result["detail"][0]
