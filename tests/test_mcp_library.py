"""MCP library tools, driven against the real Flask app in-process.

Two things are stubbed, both because the container is not the point of these
tests: the rescan's `beet` subprocess (a `Popen` mock, as `tests/test_scan.py`
does) and the autobpm plugin (librosa on a real audio file, ~9 s per track).
Everything else — the routes, the beets `Library` behind the ignore toggle and
the SQLite rows it writes — is real.
"""

import io

import httpx
import pytest

from beetdeck import state
from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.tools.library import BPM_TIMEOUT, TOOL_NAMES, build_tools
from beetdeck_mcp.tools.read import build_tools as build_read_tools
from tests.conftest import insert_album, insert_item


@pytest.fixture()
def tools(beetdeck_client):
    """The registered library tools, bound to the in-process app."""
    return {tool.__name__: tool for tool in build_tools(beetdeck_client)}


def stub_tools(mcp_config, handler, **kwargs):
    """The tools bound to a hand-written backend response."""
    client = BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))
    return {tool.__name__: tool for tool in build_tools(client, **kwargs)}


def dead_tools(mcp_config):
    """The tools bound to a backend that refuses every connection."""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    return stub_tools(mcp_config, handler)


@pytest.fixture()
def scan_subprocess(mocker):
    """A `beet` subprocess that exits cleanly with no output."""
    proc = mocker.MagicMock()
    proc.returncode = 0
    proc.wait.return_value = None
    proc.stdout = io.BytesIO(b"")
    mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})
    mocker.patch("beetdeck.routes.scan._take_snapshot_db", return_value={})
    return mocker.patch("beetdeck.routes.scan.subprocess.Popen", return_value=proc)


def import_command(popen_mock):
    """The `beet import` argv, once the controller thread has reached it."""
    state.rescan_thread.join(timeout=5)
    return popen_mock.call_args_list[0].args[0]


class TestBuildTools:
    def test_exposes_exactly_the_declared_tool_names(self, tools):
        assert tuple(tools) == TOOL_NAMES


# ---------------------------------------------------------------------------
# rescan_library
# ---------------------------------------------------------------------------


class TestRescanLibrary:
    def test_starts_a_quick_scan_and_reads_the_run_id_back(self, tools, scan_subprocess):
        result = tools["rescan_library"]()
        assert result["status"] == "started"
        assert result["mode"] == "quick"
        # The POST does not carry it (documented API quirk) — it is read back
        # from the status endpoint, and it is the only handle on this run.
        assert result["run_id"] == state.rescan_run_id
        assert "notes" not in result

    def test_full_mode_reaches_the_importer(self, tools, scan_subprocess):
        assert tools["rescan_library"](mode="full")["mode"] == "full"
        assert "-I" in import_command(scan_subprocess)

    def test_quick_mode_reaches_the_importer(self, tools, scan_subprocess):
        tools["rescan_library"]()
        assert "-i" in import_command(scan_subprocess)

    def test_a_running_scan_is_reported_not_restarted(self, tools, mocker):
        popen = mocker.patch("beetdeck.routes.scan.subprocess.Popen")
        state.rescan_phase = "importing"

        result = tools["rescan_library"]()
        assert "already running" in result["error"]
        assert "get_rescan_status" in result["error"]
        assert "status" not in result
        popen.assert_not_called()

    def test_an_unreadable_run_id_is_a_note_not_a_failure(self, mcp_config):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={"status": "started", "mode": "quick"})
            return httpx.Response(500, json={"error": "boom"})

        result = stub_tools(mcp_config, handler)["rescan_library"]()
        assert result["status"] == "started"
        assert "run_id" not in result
        assert "get_rescan_status" in result["notes"][0]

    def test_reports_an_unreachable_backend(self, mcp_config):
        result = dead_tools(mcp_config)["rescan_library"]()
        assert "could not reach the beetDeck API" in result["error"]
        assert "status" not in result


# ---------------------------------------------------------------------------
# get_rescan_status
# ---------------------------------------------------------------------------


