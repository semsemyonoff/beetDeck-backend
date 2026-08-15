"""`GET /api/album/<id>/artwork[/<image_id>]` — the Cover Art Archive routes.

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

The binary route adds two of its own: the mirror is **lazy** (one rendition per
request, never a set), and a cold fetch reads its URL out of an `index.json` the
*listing route* wrote — which is why the closing test drives both routes rather
than hand-building a richer index than the code actually produces.
"""

import json
import os
import re
import sqlite3
from io import BytesIO

import httpx
import pytest
from PIL import Image

from beetdeck import state
from beetdeck.artwork_store import INDEX_FILENAME
from beetdeck.coverart import RateGate
from beetdeck.routes.artwork import STORAGE_FAILED

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


def image_bytes(width=250, height=250, fmt="JPEG"):
    """Real encoded image bytes — the store measures a header, not a mock."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format=fmt)
    return buffer.getvalue()


#: What a *listing* URL looks like: `/release/<mbid>` and nothing after it.
#: Deliberately not a `"/release/" in url` substring test — an image's original
#: lives at `/release/<mbid>/<image_id>.jpg`, so the substring matches both and a
#: request for `?size=full` would be answered with the JSON payload.
LISTING_URL = re.compile(r"/release/[0-9a-fA-F-]{36}$")


class FakeCaa:
    """A scriptable Cover Art Archive, plus the record of what was asked of it.

    One handler serves both halves of the upstream: `/release/<mbid>` answers the
    JSON listing, and every other URL is an image request. `calls` keeps them in
    one list on purpose — several tests assert on *what* was fetched, not only on
    how much.
    """

    def __init__(self):
        self.calls = []
        self.status = 200
        self.payload = CAA_PAYLOAD
        self.body = None
        self.raises = None
        self.image_status = 200
        self.image_content_type = "image/jpeg"
        self.image_data = image_bytes()
        self.image_raises = None

    @property
    def image_calls(self):
        return [url for url in self.calls if not LISTING_URL.search(url)]

    @property
    def listing_calls(self):
        return [url for url in self.calls if LISTING_URL.search(url)]

    def handler(self, request):
        self.calls.append(str(request.url))
        if not LISTING_URL.search(str(request.url)):
            return self._image()
        if self.raises is not None:
            raise self.raises
        if self.body is not None:
            return httpx.Response(self.status, text=self.body)
        if self.status == 200:
            return httpx.Response(200, json=self.payload)
        return httpx.Response(self.status, json={"error": "no"})

    def _image(self):
        if self.image_raises is not None:
            raise self.image_raises
        if self.image_status != 200:
            return httpx.Response(self.image_status, text="nope")
        return httpx.Response(
            200,
            content=self.image_data,
            headers={"Content-Type": self.image_content_type},
        )


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
        # ascending ints; the `small`/`large` aliases translate onto the 250/500
        # keys already present rather than adding entries of their own
        "thumb_sizes": [250, 500, 1200],
        "mb_url": MB_URL,
        "width": None,
        "height": None,
    }
    assert booklet["types"] == ["Booklet", "Medium"]
    assert booklet["approved"] is False
    assert booklet["comment"] == "booklet page 3"
    assert booklet["thumb_sizes"] == [250]


def test_a_thumbnail_size_this_build_cannot_serve_is_not_advertised(client, album_id, caa):
    """`thumb_sizes` is the menu; `sanitize_size` is what answers the order.

    CAA publishing a new rendition key must not make the API contradict itself —
    advertising a size here that the binary route then rejects with a `400`.
    """
    caa.payload = {
        "release": MB_URL,
        "images": [{**FRONT_IMAGE, "thumbnails": {**FRONT_IMAGE["thumbnails"], "2000": "u"}}],
    }

    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert body["images"][0]["thumb_sizes"] == [250, 500, 1200]
    assert client.get(image_url(album_id, size="2000")).status_code == 400


def test_the_named_thumbnail_aliases_are_translated_not_dropped(client, album_id, caa):
    """Older releases carry `small`/`large` and no numeric keys at all.

    Dropping them leaves `thumb_sizes` empty, and the grid then asks for the
    original on every tile — the traffic the numeric filter exists to prevent,
    reached from the other side.
    """
    caa.payload = {
        "release": MB_URL,
        "images": [
            {
                **FRONT_IMAGE,
                "thumbnails": {"small": "https://caa/s.jpg", "large": "https://caa/l.jpg"},
            }
        ],
    }

    body = client.get(f"/api/album/{album_id}/artwork").get_json()

    assert body["images"][0]["thumb_sizes"] == [250, 500]


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


# --- the binary route: resolution order -------------------------------------


def plant_image(root, name, mbid=MBID, data=None):
    """Drop an image straight into a root, bypassing the store's write path."""
    path = os.path.join(str(root), mbid, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data if data is not None else image_bytes())
    return path


