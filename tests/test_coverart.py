"""Cover Art Archive client.

The one part of the artwork feature whose backing service is real network egress,
so it is driven entirely through a mocked HTTP layer (`httpx.MockTransport`) —
the same shape as `tests/test_mcp_musicbrainz.py`. The rate gate runs on an
injected clock: the point is that it *spaces* the JSON lookup and leaves the image
CDN alone, and asserting that by sleeping for real would cost a second per test.
"""

import logging

import httpx
import pytest

from beetdeck.coverart import (
    MIN_REQUEST_INTERVAL,
    CaaClient,
    RateGate,
    normalize_images,
)

USER_AGENT = "beetdeck-tests/0.0 ( tests@example.invalid )"

MBID = "11111111-1111-1111-1111-111111111111"


class FakeTime:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture()
def fake_time():
    return FakeTime()


def make_client(handler, fake_time=None, *, user_agent=USER_AGENT):
    """A client whose requests go to `handler` and whose gate never really waits."""
    timing = fake_time or FakeTime()
    return CaaClient(
        user_agent=user_agent,
        transport=httpx.MockTransport(handler),
        gate=RateGate(clock=timing.clock, sleep=timing.sleep),
    )


def json_handler(payload, status_code=200):
    """A handler answering every request with the same JSON body, recording them."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status_code, json=payload)

    handler.seen = seen
    return handler


def image_handler(data=b"\x89PNG\r\n", content_type="image/png", status_code=200):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status_code, content=data, headers={"Content-Type": content_type})

    handler.seen = seen
    return handler


#: A trimmed but faithful CAA payload: a front cover with the full thumbnail set,
#: and a booklet spread that predates the 1200 px derivative.
RELEASE_PAYLOAD = {
    "release": f"https://musicbrainz.org/release/{MBID}",
    "images": [
        {
            "id": 1234567890,
            "types": ["Front"],
            "front": True,
            "back": False,
            "approved": True,
            "comment": "",
            "edit": 42,
            "image": "https://coverartarchive.org/release/x/1234567890.jpg",
            "thumbnails": {
                "250": "https://coverartarchive.org/release/x/1234567890-250.jpg",
                "500": "https://coverartarchive.org/release/x/1234567890-500.jpg",
                "1200": "https://coverartarchive.org/release/x/1234567890-1200.jpg",
                "small": "https://coverartarchive.org/release/x/1234567890-250.jpg",
                "large": "https://coverartarchive.org/release/x/1234567890-500.jpg",
            },
        },
        {
            "id": "9876543210",
            "types": ["Booklet", "Spine"],
            "front": False,
            "back": False,
            "approved": False,
            "image": "https://coverartarchive.org/release/x/9876543210.png",
            "thumbnails": {
                "250": "https://coverartarchive.org/release/x/9876543210-250.jpg",
                "small": "https://coverartarchive.org/release/x/9876543210-250.jpg",
            },
        },
    ],
}


# ---------------------------------------------------------------------------
# Rate gate
# ---------------------------------------------------------------------------


class TestRateGate:
    def test_first_call_does_not_wait(self, fake_time):
        gate = RateGate(clock=fake_time.clock, sleep=fake_time.sleep)
        assert gate.wait() == 0.0
        assert fake_time.slept == []

    def test_second_call_waits_out_the_interval(self, fake_time):
        gate = RateGate(clock=fake_time.clock, sleep=fake_time.sleep)
        gate.wait()
        assert gate.wait() == pytest.approx(MIN_REQUEST_INTERVAL)
        assert fake_time.slept == [pytest.approx(MIN_REQUEST_INTERVAL)]

    def test_a_slow_caller_does_not_accumulate_credit(self, fake_time):
        gate = RateGate(clock=fake_time.clock, sleep=fake_time.sleep)
        gate.wait()
        fake_time.now += 30.0
        assert gate.wait() == 0.0
        assert gate.wait() == pytest.approx(MIN_REQUEST_INTERVAL)

    def test_a_custom_interval_is_honoured(self, fake_time):
        gate = RateGate(0.25, clock=fake_time.clock, sleep=fake_time.sleep)
        gate.wait()
        assert gate.wait() == pytest.approx(0.25)

    def test_consecutive_lookups_are_spaced_by_the_gate(self, fake_time):
        handler = json_handler(RELEASE_PAYLOAD)
        client = make_client(handler, fake_time)

        client.fetch_release(MBID)
        client.fetch_release(MBID)

        assert len(handler.seen) == 2
        assert fake_time.slept == [pytest.approx(MIN_REQUEST_INTERVAL)]

    def test_image_bytes_do_not_consume_the_gate(self, fake_time):
        """A 40-tile gallery must not serialize behind a 1 req/s courtesy gate: the
        bytes come from the archive.org CDN, which is outside that budget."""
        handler = image_handler()
        client = make_client(handler, fake_time)

        for _ in range(5):
            client.fetch_image("https://ia800.us.archive.org/x/1234567890-250.jpg")

        assert len(handler.seen) == 5
        assert fake_time.slept == []

    def test_an_image_fetch_does_not_delay_the_next_lookup(self, fake_time):
        """Not merely un-gated: it must not stamp the gate either, or the lookup
        after it would wait out an interval it never spent."""

        def handler(request):
            if "/release/" in request.url.path and not request.url.path.endswith(".jpg"):
                return httpx.Response(200, json=RELEASE_PAYLOAD)
            return httpx.Response(200, content=b"bytes", headers={"Content-Type": "image/jpeg"})

        client = make_client(handler, fake_time)
        client.fetch_image("https://ia800.us.archive.org/x/1234567890-250.jpg")
        client.fetch_release(MBID)

        assert fake_time.slept == []


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalizeImages:
    def test_the_record_keeps_the_metadata_and_the_urls(self):
        front = normalize_images(RELEASE_PAYLOAD)[0]
        assert front == {
            "image_id": "1234567890",
            "types": ["Front"],
            "front": True,
            "back": False,
            "approved": True,
            "comment": "",
            "urls": {
                "250": "https://coverartarchive.org/release/x/1234567890-250.jpg",
                "500": "https://coverartarchive.org/release/x/1234567890-500.jpg",
                "1200": "https://coverartarchive.org/release/x/1234567890-1200.jpg",
                "full": "https://coverartarchive.org/release/x/1234567890.jpg",
            },
        }

    def test_a_numeric_id_is_coerced_to_a_decimal_string(self):
        records = normalize_images(RELEASE_PAYLOAD)
        assert [r["image_id"] for r in records] == ["1234567890", "9876543210"]
        assert all(isinstance(r["image_id"], str) for r in records)

    def test_the_alias_keys_never_survive_as_keys(self):
        """`thumb_sizes` is derived from these keys — an alias left in place would
        make the binary route reject a size the API itself advertised."""
        for record in normalize_images(RELEASE_PAYLOAD):
            assert "small" not in record["urls"]
            assert "large" not in record["urls"]

    def test_alias_only_thumbnails_are_translated_to_their_numeric_sizes(self):
        """Not hypothetical: this is what CAA ships for the release Task 19 verified
        against. Discarding the aliases leaves `thumb_sizes` empty, and every tile
        then asks for the original."""
        records = normalize_images(
            {
                "images": [
                    {
                        "id": 3514546977,
                        "types": ["Front"],
                        "image": "https://coverartarchive.org/release/x/3514546977.jpg",
                        "thumbnails": {
                            "small": "https://coverartarchive.org/release/x/3514546977-250.jpg",
                            "large": "https://coverartarchive.org/release/x/3514546977-500.jpg",
                        },
                    }
                ]
            }
        )
        assert records[0]["urls"] == {
            "250": "https://coverartarchive.org/release/x/3514546977-250.jpg",
            "500": "https://coverartarchive.org/release/x/3514546977-500.jpg",
            "full": "https://coverartarchive.org/release/x/3514546977.jpg",
        }

    def test_a_numeric_key_wins_over_its_alias(self):
        """Both point at the same file, so the tie only decides which string is
        stored — but it must be decided, not left to dict ordering."""
        records = normalize_images(
            {
                "images": [
                    {
                        "id": 1,
                        "image": "https://x/1.jpg",
                        "thumbnails": {
                            "small": "https://x/1-alias.jpg",
                            "250": "https://x/1-numeric.jpg",
                        },
                    }
                ]
            }
        )
        assert records[0]["urls"]["250"] == "https://x/1-numeric.jpg"

    def test_an_unknown_non_numeric_key_is_still_discarded(self):
        records = normalize_images(
            {
                "images": [
                    {
                        "id": 1,
                        "image": "https://x/1.jpg",
                        "thumbnails": {"huge": "https://x/1-huge.jpg"},
                    }
                ]
            }
        )
        assert records[0]["urls"] == {"full": "https://x/1.jpg"}

    def test_a_missing_1200_thumbnail_is_simply_absent(self):
        booklet = normalize_images(RELEASE_PAYLOAD)[1]
        assert sorted(k for k in booklet["urls"] if k != "full") == ["250"]

    def test_the_originals_extension_is_preserved(self):
        """Rebuilding the URL by convention would guess `.jpg` for a PNG scan."""
        booklet = normalize_images(RELEASE_PAYLOAD)[1]
        assert booklet["urls"]["full"] == "https://coverartarchive.org/release/x/9876543210.png"

    def test_multiple_types_on_one_image_are_all_kept(self):
        assert normalize_images(RELEASE_PAYLOAD)[1]["types"] == ["Booklet", "Spine"]

    def test_a_missing_comment_becomes_an_empty_string(self):
        assert normalize_images(RELEASE_PAYLOAD)[1]["comment"] == ""

    def test_a_missing_flag_becomes_false_not_absent(self):
        record = normalize_images({"images": [{"id": 1, "image": "https://x/1.jpg"}]})[0]
        assert record["front"] is False
        assert record["back"] is False
        assert record["approved"] is False
        assert record["types"] == []

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"images": None},
            {"images": "nope"},
            [],
            None,
            "<html>",
        ],
    )
    def test_a_payload_without_a_usable_image_list_normalizes_to_nothing(self, payload):
        assert normalize_images(payload) == []

    def test_an_entry_without_a_numeric_id_is_dropped(self):
        payload = {
            "images": [
                {"id": "not-a-number", "image": "https://x/a.jpg"},
                {"image": "https://x/b.jpg"},
                {"id": True, "image": "https://x/c.jpg"},
                "garbage",
                # Arabic-Indic digits: `isdigit()` alone accepts them and `int()`
                # transliterates them into a *different* id than CAA sent, so the
                # record would be written under an id nothing can address again.
                {"id": "١٢٣", "image": "https://x/e.jpg"},
                {"id": -7, "image": "https://x/f.jpg"},
                {"id": 7, "image": "https://x/d.jpg"},
            ]
        }
        assert [r["image_id"] for r in normalize_images(payload)] == ["7"]

    def test_a_non_ascii_digit_thumbnail_key_is_not_transliterated(self):
        """Same rule as the id, on the other axis: `"٢٥٠"` is not `"250"`.

        Accepting it would put a key in `urls` that `thumb_sizes` advertises and
        the binary route then rejects, and one CAA never published.
        """
        payload = {
            "images": [
                {"id": 5, "image": "https://x/5.jpg", "thumbnails": {"٢٥٠": "https://x/5-x.jpg"}}
            ]
        }
        assert normalize_images(payload)[0]["urls"] == {"full": "https://x/5.jpg"}

    def test_a_non_string_thumbnail_url_is_skipped(self):
        payload = {
            "images": [
                {"id": 5, "image": "https://x/5.jpg", "thumbnails": {"250": None, "500": ""}}
            ]
        }
        assert normalize_images(payload)[0]["urls"] == {"full": "https://x/5.jpg"}

    def test_an_image_without_an_original_still_carries_its_thumbnails(self):
        payload = {"images": [{"id": 5, "thumbnails": {"250": "https://x/5-250.jpg"}}]}
        record = normalize_images(payload)[0]
        assert "full" not in record["urls"]
        assert record["urls"]["250"] == "https://x/5-250.jpg"


# ---------------------------------------------------------------------------
# `fetch_release`
# ---------------------------------------------------------------------------


class TestFetchRelease:
    def test_a_hit_comes_back_as_internal_records(self):
        handler = json_handler(RELEASE_PAYLOAD)
        result = make_client(handler).fetch_release(MBID)

        assert result.ok
        assert result.status_code == 200
        assert result.not_found is False
        assert [r["image_id"] for r in result.images] == ["1234567890", "9876543210"]
        # The raw payload never reaches a caller: normalization happens once, here.
        assert "thumbnails" not in result.images[0]
        assert "urls" in result.images[0]

    def test_the_request_asks_for_json_and_follows_the_redirect(self):
        redirected = []

        def handler(request):
            redirected.append(request)
            if len(redirected) == 1:
                return httpx.Response(
                    307, headers={"Location": "https://ia800.us.archive.org/x/index.json"}
                )
            return httpx.Response(200, json=RELEASE_PAYLOAD)

        result = make_client(handler).fetch_release(MBID)

        assert result.ok
        assert len(redirected) == 2
        assert redirected[0].url.path == f"/release/{MBID}"
        assert redirected[0].headers["accept"] == "application/json"
        assert redirected[0].headers["user-agent"] == USER_AGENT

    def test_a_release_with_an_empty_image_list_is_a_fact_not_an_error(self):
        result = make_client(json_handler({"images": []})).fetch_release(MBID)

        assert result.ok
        assert result.images == []
        assert result.not_found is False

    def test_a_404_is_data_not_an_error(self):
        """ "CAA has no images for this release" must never collapse into "CAA could
        not be reached" — the route branches on exactly this."""
        result = make_client(json_handler({"error": "Not Found"}, status_code=404)).fetch_release(
            MBID
        )

        assert result.ok
        assert result.not_found is True
        assert result.images == []
        assert result.status_code == 404

    @pytest.mark.parametrize("status_code", [500, 502, 503])
    def test_a_service_failure_is_an_error_and_is_not_retried(self, status_code):
        handler = json_handler({"error": "down"}, status_code=status_code)
        result = make_client(handler).fetch_release(MBID)

        assert not result.ok
        assert result.not_found is False
        assert f"HTTP {status_code}" in result.message
        assert len(handler.seen) == 1

    def test_a_refused_connection_comes_back_as_data(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        result = make_client(handler).fetch_release(MBID)

        assert result.status_code is None
        assert "unreachable" in result.message

    def test_a_timeout_comes_back_as_data(self):
        def handler(request):
            raise httpx.ReadTimeout("too slow", request=request)

        result = make_client(handler).fetch_release(MBID)

        assert result.status_code is None
        assert "timed out" in result.message

    def test_an_unbuildable_url_comes_back_as_data(self):
        """`httpx.InvalidURL` does not inherit from `HTTPError`; `.strip()` removes
        surrounding whitespace but not an embedded control byte."""
        result = make_client(json_handler(RELEASE_PAYLOAD)).fetch_release("bad\x00mbid")

        assert result.status_code is None
        assert "not a usable release id" in result.message

    def test_a_non_json_body_is_an_error_not_an_empty_release(self):
        def handler(request):
            return httpx.Response(200, text="<html>maintenance</html>")

        result = make_client(handler).fetch_release(MBID)

        assert not result.ok
        assert "non-JSON" in result.message

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_a_blank_mbid_never_leaves_the_process(self, blank):
        handler = json_handler(RELEASE_PAYLOAD)
        result = make_client(handler).fetch_release(blank)

        assert not result.ok
        assert handler.seen == []

    def test_it_logs_one_line_per_outbound_request(self, caplog):
        """Task 19 observes "the second gallery open never went to the network" by
        the absence of this line — it is instrumentation, not decoration."""
        handler = json_handler(RELEASE_PAYLOAD)
        client = make_client(handler)

        with caplog.at_level(logging.INFO, logger="beetdeck"):
            client.fetch_release(MBID)

        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("CAA GET")]
        assert lines == [f"CAA GET https://coverartarchive.org/release/{MBID}"]


