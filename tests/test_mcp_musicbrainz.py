"""MusicBrainz client and tools.

This is the one part of the MCP server whose backing service is real network
egress, so it is the one part driven entirely through a mocked HTTP layer
(`httpx.MockTransport`). The rate gate is exercised with an injected clock — the
point is that it *spaces* requests, and asserting that by sleeping for real would
cost a second per test.
"""

import httpx
import pytest

from beetdeck_mcp.config import ConfigError, load_config
from beetdeck_mcp.musicbrainz import (
    MIN_REQUEST_INTERVAL,
    MusicBrainzClient,
    RateGate,
    build_release_query,
)
from beetdeck_mcp.tools import musicbrainz_client_for
from beetdeck_mcp.tools.musicbrainz import (
    MAX_SEARCH_LIMIT,
    TOOL_NAMES,
    artist_credit,
    build_tools,
    trim_release,
    trim_search_row,
)
from tests.conftest import MCP_TEST_ENV

USER_AGENT = "beetdeck-tests/0.0 (tests@example.invalid)"


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
    return MusicBrainzClient(
        user_agent,
        transport=httpx.MockTransport(handler),
        gate=RateGate(clock=timing.clock, sleep=timing.sleep),
    )


def json_handler(payload, status_code=200, headers=None):
    """A handler answering every request with the same JSON body, recording them."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status_code, json=payload, headers=headers or {})

    handler.seen = seen
    return handler


SEARCH_PAYLOAD = {
    "count": 2,
    "releases": [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "score": 100,
            "title": "Kid A",
            "status": "Official",
            "disambiguation": "reissue",
            "date": "2000-10-02",
            "country": "GB",
            "track-count": 10,
            "barcode": "724352773626",
            "text-representation": {"language": "eng", "script": "Latn"},
            "artist-credit": [
                {"name": "Radiohead", "artist": {"id": "abc", "name": "Radiohead"}},
            ],
            "label-info": [{"catalog-number": "7243 5 27753 2 6", "label": {"name": "Parlophone"}}],
            "media": [{"format": "CD", "disc-count": 1, "track-count": 10}],
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "score": 84,
            "title": "Kid A Mnesia",
            "date": "2021",
            "media": [{"format": "CD"}, {"format": "CD"}, {"format": "CD"}],
            "track-count": 34,
        },
    ],
}

RELEASE_PAYLOAD = {
    "id": "11111111-1111-1111-1111-111111111111",
    "title": "Kid A",
    "date": "2000-10-02",
    "country": "GB",
    "status": "Official",
    "disambiguation": "",
    "barcode": "724352773626",
    "packaging": "Jewel Case",
    "quality": "normal",
    "artist-credit": [{"name": "Radiohead", "artist": {"id": "abc", "name": "Radiohead"}}],
    "release-group": {"id": "grp", "primary-type": "Album", "secondary-types": []},
    "label-info": [{"catalog-number": "CDKID 1", "label": {"name": "Parlophone"}}],
    "media": [
        {
            "position": 1,
            "format": "CD",
            "track-count": 2,
            "tracks": [
                {
                    "id": "t1",
                    "position": 1,
                    "number": "1",
                    "title": "Everything In Its Right Place",
                    "length": 251000,
                    "recording": {"id": "r1", "length": 251000, "video": False},
                },
                {
                    "id": "t2",
                    "position": 2,
                    "number": "2",
                    "title": "Kid A",
                    "recording": {"id": "r2", "length": 264066},
                },
            ],
        }
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

    def test_consecutive_requests_are_spaced_by_the_gate(self, fake_time):
        handler = json_handler(SEARCH_PAYLOAD)
        client = make_client(handler, fake_time)

        client.search_releases(artist="Radiohead")
        client.search_releases(artist="Radiohead")

        assert len(handler.seen) == 2
        assert fake_time.slept == [pytest.approx(MIN_REQUEST_INTERVAL)]


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


class TestQueryConstruction:
    def test_artist_and_album_become_quoted_phrases(self):
        assert build_release_query("Radiohead", "Kid A") == 'artist:"Radiohead" AND release:"Kid A"'

    def test_track_count_and_year_are_added_as_filters(self):
        query = build_release_query("Radiohead", "Kid A", track_count=10, year=2000)
        assert query == 'artist:"Radiohead" AND release:"Kid A" AND tracks:10 AND date:2000*'

    def test_only_quotes_and_backslashes_are_escaped(self):
        assert (
            build_release_query(album='AC/DC "Live" \\ Rare')
            == 'release:"AC/DC \\"Live\\" \\\\ Rare"'
        )

    def test_empty_input_produces_no_query(self):
        assert build_release_query("  ", "", None, None) == ""

    def test_search_sends_the_query_limit_and_json_format(self):
        handler = json_handler(SEARCH_PAYLOAD)
        client = make_client(handler)

        client.search_releases(artist="Radiohead", album="Kid A", limit=3)

        request = handler.seen[0]
        assert request.url.path == "/ws/2/release"
        assert request.url.params["query"] == 'artist:"Radiohead" AND release:"Kid A"'
        assert request.url.params["limit"] == "3"
        assert request.url.params["fmt"] == "json"

    def test_lookup_requests_the_includes_the_tracklist_needs(self):
        handler = json_handler(RELEASE_PAYLOAD)
        client = make_client(handler)

        client.get_release(" 11111111-1111-1111-1111-111111111111 ")

        request = handler.seen[0]
        assert request.url.path == "/ws/2/release/11111111-1111-1111-1111-111111111111"
        assert "recordings" in request.url.params["inc"]
        assert "labels" in request.url.params["inc"]

    def test_a_search_with_nothing_to_search_for_never_leaves_the_process(self):
        handler = json_handler(SEARCH_PAYLOAD)
        client = make_client(handler)

        response = client.search_releases()

        assert not response.ok
        assert "artist or an album" in response.message
        assert handler.seen == []

    def test_a_blank_mbid_never_leaves_the_process(self):
        handler = json_handler(RELEASE_PAYLOAD)
        client = make_client(handler)

        response = client.get_release("   ")

        assert not response.ok
        assert handler.seen == []


# ---------------------------------------------------------------------------
# User agent
# ---------------------------------------------------------------------------


class TestUserAgent:
    def test_every_request_carries_it(self):
        handler = json_handler(SEARCH_PAYLOAD)
        client = make_client(handler)

        client.search_releases(album="Kid A")

        assert handler.seen[0].headers["user-agent"] == USER_AGENT

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_an_empty_one_is_refused_at_construction(self, blank):
        with pytest.raises(ValueError, match="User-Agent"):
            MusicBrainzClient(blank)

    def test_the_config_refuses_to_start_without_one(self):
        env = {**MCP_TEST_ENV, "MUSICBRAINZ_USER_AGENT": ""}
        with pytest.raises(ConfigError, match="MUSICBRAINZ_USER_AGENT"):
            load_config(env)


# ---------------------------------------------------------------------------
# Transport and service errors
# ---------------------------------------------------------------------------


class TestErrors:
    @pytest.mark.parametrize("status_code", [503, 429])
    def test_rate_limiting_is_surfaced_and_not_retried(self, status_code):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                status_code, json={"error": "slow down"}, headers={"Retry-After": "3"}
            )

        client = make_client(handler)
        response = client.search_releases(artist="Radiohead")

        assert not response.ok
        assert response.rate_limited is True
        assert "rate limiting" in response.message
        assert "Retry-After: 3" in response.message
        assert len(seen) == 1

    def test_a_not_found_release_carries_the_service_message(self):
        handler = json_handler({"error": "Not Found"}, status_code=404)
        client = make_client(handler)

        response = client.get_release("00000000-0000-0000-0000-000000000000")

        assert not response.ok
        assert response.rate_limited is False
        assert "HTTP 404" in response.message
        assert "Not Found" in response.message

    def test_a_non_json_body_is_an_error_not_an_empty_result(self):
        def handler(request):
            return httpx.Response(200, text="<html>maintenance</html>")

        response = make_client(handler).search_releases(album="Kid A")

        assert not response.ok
        assert "non-JSON" in response.message

    def test_a_refused_connection_comes_back_as_data(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        response = make_client(handler).search_releases(album="Kid A")

        assert response.status_code is None
        assert "unreachable" in response.message

    def test_a_timeout_comes_back_as_data(self):
        def handler(request):
            raise httpx.ReadTimeout("too slow", request=request)

        response = make_client(handler).search_releases(album="Kid A")

        assert response.status_code is None
        assert "timed out" in response.message


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------


class TestTrimming:
    def test_a_search_row_keeps_only_the_deciding_fields(self):
        row = trim_search_row(SEARCH_PAYLOAD["releases"][0])
        assert row == {
            "mbid": "11111111-1111-1111-1111-111111111111",
            "title": "Kid A",
            "artist": "Radiohead",
            "date": "2000-10-02",
            "country": "GB",
            "label": "Parlophone",
            "catalog_number": "7243 5 27753 2 6",
            "track_count": 10,
            "media": "CD",
            "status": "Official",
            "disambiguation": "reissue",
            "score": 100,
        }

    def test_absent_fields_are_dropped_rather_than_sent_as_null(self):
        row = trim_search_row(SEARCH_PAYLOAD["releases"][1])
        assert set(row) == {"mbid", "title", "date", "media", "track_count", "score"}

    def test_repeated_media_formats_are_collapsed(self):
        assert trim_search_row(SEARCH_PAYLOAD["releases"][1])["media"] == "3×CD"

    def test_mixed_media_formats_are_listed(self):
        row = trim_search_row({"id": "x", "media": [{"format": "CD"}, {"format": "DVD"}]})
        assert row["media"] == "CD + DVD"

    def test_join_phrases_are_kept_in_the_credit(self):
        credit = artist_credit(
            [
                {"name": "Radiohead", "joinphrase": " feat. "},
                {"artist": {"name": "Björk"}},
            ]
        )
        assert credit == "Radiohead feat. Björk"

    def test_a_release_carries_its_tracklist_with_lengths(self):
        release = trim_release(RELEASE_PAYLOAD)
        assert release["mbid"] == "11111111-1111-1111-1111-111111111111"
        assert release["artist"] == "Radiohead"
        assert release["label"] == "Parlophone"
        assert release["catalog_number"] == "CDKID 1"
        assert release["primary_type"] == "Album"
        assert release["track_count"] == 2
        assert release["media"][0]["format"] == "CD"
        assert release["media"][0]["tracks"] == [
            {
                "position": 1,
                "number": "1",
                "title": "Everything In Its Right Place",
                "length_sec": 251.0,
            },
            {"position": 2, "number": "2", "title": "Kid A", "length_sec": 264.1},
        ]

    def test_raw_musicbrainz_noise_never_reaches_the_result(self):
        release = trim_release(RELEASE_PAYLOAD)
        assert "packaging" not in release
        assert "quality" not in release
        assert "recording" not in release["media"][0]["tracks"][0]

    def test_a_track_carries_an_artist_only_when_it_differs_from_the_release(self):
        payload = {
            "id": "va",
            "title": "Now That's What I Call Music",
            "artist-credit": [{"name": "Various Artists"}],
            "media": [
                {
                    "position": 1,
                    "format": "CD",
                    "track-count": 2,
                    "tracks": [
                        {
                            "position": 1,
                            "title": "One",
                            "artist-credit": [{"name": "Various Artists"}],
                        },
                        {"position": 2, "title": "Two", "artist-credit": [{"name": "Aphex Twin"}]},
                    ],
                }
            ],
        }
        tracks = trim_release(payload)["media"][0]["tracks"]
        assert "artist" not in tracks[0]
        assert tracks[1]["artist"] == "Aphex Twin"

    def test_a_missing_or_zero_length_is_dropped_not_reported_as_zero(self):
        payload = {
            "id": "x",
            "media": [{"tracks": [{"position": 1, "title": "Untimed", "length": 0}]}],
        }
        assert trim_release(payload)["media"][0]["tracks"][0] == {
            "position": 1,
            "title": "Untimed",
        }

    def test_a_release_without_media_still_trims(self):
        assert trim_release({"id": "x", "title": "Bare"}) == {"mbid": "x", "title": "Bare"}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@pytest.fixture()
def tools_for():
    """Build the MusicBrainz tools over a given HTTP handler."""

    def factory(handler, fake_time=None):
        client = make_client(handler, fake_time)
        return {tool.__name__: tool for tool in build_tools(client)}

    return factory


class TestSearchTool:
    def test_exposes_exactly_the_declared_tool_names(self, tools_for):
        tools = tools_for(json_handler(SEARCH_PAYLOAD))
        assert tuple(tools) == TOOL_NAMES

    def test_returns_trimmed_rows_and_the_total_count(self, tools_for):
        tools = tools_for(json_handler(SEARCH_PAYLOAD))
        result = tools["musicbrainz_search_release"](artist="Radiohead", album="Kid A")

        assert result["count"] == 2
        assert [row["mbid"] for row in result["releases"]] == [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ]
        assert "notes" not in result

    def test_track_count_and_year_reach_the_query_only_when_set(self, tools_for):
        handler = json_handler(SEARCH_PAYLOAD)
        tools = tools_for(handler)

        tools["musicbrainz_search_release"](artist="Radiohead", track_count=0, year=0)
        assert handler.seen[0].url.params["query"] == 'artist:"Radiohead"'

        tools["musicbrainz_search_release"](artist="Radiohead", track_count=10, year=2000)
        assert (
            handler.seen[1].url.params["query"] == 'artist:"Radiohead" AND tracks:10 AND date:2000*'
        )

    def test_no_results_is_an_empty_list_with_a_way_forward(self, tools_for):
        tools = tools_for(json_handler({"count": 0, "releases": []}))
        result = tools["musicbrainz_search_release"](artist="Nobody", album="Nothing")

        assert result["count"] == 0
        assert result["releases"] == []
        assert "notes" in result

    def test_rate_limiting_is_reported_as_an_error_not_as_no_results(self, tools_for):
        tools = tools_for(json_handler({"error": "slow down"}, status_code=503))
        result = tools["musicbrainz_search_release"](artist="Radiohead")

        assert "releases" not in result
        assert "rate limiting" in result["error"]

    def test_a_missing_query_is_reported_without_a_round_trip(self, tools_for):
        handler = json_handler(SEARCH_PAYLOAD)
        tools = tools_for(handler)

        result = tools["musicbrainz_search_release"]()

        assert "artist or an album" in result["error"]
        assert handler.seen == []

    @pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_LIMIT + 1])
    def test_a_nonsensical_limit_is_rejected_rather_than_clamped(self, tools_for, limit):
        handler = json_handler(SEARCH_PAYLOAD)
        tools = tools_for(handler)

        result = tools["musicbrainz_search_release"](artist="Radiohead", limit=limit)

        assert "limit must be between" in result["error"]
        assert handler.seen == []


class TestGetReleaseTool:
    def test_returns_the_trimmed_release(self, tools_for):
        tools = tools_for(json_handler(RELEASE_PAYLOAD))
        result = tools["musicbrainz_get_release"]("11111111-1111-1111-1111-111111111111")

        assert result["title"] == "Kid A"
        assert result["media"][0]["tracks"][0]["length_sec"] == 251.0

    def test_an_unknown_mbid_is_an_error_not_an_empty_release(self, tools_for):
        tools = tools_for(json_handler({"error": "Not Found"}, status_code=404))
        result = tools["musicbrainz_get_release"]("00000000-0000-0000-0000-000000000000")

        assert "media" not in result
        assert "HTTP 404" in result["error"]

    def test_an_unreachable_service_is_an_error(self, tools_for):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        result = tools_for(handler)["musicbrainz_get_release"]("abc")
        assert "unreachable" in result["error"]


class TestClientSharing:
    def test_one_musicbrainz_client_per_config(self, mcp_config):
        # The ≤1 req/s gate lives on the client, so a second instance would
        # silently double the request rate.
        assert musicbrainz_client_for(mcp_config) is musicbrainz_client_for(mcp_config)

    def test_it_carries_the_configured_user_agent(self, mcp_config):
        assert musicbrainz_client_for(mcp_config).user_agent == (mcp_config.musicbrainz_user_agent)
