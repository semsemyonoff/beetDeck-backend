"""Tests for identify blueprint routes."""

import beetdeck.state as state


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

        mocker.patch("beetdeck.routes.identify._init_beets", return_value=mock_lib)
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
        mocker.patch("beetdeck.routes.identify._init_beets", return_value=mock_lib)

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
        mocker.patch("beetdeck.routes.identify._init_beets", return_value=mock_lib)
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


# ---------------------------------------------------------------------------
# OpenAPI spec coverage
# ---------------------------------------------------------------------------


class TestIdentifyOpenAPISpec:
    """All five identify routes are decorated with ``@api.validate`` (tagged
    ``identify``). ``ignore`` and ``identify/status`` are body-less (response-only);
    ``identify``, ``apply`` and ``confirm`` document a JSON body. Assert they appear
    in the spec under their ``/api/album/...`` paths."""

    def _spec(self, client):
        return client.get("/apidoc/openapi.json").get_json()

    def test_ignore_in_spec(self, client):
        op = self._spec(client)["paths"]["/api/album/{album_id}/ignore"]["post"]
        assert "identify" in op["tags"]
        assert "200" in op["responses"]
        assert "404" in op["responses"]
        # Body-less POST — no requestBody documented.
        assert "requestBody" not in op

    def test_status_in_spec(self, client):
        op = self._spec(client)["paths"]["/api/album/{album_id}/identify/status"]["get"]
        assert "identify" in op["tags"]
        assert "200" in op["responses"]
        assert "requestBody" not in op

    def test_identify_documents_json_body(self, client):
        op = self._spec(client)["paths"]["/api/album/{album_id}/identify"]["post"]
        assert "identify" in op["tags"]
        assert "200" in op["responses"]
        assert "409" in op["responses"]
        assert "application/json" in op["requestBody"]["content"]

    def test_apply_documents_json_body(self, client):
        op = self._spec(client)["paths"]["/api/album/{album_id}/apply"]["post"]
        assert "identify" in op["tags"]
        assert "200" in op["responses"]
        assert "application/json" in op["requestBody"]["content"]

    def test_confirm_documents_json_body(self, client):
        op = self._spec(client)["paths"]["/api/album/{album_id}/confirm"]["post"]
        assert "identify" in op["tags"]
        assert "200" in op["responses"]
        assert "application/json" in op["requestBody"]["content"]


# ---------------------------------------------------------------------------
# Behavior unchanged after decoration
# ---------------------------------------------------------------------------