class TestGetRescanStatus:
    def test_idle_library(self, tools):
        result = tools["get_rescan_status"]()
        assert result["status"] == "idle"
        assert result["phase"] == "idle"
        assert "indeterminate" not in result

    def test_a_quick_scan_in_flight_is_indeterminate_not_zero(self, tools):
        state.rescan_phase = "importing"
        state.rescan_processed = 7
        state.rescan_total = None
        state.rescan_run_id = "run-1"

        result = tools["get_rescan_status"]()
        assert result["status"] == "running"
        assert result["processed"] == 7
        # A missing denominator must not read as "nothing to do".
        assert "total" not in result
        assert result["indeterminate"] is True
        assert result["run_id"] == "run-1"

    def test_a_full_scan_in_flight_carries_a_percentage(self, tools):
        state.rescan_phase = "importing"
        state.rescan_processed = 3
        state.rescan_total = 12
        state.rescan_current_item = "/music/Radiohead/Kid A"

        result = tools["get_rescan_status"]()
        assert result["total"] == 12
        assert result["percent"] == 25.0
        assert result["current_item"] == "/music/Radiohead/Kid A"
        assert "indeterminate" not in result

    def test_a_zero_total_does_not_divide(self, tools):
        state.rescan_phase = "importing"
        state.rescan_total = 0

        result = tools["get_rescan_status"]()
        assert result["total"] == 0
        assert "percent" not in result

    def test_a_finished_scan_carries_its_diff(self, tools):
        state.rescan_phase = "done"
        state.rescan_result = {
            "status": "done",
            "returncode": 0,
            "added": [{"id": 1, "title": "Idioteque", "artist": None}],
            "removed": [],
        }

        result = tools["get_rescan_status"]()
        assert result["status"] == "done"
        assert result["returncode"] == 0
        assert result["added_count"] == 1
        assert result["added"] == [{"id": 1, "title": "Idioteque"}]
        assert "removed" not in result
        assert "removed_count" not in result

    def test_an_error_scan_keeps_its_return_code(self, tools):
        state.rescan_phase = "error"
        state.rescan_result = {"status": "error", "returncode": 1, "added": [], "removed": []}

        result = tools["get_rescan_status"]()
        assert result["status"] == "error"
        assert result["returncode"] == 1

    def test_a_long_diff_is_truncated_with_an_exact_count(self, beetdeck_client):
        state.rescan_phase = "done"
        state.rescan_result = {
            "status": "done",
            "returncode": 0,
            "added": [{"id": index, "title": f"Track {index}"} for index in range(5)],
            "removed": [],
        }
        tools = {tool.__name__: tool for tool in build_tools(beetdeck_client, change_limit=2)}

        result = tools["get_rescan_status"]()
        assert result["added_count"] == 5
        assert len(result["added"]) == 2
        assert result["notes"] == ["added: listing the first 2 of 5"]

    def test_reports_an_unreachable_backend(self, mcp_config):
        result = dead_tools(mcp_config)["get_rescan_status"]()
        assert "could not reach the beetDeck API" in result["error"]
        assert "status" not in result


# ---------------------------------------------------------------------------
# compute_bpm
# ---------------------------------------------------------------------------


@pytest.fixture()
def bpm_plugin(mocker):
    """The autobpm plugin, stubbed: librosa is ~9 s of CPU per track."""

    def _install(bpm=128, album_id=1, item=None):
        def _set_bpm(items, write=True, force=True):
            for entry in items:
                entry.bpm = bpm

        track = item
        if track is None:
            track = mocker.MagicMock()
            track.album_id = album_id
            track.artist = "Radiohead"
            track.title = "Idioteque"
            track.bpm = None
            track.path = b"Radiohead/Kid A/01.mp3"

        lib = mocker.MagicMock()
        lib.get_item.return_value = track

        plugin = mocker.MagicMock()
        plugin.name = "autobpm"
        plugin.calculate_bpm.side_effect = _set_bpm

        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])
        return track, plugin

    return _install