def image_url(album_id, image_id="1234567890", size=None):
    url = f"/api/album/{album_id}/artwork/{image_id}"
    return url if size is None else f"{url}?size={size}"


def test_an_image_in_storage_is_served_without_a_network_call(client, album_id, caa, app):
    planted = plant_image(app.config["ARTWORK_STORAGE_DIR"], "1234567890-250.jpg")

    resp = client.get(image_url(album_id, size=250))

    assert resp.status_code == 200
    with open(planted, "rb") as handle:
        assert resp.data == handle.read()
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert resp.headers["Cache-Control"] == "public, max-age=86400"
    assert caa.calls == []


def test_a_cache_only_image_is_served_while_storage_is_enabled(client, album_id, caa, app):
    """Flipping storage on must not orphan what the cache already holds."""
    plant_image(app.config["ARTWORK_CACHE_DIR"], "1234567890-500.png")

    resp = client.get(image_url(album_id, size=500))

    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "image/png"
    assert caa.calls == []
    # …and it is not promoted into the storage root behind the request's back.
    assert not os.path.exists(
        os.path.join(app.config["ARTWORK_STORAGE_DIR"], MBID, "1234567890-500.png")
    )


def test_omitting_the_size_serves_the_smallest_rendition(client, album_id, app):
    plant_image(app.config["ARTWORK_STORAGE_DIR"], "1234567890-250.jpg")

    resp = client.get(image_url(album_id))

    assert resp.status_code == 200


def test_a_miss_is_fetched_stored_and_served(client, album_id, caa, app):
    caa.image_data = image_bytes(500, 400)

    resp = client.get(image_url(album_id, size=500))

    assert resp.status_code == 200
    assert resp.data == caa.image_data
    assert caa.image_calls == ["https://caa/1234567890-500.jpg"]
    # The bytes landed in the write root under the Content-Type's extension…
    stored = os.path.join(app.config["ARTWORK_STORAGE_DIR"], MBID, "1234567890-500.jpg")
    assert os.path.isfile(stored)
    # …and were measured on the way in, which is the only source of dimensions.
    assert read_index(app)["dims"]["1234567890"]["500"] == [500, 400]


def test_a_stored_image_is_not_fetched_twice(client, album_id, caa):
    """The mirror is the point: the second request must not leave the disk."""
    client.get(image_url(album_id, size=500))
    resp = client.get(image_url(album_id, size=500))

    assert resp.status_code == 200
    assert len(caa.image_calls) == 1


def test_the_content_type_survives_the_round_trip(client, album_id, caa):
    """The stored extension is the only surviving record of the CAA header."""
    caa.image_content_type = "image/png"
    caa.image_data = image_bytes(fmt="PNG")

    resp = client.get(image_url(album_id, size="full"))

    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "image/png"


# --- the binary route: the lazy mirror --------------------------------------


def test_asking_for_full_does_not_pre_fetch_the_thumbnails(client, album_id, caa, app):
    client.get(image_url(album_id, size="full"))

    assert caa.image_calls == ["https://coverartarchive.org/release/x/1234567890.jpg"]
    release_dir = os.path.join(app.config["ARTWORK_STORAGE_DIR"], MBID)
    assert sorted(os.listdir(release_dir)) == ["1234567890-full.jpg", INDEX_FILENAME]


def test_asking_for_a_thumbnail_does_not_pre_fetch_the_original(client, album_id, caa, app):
    client.get(image_url(album_id, size=250))

    assert caa.image_calls == ["https://caa/1234567890-250.jpg"]
    release_dir = os.path.join(app.config["ARTWORK_STORAGE_DIR"], MBID)
    assert sorted(os.listdir(release_dir)) == ["1234567890-250.jpg", INDEX_FILENAME]


def test_only_the_requested_image_is_mirrored(client, album_id, caa):
    """One tile is one fetch — the other scans in the release stay untouched."""
    client.get(image_url(album_id, size=250))

    assert caa.image_calls == ["https://caa/1234567890-250.jpg"]