# ---------------------------------------------------------------------------
# `fetch_image`
# ---------------------------------------------------------------------------


class TestFetchImage:
    def test_a_hit_carries_the_bytes_and_the_content_type(self):
        handler = image_handler(b"jpegbytes", "image/jpeg")
        result = make_client(handler).fetch_image("https://ia800.us.archive.org/x/1-250.jpg")

        assert result.ok
        assert result.data == b"jpegbytes"
        assert result.content_type == "image/jpeg"
        assert handler.seen[0].url.host == "ia800.us.archive.org"

    def test_content_type_parameters_are_stripped(self):
        handler = image_handler(b"png", "image/png; charset=binary")
        result = make_client(handler).fetch_image("https://ia800.us.archive.org/x/1.png")

        assert result.content_type == "image/png"

    def test_a_service_failure_is_an_error(self):
        handler = image_handler(b"", "text/html", status_code=503)
        result = make_client(handler).fetch_image("https://ia800.us.archive.org/x/1.png")

        assert not result.ok
        assert result.data is None
        assert "HTTP 503" in result.message

    def test_an_empty_body_is_an_error_not_an_empty_image(self):
        handler = image_handler(b"", "image/jpeg")
        result = make_client(handler).fetch_image("https://ia800.us.archive.org/x/1.jpg")

        assert not result.ok
        assert "empty image body" in result.message

    def test_a_transport_failure_comes_back_as_data(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        result = make_client(handler).fetch_image("https://ia800.us.archive.org/x/1.jpg")

        assert result.status_code is None
        assert "unreachable" in result.message

    def test_a_timeout_comes_back_as_data(self):
        def handler(request):
            raise httpx.ReadTimeout("too slow", request=request)

        result = make_client(handler).fetch_image("https://ia800.us.archive.org/x/1.jpg")

        assert result.status_code is None
        assert "timed out" in result.message

    def test_an_unbuildable_url_comes_back_as_data(self):
        result = make_client(image_handler()).fetch_image("https://ia800.us.archive.org/\x00.jpg")

        assert result.status_code is None
        assert "not a usable image URL" in result.message

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_a_blank_url_never_leaves_the_process(self, blank):
        handler = image_handler()
        result = make_client(handler).fetch_image(blank)

        assert not result.ok
        assert handler.seen == []

    def test_it_logs_one_line_per_outbound_request(self, caplog):
        client = make_client(image_handler())
        url = "https://ia800.us.archive.org/x/1-250.jpg"

        with caplog.at_level(logging.INFO, logger="beetdeck"):
            client.fetch_image(url)

        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("CAA GET")]
        assert lines == [f"CAA GET {url}"]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_an_empty_user_agent_is_not_fatal(self, blank):
        """CAA/archive.org, unlike MusicBrainz `ws/2`, serves anonymous clients.
        Raising here would take `create_app()` down for an unset config default."""
        handler = json_handler(RELEASE_PAYLOAD)
        client = make_client(handler, user_agent=blank)

        assert client.fetch_release(MBID).ok
        # No header of ours goes out; httpx falls back to its own default rather
        # than sending a blank one.
        assert handler.seen[0].headers["user-agent"].startswith("python-httpx/")

    def test_it_builds_its_own_gate_when_none_is_injected(self):
        client = CaaClient(user_agent=USER_AGENT)
        try:
            assert isinstance(client._gate, RateGate)
        finally:
            client.close()

    def test_it_closes_as_a_context_manager(self):
        with make_client(json_handler(RELEASE_PAYLOAD)) as client:
            assert client.fetch_release(MBID).ok
        assert client._client.is_closed
