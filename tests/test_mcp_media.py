"""MCP cover-art and lyrics previews, driven against the real Flask app.

Only the two network sources are faked — the `fetchart` and `lyrics` beets
plugins, which would otherwise hit the internet. Everything else is real: the
routes, the anonymous stash in `state.identify_tasks`, the beets `Library` that
performs the write and the SQLite rows it lands in.

That matters most for the limitation these two kinds carry. Their guarantee is
the token's 5-minute lifetime and nothing else, because the stash has no identity
and no read endpoint — so `test_a_second_fetch_between_preview_and_apply_wins`
(one per kind) pins the documented behaviour with a test instead of leaving it as
prose that could quietly stop being true.
"""

import sqlite3

import httpx
import pytest

from beetdeck import state
from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.preview import PreviewStore
from beetdeck_mcp.tools.apply import TOOL_NAMES as APPLY_TOOL_NAMES
from beetdeck_mcp.tools.apply import build_tools as build_apply_tools
from beetdeck_mcp.tools.media import EXCERPT_CHARS, TOOL_NAMES, _excerpt, build_tools
from tests.conftest import insert_album, insert_item


@pytest.fixture()
def store():
    """A preview store per test — tokens must not leak between them."""
    return PreviewStore()


@pytest.fixture()
def make_tools(beetdeck_client, store):
    """The media + apply tools, sharing one store and one REST client."""

    def _make(client=None, *, readonly=False):
        rest = beetdeck_client if client is None else client
        tools = {tool.__name__: tool for tool in build_tools(rest, store)}
        tools.update(
            {tool.__name__: tool for tool in build_apply_tools(rest, store, readonly=readonly)}
        )
        return tools

    return _make


@pytest.fixture()
def tools(make_tools):
    return make_tools()


def dead_client(mcp_config):
    """A client whose backend refuses every connection."""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    return BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))


def fake_plugin(mocker, name):
    """A beets plugin stand-in. `name` cannot go through the constructor."""
    plugin = mocker.MagicMock()
    plugin.name = name
    return plugin


# ---------------------------------------------------------------------------
# preview_cover
# ---------------------------------------------------------------------------


@pytest.fixture()
def album(db_path):
    """One album whose art can be fetched."""
    album_id = insert_album(db_path, album="Test Album", albumartist="Test Artist")
    insert_item(db_path, album_id, title="Track One", path=b"/music/test/01.mp3")
    return album_id


@pytest.fixture()
def art_source(mocker, tmp_path):
    """The `fetchart` plugin, answering with a real file on disk."""

    def _install(source="coverart", filename="candidate.jpg"):
        image = tmp_path / filename
        image.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
        candidate = mocker.MagicMock()
        candidate.path = str(image).encode()
        candidate.source_name = source
        plugin = fake_plugin(mocker, "fetchart")
        plugin.art_for_album.return_value = candidate
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])
        return str(image)

    return _install


@pytest.fixture()
def saved_cover(mocker):
    """The embed step — real file writes are not what these tests are about."""
    return mocker.patch("beetdeck.routes.cover._save_cover_to_album")


class TestBuildTools:
    def test_exposes_exactly_the_declared_tool_names(self, tools):
        assert set(tools) == set(TOOL_NAMES) | set(APPLY_TOOL_NAMES)


class TestExcerpt:
    """`EXCERPT_CHARS` is what keeps a whole album's lyrics out of the agent's
    context; every other test here uses short fixtures, so nothing else would
    notice the truncation going away."""

    def test_text_at_the_limit_is_returned_whole(self):
        text = "a" * EXCERPT_CHARS
        assert _excerpt(text) == text

    def test_longer_text_is_cut_and_marked(self):
        excerpt = _excerpt("b" * (EXCERPT_CHARS + 1))
        assert len(excerpt) == EXCERPT_CHARS + 1  # the ellipsis is one char
        assert excerpt.endswith("…")
        assert excerpt[:-1] == "b" * EXCERPT_CHARS

    def test_line_breaks_and_runs_of_space_collapse_onto_one_line(self):
        assert _excerpt("first line\n\n  second   line ") == "first line second line"

    def test_trailing_space_is_not_left_before_the_ellipsis(self):
        # A cut landing on a space would otherwise read as "word …".
        text = ("word " * 200).strip()
        assert " …" not in _excerpt(text)


