"""Tests for the shared schema models (beetdeck/schemas/common.py) and feature
schema modules (bpm, etc.).

Assert the models serialize to the JSON shapes the spec/handlers expect:
- ``AlbumSummary`` / ``ArtistGroup`` mirror the ``GET /api/library`` and
  ``GET /api/artist`` response dicts, and
- ``ErrorResponse`` carries a short ``error`` plus optional field-level ``detail``,
- ``TrackBpmComputeResponse`` carries ``status`` and optional ``bpm``.
"""

from beetdeck.schemas import (
    AlbumSummary,
    ArtistGroup,
    ErrorResponse,
    ValidationErrorDetail,
)
from beetdeck.schemas.bpm import TrackBpmComputeResponse


def test_album_summary_serializes_expected_keys():
    summary = AlbumSummary(
        id=1, album="Test Album", year=2020, tagged=True, ignored=False, has_cover=True
    )
    assert summary.model_dump() == {
        "id": 1,
        "album": "Test Album",
        "year": 2020,
        "tagged": True,
        "ignored": False,
        "has_cover": True,
    }


def test_album_summary_optional_fields_default_to_none():
    summary = AlbumSummary(id=2, tagged=False, ignored=False, has_cover=False)
    dumped = summary.model_dump()
    assert dumped["album"] is None
    assert dumped["year"] is None


def test_artist_group_nests_album_summaries():
    group = ArtistGroup(
        artist="Test Artist",
        albums=[
            AlbumSummary(
                id=1,
                album="A",
                year=2001,
                tagged=False,
                ignored=False,
                has_cover=False,
            )
        ],
    )
    dumped = group.model_dump()
    assert dumped["artist"] == "Test Artist"
    assert dumped["albums"][0]["id"] == 1
    assert set(dumped["albums"][0]) == {
        "id",
        "album",
        "year",
        "tagged",
        "ignored",
        "has_cover",
    }


def test_artist_group_defaults_to_empty_albums():
    group = ArtistGroup(artist="Unknown Artist")
    assert group.model_dump()["albums"] == []


def test_error_response_minimal():
    err = ErrorResponse(error="not_initialized")
    assert err.model_dump() == {"error": "not_initialized", "detail": None}


def test_error_response_with_detail():
    err = ErrorResponse(
        error="validation_error",
        detail=[
            ValidationErrorDetail(
                loc=["body", "items", 0, "id"],
                msg="field required",
                type="missing",
            )
        ],
    )
    dumped = err.model_dump()
    assert dumped["error"] == "validation_error"
    assert dumped["detail"][0] == {
        "loc": ["body", "items", 0, "id"],
        "msg": "field required",
        "type": "missing",
    }


def test_error_response_appears_in_json_schema():
    schema = ErrorResponse.model_json_schema()
    assert schema["properties"]["error"]["type"] == "string"
    assert "detail" in schema["properties"]


# ---------------------------------------------------------------------------
# TrackBpmComputeResponse
# ---------------------------------------------------------------------------


def test_track_bpm_compute_response_ok():
    resp = TrackBpmComputeResponse(status="ok", bpm=128)
    assert resp.model_dump() == {"status": "ok", "bpm": 128}


def test_track_bpm_compute_response_optional_fields_default_none():
    resp = TrackBpmComputeResponse()
    assert resp.model_dump() == {"status": None, "bpm": None}


def test_track_bpm_compute_response_bpm_none():
    resp = TrackBpmComputeResponse(status="ok", bpm=None)
    assert resp.model_dump() == {"status": "ok", "bpm": None}


def test_track_bpm_compute_response_json_schema():
    schema = TrackBpmComputeResponse.model_json_schema()
    assert "status" in schema["properties"]
    assert "bpm" in schema["properties"]
