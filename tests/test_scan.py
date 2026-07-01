"""Tests for scan blueprint routes."""

import io
import os
import re
import sqlite3
import subprocess
import threading
import time

import beetdeck.state as state
from beetdeck.routes.scan import (
    _compute_scan_diff,
    _count_album_dirs,
    _make_log_path,
    _make_run_id,
    _normalize_absolute_paths,
    _run_tail_thread,
    _take_snapshot,
    _take_snapshot_db,
)
from tests.conftest import insert_album, insert_item


def _reset_scan_state():
    """Reset scan globals between tests."""
    state.rescan_proc = None
    state.rescan_snapshot = None
    state.rescan_run_id = None
    state.rescan_log_path = None
    state.rescan_phase = "idle"
    state.rescan_result = None
    state.rescan_thread = None
    state.rescan_processed = 0
    state.rescan_total = None
    state.rescan_current_item = None


def _make_mock_proc(mocker, returncode=0, output=b""):
    """Return a mock Popen object that completes with the given returncode.

    ``output`` is the bytes the controller's pump loop reads from ``stdout``
    (the merged stdout/stderr pipe); it is exposed via a real ``BytesIO`` so
    ``stream.readline`` behaves like a closed pipe once drained.
    """
    proc = mocker.MagicMock()
    proc.returncode = returncode
    proc.wait.return_value = None
    proc.stdout = io.BytesIO(output)
    return proc


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestMakeRunId:
    def test_returns_string(self):
        assert isinstance(_make_run_id(), str)

    def test_unique_across_calls(self):
        # Microsecond precision (-%f) must make every id distinct; collisions
        # would corrupt the per-run log filename and the staleness guard.
        ids = {_make_run_id() for _ in range(10)}
        assert len(ids) == 10

    def test_format(self):
        run_id = _make_run_id()
        # YYYYMMDD-HHMMSS-ffffff
        parts = run_id.split("-")
        assert len(parts) == 3
        assert len(parts[0]) == 8  # YYYYMMDD
        assert len(parts[1]) == 6  # HHMMSS


class TestMakeLogPath:
    def test_returns_path_under_scan_logs(self, tmp_path):
        db = str(tmp_path / "library.db")
        path = _make_log_path("20240101-120000-000000", db)
        assert path == str(tmp_path / "scan-logs" / "20240101-120000-000000.log")

    def test_creates_scan_logs_dir(self, tmp_path):
        db = str(tmp_path / "sub" / "library.db")
        os.makedirs(os.path.dirname(db))
        _make_log_path("run1", db)
        assert os.path.isdir(str(tmp_path / "sub" / "scan-logs"))

    def test_creates_dir_idempotent(self, tmp_path):
        db = str(tmp_path / "library.db")
        _make_log_path("run1", db)
        _make_log_path("run2", db)  # should not raise
        assert os.path.isdir(str(tmp_path / "scan-logs"))


class TestTakeSnapshot:
    def test_returns_empty_dict_for_empty_db(self, app, db_path):
        with app.app_context():
            snap = _take_snapshot()
        assert snap == {}

    def test_returns_items_from_db(self, app, db_path):
        album_id = insert_album(db_path)
        item_id = insert_item(db_path, album_id, title="Song A", artist="Band")
        with app.app_context():
            snap = _take_snapshot()
        assert item_id in snap
        title, artist, aid, path = snap[item_id]
        assert title == "Song A"
        assert artist == "Band"
        assert aid == album_id

    def test_snapshot_includes_path(self, app, db_path):
        album_id = insert_album(db_path)
        insert_item(db_path, album_id, path=b"/music/artist/album/track.mp3")
        with app.app_context():
            snap = _take_snapshot()
        paths = [data[3] for data in snap.values()]
        assert "/music/artist/album/track.mp3" in paths


class TestTakeSnapshotDb:
    def test_returns_empty_dict_for_empty_db(self, db_path):
        snap = _take_snapshot_db(db_path, "")
        assert snap == {}

    def test_returns_items_from_db(self, db_path):
        album_id = insert_album(db_path)
        item_id = insert_item(db_path, album_id, title="Song A", artist="Band")
        snap = _take_snapshot_db(db_path, "")
        assert item_id in snap
        title, artist, aid, path = snap[item_id]
        assert title == "Song A"
        assert artist == "Band"
        assert aid == album_id

    def test_resolves_relative_paths_against_root(self, db_path):
        album_id = insert_album(db_path)
        insert_item(db_path, album_id, path=b"artist/album/track.mp3")
        snap = _take_snapshot_db(db_path, "/music")
        paths = [data[3] for data in snap.values()]
        assert "/music/artist/album/track.mp3" in paths

    def test_returns_empty_on_missing_db(self, tmp_path):
        snap = _take_snapshot_db(str(tmp_path / "nonexistent.db"), "")
        assert snap == {}