class TestPreviewCover:
    def test_reports_the_source_and_mints_a_token(self, tools, album, art_source, store):
        art_source(source="itunes")
        result = tools["preview_cover"](album)

        assert result["preview_token"].startswith("cover-")
        assert result["kind"] == "cover"
        assert result["target"] == f"album {album}"
        assert result["changes"] == {
            "album_id": album,
            "source": "itunes",
            "preview_url": f"/api/album/{album}/cover/preview",
        }
        assert len(store) == 1

    def test_says_the_guarantee_is_the_clock_alone(self, tools, album, art_source):
        art_source()
        result = tools["preview_cover"](album)

        assert "Time-limited only" in result["guarantee"]
        assert any("overwrites it and wins" in note for note in result["notes"])
        # 5 minutes, not the 15 the checked kinds get.
        assert 240 < result["expires_in_seconds"] <= 300

    def test_the_token_commits_the_bodyless_confirm(self, tools, album, art_source, store):
        art_source()
        token = tools["preview_cover"](album)["preview_token"]

        preview = store.peek(token)
        assert preview.method == "POST"
        assert preview.endpoint == f"/api/album/{album}/cover/confirm"
        assert preview.request_json() is None

    def test_nothing_found_mints_nothing(self, tools, album, mocker, store):
        plugin = fake_plugin(mocker, "fetchart")
        plugin.art_for_album.return_value = None
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])

        result = tools["preview_cover"](album)
        assert "no cover art was found" in result["error"]
        assert "no preview was created" in result["error"]
        assert len(store) == 0

    def test_a_backend_error_is_reported_not_swallowed(self, tools, album, mocker, store):
        """No fetchart plugin ⇒ the route 500s; that must not read as "no art"."""
        mocker.patch("beets.plugins.find_plugins", return_value=[])

        result = tools["preview_cover"](album)
        assert "fetchart plugin not loaded" in result["error"]
        assert "album_id" not in result
        assert len(store) == 0

    def test_an_unknown_album_is_reported(self, tools, mocker, store):
        mocker.patch("beets.plugins.find_plugins", return_value=[])
        result = tools["preview_cover"](9999)
        assert "Album not found" in result["error"]
        assert len(store) == 0

    def test_an_unreachable_backend_is_reported(self, make_tools, mcp_config, store):
        result = make_tools(dead_client(mcp_config))["preview_cover"](1)
        assert "could not reach" in result["error"]
        assert len(store) == 0


class TestApplyCover:
    def test_embeds_the_previewed_candidate(self, tools, album, art_source, saved_cover):
        image = art_source()
        token = tools["preview_cover"](album)["preview_token"]

        result = tools["apply_change"](token)

        assert result["status"] == "ok"
        assert result["kind"] == "cover"
        assert result["target"] == f"album {album}"
        assert saved_cover.call_args[0][1] == image
        # The backend consumed its stash, so a repeat is not silently possible.
        assert f"cover_{album}" not in state.identify_tasks

    def test_a_stash_consumed_elsewhere_fails_loudly(self, tools, album, art_source, saved_cover):
        """The web UI confirmed the same fetch first: the stash is gone and there
        is nothing left to write — which the tool must not report as success."""
        art_source()
        token = tools["preview_cover"](album)["preview_token"]
        state.identify_tasks.pop(f"cover_{album}")

        result = tools["apply_change"](token)
        assert result["status"] == "failed"
        assert "No cover art to confirm" in result["detail"][0]
        assert not saved_cover.called

    def test_a_second_fetch_between_preview_and_apply_wins(
        self, tools, album, art_source, saved_cover
    ):
        """The documented limitation, pinned. The stash has no identity, so the
        token cannot tell that what it previewed was replaced."""
        art_source(source="first", filename="first.jpg")
        token = tools["preview_cover"](album)["preview_token"]

        second = art_source(source="second", filename="second.jpg")
        assert tools["preview_cover"](album)["changes"]["source"] == "second"

        assert tools["apply_change"](token)["status"] == "ok"
        assert saved_cover.call_args[0][1] == second

    def test_the_token_commits_exactly_once(self, tools, album, art_source, saved_cover):
        art_source()
        token = tools["preview_cover"](album)["preview_token"]
        assert tools["apply_change"](token)["status"] == "ok"

        again = tools["apply_change"](token)
        assert again["status"] == "failed"
        assert "already been applied" in again["detail"][0]

    def test_a_readonly_server_writes_nothing_and_keeps_the_token(
        self, make_tools, album, art_source, saved_cover, store
    ):
        art_source()
        readonly = make_tools(readonly=True)
        token = readonly["preview_cover"](album)["preview_token"]

        refused = readonly["apply_change"](token)
        assert refused["status"] == "failed"
        assert not saved_cover.called
        assert len(store) == 1