# --- the binary route: the refusals -----------------------------------------


def test_unknown_album_is_404_for_an_image_too(client):
    resp = client.get(image_url(4242, size=250))

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Album not found"}


def test_an_album_without_a_release_id_has_no_image(client, db_path, caa):
    album = insert_album(db_path, mb_albumid="")

    resp = client.get(image_url(album, size=250))

    assert resp.status_code == 404
    assert caa.calls == []


def test_an_image_id_outside_the_release_is_404(client, album_id, caa):
    resp = client.get(image_url(album_id, image_id="5555555555", size=250))

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Image not found"}
    # The listing was established once; the unknown id did not buy a second one.
    assert caa.image_calls == []
    assert len(caa.calls) == 1


def test_an_unknown_id_against_an_existing_index_does_not_go_upstream(client, album_id, caa):
    """One CAA lookup per release, not one per bad request."""
    client.get(f"/api/album/{album_id}/artwork")
    caa.calls.clear()

    resp = client.get(image_url(album_id, image_id="5555555555", size=250))

    assert resp.status_code == 404
    assert caa.calls == []


@pytest.mark.parametrize("size", ["0", "251", "small", "large", "", "-250"])
def test_a_size_outside_the_set_is_400(client, album_id, caa, size):
    resp = client.get(image_url(album_id, size=size))

    assert resp.status_code == 400
    assert "size must be one of" in resp.get_json()["error"]
    assert caa.calls == []


@pytest.mark.parametrize("size", ["full ", " 250", "FULL"])
def test_a_padded_or_uppercased_size_is_normalised_rather_than_refused(client, album_id, app, size):
    """`sanitize_size` strips and lowercases, so a stray space is not a refusal.

    The value only ever names one of four renditions; taking the gallery down
    over whitespace would be a worse failure than reading through it.
    """
    plant_image(app.config["ARTWORK_STORAGE_DIR"], "1234567890-250.jpg")
    plant_image(app.config["ARTWORK_STORAGE_DIR"], "1234567890-full.jpg")

    resp = client.get(image_url(album_id, size=size))

    assert resp.status_code == 200


@pytest.mark.parametrize("image_id", ["12a", r"..%5C..%5Cetc", "-12", "12.0", "%D9%A1%D9%A2"])
def test_a_non_numeric_image_id_is_400(client, album_id, caa, image_id):
    """The id becomes a filename, so it is checked before any path is built.

    The traversal case uses backslashes: `%2F` decodes to a separator werkzeug
    splits on, so that shape never reaches the handler to be refused by it — the
    router answers `404` first. What is pinned here is the handler's own check.
    """
    resp = client.get(image_url(album_id, image_id=image_id, size=250))

    assert resp.status_code == 400
    assert "image_id" in resp.get_json()["error"]
    assert caa.calls == []


def test_a_traversal_shaped_path_never_reaches_the_route(client, caa):
    """`%2F` is a separator to werkzeug, so `..%2F..%2Fetc` is not one segment."""
    resp = client.get("/api/album/1/artwork/..%2F..%2Fetc")

    assert resp.status_code == 404
    assert caa.calls == []


def test_a_size_the_release_does_not_offer_is_400(client, album_id, caa):
    """CAA stopped generating `1200` for older uploads — ask for another size."""
    resp = client.get(image_url(album_id, image_id="9876543210", size=1200))

    assert resp.status_code == 400
    assert "1200" in resp.get_json()["error"]
    assert caa.image_calls == []


def test_a_failed_image_fetch_is_502(client, album_id, caa):
    caa.image_status = 503

    resp = client.get(image_url(album_id, size=250))

    assert resp.status_code == 502
    assert "503" in resp.get_json()["error"]


def test_an_image_transport_failure_is_502(client, album_id, caa):
    caa.image_raises = httpx.ConnectError("no route to host")

    resp = client.get(image_url(album_id, size=250))

    assert resp.status_code == 502
    assert "unreachable" in resp.get_json()["error"]


def test_a_failed_listing_lookup_is_502_for_a_cold_image(client, album_id, caa):
    """No index and no CAA: nothing about this image was established."""
    caa.raises = httpx.ConnectError("down")

    resp = client.get(image_url(album_id, size=250))

    assert resp.status_code == 502
    assert "unreachable" in resp.get_json()["error"]


