"""MCP REST client: round trips, transport failures, and result normalization.

The normalization rows that the real API can produce cheaply are exercised
against the in-process Flask app (`beetdeck_client`); the rest — a 500 from an
identification confirm, a `.lrc` that could not be removed — are driven through
an `httpx.MockTransport` carrying the exact documented body.
"""

import httpx
import pytest

from beetdeck_mcp.client import (
    WRITE_KINDS,
    ApiResponse,
    ApplyResult,
    BeetdeckClient,
    normalize,
)
from tests.conftest import insert_album, insert_item


def _stub_client(mcp_config, handler):
    """A client whose transport is a callable returning `httpx.Response`."""
    return BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))


def _responder(status_code, payload=None, *, text=None, headers=None):
    def handler(_request):
        if text is not None:
            return httpx.Response(status_code, text=text, headers=headers or {})
        if payload is None:
            return httpx.Response(status_code)
        return httpx.Response(status_code, json=payload)

    return handler


# ---------------------------------------------------------------------------
# round trips
# ---------------------------------------------------------------------------


class TestRequest:
    def test_reaches_the_real_app_through_the_injected_transport(self, beetdeck_client, db_path):
        insert_album(db_path, album="Kid A", albumartist="Radiohead")
        response = beetdeck_client.get("/api/library")
        assert response.ok
        assert response.status_code == 200
        # /api/library answers with a top-level list, so `data` stays empty.
        assert [group["artist"] for group in response.payload] == ["Radiohead"]
        assert response.data == {}

    def test_reports_a_semantic_error_without_raising(self, beetdeck_client):
        response = beetdeck_client.get("/api/album/4242")
        assert response.status_code == 404
        assert response.ok is False
        assert "HTTP 404" in response.message
        assert "not found" in response.message.lower()

    def test_carries_query_parameters(self, beetdeck_client, db_path):
        album_id = insert_album(db_path, album="Amnesiac", albumartist="Radiohead")
        insert_item(db_path, album_id, title="Pyramid Song")
        response = beetdeck_client.get("/api/search", params={"q": "Pyramid"})
        assert response.ok
        assert response.data

    def test_an_empty_body_is_not_an_error(self, mcp_config):
        with _stub_client(mcp_config, _responder(204)) as client:
            response = client.get("/api/anything")
        assert response.ok
        assert response.payload is None

    def test_a_non_json_body_is_an_error_not_a_silent_empty_result(self, mcp_config):
        handler = _responder(200, text="<html>oops</html>", headers={"content-type": "text/html"})
        with _stub_client(mcp_config, handler) as client:
            response = client.get("/api/album/1/cover")
        assert response.ok is False
        assert "non-JSON body" in response.error
        assert normalize("cover", response).status == "failed"

    def test_a_non_object_json_body_leaves_data_empty(self, mcp_config):
        with _stub_client(mcp_config, _responder(200, ["a", "b"])) as client:
            response = client.get("/api/whatever")
        assert response.payload == ["a", "b"]
        assert response.data == {}

    def test_422_detail_entries_reach_the_message(self, mcp_config):
        body = {"error": "Validation Error", "detail": ["fields: field required"]}
        with _stub_client(mcp_config, _responder(422, body)) as client:
            response = client.patch("/api/items/1/tags", json={})
        assert "Validation Error" in response.message
        assert "field required" in response.message

    def test_close_is_idempotent_and_context_managed(self, mcp_config):
        client = _stub_client(mcp_config, _responder(200, {"status": "ok"}))
        with client:
            assert client.get("/api/ping").ok
        client.close()


class TestTransportFailures:
    """A dead backend must read as `failed`, never as "nothing to do"."""

    def test_connection_refused(self, mcp_config):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        with _stub_client(mcp_config, handler) as client:
            response = client.get("/api/library")
        assert response.status_code is None
        assert "could not reach the beetDeck API" in response.error
        assert normalize("tags", response) == ApplyResult("failed", (response.error,))

    def test_timeout(self, mcp_config):
        def handler(request):
            raise httpx.ReadTimeout("too slow", request=request)

        with _stub_client(mcp_config, handler) as client:
            result = client.write("tags", "PATCH", "/api/items/1/tags", json={}, timeout=0.5)
        assert result.status == "failed"
        assert "timed out after 0.5s" in result.detail[0]

    def test_a_transport_failure_on_a_write_is_never_reported_as_partial(self, mcp_config):
        def handler(request):
            raise httpx.ConnectError("down", request=request)

        with _stub_client(mcp_config, handler) as client:
            for kind in WRITE_KINDS:
                assert client.write(kind, "POST", "/api/x").status == "failed"


# ---------------------------------------------------------------------------
# normalization table — one class per row
# ---------------------------------------------------------------------------