# ---------------------------------------------------------------------------
# preview_lyrics
# ---------------------------------------------------------------------------


@pytest.fixture()
def tracks(db_path, tmp_path):
    """A three-track album with real (empty) files, so `.lrc` lookup works."""
    album_id = insert_album(db_path, album="Lyric Album", albumartist="Lyric Artist")
    ids = []
    for number, title in enumerate(("One", "Two", "Three"), start=1):
        audio = tmp_path / f"{number:02d}.mp3"
        audio.write_bytes(b"")
        ids.append(
            insert_item(
                db_path,
                album_id,
                title=title,
                track=number,
                path=str(audio).encode(),
            )
        )
    return album_id, ids


def lyrics_result(mocker, text="Verse one\nVerse two", *, synced=False, instrumental=False):
    """A `lyrics` plugin result.

    `instrumental` must be assigned explicitly: an auto-created MagicMock child
    is truthy, which would make every result look like an instrumental verdict.
    """
    result = mocker.MagicMock()
    result.text = "" if instrumental else text
    result.full_text = "" if instrumental else text
    result.synced = synced
    result.backend = "lrclib"
    result.url = "https://lrclib.example/1"
    result.language = None
    result.translation_language = None
    result.instrumental = instrumental
    return result


@pytest.fixture()
def lyrics_source(mocker):
    """The `lyrics` plugin, answering per item id from a mapping."""

    def _install(by_item_id):
        plugin = fake_plugin(mocker, "lyrics")
        plugin.find_lyrics.side_effect = lambda item: by_item_id.get(item.id)
        mocker.patch("beets.plugins.find_plugins", return_value=[plugin])
        return plugin

    return _install


@pytest.fixture()
def writable(mocker):
    """File tag writes succeed; the database half is what these tests assert."""
    return mocker.patch("beetdeck.routes.lyrics._try_write_item", return_value=True)