class TestComputeScanDiff:
    # Helper: build a snapshot entry with path
    def _item(self, title, artist, album_id=10, path=""):
        return (title, artist, album_id, path)

    def test_no_changes(self):
        snap = {1: self._item("Song", "Artist", path="/music/song.mp3")}
        added, removed = _compute_scan_diff(snap, snap)
        assert added == []
        assert removed == []

    def test_no_changes_no_path(self):
        snap = {1: self._item("Song", "Artist")}
        added, removed = _compute_scan_diff(snap, snap)
        assert added == []
        assert removed == []

    def test_detects_added(self):
        before = {1: self._item("Old Song", "Artist", path="/music/old.mp3")}
        after = {
            1: self._item("Old Song", "Artist", path="/music/old.mp3"),
            2: self._item("New Song", "New Artist", path="/music/new.mp3"),
        }
        added, removed = _compute_scan_diff(before, after)
        assert len(added) == 1
        assert added[0]["id"] == 2
        assert added[0]["title"] == "New Song"
        assert added[0]["artist"] == "New Artist"
        assert removed == []

    def test_detects_removed(self):
        before = {
            1: self._item("Song A", "Artist", path="/music/a.mp3"),
            2: self._item("Song B", "Artist", path="/music/b.mp3"),
        }
        after = {1: self._item("Song A", "Artist", path="/music/a.mp3")}
        added, removed = _compute_scan_diff(before, after)
        assert added == []
        assert len(removed) == 1
        assert removed[0]["id"] == 2
        assert removed[0]["title"] == "Song B"

    def test_detects_added_and_removed(self):
        before = {1: self._item("Gone", "Artist", path="/music/gone.mp3")}
        after = {2: self._item("New", "Artist", path="/music/new.mp3")}
        added, removed = _compute_scan_diff(before, after)
        assert len(added) == 1
        assert added[0]["id"] == 2
        assert len(removed) == 1
        assert removed[0]["id"] == 1

    def test_id_reassignment_same_path_shows_no_change(self):
        """When beet import re-inserts items with new IDs for same paths, no diff."""
        before = {
            1: self._item("Track A", "Artist", path="/music/a.mp3"),
            2: self._item("Track B", "Artist", path="/music/b.mp3"),
        }
        # Same paths, different IDs (simulates beet import re-inserting items)
        after = {
            10: self._item("Track A", "Artist", path="/music/a.mp3"),
            20: self._item("Track B", "Artist", path="/music/b.mp3"),
        }
        added, removed = _compute_scan_diff(before, after)
        assert added == []
        assert removed == []

    def test_mixed_scenario(self):
        """Some items reassigned (same path), one truly removed, one truly added."""
        before = {
            1: self._item("Stays", "Artist", path="/music/stays.mp3"),
            2: self._item("Gone", "Artist", path="/music/gone.mp3"),
        }
        after = {
            10: self._item("Stays", "Artist", path="/music/stays.mp3"),
            3: self._item("New", "Artist", path="/music/new.mp3"),
        }
        added, removed = _compute_scan_diff(before, after)
        assert len(added) == 1
        assert added[0]["id"] == 3
        assert added[0]["title"] == "New"
        assert len(removed) == 1
        assert removed[0]["id"] == 2
        assert removed[0]["title"] == "Gone"

    def test_added_sorted_by_id(self):
        before = {}
        after = {
            3: self._item("C", "A", path="/music/c.mp3"),
            1: self._item("A", "A", path="/music/a.mp3"),
            2: self._item("B", "A", path="/music/b.mp3"),
        }
        added, _ = _compute_scan_diff(before, after)
        ids = [item["id"] for item in added]
        assert ids == sorted(ids)

    def test_multidisc_id_churn_no_diff(self):
        """Multi-disc rescan: beet import may re-insert items with new IDs but
        the on-disk paths under CD1/ and CD2/ stay stable. Path-based matching
        must treat these as unchanged.
        """
        before = {
            11: self._item(
                "Song A", "Pink Floyd", album_id=5, path="/music/Pink Floyd/The Wall/CD1/01.mp3"
            ),
            12: self._item(
                "Song B", "Pink Floyd", album_id=5, path="/music/Pink Floyd/The Wall/CD2/01.mp3"
            ),
        }
        after = {
            101: self._item(
                "Song A", "Pink Floyd", album_id=7, path="/music/Pink Floyd/The Wall/CD1/01.mp3"
            ),
            102: self._item(
                "Song B", "Pink Floyd", album_id=7, path="/music/Pink Floyd/The Wall/CD2/01.mp3"
            ),
        }
        added, removed = _compute_scan_diff(before, after)
        assert added == []
        assert removed == []

    def test_removed_sorted_by_id(self):
        before = {
            3: self._item("C", "A", path="/music/c.mp3"),
            1: self._item("A", "A", path="/music/a.mp3"),
            2: self._item("B", "A", path="/music/b.mp3"),
        }
        after = {}
        _, removed = _compute_scan_diff(before, after)
        ids = [item["id"] for item in removed]
        assert ids == sorted(ids)


class TestNormalizeAbsolutePaths:
    def test_rewrites_items_under_root(self, db_path):
        album_id = insert_album(db_path, artpath=b"/music/art/cover.jpg")
        iid = insert_item(db_path, album_id, path=b"/music/artist/album/track.mp3")
        updated = _normalize_absolute_paths(db_path, "/music")
        assert updated == 2
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT path FROM items WHERE id = ?", (iid,)).fetchone()
            assert row[0] == b"artist/album/track.mp3"
            art = conn.execute("SELECT artpath FROM albums WHERE id = ?", (album_id,)).fetchone()
            assert art[0] == b"art/cover.jpg"
        finally:
            conn.close()

    def test_leaves_already_relative_paths_alone(self, db_path):
        album_id = insert_album(db_path)
        iid = insert_item(db_path, album_id, path=b"already/relative.mp3")
        updated = _normalize_absolute_paths(db_path, "/music")
        assert updated == 0
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT path FROM items WHERE id = ?", (iid,)).fetchone()
            assert row[0] == b"already/relative.mp3"
        finally:
            conn.close()

    def test_leaves_paths_outside_root_alone(self, db_path):
        album_id = insert_album(db_path)
        iid = insert_item(db_path, album_id, path=b"/other/place/track.mp3")
        updated = _normalize_absolute_paths(db_path, "/music")
        assert updated == 0
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT path FROM items WHERE id = ?", (iid,)).fetchone()
            assert row[0] == b"/other/place/track.mp3"
        finally:
            conn.close()

    def test_empty_root_is_noop(self, db_path):
        album_id = insert_album(db_path)
        insert_item(db_path, album_id, path=b"/music/track.mp3")
        assert _normalize_absolute_paths(db_path, "") == 0

    def test_missing_schema_is_noop(self, tmp_path):
        """Fresh library: DB file has no items/albums tables yet (beets creates
        them only on the first import). Preflight must skip, not crash."""
        fresh_db = str(tmp_path / "library.db")
        # sqlite3.connect creates an empty schema-less DB file, like the 0-byte
        # library.db a fresh deployment leaves before the first beet import.
        sqlite3.connect(fresh_db).close()
        assert _normalize_absolute_paths(fresh_db, "/music") == 0


