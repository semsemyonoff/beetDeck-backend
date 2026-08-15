"""`GET /api/album/<id>/artwork` — the Cover Art Archive listing route.

Only the outbound CAA HTTP is mocked, through the transport the app factory
injects into the app-scoped `CaaClient` (`tests/conftest.py`). Everything else is
real: the Flask app, the read-only SQLite connection, and the on-disk artwork
store under `tmp_path`.

Three contracts these tests exist to pin, beyond the happy path:

* the three "no grid" outcomes stay distinct — `available: false`, an empty
  `images` list, and a `502` are different answers to different questions;
* a second listing is served from `index.json` with **no** second CAA request —
  the whole point of the disk layer, and invisible from the response alone;
* the response never carries a `urls` map. `ArtworkImage` forbids extras, so a
  route that forgot to project would 500 here rather than leak an upstream URL
  into the page.
"""

import json
import os
import sqlite3

import httpx
import pytest

from beetdeck.artwork_store import INDEX_FILENAME
from beetdeck.coverart import RateGate

from .conftest import insert_album

MBID = "1f2c3d4e-5a6b-4c8d-9e0f-1a2b3c4d5e6f"
MB_URL = f"https://musicbrainz.org/release/{MBID}"

FRONT_IMAGE = {
    "id": 1234567890,
    "types": ["Front"],
    "front": True,
    "back": False,
    "approved": True,
    "comment": "",
    "image": "https://coverartarchive.org/release/x/1234567890.jpg",
    "thumbnails": {
        "250": "https://caa/1234567890-250.jpg",
        "500": "https://caa/1234567890-500.jpg",
        "1200": "https://caa/1234567890-1200.jpg",
        "small": "https://caa/1234567890-250.jpg",
        "large": "https://caa/1234567890-500.jpg",
    },
}

BOOKLET_IMAGE = {
    # No `1200` thumbnail — CAA stopped generating it for older uploads, which is
    # exactly why `thumb_sizes` is derived per image rather than assumed.
    "id": 9876543210,
    "types": ["Booklet", "Medium"],
    "front": False,
    "back": False,
    "approved": False,
    "comment": "booklet page 3",
    "image": "https://coverartarchive.org/release/x/9876543210.png",
    "thumbnails": {"250": "https://caa/9876543210-250.jpg"},
}

CAA_PAYLOAD = {"release": MB_URL, "images": [FRONT_IMAGE, BOOKLET_IMAGE]}


class FakeCaa:
    """A scriptable Cover Art Archive, plus the record of what was asked of it."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.payload = CAA_PAYLOAD
        self.body = None
        self.raises = None

    def handler(self, request):
        self.calls.append(str(request.url))
        if self.raises is not None:
            raise self.raises
        if self.body is not None:
            return httpx.Response(self.status, text=self.body)
        if self.status == 200:
            return httpx.Response(200, json=self.payload)
        return httpx.Response(self.status, json={"error": "no"})


@pytest.fixture()
def caa():
    return FakeCaa()


@pytest.fixture()
def caa_handler(caa):
    """Overrides the conftest fixture: every CAA call in this module lands here."""
    return caa.handler


@pytest.fixture(autouse=True)
def ungated(app):
    """Drop the courtesy interval for the app-scoped client.

    The gate is real (and tested in `test_coverart.py`); leaving it armed here
    would spend a wall-clock second per extra lookup to assert nothing about the
    route.
    """
    app.extensions["beetdeck_caa"]._gate = RateGate(0.0)


@pytest.fixture()
def album_id(db_path):
    return insert_album(db_path, mb_albumid=MBID)


def set_marker(db_path, album_id, value):
    """Write the `beetdeck_cover_image_id` flex attr the apply route will set."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO album_attributes (entity_id, key, value) VALUES (?, ?, ?)",
            (album_id, "beetdeck_cover_image_id", value),
        )
        conn.commit()
    finally:
        conn.close()


def index_path(app, mbid=MBID):
    return os.path.join(app.config["ARTWORK_STORAGE_DIR"], mbid, INDEX_FILENAME)


def read_index(app, mbid=MBID):
    with open(index_path(app, mbid), encoding="utf-8") as handle:
        return json.load(handle)