class TestComputeBpm:
    def test_returns_the_computed_value(self, tools, bpm_plugin):
        _track, plugin = bpm_plugin(bpm=93)
        assert tools["compute_bpm"](1, 5) == {"album_id": 1, "item_id": 5, "bpm": 93}
        plugin.calculate_bpm.assert_called_once()

    def test_a_failed_computation_is_an_error_not_a_null_bpm(self, tools, mocker):
        track = mocker.MagicMock()
        track.album_id = 1
        track.artist = "Radiohead"
        track.title = "Idioteque"
        track.bpm = None
        track.path = b"Radiohead/Kid A/01.mp3"
        lib = mocker.MagicMock()
        lib.get_item.return_value = track
        plugin = mocker.MagicMock()
        plugin.name = "autobpm"
        # The plugin swallows librosa errors and leaves bpm unset — the handler
        # detects that by value and answers 400.
        plugin.calculate_bpm.side_effect = lambda *a, **kw: None
        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])

        result = tools["compute_bpm"](1, 5)
        assert "BPM computation failed" in result["error"]
        assert "bpm" not in result

    def test_an_item_from_another_album_is_not_found(self, tools, mocker):
        track = mocker.MagicMock()
        track.album_id = 99
        lib = mocker.MagicMock()
        lib.get_item.return_value = track
        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=lib)

        result = tools["compute_bpm"](1, 5)
        assert "Track not found" in result["error"]

    def test_an_unknown_item_is_not_found(self, tools, mocker):
        lib = mocker.MagicMock()
        lib.get_item.return_value = None
        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=lib)

        assert "Track not found" in tools["compute_bpm"](1, 4242)["error"]

    def test_a_success_without_a_value_is_reported_as_a_failure(self, mcp_config):
        def handler(request):
            return httpx.Response(200, json={"status": "ok"})

        result = stub_tools(mcp_config, handler)["compute_bpm"](1, 5)
        assert "returned no value" in result["error"]
        assert "bpm" not in result

    def test_uses_its_own_generous_timeout(self, mcp_config):
        seen = {}

        def handler(request):
            seen["timeout"] = request.extensions.get("timeout", {})
            return httpx.Response(200, json={"status": "ok", "bpm": 128})

        stub_tools(mcp_config, handler)["compute_bpm"](1, 5)
        # The 30 s client default would abort a slow computation mid-write.
        assert seen["timeout"]["read"] == BPM_TIMEOUT

    def test_reports_an_unreachable_backend(self, mcp_config):
        result = dead_tools(mcp_config)["compute_bpm"](1, 5)
        assert "could not reach the beetDeck API" in result["error"]
        assert "bpm" not in result


# ---------------------------------------------------------------------------
# toggle_album_ignored
# ---------------------------------------------------------------------------


class TestToggleAlbumIgnored:
    def test_toggles_the_flag_on_and_back_off(self, tools, db_path):
        album_id = insert_album(db_path, album="Bootleg", albumartist="Someone")
        insert_item(db_path, album_id)

        assert tools["toggle_album_ignored"](album_id) == {
            "album_id": album_id,
            "ignored": True,
        }
        assert tools["toggle_album_ignored"](album_id)["ignored"] is False

    def test_an_ignored_album_leaves_the_untagged_queue(self, tools, beetdeck_client, db_path):
        album_id = insert_album(db_path, album="Bootleg", albumartist="Someone")
        read = {tool.__name__: tool for tool in build_read_tools(beetdeck_client)}
        assert read["list_albums"](filter="untagged")["total"] == 1

        tools["toggle_album_ignored"](album_id)

        assert read["list_albums"](filter="untagged")["total"] == 0
        assert read["list_albums"](filter="ignored")["albums"][0]["id"] == album_id

    def test_unknown_album_is_an_error(self, tools):
        result = tools["toggle_album_ignored"](4242)
        assert "Album not found" in result["error"]
        assert "ignored" not in result

    def test_a_response_without_a_state_is_reported_as_unknown(self, mcp_config):
        def handler(request):
            return httpx.Response(200, json={})

        result = stub_tools(mcp_config, handler)["toggle_album_ignored"](1)
        assert "returned no state" in result["error"]
        assert "ignored" not in result

    def test_reports_an_unreachable_backend(self, mcp_config):
        result = dead_tools(mcp_config)["toggle_album_ignored"](1)
        assert "could not reach the beetDeck API" in result["error"]
        assert "ignored" not in result