def stored_lyrics(db_path, item_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT lyrics FROM items WHERE id = ?", (item_id,)).fetchone()
    finally:
        conn.close()
    return row[0]


def stored_flex(db_path, item_id, key):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM item_attributes WHERE entity_id = ? AND key = ?",
            (item_id, key),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


class TestPreviewLyricsAlbum:
    def test_summarizes_every_track_it_found(self, tools, tracks, lyrics_source, store, mocker):
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker), ids[1]: lyrics_result(mocker, synced=True)})

        result = tools["preview_lyrics"](album_id)

        assert result["preview_token"].startswith("lyrics-")
        assert result["kind"] == "lyrics"
        assert result["target"] == f"album {album_id}"
        found = result["changes"]["tracks"]
        assert [row["item_id"] for row in found] == [ids[0], ids[1]]
        assert found[0]["title"] == "One"
        assert found[0]["backend"] == "lrclib"
        assert found[0]["excerpt"] == "Verse one Verse two"
        assert found[0]["chars"] == len("Verse one\nVerse two")
        assert found[1]["synced"] is True
        assert len(store) == 1

    def test_tracks_without_lyrics_are_named_and_left_out(
        self, tools, tracks, lyrics_source, store, mocker
    ):
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker)})

        result = tools["preview_lyrics"](album_id)

        assert store.peek(result["preview_token"]).params == {"item_ids": [ids[0]]}
        note = next(note for note in result["notes"] if "no lyrics found" in note)
        assert "Two" in note and "Three" in note
        assert f"item {ids[1]}" in note

    def test_an_instrumental_verdict_is_a_result_not_a_miss(
        self, tools, tracks, lyrics_source, store, mocker
    ):
        """`found: false` + `new_instrumental: true` is a positive answer."""
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker, instrumental=True)})

        result = tools["preview_lyrics"](album_id)

        row = result["changes"]["tracks"][0]
        assert row["item_id"] == ids[0]
        assert row["instrumental"] is True
        assert "chars" not in row and "excerpt" not in row
        assert store.peek(result["preview_token"]).params == {"item_ids": [ids[0]]}

    def test_flags_tracks_whose_lyrics_would_be_replaced(
        self, tools, tracks, lyrics_source, db_path, mocker
    ):
        album_id, ids = tracks
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE items SET lyrics = ? WHERE id = ?", ("Old words", ids[0]))
            conn.commit()
        finally:
            conn.close()
        lyrics_source({ids[0]: lyrics_result(mocker), ids[1]: lyrics_result(mocker)})

        rows = {
            row["item_id"]: row for row in tools["preview_lyrics"](album_id)["changes"]["tracks"]
        }
        assert rows[ids[0]]["replaces_existing"] is True
        assert rows[ids[0]]["current_source"] == "embedded"
        assert "replaces_existing" not in rows[ids[1]]

    def test_the_race_note_is_always_first(self, tools, tracks, lyrics_source, mocker):
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker)})
        result = tools["preview_lyrics"](album_id)
        assert "overwrites it and wins" in result["notes"][0]

    def test_nothing_found_mints_nothing(self, tools, tracks, lyrics_source, store):
        album_id, _ids = tracks
        lyrics_source({})

        result = tools["preview_lyrics"](album_id)
        assert "no lyrics were found" in result["error"]
        assert "3 track(s) searched" in result["error"]
        assert len(store) == 0

    def test_a_backend_error_is_reported(self, tools, tracks, mocker, store):
        album_id, _ids = tracks
        mocker.patch("beets.plugins.find_plugins", return_value=[])

        result = tools["preview_lyrics"](album_id)
        assert "lyrics plugin not loaded" in result["error"]
        assert len(store) == 0

    def test_an_unreachable_backend_is_reported(self, make_tools, mcp_config, store):
        result = make_tools(dead_client(mcp_config))["preview_lyrics"](1)
        assert "could not reach" in result["error"]
        assert len(store) == 0


class TestPreviewLyricsTrack:
    def test_fetches_one_track_and_carries_its_id(
        self, tools, tracks, lyrics_source, store, mocker
    ):
        album_id, ids = tracks
        plugin = lyrics_source({ids[1]: lyrics_result(mocker, text="Only this one")})

        result = tools["preview_lyrics"](album_id, ids[1])

        assert result["target"] == f"item {ids[1]}"
        assert result["changes"]["tracks"] == [
            {
                "item_id": ids[1],
                "instrumental": False,
                "synced": False,
                "backend": "lrclib",
                "chars": len("Only this one"),
                "excerpt": "Only this one",
            }
        ]
        assert store.peek(result["preview_token"]).params == {"item_ids": [ids[1]]}
        # Only the requested track was looked up, not the whole album.
        assert plugin.find_lyrics.call_count == 1

    def test_nothing_found_for_the_track_mints_nothing(self, tools, tracks, lyrics_source, store):
        album_id, ids = tracks
        lyrics_source({})

        result = tools["preview_lyrics"](album_id, ids[0])
        assert "no lyrics were found" in result["error"]
        assert f"track {ids[0]} of album {album_id}" in result["error"]
        assert len(store) == 0

    def test_a_track_from_another_album_is_reported(self, tools, tracks, lyrics_source, db_path):
        album_id, ids = tracks
        lyrics_source({})
        other = insert_album(db_path, album="Other")
        result = tools["preview_lyrics"](other, ids[0])
        assert "Track not found" in result["error"]