# ---------------------------------------------------------------------------
# POST /api/rescan
# ---------------------------------------------------------------------------


class TestRescan:
    def setup_method(self):
        _reset_scan_state()

    def test_starts_rescan_quick_mode(self, client, mocker):
        mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})

        resp = client.post("/api/rescan")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "started"
        assert data["mode"] == "quick"

    def test_starts_rescan_full_mode(self, client, mocker):
        popen_mock = mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})

        resp = client.post("/api/rescan?mode=full")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "full"

        # Wait for controller and verify -I flag was used in import command
        state.rescan_thread.join(timeout=5)
        import_call_args = popen_mock.call_args_list[0][0][0]
        assert "-I" in import_call_args

    def test_quick_mode_uses_incremental_flag(self, client, mocker):
        popen_mock = mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})

        client.post("/api/rescan?mode=quick")
        state.rescan_thread.join(timeout=5)
        import_call_args = popen_mock.call_args_list[0][0][0]
        assert "-i" in import_call_args

    def test_returns_422_for_invalid_mode(self, client):
        """Invalid `mode` is now rejected by spectree (Literal) with a 422,
        replacing the handler's old ad-hoc 400 check."""
        resp = client.post("/api/rescan?mode=bogus")
        assert resp.status_code == 422
        body = resp.get_json()
        # Task 11: uniform ErrorResponse body {error, detail:[{loc,msg,type}]}.
        assert body["error"] == "Validation failed"
        assert body["detail"] and {"loc", "msg", "type"} <= set(body["detail"][0])

    def test_unknown_query_param_returns_422(self, client):
        """An unknown query param is rejected with 422 (extra=forbid)."""
        resp = client.post("/api/rescan?bogus=1")
        assert resp.status_code == 422

    def test_returns_409_when_already_running(self, client):
        state.rescan_phase = "importing"

        resp = client.post("/api/rescan")
        assert resp.status_code == 409
        assert resp.get_json()["status"] == "running"

    def test_returns_409_when_in_update_phase(self, client):
        state.rescan_phase = "updating"

        resp = client.post("/api/rescan")
        assert resp.status_code == 409
        assert resp.get_json()["status"] == "running"

    def test_allows_new_scan_after_previous_finished(self, client, mocker):
        state.rescan_phase = "done"
        state.rescan_result = {"status": "done", "returncode": 0, "added": [], "removed": []}

        mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})

        resp = client.post("/api/rescan")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"

    def test_allows_new_scan_after_error(self, client, mocker):
        state.rescan_phase = "error"
        state.rescan_result = {"status": "error", "returncode": 1, "added": [], "removed": []}

        mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})

        resp = client.post("/api/rescan")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"

    def test_takes_snapshot_before_starting(self, client, mocker):
        mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        snap = {1: ("Song", "Artist", 10, "/music/song.mp3")}
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value=snap)

        client.post("/api/rescan")
        assert state.rescan_snapshot == snap

    def test_sets_run_id_on_start(self, client, mocker):
        mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})

        client.post("/api/rescan")
        assert state.rescan_run_id is not None
        assert isinstance(state.rescan_run_id, str)

    def test_sets_log_path_on_start(self, client, mocker):
        mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})

        client.post("/api/rescan")
        assert state.rescan_log_path is not None
        assert state.rescan_log_path.endswith(".log")


# ---------------------------------------------------------------------------
# GET /api/rescan/status
# ---------------------------------------------------------------------------


