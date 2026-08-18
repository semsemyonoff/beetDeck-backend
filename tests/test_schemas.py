"""Tests for the shared schema models (beetdeck/schemas/common.py) and feature
schema modules (bpm, artwork, etc.).

Assert the models serialize to the JSON shapes the spec/handlers expect:
- ``AlbumSummary`` / ``ArtistGroup`` mirror the ``GET /api/library`` and
  ``GET /api/artist`` response dicts, and
- ``ErrorResponse`` carries a short ``error`` plus optional field-level ``detail``,
- ``TrackBpmComputeResponse`` carries ``status`` and optional ``bpm``,
- the three artwork models pin the two contracts the gallery depends on: the
  public image projection carries **no upstream URL**, and both id fields are
  decimal strings.
"""

import json

import pytest
from pydantic import ValidationError

from beetdeck.schemas import (
    AlbumSummary,
    ArtistGroup,
    ErrorResponse,
    ValidationErrorDetail,
)
from beetdeck.schemas.artwork import ArtworkImage, ArtworkListQuery, ArtworkListResponse
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


# ---------------------------------------------------------------------------
# ArtworkListQuery
# ---------------------------------------------------------------------------


def test_artwork_list_query_defaults_to_no_refresh():
    assert ArtworkListQuery().model_dump() == {"refresh": False}


@pytest.mark.parametrize("raw", ["1", "true", "True", "yes", "on"])
def test_artwork_list_query_accepts_truthy_query_strings(raw):
    assert ArtworkListQuery(refresh=raw).refresh is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_artwork_list_query_accepts_falsy_query_strings(raw):
    assert ArtworkListQuery(refresh=raw).refresh is False


def test_artwork_list_query_rejects_non_boolean():
    # The route's 422 contract: a bad value is a framework validation error,
    # not a handler-owned 400.
    with pytest.raises(ValidationError):
        ArtworkListQuery(refresh="notabool")


def test_artwork_list_query_tolerates_unknown_params():
    # No extra="forbid": the SPA may cache-bust a listing GET, and refusing the
    # whole gallery over a stray param would be worse than ignoring it.
    query = ArtworkListQuery(refresh=True, ts="123")
    assert query.model_dump() == {"refresh": True}


# ---------------------------------------------------------------------------
# ArtworkImage
# ---------------------------------------------------------------------------


def _artwork_image(**overrides):
    kwargs = {
        "image_id": "1234567890",
        "types": ["Front"],
        "front": True,
        "back": False,
        "approved": True,
        "comment": "",
        "thumb_sizes": [250, 500, 1200],
        "mb_url": "https://musicbrainz.org/release/mbid-1",
    }
    kwargs.update(overrides)
    return ArtworkImage(**kwargs)


def test_artwork_image_serializes_expected_keys():
    assert _artwork_image().model_dump() == {
        "image_id": "1234567890",
        "types": ["Front"],
        "front": True,
        "back": False,
        "approved": True,
        "comment": "",
        "thumb_sizes": [250, 500, 1200],
        "mb_url": "https://musicbrainz.org/release/mbid-1",
        "width": None,
        "height": None,
    }


def test_artwork_image_dimensions_default_to_none():
    dumped = _artwork_image().model_dump()
    assert dumped["width"] is None
    assert dumped["height"] is None


def test_artwork_image_reports_measured_dimensions():
    dumped = _artwork_image(width=1425, height=1425).model_dump()
    assert (dumped["width"], dumped["height"]) == (1425, 1425)


def test_artwork_image_optional_fields_default():
    minimal = ArtworkImage(
        image_id="7", front=False, back=False, approved=False, mb_url="https://mb/r/x"
    )
    assert minimal.types == []
    assert minimal.thumb_sizes == []
    assert minimal.comment == ""


def test_artwork_image_refuses_the_internal_urls_map():
    # The public projection must never carry an upstream URL — every byte in this
    # feature goes through the backend proxy. extra="forbid" turns "the route
    # forgot to project" into a loud error instead of a leak.
    with pytest.raises(ValidationError) as excinfo:
        _artwork_image(urls={"250": "https://coverartarchive.org/…-250.jpg"})
    assert "urls" in str(excinfo.value)


