"""Tests for the BPM compute route."""


# ---------------------------------------------------------------------------
# POST /api/album/<id>/track/<id>/bpm/compute
# ---------------------------------------------------------------------------


class TestComputeTrackBpm:
    def _mock_setup(self, mocker, album_id=1, bpm_result=128):
        """Return a mock lib + plugin pair; plugin sets item.bpm as a side effect."""

        def _set_bpm(items, write=True, force=True):
            for it in items:
                it.bpm = bpm_result

        mock_item = mocker.MagicMock()
        mock_item.album_id = album_id
        mock_item.artist = "Artist"
        mock_item.title = "Track"
        mock_item.bpm = None

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "autobpm"
        mock_plugin.calculate_bpm.side_effect = _set_bpm

        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])
        return mock_item, mock_lib, mock_plugin

    def test_returns_bpm_on_success(self, client, mocker):
        mock_item, _, mock_plugin = self._mock_setup(mocker)
        resp = client.post("/api/album/1/track/1/bpm/compute")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["bpm"] == 128

    def test_calls_calculate_bpm_with_correct_args(self, client, mocker):
        mock_item, _, mock_plugin = self._mock_setup(mocker)
        client.post("/api/album/1/track/1/bpm/compute")
        mock_plugin.calculate_bpm.assert_called_once()
        args, kwargs = mock_plugin.calculate_bpm.call_args
        assert args[0] == [mock_item]
        assert kwargs.get("write") is True
        assert kwargs.get("force") is True

    def test_returns_404_when_track_not_found(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None
        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/1/track/9999/bpm/compute")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Track not found"

    def test_returns_404_when_item_belongs_to_different_album(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 99
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=mock_lib)
        resp = client.post("/api/album/1/track/1/bpm/compute")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Track not found"

    def test_returns_500_when_plugin_not_loaded(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item
        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[])
        resp = client.post("/api/album/1/track/1/bpm/compute")
        assert resp.status_code == 500
        assert resp.get_json()["error"] == "autobpm plugin not loaded"

    def test_returns_400_when_compute_leaves_bpm_unset(self, client, mocker):
        """autobpm swallows errors internally; handler detects failure by value."""
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.artist = "Artist"
        mock_item.title = "Track"

        def _noop(items, write=True, force=True):
            for it in items:
                it.bpm = None  # simulate failed compute — plugin leaves bpm unset

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "autobpm"
        mock_plugin.calculate_bpm.side_effect = _noop

        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        resp = client.post("/api/album/1/track/1/bpm/compute")
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data
        assert "BPM computation failed" in data["error"]

    def test_returns_400_when_compute_leaves_bpm_zero(self, client, mocker):
        """A zero bpm is also treated as a failed compute (bool(0) is False)."""
        mock_item, _, mock_plugin = self._mock_setup(mocker, bpm_result=0)
        resp = client.post("/api/album/1/track/1/bpm/compute")
        assert resp.status_code == 400
        assert "BPM computation failed" in resp.get_json()["error"]

    def test_handler_clears_stale_bpm_before_compute(self, client, mocker):
        """The handler must pre-clear item.bpm so a no-op plugin (one that never
        touches bpm) is detected as a failure rather than reporting the stale
        value. Uses a real object (not a MagicMock) so an un-set attribute would
        keep the seeded value instead of returning a truthy mock."""
        from types import SimpleNamespace

        item = SimpleNamespace(album_id=1, artist="Artist", title="Track", bpm=99)

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = item

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "autobpm"
        mock_plugin.calculate_bpm.side_effect = lambda *a, **k: None  # no-op

        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        resp = client.post("/api/album/1/track/1/bpm/compute")
        assert resp.status_code == 400
        assert "BPM computation failed" in resp.get_json()["error"]

    def test_returns_500_when_compute_raises(self, client, mocker):
        """Unexpected exceptions surface as 500 and the lib is still closed."""
        mock_item, mock_lib, mock_plugin = self._mock_setup(mocker)
        mock_plugin.calculate_bpm.side_effect = RuntimeError("boom")
        resp = client.post("/api/album/1/track/1/bpm/compute")
        assert resp.status_code == 500
        assert "boom" in resp.get_json()["error"]
        mock_lib._close.assert_called_once()

    def test_lib_is_closed_on_success(self, client, mocker):
        mock_item, mock_lib, _ = self._mock_setup(mocker)
        client.post("/api/album/1/track/1/bpm/compute")
        mock_lib._close.assert_called_once()

    def test_lib_is_closed_on_404(self, client, mocker):
        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = None
        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=mock_lib)
        client.post("/api/album/1/track/9999/bpm/compute")
        mock_lib._close.assert_called_once()

    def test_lib_is_closed_on_compute_failure(self, client, mocker):
        mock_item = mocker.MagicMock()
        mock_item.album_id = 1
        mock_item.artist = "Artist"
        mock_item.title = "Track"

        def _noop(items, write=True, force=True):
            for it in items:
                it.bpm = None

        mock_lib = mocker.MagicMock()
        mock_lib.get_item.return_value = mock_item

        mock_plugin = mocker.MagicMock()
        mock_plugin.name = "autobpm"
        mock_plugin.calculate_bpm.side_effect = _noop

        mocker.patch("beetdeck.routes.bpm._init_beets", return_value=mock_lib)
        mocker.patch("beets.plugins.find_plugins", return_value=[mock_plugin])

        client.post("/api/album/1/track/1/bpm/compute")
        mock_lib._close.assert_called_once()


# ---------------------------------------------------------------------------
# OpenAPI spec coverage
# ---------------------------------------------------------------------------


class TestBpmOpenAPISpec:
    def _spec(self, client):
        return client.get("/apidoc/openapi.json").get_json()

    def test_compute_route_in_spec(self, client):
        spec = self._spec(client)
        path = "/api/album/{album_id}/track/{item_id}/bpm/compute"
        assert path in spec["paths"], f"{path} not found in OpenAPI spec"
        op = spec["paths"][path]["post"]
        assert "bpm" in op["tags"]
        assert "200" in op["responses"]
        assert "400" in op["responses"]
        assert "404" in op["responses"]
        assert "requestBody" not in op