class TestNormalizeTags:
    def test_no_warnings_is_ok(self):
        response = ApiResponse(200, {"status": "ok", "warnings": []})
        assert normalize("tags", response) == ApplyResult("ok", ())

    def test_warnings_are_partial_and_carry_the_paths(self):
        response = ApiResponse(200, {"status": "ok", "warnings": ["file write failed: /a.mp3"]})
        result = normalize("tags", response)
        assert result.status == "partial"
        assert result.detail == ("file write failed: /a.mp3",)

    def test_against_the_real_app(self, beetdeck_client, db_path, mocker):
        """A DB write that lands with an unwritable file → 200 + warnings."""
        album_id = insert_album(db_path)
        item_id = insert_item(db_path, album_id)
        item = mocker.MagicMock()
        item.id = item_id
        item.path = b"/music/test/track.mp3"
        lib = mocker.MagicMock()
        lib.get_item.return_value = item
        mocker.patch("beetdeck.routes.items._init_beets", return_value=lib)
        mocker.patch("beetdeck.routes.items._try_write_item", return_value=False)

        result = beetdeck_client.write(
            "tags", "PATCH", f"/api/items/{item_id}/tags", json={"fields": {"title": "New"}}
        )
        assert result.status == "partial"
        assert "file write failed" in result.detail[0]

    def test_a_rejected_field_is_failed_against_the_real_app(self, beetdeck_client, db_path):
        item_id = insert_item(db_path, insert_album(db_path))
        result = beetdeck_client.write(
            "tags", "PATCH", f"/api/items/{item_id}/tags", json={"fields": {"mb_trackid": "x"}}
        )
        assert result.status == "failed"
        assert "not editable" in result.detail[0]


class TestNormalizeGenres:
    def test_ok_status_is_ok(self):
        response = ApiResponse(200, {"status": "ok", "genre": "Rock"})
        assert normalize("genres", response).status == "ok"

    def test_partial_status_names_the_failed_items(self):
        response = ApiResponse(
            200, {"status": "partial", "genre": "Rock", "write_failures": [3, 7]}
        )
        result = normalize("genres", response)
        assert result.status == "partial"
        assert result.detail == ("genre tag write failed for item(s) 3, 7",)

    def test_partial_without_ids_still_reports_partial(self):
        response = ApiResponse(200, {"status": "partial", "genre": "Rock"})
        result = normalize("genres", response)
        assert result.status == "partial"
        assert result.detail

    def test_against_the_real_app(self, beetdeck_client, db_path, mocker):
        album_id = insert_album(db_path)
        item = mocker.MagicMock()
        item.id = 11
        album = mocker.MagicMock()
        album.id = album_id
        album.items.return_value = [item]
        lib = mocker.MagicMock()
        lib.get_album.return_value = album
        mocker.patch("beetdeck.routes.genres._init_beets", return_value=lib)
        mocker.patch("beetdeck.routes.genres._try_write_item", return_value=False)

        result = beetdeck_client.write(
            "genres", "POST", f"/api/album/{album_id}/genre/save", json={"genre": "Ambient"}
        )
        assert result.status == "partial"
        assert "11" in result.detail[0]


class TestNormalizeLyrics:
    def test_everything_written_is_ok(self):
        response = ApiResponse(
            200,
            {
                "status": "ok",
                "written": 2,
                "written_item_ids": [1, 2],
                "retained_lrc_item_ids": [],
                "failed": [],
            },
        )
        assert normalize("lyrics", response, requested_item_ids=[1, 2]).status == "ok"

    def test_failed_items_are_partial(self):
        response = ApiResponse(
            200, {"written_item_ids": [1], "retained_lrc_item_ids": [], "failed": [2]}
        )
        result = normalize("lyrics", response, requested_item_ids=[1, 2])
        assert result.status == "partial"
        assert "lyrics write failed for item(s) 2" in result.detail[0]

    def test_a_retained_lrc_sidecar_is_partial(self):
        response = ApiResponse(
            200, {"written_item_ids": [1], "retained_lrc_item_ids": [1], "failed": []}
        )
        result = normalize("lyrics", response, requested_item_ids=[1])
        assert result.status == "partial"
        assert "sidecar .lrc could not be removed for item(s) 1" in result.detail[0]

    def test_a_silently_skipped_id_is_partial(self):
        """The API skips ids with no stashed fetch and still answers 200/ok."""
        response = ApiResponse(
            200, {"written_item_ids": [1], "retained_lrc_item_ids": [], "failed": []}
        )
        result = normalize("lyrics", response, requested_item_ids=[1, 2, 3])
        assert result.status == "partial"
        assert "no stashed lyrics fetch for item(s) 2, 3" in result.detail[0]

    def test_without_the_requested_ids_the_short_write_cannot_be_detected(self):
        response = ApiResponse(
            200, {"written_item_ids": [1], "retained_lrc_item_ids": [], "failed": []}
        )
        assert normalize("lyrics", response).status == "ok"

    def test_all_three_signals_at_once(self):
        response = ApiResponse(
            200, {"written_item_ids": [1], "retained_lrc_item_ids": [1], "failed": [2]}
        )
        result = normalize("lyrics", response, requested_item_ids=[1, 2, 3])
        assert result.status == "partial"
        assert len(result.detail) == 3

    def test_against_the_real_app_with_nothing_stashed(self, beetdeck_client, db_path, mocker):
        album_id = insert_album(db_path)
        item_id = insert_item(db_path, album_id)
        mocker.patch("beetdeck.routes.lyrics._init_beets", return_value=mocker.MagicMock())

        result = beetdeck_client.write(
            "lyrics",
            "POST",
            f"/api/album/{album_id}/lyrics/confirm",
            json={"item_ids": [item_id]},
            requested_item_ids=[item_id],
        )
        assert result.status == "partial"
        assert "no stashed lyrics fetch" in result.detail[0]