class TestRescanStatus:
    def setup_method(self):
        _reset_scan_state()

    def test_returns_idle_when_phase_idle(self, client):
        resp = client.get("/api/rescan/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "idle"

    def test_returns_running_when_importing(self, client):
        state.rescan_phase = "importing"
        resp = client.get("/api/rescan/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"

    def test_returns_running_when_updating(self, client):
        state.rescan_phase = "updating"
        resp = client.get("/api/rescan/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"

    def test_returns_done_with_returncode_when_finished(self, client):
        state.rescan_phase = "done"
        state.rescan_result = {"status": "done", "returncode": 0, "added": [], "removed": []}

        resp = client.get("/api/rescan/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "done"
        assert data["returncode"] == 0

    def test_returns_diff_in_result(self, client):
        state.rescan_phase = "done"
        state.rescan_result = {
            "status": "done",
            "returncode": 0,
            "added": [{"id": 2, "title": "New Song", "artist": "Artist"}],
            "removed": [],
        }

        resp = client.get("/api/rescan/status")
        data = resp.get_json()
        assert data["status"] == "done"
        assert len(data["added"]) == 1
        assert data["added"][0]["id"] == 2
        assert data["removed"] == []

    def test_returns_nonzero_returncode_on_failure(self, client):
        state.rescan_phase = "error"
        state.rescan_result = {"status": "error", "returncode": 1, "added": [], "removed": []}

        resp = client.get("/api/rescan/status")
        data = resp.get_json()
        assert data["returncode"] == 1
        assert data["status"] == "error"

    def test_persists_result_on_repeated_poll(self, client):
        state.rescan_phase = "done"
        state.rescan_result = {"status": "done", "returncode": 0, "added": [], "removed": []}

        resp1 = client.get("/api/rescan/status")
        resp2 = client.get("/api/rescan/status")
        assert resp1.get_json()["status"] == "done"
        assert resp2.get_json()["status"] == "done"

    def test_running_includes_phase_and_progress_fields(self, client):
        state.rescan_phase = "importing"
        state.rescan_processed = 3
        state.rescan_total = 10
        state.rescan_current_item = "/music/Artist/Album"
        state.rescan_run_id = "20240101-120000-000000"

        data = client.get("/api/rescan/status").get_json()
        assert data["status"] == "running"
        assert data["phase"] == "importing"
        assert data["processed"] == 3
        assert data["total"] == 10
        assert data["current_item"] == "/music/Artist/Album"
        assert data["run_id"] == "20240101-120000-000000"

    def test_running_full_total_set(self, client):
        state.rescan_phase = "importing"
        state.rescan_total = 7

        data = client.get("/api/rescan/status").get_json()
        assert data["total"] == 7

    def test_running_quick_total_is_null(self, client):
        state.rescan_phase = "importing"
        state.rescan_total = None

        data = client.get("/api/rescan/status").get_json()
        assert data["total"] is None

    def test_updating_phase_includes_phase_field(self, client):
        state.rescan_phase = "updating"
        state.rescan_processed = 5
        state.rescan_total = 5

        data = client.get("/api/rescan/status").get_json()
        assert data["status"] == "running"
        assert data["phase"] == "updating"
        assert data["processed"] == 5

    def test_done_status_includes_run_id(self, client):
        state.rescan_phase = "done"
        state.rescan_result = {"status": "done", "returncode": 0, "added": [], "removed": []}
        state.rescan_run_id = "test-run-123"
        state.rescan_processed = 8
        state.rescan_total = 8

        data = client.get("/api/rescan/status").get_json()
        assert data["run_id"] == "test-run-123"
        assert data["phase"] == "done"
        assert data["processed"] == 8
        assert data["total"] == 8

    def test_error_phase_persists_with_progress_fields(self, client):
        state.rescan_phase = "error"
        state.rescan_result = {"status": "error", "returncode": 1, "added": [], "removed": []}
        state.rescan_run_id = "err-run-456"

        data = client.get("/api/rescan/status").get_json()
        assert data["status"] == "error"
        assert data["phase"] == "error"
        assert data["run_id"] == "err-run-456"

    def test_idle_response_includes_phase_field(self, client):
        data = client.get("/api/rescan/status").get_json()
        assert data["status"] == "idle"
        assert data["phase"] == "idle"


# ---------------------------------------------------------------------------
# POST /api/rescan/dismiss
# ---------------------------------------------------------------------------


class TestRescanDismiss:
    def setup_method(self):
        _reset_scan_state()

    def test_dismiss_done_returns_ok(self, client):
        state.rescan_phase = "done"
        state.rescan_result = {"status": "done", "returncode": 0, "added": [], "removed": []}

        resp = client.post("/api/rescan/dismiss")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_dismiss_done_sets_idle(self, client):
        state.rescan_phase = "done"
        state.rescan_result = {"status": "done", "returncode": 0, "added": [], "removed": []}

        client.post("/api/rescan/dismiss")

        data = client.get("/api/rescan/status").get_json()
        assert data["status"] == "idle"

    def test_dismiss_error_sets_idle(self, client):
        state.rescan_phase = "error"
        state.rescan_result = {"status": "error", "returncode": 1, "added": [], "removed": []}

        client.post("/api/rescan/dismiss")

        data = client.get("/api/rescan/status").get_json()
        assert data["status"] == "idle"

    def test_dismiss_when_idle_is_noop(self, client):
        resp = client.post("/api/rescan/dismiss")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

        data = client.get("/api/rescan/status").get_json()
        assert data["status"] == "idle"

    def test_dismiss_is_idempotent(self, client):
        state.rescan_phase = "done"
        state.rescan_result = {"status": "done", "returncode": 0, "added": [], "removed": []}

        resp1 = client.post("/api/rescan/dismiss")
        resp2 = client.post("/api/rescan/dismiss")
        assert resp1.status_code == 200
        assert resp2.status_code == 200

        data = client.get("/api/rescan/status").get_json()
        assert data["status"] == "idle"

    def test_dismiss_does_not_affect_running_scan(self, client):
        state.rescan_phase = "importing"

        client.post("/api/rescan/dismiss")

        assert state.rescan_phase == "importing"
        data = client.get("/api/rescan/status").get_json()
        assert data["status"] == "running"

    def test_dismiss_clears_result_from_state(self, client):
        state.rescan_phase = "done"
        state.rescan_result = {"status": "done", "returncode": 0, "added": [], "removed": []}

        client.post("/api/rescan/dismiss")

        assert state.rescan_phase == "idle"
        assert state.rescan_result is None

    def test_dismiss_done_repeated_polls_return_idle(self, client):
        state.rescan_phase = "done"
        state.rescan_result = {"status": "done", "returncode": 0, "added": [], "removed": []}

        client.post("/api/rescan/dismiss")

        for _ in range(3):
            data = client.get("/api/rescan/status").get_json()
            assert data["status"] == "idle"

    def test_new_scan_allowed_after_dismiss(self, client, mocker):
        state.rescan_phase = "done"
        state.rescan_result = {"status": "done", "returncode": 0, "added": [], "removed": []}

        client.post("/api/rescan/dismiss")

        mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})

        resp = client.post("/api/rescan")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"

    def test_dismiss_documented_in_openapi(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        assert "/api/rescan/dismiss" in spec["paths"]
        assert "post" in spec["paths"]["/api/rescan/dismiss"]


# ---------------------------------------------------------------------------
# Controller thread integration tests
# ---------------------------------------------------------------------------


class TestScanController:
    def setup_method(self):
        _reset_scan_state()

    # after_snap: snapshot returned by _take_snapshot_db (post-import state).
    # Defaults to {} (empty — no diff, update skipped).  Pass a non-empty dict
    # to simulate items being added/removed so the update step is triggered.
    def _start_and_wait(self, client, mocker, returncode=0, mode="quick", after_snap=None):
        """Start a scan with a mock Popen and wait for the controller to finish."""
        proc = _make_mock_proc(mocker, returncode=returncode)
        popen_mock = mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=proc,
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})
        mocker.patch(
            "beetdeck.routes.scan._take_snapshot_db",
            return_value=after_snap if after_snap is not None else {},
        )

        resp = client.post(f"/api/rescan?mode={mode}")
        assert resp.status_code == 200
        state.rescan_thread.join(timeout=5)
        return popen_mock, resp

    # Non-empty diff so the update Popen fires.
    _AFTER_SNAP_WITH_ITEM = {42: ("New Track", "Artist", 1, "/music/new.mp3")}

    def test_import_then_update_sequence_runs(self, client, mocker):
        popen_mock, _ = self._start_and_wait(client, mocker, after_snap=self._AFTER_SNAP_WITH_ITEM)
        assert popen_mock.call_count == 2
        import_cmd = popen_mock.call_args_list[0][0][0]
        update_cmd = popen_mock.call_args_list[1][0][0]
        assert "import" in import_cmd
        assert "update" in update_cmd

    def test_import_cmd_includes_beet_and_flags(self, client, mocker):
        popen_mock, _ = self._start_and_wait(client, mocker, mode="quick")
        import_cmd = popen_mock.call_args_list[0][0][0]
        assert import_cmd[0] == "beet"
        assert "-v" in import_cmd
        assert "import" in import_cmd
        assert "-i" in import_cmd  # quick mode

    def test_full_mode_uses_capital_i_flag(self, client, mocker):
        popen_mock, _ = self._start_and_wait(client, mocker, mode="full")
        import_cmd = popen_mock.call_args_list[0][0][0]
        assert "-I" in import_cmd

    def test_log_file_created(self, client, mocker):
        self._start_and_wait(client, mocker)
        assert state.rescan_log_path is not None
        assert os.path.isfile(state.rescan_log_path)

    def test_popen_pipes_stdout_with_stderr_merged(self, client, mocker):
        popen_mock, _ = self._start_and_wait(client, mocker)
        # Both Popen calls must pipe stdout (so the controller can timestamp each
        # line into the run-log file) with stderr merged in, otherwise the
        # verbose output the progress parser depends on is lost.
        assert popen_mock.call_args_list, "expected at least one Popen call"
        for call in popen_mock.call_args_list:
            assert call.kwargs.get("stdout") == subprocess.PIPE
            assert call.kwargs.get("stderr") == subprocess.STDOUT

    def test_log_lines_are_timestamped(self, client, mocker):
        # The controller stamps every subprocess output line with a
        # "[YYYY-MM-DD HH:MM:SS] " prefix as it copies it into the run log.
        proc = _make_mock_proc(
            mocker,
            output=b"Sending event: import_task_created\n/music/Artist/Album\n",
        )
        mocker.patch("beetdeck.routes.scan.subprocess.Popen", return_value=proc)
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})
        mocker.patch("beetdeck.routes.scan._take_snapshot_db", return_value={})

        # Quick mode leaves total=None so the processed counter is not clamped
        # to a pre-counted album total (0 for the test import dir).
        resp = client.post("/api/rescan?mode=quick")
        assert resp.status_code == 200
        state.rescan_thread.join(timeout=5)

        with open(state.rescan_log_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        ts_re = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
        assert lines, "expected log content"
        for line in lines:
            assert ts_re.match(line), f"line not timestamped: {line!r}"
        # Original beets text is preserved after the prefix.
        assert lines[0].endswith("Sending event: import_task_created")
        assert lines[1].endswith("/music/Artist/Album")
        # And the tail parser still tracks progress through the prefix: the
        # import_task_created marker incremented the processed counter.
        assert state.rescan_processed == 1
        assert state.rescan_current_item == "/music/Artist/Album"

    def test_status_transitions_to_done(self, client, mocker):
        self._start_and_wait(client, mocker)
        assert state.rescan_phase == "done"
        assert state.rescan_result is not None
        assert state.rescan_result["status"] == "done"
        assert state.rescan_result["returncode"] == 0

    def test_status_result_contains_diff(self, client, mocker, db_path):
        mocker.patch("beetdeck.routes.scan.subprocess.Popen", return_value=_make_mock_proc(mocker))
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})
        # Simulate a new item appearing after import
        after_snap = {42: ("New Track", "Artist", 1, "/music/new.mp3")}
        mocker.patch("beetdeck.routes.scan._take_snapshot_db", return_value=after_snap)

        client.post("/api/rescan")
        state.rescan_thread.join(timeout=5)

        assert state.rescan_result["added"] == [
            {"id": 42, "title": "New Track", "artist": "Artist"}
        ]
        assert state.rescan_result["removed"] == []

    def test_second_concurrent_start_returns_409(self, client, mocker):
        # Simulate in-progress scan without starting a real thread
        state.rescan_phase = "importing"

        resp = client.post("/api/rescan")
        assert resp.status_code == 409
        assert resp.get_json()["status"] == "running"

    def test_import_nonzero_return_sets_error_phase(self, client, mocker):
        self._start_and_wait(client, mocker, returncode=1)
        assert state.rescan_phase == "error"
        assert state.rescan_result["status"] == "error"
        assert state.rescan_result["returncode"] == 1

    def test_import_failure_skips_update(self, client, mocker):
        popen_mock, _ = self._start_and_wait(client, mocker, returncode=1)
        # Only import Popen called, update is skipped
        assert popen_mock.call_count == 1

    def test_log_dir_created_when_missing(self, client, mocker, tmp_path, app):
        """Log dir under library_db parent is created on demand by _make_log_path."""
        mocker.patch("beetdeck.routes.scan.subprocess.Popen", return_value=_make_mock_proc(mocker))
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})
        mocker.patch("beetdeck.routes.scan._take_snapshot_db", return_value={})

        with app.app_context():
            from flask import current_app

            library_db = current_app.config["LIBRARY_DB"]
            log_dir = os.path.join(os.path.dirname(library_db), "scan-logs")

        # Ensure the dir doesn't exist yet
        import shutil

        if os.path.isdir(log_dir):
            shutil.rmtree(log_dir)

        resp = client.post("/api/rescan")
        assert resp.status_code == 200
        assert os.path.isdir(log_dir)

    def test_update_failure_sets_error_phase(self, client, mocker):
        import_proc = _make_mock_proc(mocker, returncode=0)
        update_proc = _make_mock_proc(mocker, returncode=2)
        mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            side_effect=[import_proc, update_proc],
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})
        # Non-empty diff so the update Popen is actually invoked.
        mocker.patch(
            "beetdeck.routes.scan._take_snapshot_db",
            return_value={42: ("Track", "Artist", 1, "/music/new.mp3")},
        )

        client.post("/api/rescan")
        state.rescan_thread.join(timeout=5)

        assert state.rescan_phase == "error"
        assert state.rescan_result["returncode"] == 2