def test_db_failure_is_500_for_an_image_too(client, db_path, album_id, app):
    app.config["LIBRARY_DB"] = str(db_path) + ".missing"

    resp = client.get(image_url(album_id, size=250))

    assert resp.status_code == 500
    assert "error" in resp.get_json()


# --- the binary route against a real listing --------------------------------


def test_a_cold_fetch_works_against_the_index_the_listing_route_wrote(client, album_id, caa, app):
    """The loop closed: listing → `index.json` → cold binary fetch.

    Deliberately not a hand-built index. A richer fixture here would hide the
    exact defect this contract depends on — a `urls` map the listing route
    dropped on the way to disk leaves the binary route with nowhere to go, and
    only a real round trip through both routes can see that.
    """
    listing = client.get(f"/api/album/{album_id}/artwork").get_json()
    assert listing["images"][0]["width"] is None
    assert len(caa.calls) == 1
    caa.image_data = image_bytes(1425, 1425)

    resp = client.get(image_url(album_id, size="full"))

    assert resp.status_code == 200
    assert caa.image_calls == ["https://coverartarchive.org/release/x/1234567890.jpg"]
    # No second listing lookup: the index the first request wrote was enough.
    assert len(caa.listing_calls) == 1
    # …and the measurement now shows up on the listing, which had none before.
    refreshed = client.get(f"/api/album/{album_id}/artwork").get_json()
    assert refreshed["images"][0]["width"] == 1425
    assert refreshed["images"][0]["height"] == 1425


def test_a_cold_fetch_in_cache_only_mode_lands_in_the_cache(client, album_id, app):
    app.extensions["beetdeck_artwork_store"].storage_dir = None

    resp = client.get(image_url(album_id, size=250))

    assert resp.status_code == 200
    assert os.path.isfile(os.path.join(app.config["ARTWORK_CACHE_DIR"], MBID, "1234567890-250.jpg"))


# --- the apply route --------------------------------------------------------
#
# `POST …/artwork/<image_id>/apply` is the one write in this feature, and what
# it has to get right is not the write itself — `_save_cover_to_album` already
# owns that — but the two things around it:
#
# * the cover stash is invalidated **before** the first persistent mutation, so
#   a confirm token minted earlier can never revert a half-applied change;
# * the applied id is recorded as the same decimal string the listing route
#   compares against, or the *Current cover* marker vanishes on the next reload.


def apply_url(album_id, image_id="1234567890"):
    return f"/api/album/{album_id}/artwork/{image_id}/apply"