def test_artwork_image_has_no_urls_field_at_all():
    assert "urls" not in ArtworkImage.model_fields
    assert "urls" not in ArtworkImage.model_json_schema()["properties"]
    assert "urls" not in _artwork_image().model_dump()


def test_artwork_image_id_is_a_json_string():
    payload = json.loads(_artwork_image().model_dump_json())
    assert payload["image_id"] == "1234567890"
    assert isinstance(payload["image_id"], str)


def test_artwork_image_id_rejects_a_raw_caa_number():
    # CAA sends the id as a JSON number; the route has to coerce it. If this ever
    # starts passing, the frontend's `current_image_id === image_id` breaks after
    # a reload and the "Current cover" marker silently disappears.
    with pytest.raises(ValidationError):
        _artwork_image(image_id=1234567890)


def test_artwork_image_json_schema_types():
    props = ArtworkImage.model_json_schema()["properties"]
    assert props["image_id"]["type"] == "string"
    assert props["thumb_sizes"]["items"]["type"] == "integer"


# ---------------------------------------------------------------------------
# ArtworkListResponse
# ---------------------------------------------------------------------------


def test_artwork_list_response_serializes_expected_keys():
    resp = ArtworkListResponse(
        mb_albumid="mbid-1",
        available=True,
        storage_enabled=True,
        source="storage",
        fetched_at="2026-08-14T12:00:00Z",
        current_image_id="1234567890",
        images=[_artwork_image()],
    )
    dumped = resp.model_dump()
    assert set(dumped) == {
        "mb_albumid",
        "available",
        "reason",
        "storage_enabled",
        "storage_error",
        "source",
        "fetched_at",
        "current_image_id",
        "images",
    }
    assert dumped["reason"] is None
    assert dumped["storage_error"] is None
    assert dumped["images"][0]["image_id"] == "1234567890"


def test_artwork_list_response_unavailable_shape():
    resp = ArtworkListResponse(
        mb_albumid="",
        available=False,
        reason="album has no MusicBrainz id",
        storage_enabled=False,
    )
    dumped = resp.model_dump()
    assert dumped["reason"] == "album has no MusicBrainz id"
    assert dumped["source"] is None
    assert dumped["fetched_at"] is None
    assert dumped["current_image_id"] is None
    assert dumped["images"] == []


def test_artwork_list_response_reports_storage_error():
    resp = ArtworkListResponse(
        mb_albumid="mbid-1",
        available=True,
        storage_enabled=True,
        storage_error="permission denied",
        source="cache",
    )
    dumped = resp.model_dump()
    assert dumped["storage_enabled"] is True
    assert dumped["storage_error"] == "permission denied"


@pytest.mark.parametrize("source", ["storage", "cache", "remote"])
def test_artwork_list_response_accepts_every_source(source):
    resp = ArtworkListResponse(
        mb_albumid="mbid-1", available=True, storage_enabled=False, source=source
    )
    assert resp.source == source


def test_artwork_list_response_rejects_unknown_source():
    with pytest.raises(ValidationError):
        ArtworkListResponse(
            mb_albumid="mbid-1", available=True, storage_enabled=False, source="disk"
        )


def test_artwork_list_response_current_image_id_is_a_json_string():
    payload = json.loads(
        ArtworkListResponse(
            mb_albumid="mbid-1",
            available=True,
            storage_enabled=False,
            current_image_id="1234567890",
        ).model_dump_json()
    )
    assert payload["current_image_id"] == "1234567890"
    assert isinstance(payload["current_image_id"], str)


def test_artwork_list_response_current_image_id_rejects_a_number():
    # Same pin as ArtworkImage.image_id, from the other side of the comparison:
    # the flex attr is TEXT, so the pair must be comparable with ===.
    with pytest.raises(ValidationError):
        ArtworkListResponse(
            mb_albumid="mbid-1",
            available=True,
            storage_enabled=False,
            current_image_id=1234567890,
        )


def test_artwork_list_response_json_schema():
    schema = ArtworkListResponse.model_json_schema()
    props = schema["properties"]
    assert props["mb_albumid"]["type"] == "string"
    assert props["available"]["type"] == "boolean"
    assert "$defs" in schema and "ArtworkImage" in schema["$defs"]