# ---------------------------------------------------------------------------
# Task 6: conditional update (update only when diff is non-empty)
# ---------------------------------------------------------------------------


class TestConditionalUpdate:
    def setup_method(self):
        _reset_scan_state()

    def _start_and_wait(self, client, mocker, import_rc=0, update_rc=0, after_snap=None):
        """Start a scan and block until the controller thread finishes.

        after_snap: the value returned by _take_snapshot_db (post-import DB
        state).  Empty dict → no diff → update skipped.  Non-empty dict → diff
        detected → update runs.
        """
        import_proc = _make_mock_proc(mocker, returncode=import_rc)
        update_proc = _make_mock_proc(mocker, returncode=update_rc)
        popen_mock = mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            side_effect=[import_proc, update_proc],
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})
        mocker.patch(
            "beetdeck.routes.scan._take_snapshot_db",
            return_value=after_snap if after_snap is not None else {},
        )
        resp = client.post("/api/rescan")
        assert resp.status_code == 200
        state.rescan_thread.join(timeout=5)
        return popen_mock

    _AFTER_SNAP = {42: ("Track", "Artist", 1, "/music/new.mp3")}

    def test_update_runs_when_items_added(self, client, mocker):
        popen_mock = self._start_and_wait(client, mocker, after_snap=self._AFTER_SNAP)
        assert popen_mock.call_count == 2
        update_cmd = popen_mock.call_args_list[1][0][0]
        assert "update" in update_cmd

    def test_update_runs_when_items_removed(self, client, mocker):
        # pre_snap has an item; post_snap is empty → item removed
        popen_mock = mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch(
            "beetdeck.routes.scan._take_snapshot",
            return_value={1: ("Gone", "Artist", 1, "/music/gone.mp3")},
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot_db", return_value={})
        resp = client.post("/api/rescan")
        assert resp.status_code == 200
        state.rescan_thread.join(timeout=5)
        # The removal must be detected and the update step must run (2 Popen calls).
        assert state.rescan_phase == "done"
        assert state.rescan_result["removed"] == [{"id": 1, "title": "Gone", "artist": "Artist"}]
        assert state.rescan_result["added"] == []
        assert popen_mock.call_count == 2
        assert "update" in popen_mock.call_args_list[1][0][0]

    def test_update_skipped_when_no_diff(self, client, mocker):
        # Both snapshots empty → diff is empty → update must not run
        popen_mock = self._start_and_wait(client, mocker, after_snap={})
        assert popen_mock.call_count == 1
        only_cmd = popen_mock.call_args_list[0][0][0]
        assert "import" in only_cmd

    def test_noop_scan_reaches_done(self, client, mocker):
        # No diff → update skipped; status must still reach done with rc=0
        self._start_and_wait(client, mocker, after_snap={})
        assert state.rescan_phase == "done"
        assert state.rescan_result["status"] == "done"
        assert state.rescan_result["returncode"] == 0

    def test_noop_scan_result_has_empty_diff(self, client, mocker):
        self._start_and_wait(client, mocker, after_snap={})
        assert state.rescan_result["added"] == []
        assert state.rescan_result["removed"] == []

    def test_phase_updating_set_only_when_update_runs(self, client, mocker):
        # No diff: controller never sets phase to "updating", ends at "done".
        self._start_and_wait(client, mocker, after_snap={})
        assert state.rescan_phase == "done"
        # With a diff: update runs, still ends at "done".
        _reset_scan_state()
        popen_mock2 = self._start_and_wait(client, mocker, after_snap=self._AFTER_SNAP)
        assert popen_mock2.call_count == 2
        assert state.rescan_phase == "done"

    def test_update_failure_propagates_error_when_diff(self, client, mocker):
        popen_mock = self._start_and_wait(
            client, mocker, import_rc=0, update_rc=3, after_snap=self._AFTER_SNAP
        )
        assert popen_mock.call_count == 2
        assert state.rescan_phase == "error"
        assert state.rescan_result["returncode"] == 3


# ---------------------------------------------------------------------------
# GET /api/rescan/log
# ---------------------------------------------------------------------------


class TestRescanLog:
    def setup_method(self):
        _reset_scan_state()

    def _write_log(self, app, content: bytes) -> tuple[str, str]:
        """Write a log file under the app's scan-logs dir; return (run_id, log_path)."""
        from flask import current_app

        with app.app_context():
            library_db = current_app.config["LIBRARY_DB"]
        import os

        log_dir = os.path.join(os.path.dirname(library_db), "scan-logs")
        os.makedirs(log_dir, exist_ok=True)
        run_id = "test-log-run-001"
        log_path = os.path.join(log_dir, f"{run_id}.log")
        with open(log_path, "wb") as f:
            f.write(content)
        return run_id, log_path

    def test_full_read_offset_zero(self, client, app):
        run_id, _ = self._write_log(app, b"line1\nline2\n")
        resp = client.get(f"/api/rescan/log?run_id={run_id}&offset=0")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["text"] == "line1\nline2\n"
        assert data["offset"] == 12

    def test_incremental_read_from_offset(self, client, app):
        run_id, _ = self._write_log(app, b"AAAA\nBBBB\n")
        # First fetch
        resp1 = client.get(f"/api/rescan/log?run_id={run_id}&offset=0")
        data1 = resp1.get_json()
        assert data1["text"] == "AAAA\nBBBB\n"
        new_offset = data1["offset"]
        # Second fetch from end → empty
        resp2 = client.get(f"/api/rescan/log?run_id={run_id}&offset={new_offset}")
        data2 = resp2.get_json()
        assert data2["text"] == ""
        assert data2["offset"] == new_offset

    def test_incremental_read_partial(self, client, app):
        run_id, _ = self._write_log(app, b"Hello World")
        # Read first 5 bytes
        resp = client.get(f"/api/rescan/log?run_id={run_id}&offset=5")
        data = resp.get_json()
        assert data["text"] == " World"
        assert data["offset"] == 11

    def test_unknown_run_id_returns_404(self, client):
        resp = client.get("/api/rescan/log?run_id=nonexistent-run-id-999")
        assert resp.status_code == 404
        assert "error" in resp.get_json()

    def test_offset_past_eof_returns_empty_text_same_offset(self, client, app):
        run_id, _ = self._write_log(app, b"short")
        resp = client.get(f"/api/rescan/log?run_id={run_id}&offset=9999")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["text"] == ""
        assert data["offset"] == 9999

    def test_default_offset_is_zero(self, client, app):
        run_id, _ = self._write_log(app, b"abc")
        resp = client.get(f"/api/rescan/log?run_id={run_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["text"] == "abc"
        assert data["offset"] == 3

    def test_negative_offset_returns_422(self, client, app):
        run_id, _ = self._write_log(app, b"abc")
        resp = client.get(f"/api/rescan/log?run_id={run_id}&offset=-1")
        assert resp.status_code == 422

    def test_path_traversal_run_id_returns_404(self, client):
        resp = client.get("/api/rescan/log?run_id=../../etc/passwd")
        assert resp.status_code == 404

    def test_log_documented_in_openapi(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        assert "/api/rescan/log" in spec["paths"]
        assert "get" in spec["paths"]["/api/rescan/log"]

    def test_missing_run_id_param_returns_422(self, client):
        resp = client.get("/api/rescan/log")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# OpenAPI spec coverage
# ---------------------------------------------------------------------------


class TestScanOpenAPISpec:
    def setup_method(self):
        _reset_scan_state()

    def test_both_routes_documented_and_tagged(self, client):
        """Both scan routes appear in the spec under the `scan` tag."""
        spec = client.get("/apidoc/openapi.json").get_json()
        paths = spec["paths"]

        rescan = paths["/api/rescan"]["post"]
        assert "scan" in rescan["tags"]
        status = paths["/api/rescan/status"]["get"]
        assert "scan" in status["tags"]

    def test_rescan_documents_mode_query_param(self, client):
        """`POST /api/rescan` documents the `mode` query parameter."""
        spec = client.get("/apidoc/openapi.json").get_json()
        params = spec["paths"]["/api/rescan"]["post"].get("parameters", [])
        names = {p["name"] for p in params if p.get("in") == "query"}
        assert "mode" in names

    def test_rescan_documents_422(self, client):
        """The validated rescan endpoint documents a 422 response."""
        spec = client.get("/apidoc/openapi.json").get_json()
        assert "422" in spec["paths"]["/api/rescan"]["post"]["responses"]

    def test_default_mode_when_param_missing(self, client, mocker):
        """Missing `mode` still defaults to quick (behavior unchanged)."""
        mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})

        resp = client.post("/api/rescan")
        assert resp.status_code == 200
        assert resp.get_json()["mode"] == "quick"


# ---------------------------------------------------------------------------
# _count_album_dirs helper
# ---------------------------------------------------------------------------


class TestCountAlbumDirs:
    def test_returns_zero_for_missing_dir(self, tmp_path):
        assert _count_album_dirs(str(tmp_path / "nonexistent")) == 0

    def test_returns_zero_for_empty_dir(self, tmp_path):
        assert _count_album_dirs(str(tmp_path)) == 0

    def test_counts_dirs_with_audio_files(self, tmp_path):
        album = tmp_path / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.mp3").write_bytes(b"")
        assert _count_album_dirs(str(tmp_path)) == 1

    def test_counts_multiple_album_dirs(self, tmp_path):
        for name in ("AlbumA", "AlbumB", "AlbumC"):
            d = tmp_path / "Artist" / name
            d.mkdir(parents=True)
            (d / "01.flac").write_bytes(b"")
        assert _count_album_dirs(str(tmp_path)) == 3

    def test_ignores_non_audio_files(self, tmp_path):
        d = tmp_path / "Artist" / "Album"
        d.mkdir(parents=True)
        (d / "cover.jpg").write_bytes(b"")
        (d / "info.txt").write_bytes(b"")
        assert _count_album_dirs(str(tmp_path)) == 0

    def test_counts_dir_only_once_even_with_multiple_audio_files(self, tmp_path):
        d = tmp_path / "Album"
        d.mkdir()
        for name in ("01.mp3", "02.mp3", "03.flac"):
            (d / name).write_bytes(b"")
        assert _count_album_dirs(str(tmp_path)) == 1

    def test_recognises_all_supported_extensions(self, tmp_path):
        exts = [".mp3", ".flac", ".ogg", ".wav", ".aac", ".m4a", ".wma", ".opus", ".aiff", ".ape"]
        for ext in exts:
            d = tmp_path / f"Album{ext}"
            d.mkdir()
            (d / f"track{ext}").write_bytes(b"")
        assert _count_album_dirs(str(tmp_path)) == len(exts)


# ---------------------------------------------------------------------------
# _run_tail_thread helper
# ---------------------------------------------------------------------------


class TestTailThread:
    def setup_method(self):
        _reset_scan_state()

    def _write_log(self, tmp_path, content):
        log_file = tmp_path / "run.log"
        log_file.write_bytes(content.encode("utf-8"))
        return str(log_file)

    def _run_thread_once(self, run_id, log_path):
        """Run the tail thread with a pre-set stop event so it drains exactly once."""
        stop_event = threading.Event()
        stop_event.set()
        t = threading.Thread(target=_run_tail_thread, args=(run_id, log_path, stop_event))
        t.start()
        t.join(timeout=5)

    def test_increments_processed_on_import_task_created(self, tmp_path):
        run_id = "test-run-001"
        state.rescan_run_id = run_id
        state.rescan_processed = 0
        log_path = self._write_log(tmp_path, "Sending event: import_task_created\n")

        self._run_thread_once(run_id, log_path)

        assert state.rescan_processed == 1

    def test_sets_current_item_from_album_path_line(self, tmp_path):
        run_id = "test-run-002"
        state.rescan_run_id = run_id
        # Real beets -A output: the event, then the bare album path on the next line.
        log_path = self._write_log(
            tmp_path, "Sending event: import_task_created\n/music/Artist/Album\n"
        )

        self._run_thread_once(run_id, log_path)

        assert state.rescan_processed == 1
        assert state.rescan_current_item == "/music/Artist/Album"

    def test_increments_processed_for_each_album(self, tmp_path):
        run_id = "test-run-003"
        state.rescan_run_id = run_id
        lines = (
            "\n".join(f"Sending event: import_task_created\n/music/Album{i}" for i in range(5))
            + "\n"
        )
        log_path = self._write_log(tmp_path, lines)

        self._run_thread_once(run_id, log_path)

        assert state.rescan_processed == 5

    def test_does_not_exceed_total(self, tmp_path):
        run_id = "test-run-004"
        state.rescan_run_id = run_id
        state.rescan_total = 2
        lines = "\n".join("Sending event: import_task_created" for _ in range(5)) + "\n"
        log_path = self._write_log(tmp_path, lines)

        self._run_thread_once(run_id, log_path)

        assert state.rescan_processed == 2

    def test_handles_missing_log_file(self):
        run_id = "test-run-005"
        state.rescan_run_id = run_id
        # Thread should not crash even if the file never appears
        self._run_thread_once(run_id, "/tmp/nonexistent-beetdeck-test-log.log")
        assert state.rescan_processed == 0

    def test_stops_on_stale_run_id(self, tmp_path):
        run_id = "test-run-006"
        state.rescan_run_id = "different-run-id"  # stale
        lines = "\n".join("Sending event: import_task_created" for _ in range(5)) + "\n"
        log_path = self._write_log(tmp_path, lines)

        self._run_thread_once(run_id, log_path)

        # State must not be updated because run_id doesn't match
        assert state.rescan_processed == 0
        assert state.rescan_current_item is None

    def test_ignores_unrecognized_lines(self, tmp_path):
        run_id = "test-run-007"
        state.rescan_run_id = run_id
        content = "Some random beets output line\nAnother irrelevant line\n"
        log_path = self._write_log(tmp_path, content)

        self._run_thread_once(run_id, log_path)

        assert state.rescan_processed == 0
        assert state.rescan_current_item is None

    def test_final_drain_captures_lines_written_after_stop(self, tmp_path):
        """Tail thread must do a final drain after stop_event is set."""
        run_id = "test-run-008"
        state.rescan_run_id = run_id
        log_path = self._write_log(tmp_path, "Sending event: import_task_created\n")

        # stop_event already set → skips polling loop, only runs final drain
        stop_event = threading.Event()
        stop_event.set()
        t = threading.Thread(target=_run_tail_thread, args=(run_id, log_path, stop_event))
        t.start()
        t.join(timeout=5)

        assert state.rescan_processed == 1

    def test_total_none_does_not_cap_processed(self, tmp_path):
        run_id = "test-run-009"
        state.rescan_run_id = run_id
        state.rescan_total = None  # quick/indeterminate
        lines = "\n".join("Sending event: import_task_created" for _ in range(10)) + "\n"
        log_path = self._write_log(tmp_path, lines)

        self._run_thread_once(run_id, log_path)

        assert state.rescan_processed == 10

    def test_marker_split_across_drains_is_not_lost(self, tmp_path):
        """A marker split across two live reads must still be counted exactly once.

        Exercises the live polling loop (not just the final drain): the first
        read sees only a partial line (no trailing newline) and must carry it
        over instead of consuming it as a non-matching fragment.
        """
        run_id = "test-run-010"
        state.rescan_run_id = run_id
        log_file = tmp_path / "run.log"
        # Begin with a partial marker — no newline yet.
        log_file.write_bytes(b"Sending event: import_")

        stop_event = threading.Event()
        t = threading.Thread(target=_run_tail_thread, args=(run_id, str(log_file), stop_event))
        t.start()
        try:
            time.sleep(0.25)  # let at least one drain run on the partial line
            # Complete the marker; append a second whole marker too.
            with open(log_file, "ab") as f:
                f.write(b"task_created\nSending event: import_task_created\n")
            time.sleep(0.25)  # let the loop pick up the completed lines
        finally:
            stop_event.set()
            t.join(timeout=5)

        assert state.rescan_processed == 2


# ---------------------------------------------------------------------------
# Progress counters set by the controller
# ---------------------------------------------------------------------------


class TestProgressCounters:
    def setup_method(self):
        _reset_scan_state()

    def _start_and_wait(self, client, mocker, mode="quick", count_album_dirs_return=0):
        mocker.patch(
            "beetdeck.routes.scan.subprocess.Popen",
            return_value=_make_mock_proc(mocker),
        )
        mocker.patch("beetdeck.routes.scan._take_snapshot", return_value={})
        mocker.patch("beetdeck.routes.scan._take_snapshot_db", return_value={})
        mocker.patch(
            "beetdeck.routes.scan._count_album_dirs",
            return_value=count_album_dirs_return,
        )
        resp = client.post(f"/api/rescan?mode={mode}")
        assert resp.status_code == 200
        state.rescan_thread.join(timeout=5)
        return resp

    def test_quick_scan_total_is_none_after_run(self, client, mocker):
        self._start_and_wait(client, mocker, mode="quick")
        assert state.rescan_total is None

    def test_full_scan_sets_total_from_count(self, client, mocker):
        self._start_and_wait(client, mocker, mode="full", count_album_dirs_return=7)
        assert state.rescan_total == 7

    def test_full_scan_total_zero_when_no_audio(self, client, mocker):
        self._start_and_wait(client, mocker, mode="full", count_album_dirs_return=0)
        assert state.rescan_total == 0

    def test_new_scan_resets_processed_and_current_item(self, client, mocker):
        # Simulate leftover state from a previous run
        state.rescan_processed = 99
        state.rescan_current_item = "/old/path"

        self._start_and_wait(client, mocker, mode="quick")

        # After the controller runs, processed/current_item start at 0/None
        # (no log lines written by mock Popen, so tail thread finds nothing)
        assert state.rescan_processed == 0
        assert state.rescan_current_item is None