class TestNormalizeIdentify:
    def test_ok(self):
        assert normalize("identify", ApiResponse(200, {"status": "ok"})).status == "ok"

    def test_a_write_failure_is_a_500_carrying_the_message(self):
        body = {"error": "Failed to write tags for 2 track(s)"}
        result = normalize("identify", ApiResponse(500, body))
        assert result.status == "failed"
        assert result.stale is False
        assert "Failed to write tags for 2 track(s)" in result.detail[0]

    def test_an_invalid_candidate_index_is_failed(self):
        result = normalize("identify", ApiResponse(400, {"error": "Invalid candidate index"}))
        assert result.status == "failed"
        assert "Invalid candidate index" in result.detail[0]

    def test_409_is_flagged_stale(self):
        from beetdeck.utils import STALE_GENERATION_ERROR

        result = normalize("identify", ApiResponse(409, {"error": STALE_GENERATION_ERROR}))
        assert result.status == "failed"
        assert result.stale is True
        assert STALE_GENERATION_ERROR in result.detail[0]

    def test_the_real_app_rejects_a_stale_generation(self, beetdeck_client, db_path):
        """No task at all counts as a mismatch — the Task 3 contract."""
        album_id = insert_album(db_path)
        result = beetdeck_client.write(
            "identify",
            "POST",
            f"/api/album/{album_id}/confirm",
            json={"candidate_index": 0, "expected_generation": 7},
        )
        assert result.status == "failed"
        assert result.stale is True
        assert "changed since the preview" in result.detail[0]

    def test_without_the_precondition_the_real_app_reports_no_results(
        self, beetdeck_client, db_path
    ):
        album_id = insert_album(db_path)
        result = beetdeck_client.write(
            "identify", "POST", f"/api/album/{album_id}/confirm", json={"candidate_index": 0}
        )
        assert result.status == "failed"
        assert result.stale is False
        assert "No identification results" in result.detail[0]


class TestNormalizeItemsIdentify:
    def test_ok(self):
        response = ApiResponse(200, {"status": "ok", "album_id": 5})
        assert normalize("items_identify", response).status == "ok"

    def test_warnings_are_partial(self):
        response = ApiResponse(
            200, {"status": "ok", "album_id": 5, "warnings": ["file write failed: /a.mp3"]}
        )
        result = normalize("items_identify", response)
        assert result.status == "partial"
        assert result.detail == ("file write failed: /a.mp3",)

    def test_a_rolled_back_apply_is_failed(self):
        body = {"error": "Failed to apply metadata: boom", "rollback_failures": [3]}
        result = normalize("items_identify", ApiResponse(500, body))
        assert result.status == "failed"
        assert "Failed to apply metadata" in result.detail[0]


class TestNormalizeCover:
    def test_ok(self):
        assert normalize("cover", ApiResponse(200, {"status": "ok"})).status == "ok"

    def test_nothing_stashed_is_failed(self):
        result = normalize("cover", ApiResponse(400, {"error": "No cover art to confirm"}))
        assert result.status == "failed"
        assert "No cover art to confirm" in result.detail[0]

    def test_against_the_real_app_without_a_stash(self, beetdeck_client, db_path):
        album_id = insert_album(db_path)
        result = beetdeck_client.write("cover", "POST", f"/api/album/{album_id}/cover/confirm")
        assert result.status == "failed"
        assert "No cover art to confirm" in result.detail[0]


class TestNormalizeContract:
    def test_every_write_kind_has_a_normalizer(self):
        for kind in WRITE_KINDS:
            assert normalize(kind, ApiResponse(200, {"status": "ok"})).status == "ok"

    @pytest.mark.parametrize("kind", ["", "unknown", "identity"])
    def test_an_unknown_kind_is_a_programming_error(self, kind):
        with pytest.raises(ValueError, match="no result normalizer"):
            normalize(kind, ApiResponse(200, {}))

    def test_write_rejects_an_unknown_kind_before_sending_anything(self, mcp_config):
        def handler(request):
            raise AssertionError("must not send a request for an unknown kind")

        with _stub_client(mcp_config, handler) as client:
            with pytest.raises(ValueError, match="no result normalizer"):
                client.write("unknown", "POST", "/api/x")

    def test_as_dict_is_the_agent_facing_shape(self):
        result = ApplyResult("partial", ("one", "two"), stale=False)
        assert result.as_dict() == {"status": "partial", "detail": ["one", "two"]}
