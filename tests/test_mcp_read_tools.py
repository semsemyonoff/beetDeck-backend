"""MCP read tools, driven against the real Flask app in-process.

Only the unreachable-backend cases use a stub transport; everything else goes
through `beetdeck_client`, so the tool↔REST contract is exercised for real.
"""

import sqlite3

import httpx
import pytest

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.tools import client_for
from beetdeck_mcp.tools.read import TOOL_NAMES, build_tools
from tests.conftest import insert_album, insert_item


@pytest.fixture()
def tools(beetdeck_client):
    """The registered read tools, bound to the in-process app."""
    return {tool.__name__: tool for tool in build_tools(beetdeck_client)}


def set_album_flag(db_path, album_id, key, value="1"):
    """Set a `beetdeck_*` album attribute the way the tag/ignore routes do."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO album_attributes (entity_id, key, value) VALUES (?, ?, ?)",
            (album_id, key, value),
        )
        conn.commit()
    finally:
        conn.close()


def dead_tools(mcp_config):
    """The tools bound to a backend that refuses every connection."""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))
    return {tool.__name__: tool for tool in build_tools(client)}


class TestBuildTools:
    def test_exposes_exactly_the_declared_tool_names(self, tools):
        assert tuple(tools) == TOOL_NAMES

    def test_register_shares_one_client_per_config(self, mcp_config):
        assert client_for(mcp_config) is client_for(mcp_config)


# ---------------------------------------------------------------------------
# search_library
# ---------------------------------------------------------------------------


class TestSearchLibrary:
    def test_finds_artists_albums_and_tracks(self, tools, db_path):
        album_id = insert_album(db_path, album="Kid A", albumartist="Radiohead")
        insert_item(db_path, album_id, title="Everything In Its Right Place")

        assert tools["search_library"]("radio")["artists"] == ["Radiohead"]

        by_album = tools["search_library"]("kid a")
        assert [row["album"] for row in by_album["albums"]] == ["Kid A"]
        assert by_album["albums"][0]["albumartist"] == "Radiohead"

        by_track = tools["search_library"]("right place")
        assert [row["title"] for row in by_track["tracks"]] == ["Everything In Its Right Place"]
        assert by_track["tracks"][0]["album_id"] == album_id

    def test_empty_query_returns_an_empty_result_set(self, tools, db_path):
        insert_album(db_path, album="Kid A", albumartist="Radiohead")
        result = tools["search_library"]("")
        assert result == {"query": "", "artists": [], "albums": [], "tracks": []}

    def test_no_match_is_an_empty_result_not_an_error(self, tools, db_path):
        insert_album(db_path, album="Kid A", albumartist="Radiohead")
        result = tools["search_library"]("nothing here")
        assert result["albums"] == []
        assert "error" not in result

    def test_drops_empty_fields_from_hits(self, tools, db_path):
        album_id = insert_album(db_path, album="Untitled", albumartist="Nobody", year=None)
        insert_item(db_path, album_id, title="Untitled", artist=None)
        row = tools["search_library"]("untitled")["albums"][0]
        assert "year" not in row
        assert "artist" not in tools["search_library"]("untitled")["tracks"][0]

    def test_truncates_each_group_and_says_so(self, tools, db_path):
        for index in range(3):
            insert_album(db_path, album=f"Live {index}", albumartist=f"Band {index}")
        result = tools["search_library"]("live", limit=1)
        assert len(result["albums"]) == 1
        assert result["notes"] == [
            "albums: showing 1 of 3 — raise `limit` or call again with offset=1"
        ]

    @pytest.mark.parametrize("limit", [0, -1])
    def test_rejects_a_nonsensical_limit(self, tools, limit):
        assert "limit must be at least 1" in tools["search_library"]("x", limit=limit)["error"]

    def test_reports_an_unreachable_backend(self, mcp_config):
        result = dead_tools(mcp_config)["search_library"]("kid a")
        assert "could not reach the beetDeck API" in result["error"]
        assert "albums" not in result


# ---------------------------------------------------------------------------
# list_albums
# ---------------------------------------------------------------------------


class TestListAlbums:
    def test_flattens_the_artist_grouping(self, tools, db_path):
        insert_album(db_path, album="Kid A", albumartist="Radiohead", year=2000)
        insert_album(db_path, album="Homework", albumartist="Daft Punk", year=1997)

        result = tools["list_albums"]()
        assert result["total"] == 2
        assert result["offset"] == 0
        assert [(row["artist"], row["album"], row["year"]) for row in result["albums"]] == [
            ("Daft Punk", "Homework", 1997),
            ("Radiohead", "Kid A", 2000),
        ]
        assert result["albums"][0]["tagged"] is False
        assert "notes" not in result

    def test_untagged_filter_is_the_work_queue(self, tools, db_path):
        tagged = insert_album(db_path, album="Tagged", albumartist="A")
        ignored = insert_album(db_path, album="Ignored", albumartist="B")
        insert_album(db_path, album="Fresh", albumartist="C")
        set_album_flag(db_path, tagged, "beetdeck_tagged")
        set_album_flag(db_path, ignored, "beetdeck_ignored")

        result = tools["list_albums"](filter="untagged")
        assert [row["album"] for row in result["albums"]] == ["Fresh"]
        assert result["total"] == 1

    def test_ignored_filter_returns_only_ignored_albums(self, tools, db_path):
        ignored = insert_album(db_path, album="Ignored", albumartist="B")
        insert_album(db_path, album="Fresh", albumartist="C")
        set_album_flag(db_path, ignored, "beetdeck_ignored")

        result = tools["list_albums"](filter="ignored")
        assert [row["album"] for row in result["albums"]] == ["Ignored"]
        assert result["albums"][0]["ignored"] is True

    def test_a_tagged_album_is_still_listed_by_the_all_filter(self, tools, db_path):
        album_id = insert_album(db_path, album="Tagged", albumartist="A")
        set_album_flag(db_path, album_id, "beetdeck_tagged")
        result = tools["list_albums"](filter="all")
        assert result["albums"][0]["tagged"] is True

    def test_pages_with_limit_and_offset(self, tools, db_path):
        for index in range(3):
            insert_album(db_path, album=f"Album {index}", albumartist=f"Artist {index}")

        first = tools["list_albums"](limit=2)
        assert [row["album"] for row in first["albums"]] == ["Album 0", "Album 1"]
        assert first["notes"] == [
            "albums: showing 2 of 3 — raise `limit` or call again with offset=2"
        ]

        second = tools["list_albums"](limit=2, offset=2)
        assert [row["album"] for row in second["albums"]] == ["Album 2"]
        assert second["notes"] == ["albums: showing 1 of 3 (offset 2)"]
        assert second["total"] == 3

    def test_empty_library_is_an_empty_page(self, tools):
        assert tools["list_albums"]() == {
            "filter": "all",
            "total": 0,
            "offset": 0,
            "albums": [],
        }

    def test_rejects_a_negative_offset(self, tools):
        assert "offset must not be negative" in tools["list_albums"](offset=-1)["error"]

    def test_reports_an_unreachable_backend(self, mcp_config):
        result = dead_tools(mcp_config)["list_albums"]()
        assert "could not reach the beetDeck API" in result["error"]
        assert "albums" not in result


# ---------------------------------------------------------------------------
# get_album
# ---------------------------------------------------------------------------


class TestGetAlbum:
    def test_returns_the_tracklist_with_comparable_fields(self, tools, db_path, tmp_path):
        track = tmp_path / "01 - Idioteque.mp3"
        track.write_bytes(b"")
        album_id = insert_album(db_path, album="Kid A", albumartist="Radiohead", year=2000)
        insert_item(db_path, album_id, title="Idioteque", length=248.5, path=str(track).encode())

        result = tools["get_album"](album_id)
        assert result["album"] == "Kid A"
        assert len(result["tracks"]) == 1
        assert result["tracks"][0]["length_sec"] == 248.5
        assert result["tracks"][0]["length"] == "4:08"
        assert result["tracks"][0]["filename"] == "01 - Idioteque.mp3"
        assert "raw_tags" not in result["tracks"][0]

    def test_drops_empty_album_and_track_fields(self, tools, db_path):
        album_id = insert_album(db_path, album="Kid A", albumartist="Radiohead", label=None)
        insert_item(db_path, album_id, title="Idioteque", path=None, bpm=None)
        result = tools["get_album"](album_id)
        assert "label" not in result
        assert "mb_albumid" not in result
        assert "filename" not in result["tracks"][0]
        assert "bpm" not in result["tracks"][0]

    def test_include_raw_tags_adds_the_full_beets_row(self, tools, db_path):
        album_id = insert_album(db_path, album="Kid A", albumartist="Radiohead")
        item_id = insert_item(db_path, album_id, title="Idioteque", mb_trackid="mbid-1")

        result = tools["get_album"](album_id, include_raw_tags=True)
        raw = result["tracks"][0]["raw_tags"]
        assert raw["title"] == "Idioteque"
        assert raw["mb_trackid"] == "mbid-1"
        assert "raw_tags_error" not in result["tracks"][0]
        assert result["tracks"][0]["id"] == item_id

    def test_include_raw_tags_works_when_the_files_are_missing(self, tools, db_path):
        album_id = insert_album(db_path, album="Kid A", albumartist="Radiohead")
        insert_item(db_path, album_id, title="Idioteque", path=b"/gone/missing.mp3")

        result = tools["get_album"](album_id, include_raw_tags=True)
        track = result["tracks"][0]
        # The tag row comes from the DB, so a missing file costs the file-derived
        # flags, not the tags themselves.
        assert track["raw_tags"]["title"] == "Idioteque"
        assert track["filename"] == "missing.mp3"
        assert track["has_lrc"] is False

    def test_one_unreadable_track_does_not_abort_the_whole_album(self, mcp_config):
        """The tool's own promise: a bad track carries `raw_tags_error` and the
        rest of the album still comes back."""
        album = {
            "id": 1,
            "album": "Kid A",
            "albumartist": "Radiohead",
            "tracks": [{"id": 10, "title": "Idioteque"}, {"id": 11, "title": "Optimistic"}],
        }

        def handler(request):
            if request.url.path.endswith("/track/11/tags"):
                return httpx.Response(404, json={"error": "Track not found"})
            if request.url.path.endswith("/tags"):
                return httpx.Response(200, json={"title": "Idioteque"})
            return httpx.Response(200, json=album)

        client = BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))
        tools = {tool.__name__: tool for tool in build_tools(client)}

        result = tools["get_album"](1, include_raw_tags=True)

        good, bad = result["tracks"]
        assert good["raw_tags"]["title"] == "Idioteque"
        assert "raw_tags" not in bad
        assert "Track not found" in bad["raw_tags_error"]

    def test_an_album_with_no_tracks_still_carries_an_empty_tracklist(self, tools, db_path):
        album_id = insert_album(db_path, album="Empty", albumartist="Nobody")
        assert tools["get_album"](album_id)["tracks"] == []

    def test_unknown_album_id_is_an_error(self, tools):
        result = tools["get_album"](4242)
        assert "HTTP 404" in result["error"]
        assert "tracks" not in result

    def test_reports_an_unreachable_backend(self, mcp_config):
        result = dead_tools(mcp_config)["get_album"](1)
        assert "could not reach the beetDeck API" in result["error"]


# ---------------------------------------------------------------------------
# list_untagged_items
# ---------------------------------------------------------------------------


class TestListUntaggedItems:
    def test_lists_items_whose_album_has_no_album_artist(self, tools, db_path):
        loose = insert_album(db_path, album="Loose", albumartist="")
        tagged = insert_album(db_path, album="Tagged", albumartist="Radiohead")
        item_id = insert_item(db_path, loose, title="Track A", path=b"/music/a.mp3")
        insert_item(db_path, tagged, title="Track B")

        result = tools["list_untagged_items"]()
        assert result["total"] == 1
        assert result["items"][0]["id"] == item_id
        assert result["items"][0]["path"] == "/music/a.mp3"

    def test_empty_queue_is_an_empty_page(self, tools, db_path):
        album_id = insert_album(db_path, album="Tagged", albumartist="Radiohead")
        insert_item(db_path, album_id)
        assert tools["list_untagged_items"]() == {"total": 0, "offset": 0, "items": []}

    def test_pages_with_limit_and_offset(self, tools, db_path):
        loose = insert_album(db_path, album="Loose", albumartist="")
        for index in range(3):
            insert_item(db_path, loose, title=f"Track {index}", track=index + 1)

        result = tools["list_untagged_items"](limit=2)
        assert [item["title"] for item in result["items"]] == ["Track 0", "Track 1"]
        assert result["notes"] == [
            "items: showing 2 of 3 — raise `limit` or call again with offset=2"
        ]
        assert tools["list_untagged_items"](limit=2, offset=2)["items"][0]["title"] == "Track 2"

    def test_rejects_a_nonsensical_window(self, tools):
        assert "limit must be at least 1" in tools["list_untagged_items"](limit=0)["error"]

    def test_reports_an_unreachable_backend(self, mcp_config):
        result = dead_tools(mcp_config)["list_untagged_items"]()
        assert "could not reach the beetDeck API" in result["error"]
        assert "items" not in result