def marker(db_path, album_id):
    """The `beetdeck_cover_image_id` flex attr as the DB holds it, or `None`."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM album_attributes WHERE entity_id = ? AND key = ?",
            (album_id, "beetdeck_cover_image_id"),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


@pytest.fixture()
def beets(mocker):
    """A stand-in beets library for the apply route: one album, no file work.

    Returns the album mock. `_save_cover_to_album` is left alone — a test that
    wants the real one (the failure-injection group below) gets it.
    """
    album = mocker.MagicMock()
    library = mocker.MagicMock()
    library.get_album.return_value = album
    mocker.patch("beetdeck.routes.artwork._init_beets", return_value=library)
    return album


@pytest.fixture()
def saved(mocker):
    """`_save_cover_to_album` stubbed out, as seen by the apply route."""
    return mocker.patch("beetdeck.routes.artwork._save_cover_to_album")


def test_apply_writes_the_stored_original_as_the_cover(client, album_id, caa, app, beets, saved):
    resp = client.post(apply_url(album_id))

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
    stored = os.path.join(app.config["ARTWORK_STORAGE_DIR"], MBID, "1234567890-full.jpg")
    assert saved.call_args.args[1] == stored
    # The original, never a thumbnail — a 250 px file installed as the album
    # cover is a downgrade nobody asked for.
    assert caa.image_calls == [FRONT_IMAGE["image"]]


def test_apply_reuses_an_already_mirrored_original(client, album_id, caa, app, beets, saved):
    plant_image(app.config["ARTWORK_STORAGE_DIR"], "1234567890-full.jpg")

    resp = client.post(apply_url(album_id))

    assert resp.status_code == 200
    assert caa.calls == []


def test_apply_records_the_image_id_as_a_decimal_string(client, album_id, beets, saved, mocker):
    """The marker is set *after* the helper, with a `store()` of its own.

    `_save_cover_to_album` clears the attr and runs its own `store()` mid-way,
    so a marker written before it would be persisted as `""` and one written
    after it without a second `store()` would never reach the DB at all.
    """
    order = mocker.MagicMock()
    order.attach_mock(saved, "save")
    order.attach_mock(beets.__setitem__, "mark")
    order.attach_mock(beets.store, "store")

    resp = client.post(apply_url(album_id))

    assert resp.status_code == 200
    assert [call[0] for call in order.mock_calls] == ["save", "mark", "store"]
    beets.__setitem__.assert_called_once_with("beetdeck_cover_image_id", "1234567890")


def test_apply_drops_the_cover_stash(client, album_id, beets, saved):
    state.identify_tasks[f"cover_{album_id}"] = {
        "candidate_path": "/tmp/candidate.jpg",
        "stash_generation": 1,
    }

    resp = client.post(apply_url(album_id))

    assert resp.status_code == 200
    assert f"cover_{album_id}" not in state.identify_tasks


def test_the_stash_is_dropped_before_the_first_persistent_mutation(client, album_id, beets, saved):
    """Not after the write: `_save_cover_to_album` can raise half-way through.

    It persists `artpath` and only then embeds and sends `art_set`. A stash
    dropped afterwards would still be valid at that point, so a confirm holding
    its generation would pass its precondition and overwrite the half-applied
    cover — the exact invariant `_drop_cover_stash` exists to protect.
    """
    seen = {}
    saved.side_effect = lambda album, path: seen.update(
        stash_present=f"cover_{album_id}" in state.identify_tasks
    )
    state.identify_tasks[f"cover_{album_id}"] = {
        "candidate_path": "/tmp/candidate.jpg",
        "stash_generation": 1,
    }

    assert client.post(apply_url(album_id)).status_code == 200
    assert seen == {"stash_present": False}


# --- the apply route: the refusals ------------------------------------------


def test_apply_on_an_unknown_album_is_404(client, caa):
    resp = client.post(apply_url(4242))

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Album not found"}
    assert caa.calls == []


def test_apply_on_an_album_without_a_release_id_is_404(client, db_path, caa):
    album = insert_album(db_path, mb_albumid="")

    resp = client.post(apply_url(album))

    assert resp.status_code == 404
    assert caa.calls == []


@pytest.mark.parametrize("image_id", ["12a", "-12", "12.0", r"..%5C..%5Cetc"])
def test_apply_with_a_non_numeric_image_id_is_400(client, album_id, caa, image_id):
    resp = client.post(apply_url(album_id, image_id=image_id))

    assert resp.status_code == 400
    assert "image_id" in resp.get_json()["error"]
    assert caa.calls == []


def test_apply_of_an_image_outside_the_release_is_404(client, album_id, beets, saved):
    resp = client.post(apply_url(album_id, image_id="5555555555"))

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Image not found"}
    assert not saved.called


def test_apply_is_502_when_the_original_cannot_be_fetched(client, album_id, caa, beets, saved):
    caa.image_status = 503

    resp = client.post(apply_url(album_id))

    assert resp.status_code == 502
    assert "503" in resp.get_json()["error"]
    assert not saved.called


def test_apply_is_502_when_the_release_cannot_be_looked_up(client, album_id, caa, beets, saved):
    caa.raises = httpx.ConnectError("down")

    resp = client.post(apply_url(album_id))

    assert resp.status_code == 502
    assert not saved.called


def test_apply_is_404_when_the_album_vanished_between_read_and_write(
    client, album_id, beets, saved, mocker
):
    """The row is read through SQLite and re-fetched through beets under the lock."""
    mocker.patch("beetdeck.routes.artwork._init_beets").return_value.get_album.return_value = None

    resp = client.post(apply_url(album_id))

    assert resp.status_code == 404
    assert not saved.called


def test_apply_db_failure_is_500(client, db_path, album_id, app, caa):
    app.config["LIBRARY_DB"] = str(db_path) + ".missing"

    resp = client.post(apply_url(album_id))

    assert resp.status_code == 500
    assert "error" in resp.get_json()
    assert caa.calls == []


# --- the apply route: the marker round trip ---------------------------------


def test_current_image_id_survives_a_round_trip(client, album_id, db_path, mocker):
    """Apply, then re-read the listing: one decimal string on both sides.

    Driven through a **real** beets library rather than a mock, because the
    boundary this pins is the flex attr itself — CAA sends the id as a number
    and beets attrs are TEXT, so anything less than one canonical spelling makes
    `current_image_id === image_id` false in the page after a reload and the
    *Current cover* marker silently disappears.
    """
    mocker.patch("beetdeck.routes.artwork._save_cover_to_album")

    assert client.post(apply_url(album_id)).status_code == 200

    assert marker(db_path, album_id) == "1234567890"
    body = client.get(f"/api/album/{album_id}/artwork").get_json()
    assert body["current_image_id"] == "1234567890"
    assert body["current_image_id"] in [image["image_id"] for image in body["images"]]


# --- the apply route: the stash invariant, end to end -----------------------


def mint_cover_stash(client, mocker, tmp_path, album_id):
    """Drive `…/cover/fetch` for real and return the `stash_generation` it stamped.

    A hand-planted `identify_tasks` entry would prove the same thing about the
    apply route and nothing about the pair — the generation has to come from the
    endpoint that mints it for "a token issued before the apply" to mean what it
    says.
    """
    candidate_file = tmp_path / "candidate.jpg"
    Image.new("RGB", (600, 600), "blue").save(str(candidate_file), format="JPEG")
    library = mocker.MagicMock()
    library.get_album.return_value = mocker.MagicMock()
    mocker.patch("beetdeck.routes.cover._init_beets", return_value=library)
    candidate = mocker.MagicMock()
    candidate.path = str(candidate_file).encode()
    candidate.source_name = "coverart"
    plugin = mocker.MagicMock()
    plugin.name = "fetchart"
    plugin.art_for_album.return_value = candidate
    mocker.patch("beets.plugins.find_plugins", return_value=[plugin])

    fetched = client.post(f"/api/album/{album_id}/cover/fetch").get_json()
    assert fetched["found"] is True
    return fetched["stash_generation"]


def test_a_confirm_token_minted_before_an_apply_is_refused_after_it(
    client, album_id, tmp_path, beets, saved, mocker
):
    """The end-to-end form of the stash invariant.

    Asserting the key is absent proves the drop ran; it does not prove the
    precondition the drop exists to serve. This drives all three endpoints: a
    real fetch mints a generation, the apply overrules it, and the confirm
    holding it is refused instead of reverting the cover that replaced it.
    """
    generation = mint_cover_stash(client, mocker, tmp_path, album_id)
    reverting_save = mocker.patch("beetdeck.routes.cover._save_cover_to_album")

    assert client.post(apply_url(album_id)).status_code == 200

    resp = client.post(
        f"/api/album/{album_id}/cover/confirm", json={"expected_generation": generation}
    )
    assert resp.status_code == 409
    assert "changed since the preview" in resp.get_json()["error"]
    # …and the cover the apply installed is still the one on the album.
    assert not reverting_save.called
    assert saved.call_count == 1


# --- the apply route: failure injection -------------------------------------
#
# Every arm answers 500 **and** leaves the previously minted generation refused.
# A 500 that left the stash valid is the dangerous shape: the cover may already
# have been half-written, and a confirm holding that token would overwrite it.


@pytest.fixture()
def library_on_disk(mocker, tmp_path, db_path):
    """A beets album backed by a real directory, so the *real* save runs.

    Only the two beets calls that touch audio files are stubbed by the tests
    below; everything else — the resize, the move, the flex-attr write — is the
    code under test.
    """
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    item = mocker.MagicMock()
    item.path = str(album_dir / "01.mp3").encode()
    album = mocker.MagicMock()
    album.items.return_value = [item]
    album.art_destination.return_value = str(album_dir / "cover.jpg").encode()
    library = mocker.MagicMock()
    library.get_album.return_value = album
    mocker.patch("beetdeck.routes.artwork._init_beets", return_value=library)
    return album


def assert_apply_failed_with_the_stash_invalidated(client, album_id, generation, resp, message):
    assert resp.status_code == 500
    assert message in resp.get_json()["error"]
    confirm = client.post(
        f"/api/album/{album_id}/cover/confirm", json={"expected_generation": generation}
    )
    assert confirm.status_code == 409


def test_a_failing_embed_is_500_and_still_invalidates_the_stash(
    client, album_id, tmp_path, library_on_disk, mocker
):
    generation = mint_cover_stash(client, mocker, tmp_path, album_id)
    mocker.patch("beetsplug._utils.art.embed_album", side_effect=RuntimeError("no mutagen"))
    mocker.patch("beets.plugins.send")

    resp = client.post(apply_url(album_id))

    assert_apply_failed_with_the_stash_invalidated(client, album_id, generation, resp, "no mutagen")


def test_a_failing_art_set_is_500_and_still_invalidates_the_stash(
    client, album_id, tmp_path, library_on_disk, mocker
):
    """`artpath` is already persisted by the time this event goes out."""
    generation = mint_cover_stash(client, mocker, tmp_path, album_id)
    mocker.patch("beetsplug._utils.art.embed_album")
    mocker.patch("beets.plugins.send", side_effect=RuntimeError("plugin blew up"))

    resp = client.post(apply_url(album_id))

    assert_apply_failed_with_the_stash_invalidated(
        client, album_id, generation, resp, "plugin blew up"
    )


def test_a_failing_marker_store_is_500_and_still_invalidates_the_stash(
    client, album_id, tmp_path, beets, saved, mocker
):
    """The cover is written by now; only the marker write is left to fail."""
    generation = mint_cover_stash(client, mocker, tmp_path, album_id)
    beets.store.side_effect = RuntimeError("database is locked")

    resp = client.post(apply_url(album_id))

    assert saved.called
    assert_apply_failed_with_the_stash_invalidated(
        client, album_id, generation, resp, "database is locked"
    )


def test_an_undecodable_original_leaves_the_existing_cover_untouched(
    client, album_id, caa, tmp_path, library_on_disk, mocker
):
    """The one failure that must not be destructive.

    Nothing upstream of `_save_cover_to_album` proves the bytes are an image:
    `fetch_image` checks only the status and a non-empty body, and an
    unrecognised `Content-Type` is stored under `.bin` rather than dropped. So
    an archive.org HTML error page served with a `200` reaches the helper — and
    a helper that cleared the album directory before asking PIL to decode would
    answer `500` having already deleted the cover the user still had.
    """
    caa.image_content_type = "text/html"
    caa.image_data = b"<html><body>Item not available</body></html>"
    album_dir = os.path.dirname(library_on_disk.art_destination.return_value.decode())
    existing = os.path.join(album_dir, "cover.jpg")
    Image.new("RGB", (600, 600), "green").save(existing, format="JPEG")
    mocker.patch("beetsplug._utils.art.embed_album")
    mocker.patch("beets.plugins.send")

    resp = client.post(apply_url(album_id))

    assert resp.status_code == 500
    assert os.path.exists(existing)


# --- the disk layer failing under a live app --------------------------------
#
# `ArtworkStore` re-raises rather than swallowing, and neither root is
# guaranteed writable for the life of the process. Every route that touches it
# owes the SPA a JSON body: Flask's HTML 500 makes `r.json()` yield null, and
# the gallery then sits on its loading state with nothing to retry.


def test_a_listing_whose_index_cannot_be_written_is_500(client, album_id, caa, mocker):
    mocker.patch(
        "beetdeck.artwork_store.ArtworkStore.replace_metadata",
        side_effect=OSError("No space left on device"),
    )

    resp = client.get(f"/api/album/{album_id}/artwork")

    assert resp.status_code == 500
    assert resp.get_json() == {"error": STORAGE_FAILED}


def test_an_image_that_cannot_be_read_from_disk_is_500(client, album_id, app, mocker):
    """The eviction race: `find_image` hands back a path, `send_file` opens it.

    The inline cache sweep runs on whichever thread crossed the byte threshold,
    so the file can go away between the two — which is why `send_file` sits
    inside the same guard as the resolve rather than after it.
    """
    plant_image(app.config["ARTWORK_STORAGE_DIR"], "1234567890-250.jpg")
    mocker.patch("beetdeck.routes.artwork.send_file", side_effect=OSError("stale file handle"))

    resp = client.get(image_url(album_id, size="250"))

    assert resp.status_code == 500
    assert resp.get_json() == {"error": STORAGE_FAILED}


def test_apply_is_500_when_the_original_cannot_be_stored(
    client, album_id, caa, beets, saved, mocker
):
    mocker.patch(
        "beetdeck.artwork_store.ArtworkStore.store_image",
        side_effect=OSError("Read-only file system"),
    )

    resp = client.post(apply_url(album_id))

    assert resp.status_code == 500
    assert resp.get_json() == {"error": STORAGE_FAILED}
    assert not saved.called