def write_index(app, payload, mbid=MBID):
    path = index_path(app, mbid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def stored_record(image, urls):
    """The internal record shape `normalize_images` produces, for a hand-built index."""
    return {
        "image_id": str(image["id"]),
        "types": image["types"],
        "front": image["front"],
        "back": image["back"],
        "approved": image["approved"],
        "comment": image["comment"],
        "urls": urls,
    }


# --- the album itself -------------------------------------------------------


def test_unknown_album_is_404(client):
    resp = client.get("/api/album/4242/artwork")

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Album not found"}


def test_album_without_mbid_is_available_false(client, db_path, caa):
    album = insert_album(db_path, mb_albumid="")

    resp = client.get(f"/api/album/{album}/artwork")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is False
    assert body["reason"] == "album has no MusicBrainz id"
    assert body["mb_albumid"] == ""
    assert body["images"] == []
    assert body["source"] is None
    assert body["fetched_at"] is None
    # No gallery is possible, so nothing was asked of CAA.
    assert caa.calls == []


def test_album_with_unusable_mbid_is_available_false(client, db_path, caa):
    """A row whose `mb_albumid` is not a release id cannot address CAA at all."""
    album = insert_album(db_path, mb_albumid="not-a-release-id")

    resp = client.get(f"/api/album/{album}/artwork")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is False
    assert body["reason"] == "album's MusicBrainz id is not a release id"
    assert body["images"] == []
    assert caa.calls == []


def test_db_failure_is_500(client, db_path, album_id, app):
    app.config["LIBRARY_DB"] = str(db_path) + ".missing"

    resp = client.get(f"/api/album/{album_id}/artwork")

    assert resp.status_code == 500
    assert "error" in resp.get_json()


# --- the CAA outcomes -------------------------------------------------------


def test_listing_returns_the_release_images(client, album_id, caa):
    resp = client.get(f"/api/album/{album_id}/artwork")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is True
    assert body["reason"] is None
    assert body["mb_albumid"] == MBID
    assert body["source"] == "remote"
    assert body["fetched_at"] is None
    assert [image["image_id"] for image in body["images"]] == ["1234567890", "9876543210"]
    assert len(caa.calls) == 1
    assert caa.calls[0].endswith(f"/release/{MBID}")


def test_listing_projects_every_public_field(client, album_id):
    body = client.get(f"/api/album/{album_id}/artwork").get_json()
    front, booklet = body["images"]

    assert front == {
        "image_id": "1234567890",
        "types": ["Front"],
        "front": True,
        "back": False,
        "approved": True,
        "comment": "",
        # ascending ints, with the `small`/`large` aliases filtered out
        "thumb_sizes": [250, 500, 1200],
        "mb_url": MB_URL,
        "width": None,
        "height": None,
    }
    assert booklet["types"] == ["Booklet", "Medium"]
    assert booklet["approved"] is False
    assert booklet["comment"] == "booklet page 3"
    assert booklet["thumb_sizes"] == [250]


def test_listing_never_exposes_upstream_urls(client, album_id, app):
    """The proxy is the feature; an upstream URL in the page defeats all of it."""
    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    for image in body["images"]:
        assert "urls" not in image
    serialized = json.dumps(body)
    assert "coverartarchive.org" not in serialized
    assert "caa/" not in serialized
    # …but the index keeps them, or a cold binary fetch has nowhere to go.
    assert read_index(app)["images"][0]["urls"]["full"] == FRONT_IMAGE["image"]


def test_caa_404_is_an_empty_gallery_not_an_error(client, album_id, caa):
    caa.status = 404

    resp = client.get(f"/api/album/{album_id}/artwork")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is True
    assert body["reason"] is None
    assert body["images"] == []


def test_caa_503_is_502(client, album_id, caa):
    caa.status = 503

    resp = client.get(f"/api/album/{album_id}/artwork")

    assert resp.status_code == 502
    assert "503" in resp.get_json()["error"]


def test_transport_failure_is_502(client, album_id, caa):
    caa.raises = httpx.ConnectError("no route to host")

    resp = client.get(f"/api/album/{album_id}/artwork")

    assert resp.status_code == 502
    assert "unreachable" in resp.get_json()["error"]


def test_non_json_body_is_502(client, album_id, caa):
    caa.body = "<html>maintenance</html>"

    resp = client.get(f"/api/album/{album_id}/artwork")

    assert resp.status_code == 502
    assert "non-JSON" in resp.get_json()["error"]


def test_upstream_failure_does_not_write_an_index(client, album_id, caa, app):
    """A 502 established nothing, so it must not leave a listing behind."""
    caa.raises = httpx.ConnectError("down")

    client.get(f"/api/album/{album_id}/artwork")

    assert not os.path.exists(index_path(app))


# --- the disk layer ---------------------------------------------------------


def test_second_listing_is_served_from_disk(client, album_id, caa):
    first = client.get(f"/api/album/{album_id}/artwork").get_json()
    second = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert len(caa.calls) == 1
    assert second["source"] == "storage"
    assert second["fetched_at"] is not None
    assert first["fetched_at"] is None
    assert [i["image_id"] for i in second["images"]] == [i["image_id"] for i in first["images"]]


def test_cache_only_mode_reports_the_cache_as_the_source(client, album_id, app):
    """With storage off the same write lands in the cache root and says so."""
    app.extensions["beetdeck_artwork_store"].storage_dir = None

    client.get(f"/api/album/{album_id}/artwork")
    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert body["source"] == "cache"
    assert os.path.exists(
        os.path.join(app.config["ARTWORK_CACHE_DIR"], MBID, INDEX_FILENAME),
    )


def test_refresh_forces_a_new_lookup(client, album_id, caa):
    client.get(f"/api/album/{album_id}/artwork")
    body = client.get(f"/api/album/{album_id}/artwork?refresh=1").get_json()

    assert len(caa.calls) == 2
    assert body["source"] == "remote"
    assert body["fetched_at"] is None


def test_a_stale_index_is_refreshed(client, album_id, caa, app):
    write_index(
        app,
        {
            "fetched_at": "2001-01-01T00:00:00Z",
            "images": [stored_record(FRONT_IMAGE, {"250": "https://caa/old.jpg"})],
            "dims": {},
        },
    )

    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert len(caa.calls) == 1
    assert body["source"] == "remote"
    assert [i["image_id"] for i in body["images"]] == ["1234567890", "9876543210"]


def test_a_refresh_keeps_the_dims_already_measured(client, album_id, app):
    """`replace_metadata`, not a blind overwrite — a tile download races this."""
    write_index(
        app,
        {
            "fetched_at": "2001-01-01T00:00:00Z",
            "images": [],
            "dims": {"1234567890": {"full": [1425, 1425]}},
        },
    )

    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert body["images"][0]["width"] == 1425
    assert body["images"][0]["height"] == 1425
    assert read_index(app)["dims"]["1234567890"]["full"] == [1425, 1425]


def test_only_the_original_measurement_is_reported_as_the_size(client, album_id, app):
    """A thumbnail's dimensions are a fact about the thumbnail, not the scan."""
    write_index(
        app,
        {
            "fetched_at": "2001-01-01T00:00:00Z",
            "images": [],
            "dims": {"9876543210": {"250": [250, 177]}},
        },
    )

    body = client.get(f"/api/album/{album_id}/artwork").get_json()
    booklet = body["images"][1]

    assert booklet["image_id"] == "9876543210"
    assert booklet["width"] is None
    assert booklet["height"] is None


def test_an_unreadable_index_falls_back_to_a_lookup(client, album_id, caa, app):
    path = index_path(app)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ truncated")

    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert len(caa.calls) == 1
    assert body["source"] == "remote"


# --- the storage fields and the current-cover marker ------------------------


def test_storage_fields_report_the_configured_root(client, album_id):
    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert body["storage_enabled"] is True
    assert body["storage_error"] is None


def test_storage_error_is_surfaced(client, album_id, app):
    store = app.extensions["beetdeck_artwork_store"]
    store.storage_error = "Permission denied"

    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert body["storage_enabled"] is True
    assert body["storage_error"] == "Permission denied"


def test_current_image_id_comes_from_the_flex_attr(client, db_path, album_id):
    set_marker(db_path, album_id, "1234567890")

    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert body["current_image_id"] == "1234567890"
    assert body["images"][0]["image_id"] == body["current_image_id"]


@pytest.mark.parametrize("value", ["", "   ", "not-an-id"])
def test_a_blank_or_unusable_marker_reads_as_no_marker(client, db_path, album_id, value):
    """The attr is cleared by writing `""`, so empty means "no marker"."""
    set_marker(db_path, album_id, value)

    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert body["current_image_id"] is None


def test_the_marker_is_reported_even_without_a_gallery(client, db_path):
    album = insert_album(db_path, mb_albumid="")
    set_marker(db_path, album, "1234567890")

    body = client.get(f"/api/album/{album}/artwork").get_json()

    assert body["available"] is False
    assert body["current_image_id"] == "1234567890"


# --- request validation -----------------------------------------------------


def test_bad_refresh_value_is_422(client, album_id, caa):
    resp = client.get(f"/api/album/{album_id}/artwork?refresh=notabool")

    assert resp.status_code == 422
    body = resp.get_json()
    assert body["error"] == "Validation failed"
    assert any("refresh" in entry["loc"] for entry in body["detail"])
    # Validation runs before the handler, so nothing reached CAA.
    assert caa.calls == []


def test_an_unknown_query_param_is_tolerated(client, album_id):
    """A listing GET the SPA may cache-bust: a stray param must not 422."""
    resp = client.get(f"/api/album/{album_id}/artwork?_=1699999999")

    assert resp.status_code == 200