class TestIdentifyBehaviorUnchanged:
    """Decoration must not change behavior for the existing contracts: only a
    malformed JSON body (unknown field / wrong-typed ``candidate_index``) on the
    body-validated routes now yields 422; everything else reaches the handler as
    before."""

    def setup_method(self):
        _reset_state()

    def test_identify_unknown_field_returns_422(self, client):
        resp = client.post("/api/album/1/identify", json={"bogus": "x"})
        assert resp.status_code == 422

    def test_identify_empty_body_reaches_handler(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_album = mocker.MagicMock()
        mock_album.albumartist = "Artist"
        mock_album.album = "Album"
        mock_album.items.return_value = [mocker.MagicMock()]
        mock_lib.get_album.return_value = mock_album
        mock_proposal = mocker.MagicMock()
        mock_proposal.candidates = []
        mocker.patch("beetdeck.routes.identify._init_beets", return_value=mock_lib)
        mocker.patch(
            "beets.autotag.tag_album",
            return_value=("Artist", "Album", mock_proposal),
        )
        resp = client.post("/api/album/1/identify", json={})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"

    def test_apply_wrong_type_candidate_returns_422(self, client):
        resp = client.post("/api/album/1/apply", json={"candidate_index": "nope"})
        assert resp.status_code == 422

    def test_apply_unknown_field_returns_422(self, client):
        resp = client.post("/api/album/1/apply", json={"bogus": 1})
        assert resp.status_code == 422

    def test_apply_empty_body_reaches_handler(self, client):
        # Empty body → candidate_index defaults to 0 → handler's own "no task" 400.
        resp = client.post("/api/album/1/apply", json={})
        assert resp.status_code == 400

    def test_confirm_wrong_type_candidate_returns_422(self, client):
        resp = client.post("/api/album/1/confirm", json={"candidate_index": 1.5})
        assert resp.status_code == 422

    def test_confirm_empty_body_reaches_handler(self, client):
        resp = client.post("/api/album/1/confirm", json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# match_generation / expected_generation precondition
# ---------------------------------------------------------------------------


def _mock_match(mocker, distance=0.1, write_ok=True):
    """An AlbumMatch stand-in that survives _serialize_candidate and confirm."""
    item = mocker.MagicMock()
    item.id = 1
    item.track = 1
    item.title = "Old Title"
    item.path = b"/music/track.mp3"
    item.try_write.return_value = write_ok

    track = mocker.MagicMock()
    track.title = "New Title"
    track.index = 1
    track.artist = "Artist"
    track.length = 100

    match = mocker.MagicMock()
    match.distance = distance
    match.info.artist = "Artist"
    match.info.album = "Album"
    match.info.year = 2000
    match.info.label = "Label"
    match.info.media = "CD"
    match.info.data_source = "MusicBrainz"
    match.info.album_id = "mbid-1"
    match.info.tracks = [track]
    match.mapping = {item: track}
    return match


def _run_identify(client, mocker, match, album_id=1):
    """Drive POST /identify with mocked beets and return the done status body."""
    import time

    mock_album = mocker.MagicMock()
    mock_album.albumartist = "Artist"
    mock_album.album = "Album"
    mock_album.items.return_value = [next(iter(match.mapping))]

    mock_lib = mocker.MagicMock()
    mock_lib.get_album.return_value = mock_album

    mocker.patch("beetdeck.routes.identify._init_beets", return_value=mock_lib)
    mock_proposal = mocker.MagicMock()
    mock_proposal.candidates = [match]
    mocker.patch(
        "beets.autotag.tag_album",
        return_value=("Artist", "Album", mock_proposal),
    )

    assert client.post(f"/api/album/{album_id}/identify", json={}).status_code == 200

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        data = client.get(f"/api/album/{album_id}/identify/status").get_json()
        if data["status"] in ("done", "error"):
            assert data["status"] == "done", data
            return data
        time.sleep(0.02)
    raise AssertionError("identify did not finish in time")


class TestMatchGenerationStatus:
    def setup_method(self):
        _reset_state()

    def test_status_reports_generation_and_matches_available(self, client, mocker):
        data = _run_identify(client, mocker, _mock_match(mocker))
        assert isinstance(data["match_generation"], int)
        assert data["matches_available"] is True

    def test_running_task_has_no_generation_and_no_matches(self, client):
        state.identify_tasks["album_1"] = {
            "task_id": "abc",
            "status": "running",
            "candidates": [],
            "match_generation": None,
        }
        data = client.get("/api/album/1/identify/status").get_json()
        assert data["match_generation"] is None
        assert data["matches_available"] is False

    def test_consumed_task_reports_matches_unavailable(self, client, mocker):
        gen = _run_identify(client, mocker, _mock_match(mocker))["match_generation"]

        resp = client.post(
            "/api/album/1/confirm",
            json={"candidate_index": 0, "expected_generation": gen},
        )
        assert resp.status_code == 200

        data = client.get("/api/album/1/identify/status").get_json()
        # Same task_id and still "done" — matches_available is the only signal.
        assert data["status"] == "done"
        assert data["matches_available"] is False
        assert data["match_generation"] != gen


class TestConfirmPrecondition:
    def setup_method(self):
        _reset_state()

    def test_confirm_succeeds_with_matching_generation(self, client, mocker):
        gen = _run_identify(client, mocker, _mock_match(mocker))["match_generation"]

        resp = client.post(
            "/api/album/1/confirm",
            json={"candidate_index": 0, "expected_generation": gen},
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_reidentify_bumps_generation_and_confirm_is_rejected(self, client, mocker):
        first = _run_identify(client, mocker, _mock_match(mocker))["match_generation"]
        second = _run_identify(client, mocker, _mock_match(mocker))["match_generation"]
        assert second != first

        resp = client.post(
            "/api/album/1/confirm",
            json={"candidate_index": 0, "expected_generation": first},
        )
        assert resp.status_code == 409
        assert "changed since the preview" in resp.get_json()["error"]
        # Rejected before the task was touched — the fresh candidates survive.
        assert state.identify_tasks["album_1"]["status"] == "done"
        assert state.identify_tasks["album_1"]["_matches"]

    def test_second_confirm_rejected_by_precondition_not_index(self, client, mocker):
        gen = _run_identify(client, mocker, _mock_match(mocker))["match_generation"]

        assert (
            client.post(
                "/api/album/1/confirm",
                json={"candidate_index": 0, "expected_generation": gen},
            ).status_code
            == 200
        )

        resp = client.post(
            "/api/album/1/confirm",
            json={"candidate_index": 0, "expected_generation": gen},
        )
        assert resp.status_code == 409
        assert "changed since the preview" in resp.get_json()["error"]

    def test_second_confirm_without_precondition_falls_through_to_400(self, client, mocker):
        """The behaviour the precondition exists to replace, pinned as-is."""
        _run_identify(client, mocker, _mock_match(mocker))
        assert client.post("/api/album/1/confirm", json={"candidate_index": 0}).status_code == 200

        resp = client.post("/api/album/1/confirm", json={"candidate_index": 0})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Invalid candidate index"

    def test_missing_task_with_precondition_returns_409(self, client):
        resp = client.post(
            "/api/album/9/confirm",
            json={"candidate_index": 0, "expected_generation": 7},
        )
        assert resp.status_code == 409

    def test_missing_task_without_precondition_still_returns_400(self, client):
        resp = client.post("/api/album/9/confirm", json={"candidate_index": 0})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No identification results"

    def test_apply_rejects_expected_generation_as_unknown_field(self, client):
        # apply cannot act on a precondition, so the field is not on its model.
        resp = client.post(
            "/api/album/1/apply",
            json={"candidate_index": 0, "expected_generation": 1},
        )
        assert resp.status_code == 422

    def test_confirm_documents_409_and_precondition_in_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/album/{album_id}/confirm"]["post"]
        assert "409" in op["responses"]
        ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        model = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
        assert "expected_generation" in model["properties"]

    def test_status_documents_generation_fields_in_spec(self, client):
        spec = client.get("/apidoc/openapi.json").get_json()
        op = spec["paths"]["/api/album/{album_id}/identify/status"]["get"]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        model = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
        assert "match_generation" in model["properties"]
        assert "matches_available" in model["properties"]
