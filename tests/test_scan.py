"""Tests for scan blueprint routes."""

import sqlite3

import src.state as state
from src.routes.scan import _compute_scan_diff, _normalize_absolute_paths, _take_snapshot
from tests.conftest import insert_album, insert_item


def _reset_scan_state():
    """Reset scan globals between tests."""
    state.rescan_proc = None
    state.rescan_snapshot = None


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# POST /api/rescan
# ---------------------------------------------------------------------------


class TestRescan:
    def setup_method(self):
        _reset_scan_state()

    def test_starts_rescan_quick_mode(self, client, mocker):
        mock_proc = mocker.MagicMock()
        mock_proc.poll.return_value = None  # running
        mocker.patch("src.routes.scan.subprocess.Popen", return_value=mock_proc)
        mocker.patch("src.routes.scan._take_snapshot", return_value={})

        resp = client.post("/api/rescan")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "started"
        assert data["mode"] == "quick"

    def test_starts_rescan_full_mode(self, client, mocker):
        mock_proc = mocker.MagicMock()
        mock_proc.poll.return_value = None
        popen_mock = mocker.patch("src.routes.scan.subprocess.Popen", return_value=mock_proc)
        mocker.patch("src.routes.scan._take_snapshot", return_value={})

        resp = client.post("/api/rescan?mode=full")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "full"
        # -I flag used for full mode
        cmd_arg = popen_mock.call_args[0][0]
        assert " -I " in cmd_arg

    def test_quick_mode_uses_incremental_flag(self, client, mocker):
        mock_proc = mocker.MagicMock()
        mock_proc.poll.return_value = None
        popen_mock = mocker.patch("src.routes.scan.subprocess.Popen", return_value=mock_proc)
        mocker.patch("src.routes.scan._take_snapshot", return_value={})

        client.post("/api/rescan?mode=quick")
        cmd_arg = popen_mock.call_args[0][0]
        assert " -i " in cmd_arg

    def test_returns_400_for_invalid_mode(self, client):
        resp = client.post("/api/rescan?mode=bogus")
        assert resp.status_code == 400
        assert "Invalid mode" in resp.get_json()["error"]

    def test_returns_409_when_already_running(self, client, mocker):
        mock_proc = mocker.MagicMock()
        mock_proc.poll.return_value = None  # still running
        state.rescan_proc = mock_proc

        resp = client.post("/api/rescan")
        assert resp.status_code == 409
        assert resp.get_json()["status"] == "running"

    def test_allows_new_scan_after_previous_finished(self, client, mocker):
        finished_proc = mocker.MagicMock()
        finished_proc.poll.return_value = 0  # done
        state.rescan_proc = finished_proc

        new_proc = mocker.MagicMock()
        new_proc.poll.return_value = None
        mocker.patch("src.routes.scan.subprocess.Popen", return_value=new_proc)
        mocker.patch("src.routes.scan._take_snapshot", return_value={})

        resp = client.post("/api/rescan")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"

    def test_takes_snapshot_before_starting(self, client, mocker):
        mock_proc = mocker.MagicMock()
        mock_proc.poll.return_value = None
        mocker.patch("src.routes.scan.subprocess.Popen", return_value=mock_proc)
        snap = {1: ("Song", "Artist", 10, "/music/song.mp3")}
        mocker.patch("src.routes.scan._take_snapshot", return_value=snap)

        client.post("/api/rescan")
        assert state.rescan_snapshot == snap


# ---------------------------------------------------------------------------
# GET /api/rescan/status
# ---------------------------------------------------------------------------


class TestRescanStatus:
    def setup_method(self):
        _reset_scan_state()

    def test_returns_idle_when_no_proc(self, client):
        resp = client.get("/api/rescan/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "idle"

    def test_returns_running_when_proc_running(self, client, mocker):
        mock_proc = mocker.MagicMock()
        mock_proc.poll.return_value = None  # still running
        state.rescan_proc = mock_proc

        resp = client.get("/api/rescan/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"

    def test_returns_done_with_returncode_when_finished(self, client, mocker):
        mock_proc = mocker.MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        state.rescan_proc = mock_proc
        state.rescan_snapshot = None
        mocker.patch("src.routes.scan._take_snapshot", return_value={})

        resp = client.get("/api/rescan/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "done"
        assert data["returncode"] == 0

    def test_returns_diff_when_snapshot_exists(self, client, mocker):
        mock_proc = mocker.MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        state.rescan_proc = mock_proc

        before = {1: ("Old Song", "Artist", 10, "/music/old.mp3")}
        state.rescan_snapshot = before

        after = {
            1: ("Old Song", "Artist", 10, "/music/old.mp3"),
            2: ("New Song", "Artist", 10, "/music/new.mp3"),
        }
        mocker.patch("src.routes.scan._take_snapshot", return_value=after)

        resp = client.get("/api/rescan/status")
        data = resp.get_json()
        assert data["status"] == "done"
        assert len(data["added"]) == 1
        assert data["added"][0]["id"] == 2
        assert data["removed"] == []

    def test_no_diff_fields_when_no_snapshot(self, client, mocker):
        mock_proc = mocker.MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        state.rescan_proc = mock_proc
        state.rescan_snapshot = None
        mocker.patch("src.routes.scan._take_snapshot", return_value={})

        resp = client.get("/api/rescan/status")
        data = resp.get_json()
        assert "added" not in data
        assert "removed" not in data

    def test_returns_nonzero_returncode_on_failure(self, client, mocker):
        mock_proc = mocker.MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1
        state.rescan_proc = mock_proc
        state.rescan_snapshot = None
        mocker.patch("src.routes.scan._take_snapshot", return_value={})

        resp = client.get("/api/rescan/status")
        data = resp.get_json()
        assert data["returncode"] == 1
