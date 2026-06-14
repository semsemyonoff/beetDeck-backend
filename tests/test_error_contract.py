"""The uniform 422 validation-error contract (Task 11).

Every invalid request — regardless of which blueprint owns the route or whether
the failure is a bad query param or a bad JSON body — returns **HTTP 422** with
the single documented :class:`src.schemas.common.ErrorResponse` body::

    {"error": "Validation failed",
     "detail": [{"loc": [...], "msg": "...", "type": "..."}, ...]}

These tests pin that body across several blueprints (so the SPA has one shape to
parse) and assert the spec documents the same model as the 422 response.

Validation happens before the handler runs, so these cases do not depend on DB
state — a non-existent album id still fails validation first.
"""

import pytest

# (method, url, json_body) — one invalid request per blueprint. None body means
# the failure is in the query string (no request body sent).
INVALID_REQUESTS = [
    pytest.param("get", "/api/search?bogus=1", None, id="library-query-extra"),
    pytest.param("post", "/api/rescan?mode=bogus", None, id="scan-query-literal"),
    pytest.param(
        "post", "/api/items/metadata-batch", {"items": "notalist"}, id="items-batch-bad-type"
    ),
    pytest.param("post", "/api/items/1/metadata", {"bogus": 1}, id="items-metadata-extra"),
    pytest.param("post", "/api/album/1/genre/save", {"bogus": 1}, id="genres-save-extra"),
    pytest.param("post", "/api/album/1/apply", {"candidate_index": "nope"}, id="identify-bad-type"),
    pytest.param(
        "post",
        "/api/album/1/lyrics/confirm",
        {"item_ids": ["x"]},
        id="lyrics-bulk-non-int",
    ),
]


# (method, url, json_body, offending_field) — the field whose name must appear in
# some detail entry's `loc`. Pins that the hook maps pydantic's `loc` faithfully
# (a regression hardcoding `loc=[]` or dropping it would pass the shape-only checks
# above but fail here).
INVALID_REQUESTS_WITH_FIELD = [
    pytest.param(
        "post",
        "/api/items/metadata-batch",
        {"items": "notalist"},
        "items",
        id="items-batch-bad-type",
    ),
    pytest.param(
        "post",
        "/api/album/1/apply",
        {"candidate_index": "nope"},
        "candidate_index",
        id="identify-bad-type",
    ),
    pytest.param(
        "post",
        "/api/album/1/genre/save",
        {"bogus": 1},
        "bogus",
        id="genres-save-extra",
    ),
    pytest.param(
        "post",
        "/api/album/1/lyrics/confirm",
        {"item_ids": ["x"]},
        "item_ids",
        id="lyrics-bulk-non-int",
    ),
]


class TestUniformErrorBody:
    @pytest.mark.parametrize("method, url, body", INVALID_REQUESTS)
    def test_invalid_request_returns_uniform_422(self, client, method, url, body):
        kwargs = {} if body is None else {"json": body}
        resp = getattr(client, method)(url, **kwargs)

        assert resp.status_code == 422
        payload = resp.get_json()
        # Object (not a bare list), with exactly the documented keys.
        assert isinstance(payload, dict)
        assert payload["error"] == "Validation failed"
        assert set(payload) == {"error", "detail"}

    @pytest.mark.parametrize("method, url, body", INVALID_REQUESTS)
    def test_detail_entries_have_loc_msg_type(self, client, method, url, body):
        kwargs = {} if body is None else {"json": body}
        resp = getattr(client, method)(url, **kwargs)

        detail = resp.get_json()["detail"]
        assert isinstance(detail, list) and detail
        for entry in detail:
            assert {"loc", "msg", "type"} <= set(entry)
            assert isinstance(entry["loc"], list)
            assert isinstance(entry["msg"], str)
            assert isinstance(entry["type"], str)

    @pytest.mark.parametrize("method, url, body, field", INVALID_REQUESTS_WITH_FIELD)
    def test_detail_loc_points_at_offending_field(self, client, method, url, body, field):
        """The hook maps pydantic's `loc`/`type` faithfully: some detail entry
        locates the offending field, and every entry carries a non-empty type
        (a hardcoded/empty `loc` or placeholder `type` would fail here)."""
        resp = getattr(client, method)(url, json=body)

        detail = resp.get_json()["detail"]
        locs = [entry["loc"] for entry in detail]
        assert any(field in loc for loc in locs), (
            f"no detail entry locates the offending field {field!r}; got {locs}"
        )
        assert all(entry["type"] for entry in detail)


class TestErrorContractInSpec:
    def test_validation_error_model_documents_422(self, client):
        """The 422 response of validated endpoints references the shared
        ErrorResponse schema (one component reused everywhere).

        spectree suffixes component names with a content hash
        (``ErrorResponse.<hash>``), so match by the ``ErrorResponse`` title
        rather than an exact key.
        """
        spec = client.get("/apidoc/openapi.json").get_json()

        schemas = spec["components"]["schemas"]
        error_keys = {
            key for key, schema in schemas.items() if schema.get("title") == "ErrorResponse"
        }
        # One reused ErrorResponse component, exposing {error, detail}.
        assert len(error_keys) == 1
        err_key = error_keys.pop()
        err_props = schemas[err_key]["properties"]
        assert "error" in err_props
        assert "detail" in err_props

        # Every validated endpoint that documents a 422 points at that same model.
        ref = f"#/components/schemas/{err_key}"
        documented = 0
        for methods in spec["paths"].values():
            for op in methods.values():
                if not isinstance(op, dict):
                    continue
                resp_422 = op.get("responses", {}).get("422")
                if not resp_422:
                    continue
                documented += 1
                schema = resp_422["content"]["application/json"]["schema"]
                assert schema.get("$ref") == ref, op.get("operationId")
        # At least the query/body-validated endpoints carry a 422.
        assert documented > 0