class TestApplyLyrics:
    def test_writes_the_previewed_lyrics(
        self, tools, tracks, lyrics_source, db_path, writable, mocker
    ):
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker), ids[1]: lyrics_result(mocker)})
        token = tools["preview_lyrics"](album_id)["preview_token"]

        result = tools["apply_change"](token)

        assert result["status"] == "ok"
        assert result["kind"] == "lyrics"
        assert stored_lyrics(db_path, ids[0]) == "Verse one\nVerse two"
        assert stored_lyrics(db_path, ids[1]) == "Verse one\nVerse two"
        assert stored_flex(db_path, ids[0], "lyrics_backend") == "lrclib"
        assert stored_lyrics(db_path, ids[2]) is None

    def test_an_instrumental_verdict_is_stored_as_the_flag(
        self, tools, tracks, lyrics_source, db_path, writable, mocker
    ):
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker, instrumental=True)})
        token = tools["preview_lyrics"](album_id, ids[0])["preview_token"]

        assert tools["apply_change"](token)["status"] == "ok"
        # beets writes the blank text as NULL; either way the lyrics tag is empty
        # and the verdict lives in the flag.
        assert not stored_lyrics(db_path, ids[0])
        assert stored_flex(db_path, ids[0], "lyrics_instrumental") in ("1", "True")

    def test_a_stash_consumed_elsewhere_is_partial_not_ok(
        self, tools, tracks, lyrics_source, db_path, writable, mocker
    ):
        """The album confirm skips an id with no stashed fetch **silently** — the
        tool has to notice that by counting, or it reports a write that never
        happened as success."""
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker), ids[1]: lyrics_result(mocker)})
        token = tools["preview_lyrics"](album_id)["preview_token"]

        state.identify_tasks.pop(f"lyrics_{ids[1]}")

        result = tools["apply_change"](token)
        assert result["status"] == "partial"
        assert any("no stashed lyrics fetch" in entry for entry in result["detail"])
        assert str(ids[1]) in " ".join(result["detail"])
        assert stored_lyrics(db_path, ids[0]) == "Verse one\nVerse two"
        assert stored_lyrics(db_path, ids[1]) is None

    def test_a_file_write_failure_is_partial(self, tools, tracks, lyrics_source, db_path, mocker):
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker)})
        token = tools["preview_lyrics"](album_id, ids[0])["preview_token"]
        mocker.patch("beetdeck.routes.lyrics._try_write_item", return_value=False)

        result = tools["apply_change"](token)
        assert result["status"] == "partial"
        assert any("lyrics write failed" in entry for entry in result["detail"])

    def test_a_retained_lrc_sidecar_is_partial(
        self, tools, tracks, lyrics_source, tmp_path, writable, mocker
    ):
        album_id, ids = tracks
        (tmp_path / "01.lrc").write_text("[00:00]old", encoding="utf-8")
        lyrics_source({ids[0]: lyrics_result(mocker)})
        token = tools["preview_lyrics"](album_id, ids[0])["preview_token"]
        mocker.patch("beetdeck.routes.lyrics.os.remove", side_effect=OSError("denied"))

        result = tools["apply_change"](token)
        assert result["status"] == "partial"
        assert any("sidecar .lrc could not be removed" in entry for entry in result["detail"])

    def test_a_second_fetch_between_preview_and_apply_wins(
        self, tools, tracks, lyrics_source, db_path, writable, mocker
    ):
        """The documented limitation, pinned — same as for covers."""
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker, text="First words")})
        token = tools["preview_lyrics"](album_id, ids[0])["preview_token"]

        lyrics_source({ids[0]: lyrics_result(mocker, text="Second words")})
        assert tools["preview_lyrics"](album_id, ids[0])["changes"]["tracks"][0]["excerpt"] == (
            "Second words"
        )

        assert tools["apply_change"](token)["status"] == "ok"
        assert stored_lyrics(db_path, ids[0]) == "Second words"

    def test_the_token_commits_exactly_once(self, tools, tracks, lyrics_source, writable, mocker):
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker)})
        token = tools["preview_lyrics"](album_id, ids[0])["preview_token"]
        assert tools["apply_change"](token)["status"] == "ok"

        again = tools["apply_change"](token)
        assert again["status"] == "failed"
        assert "already been applied" in again["detail"][0]

    def test_a_readonly_server_writes_nothing_and_keeps_the_token(
        self, make_tools, tracks, lyrics_source, db_path, writable, store, mocker
    ):
        album_id, ids = tracks
        lyrics_source({ids[0]: lyrics_result(mocker)})
        readonly = make_tools(readonly=True)
        token = readonly["preview_lyrics"](album_id, ids[0])["preview_token"]

        refused = readonly["apply_change"](token)
        assert refused["status"] == "failed"
        assert stored_lyrics(db_path, ids[0]) is None
        assert len(store) == 1
