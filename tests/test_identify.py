"""Tests for identify blueprint routes."""

import src.state as state


def _reset_state():
    """Clear identify_tasks between tests."""
    state.identify_tasks.clear()


# ---------------------------------------------------------------------------
# POST /api/album/<id>/identify
# ---------------------------------------------------------------------------


class TestStartIdentify:
    def setup_method(self):
        _reset_state()

    def test_returns_started_and_task_id(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.items.return_value = [mocker.MagicMock()]
        mock_lib.get_album.return_value = mock_album

        mock_proposal = mocker.MagicMock()
        mock_proposal.candidates = []

        mocker.patch("src.routes.identify._init_beets", return_value=mock_lib)
        mocker.patch(
            "beets.autotag.tag_album",
            return_value=("Artist", "Album", mock_proposal),
        )

        resp = client.post("/api/album/1/identify", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "started"
        assert "task_id" in data

    def test_returns_409_when_task_already_running(self, client):
        state.identify_tasks["album_1"] = {
            "task_id": "abc123",
            "status": "running",
        }
        resp = client.post("/api/album/1/identify", json={})
        assert resp.status_code == 409
        data = resp.get_json()
        assert data["status"] == "running"
        assert data["task_id"] == "abc123"

    def test_replaces_completed_task(self, client, mocker):
        state.identify_tasks["album_1"] = {
            "task_id": "old",
            "status": "done",
        }

        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None
        mocker.patch("src.routes.identify._init_beets", return_value=mock_lib)

        resp = client.post("/api/album/1/identify", json={})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"
        # Old task replaced
        assert state.identify_tasks["album_1"]["task_id"] != "old"


# ---------------------------------------------------------------------------
# GET /api/album/<id>/identify/status
# ---------------------------------------------------------------------------


class TestIdentifyStatus:
    def setup_method(self):
        _reset_state()

    def test_returns_idle_when_no_task(self, client):
        resp = client.get("/api/album/99/identify/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "idle"

    def test_returns_task_status_without_private_keys(self, client):
        state.identify_tasks["album_1"] = {
            "task_id": "abc",
            "status": "done",
            "candidates": [{"index": 0}],
            "_matches": ["private"],
            "_lib": object(),
        }
        resp = client.get("/api/album/1/identify/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "done"
        assert "_matches" not in data
        assert "_lib" not in data
        assert data["task_id"] == "abc"

    def test_returns_running_status(self, client):
        state.identify_tasks["album_2"] = {
            "task_id": "xyz",
            "status": "running",
            "candidates": [],
        }
        resp = client.get("/api/album/2/identify/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"


# ---------------------------------------------------------------------------
# POST /api/album/<id>/apply
# ---------------------------------------------------------------------------


class TestApplyMatch:
    def setup_method(self):
        _reset_state()

    def test_returns_400_when_no_task(self, client):
        resp = client.post("/api/album/1/apply", json={"candidate_index": 0})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No identification results"

    def test_returns_400_when_task_not_done(self, client):
        state.identify_tasks["album_1"] = {"status": "running", "_matches": []}
        resp = client.post("/api/album/1/apply", json={"candidate_index": 0})
        assert resp.status_code == 400

    def test_returns_400_for_invalid_candidate_index(self, client):
        state.identify_tasks["album_1"] = {"status": "done", "_matches": []}
        resp = client.post("/api/album/1/apply", json={"candidate_index": 0})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Invalid candidate index"

    def test_returns_diff_for_valid_candidate(self, client, db_path, mocker):
        import sqlite3

        # Insert album into test DB
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO albums (id, album, albumartist, year, label, mb_albumid, genre) "
            "VALUES (1, 'Old Album', 'Old Artist', 2000, 'OldLabel', 'old-mb-id', 'Rock')"
        )
        conn.commit()
        conn.close()

        mock_info = mocker.MagicMock()
        mock_info.artist = "New Artist"
        mock_info.album = "New Album"
        mock_info.year = 2023
        mock_info.album_id = "new-mb-id"
        mock_info.item_data = {}
        mock_info.tracks = []

        mock_match = mocker.MagicMock()
        mock_match.info = mock_info
        mock_match.mapping = {}

        state.identify_tasks["album_1"] = {
            "status": "done",
            "_matches": [mock_match],
        }

        resp = client.post("/api/album/1/apply", json={"candidate_index": 0})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["candidate_index"] == 0
        assert "album" in data
        assert "tracks" in data
        assert data["album"]["album"]["old"] == "Old Album"
        assert data["album"]["album"]["new"] == "New Album"


# ---------------------------------------------------------------------------
# POST /api/album/<id>/confirm
# ---------------------------------------------------------------------------


class TestConfirmMatch:
    def setup_method(self):
        _reset_state()

    def test_returns_400_when_no_task(self, client):
        resp = client.post("/api/album/1/confirm", json={"candidate_index": 0})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No identification results"

    def test_returns_400_when_task_not_done(self, client):
        state.identify_tasks["album_1"] = {"status": "running", "_matches": []}
        resp = client.post("/api/album/1/confirm", json={"candidate_index": 0})
        assert resp.status_code == 400

    def test_returns_400_for_invalid_candidate_index(self, client):
        state.identify_tasks["album_1"] = {"status": "done", "_matches": []}
        resp = client.post("/api/album/1/confirm", json={"candidate_index": 5})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Invalid candidate index"

    def test_returns_404_when_album_not_found_in_beets(self, client, mocker):
        mock_match = mocker.MagicMock()
        mock_match.info.artist = "Artist"
        mock_match.info.album = "Album"
        mock_match.info.data_source = "MusicBrainz"
        mock_match.distance = 0.1

        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = None

        state.identify_tasks["album_1"] = {
            "status": "done",
            "_matches": [mock_match],
            "_lib": mock_lib,
        }

        resp = client.post("/api/album/1/confirm", json={"candidate_index": 0})
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Album not found"

    def test_applies_metadata_and_returns_ok(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.track = 1
        mock_item.title = "Track 1"

        mock_album = mocker.MagicMock()

        mock_match = mocker.MagicMock()
        mock_match.info.artist = "Artist"
        mock_match.info.album = "Album"
        mock_match.info.data_source = "MusicBrainz"
        mock_match.distance = 0.05
        mock_match.mapping = {mock_item: mocker.MagicMock()}

        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album

        state.identify_tasks["album_1"] = {
            "status": "done",
            "_matches": [mock_match],
            "_lib": mock_lib,
        }

        resp = client.post("/api/album/1/confirm", json={"candidate_index": 0})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

        mock_match.apply_metadata.assert_called_once()
        mock_item.store.assert_called()
        mock_item.try_write.assert_called_once()
        mock_match.apply_album_metadata.assert_called_once_with(mock_album)
        mock_album.store.assert_called()

    def test_task_status_set_to_done_after_success(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.track = 1
        mock_item.title = "Track 1"
        mock_item.try_write.return_value = True

        mock_album = mocker.MagicMock()

        mock_match = mocker.MagicMock()
        mock_match.info.artist = "A"
        mock_match.info.album = "B"
        mock_match.info.data_source = "MB"
        mock_match.distance = 0.0
        mock_match.mapping = {mock_item: mocker.MagicMock()}

        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album

        state.identify_tasks["album_1"] = {
            "status": "done",
            "_matches": [mock_match],
            "_lib": mock_lib,
        }

        client.post("/api/album/1/confirm", json={"candidate_index": 0})

        assert state.identify_tasks["album_1"]["status"] == "done"

    def test_cleans_up_private_keys_after_confirm(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.track = 1
        mock_item.title = "Track 1"
        mock_album = mocker.MagicMock()

        mock_match = mocker.MagicMock()
        mock_match.info.artist = "A"
        mock_match.info.album = "B"
        mock_match.info.data_source = "MB"
        mock_match.distance = 0.0
        mock_match.mapping = {mock_item: mocker.MagicMock()}

        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album

        state.identify_tasks["album_1"] = {
            "status": "done",
            "_matches": [mock_match],
            "_lib": mock_lib,
        }

        client.post("/api/album/1/confirm", json={"candidate_index": 0})

        task = state.identify_tasks["album_1"]
        assert "_matches" not in task
        assert "_lib" not in task

    def test_returns_500_when_write_fails(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.track = 1
        mock_item.title = "Track 1"
        mock_item.id = 1
        mock_item.try_write.return_value = False

        mock_album = mocker.MagicMock()

        mock_match = mocker.MagicMock()
        mock_match.info.artist = "Artist"
        mock_match.info.album = "Album"
        mock_match.info.data_source = "MusicBrainz"
        mock_match.distance = 0.05
        mock_match.mapping = {mock_item: mocker.MagicMock()}

        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = mock_album

        state.identify_tasks["album_1"] = {
            "status": "done",
            "_matches": [mock_match],
            "_lib": mock_lib,
        }

        resp = client.post("/api/album/1/confirm", json={"candidate_index": 0})
        assert resp.status_code == 500
        assert "Failed to write tags" in resp.get_json()["error"]
        # _matches cleared after write failure: metadata already applied to in-memory
        # items and stored to DB, so a retry would re-apply stale in-memory state
        assert "_matches" not in state.identify_tasks["album_1"]
        assert state.identify_tasks["album_1"]["status"] == "done"


# ---------------------------------------------------------------------------
# POST /api/album/<id>/ignore
# ---------------------------------------------------------------------------


class TestToggleIgnore:
    def _mock_lib(self, mocker, album=None):
        mock_lib = mocker.MagicMock()
        mock_lib.get_album.return_value = album
        mocker.patch("src.routes.identify._init_beets", return_value=mock_lib)
        return mock_lib

    def test_returns_404_when_album_not_found(self, client, mocker):
        self._mock_lib(mocker, album=None)
        resp = client.post("/api/album/99/ignore")
        assert resp.status_code == 404

    def test_sets_ignored_when_not_set(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_album.beetdeck_ignored = None
        self._mock_lib(mocker, album=mock_album)

        resp = client.post("/api/album/1/ignore")
        assert resp.status_code == 200
        assert resp.get_json() == {"ignored": True}
        assert mock_album.beetdeck_ignored == "1"
        mock_album.store.assert_called_once()

    def test_clears_ignored_when_already_set(self, client, mocker):
        mock_album = mocker.MagicMock()
        mock_album.beetdeck_ignored = "1"
        self._mock_lib(mocker, album=mock_album)

        resp = client.post("/api/album/1/ignore")
        assert resp.status_code == 200
        assert resp.get_json() == {"ignored": False}
        assert mock_album.beetdeck_ignored == "0"
        mock_album.store.assert_called_once()
