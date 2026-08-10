"""Read tools — the agent's window into the library.

Three facts about the REST side shape these:

* `GET /api/library` takes **no** query parameters and returns the whole library
  grouped by artist, so the filtering, the flattening and the paging in
  `list_albums` all happen here, after one full-library request.
* `GET /api/album/<id>` carries no raw beets tags; `get_album` composes the
  per-track `…/tags` endpoint on demand, at one extra request per track.
* Every payload lands in an agent's context window, so empty fields are dropped
  and long lists are truncated with an explicit note — never silently.

Failures come back as `{"error": …}` rather than as a raise: the REST client
never raises, and a tool that reported an unreachable backend as an empty
library would be worse than one that says what went wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.tools import client_for, compact, failure

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_NAMES = ("search_library", "list_albums", "get_album", "list_untagged_items")

#: Search hits are wide rows; albums and loose items are narrow, so they get
#: roomier windows. All three are overridable per call.
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_ALBUM_LIMIT = 100
DEFAULT_ITEM_LIMIT = 200

AlbumFilter = Literal["all", "untagged", "ignored"]


def _bad_window(limit: int, offset: int) -> dict[str, Any] | None:
    """Reject a nonsensical window instead of quietly coercing it."""
    if limit < 1:
        return {"error": f"limit must be at least 1, got {limit}"}
    if offset < 0:
        return {"error": f"offset must not be negative, got {offset}"}
    return None


def _window(
    rows: Sequence[Any], limit: int, offset: int, label: str, *, pageable: bool = True
) -> tuple[list[Any], str | None]:
    """One page of `rows`, plus a note when anything was left out.

    `pageable=False` for a caller that exposes no `offset` argument, so the note
    does not send the agent into a tool call that fails schema validation.
    """
    page = list(rows[offset : offset + limit])
    if len(page) == len(rows):
        return page, None
    note = f"{label}: showing {len(page)} of {len(rows)}"
    if offset:
        note += f" (offset {offset})"
    if offset + len(page) < len(rows):
        note += (
            f" — raise `limit` or call again with offset={offset + len(page)}"
            if pageable
            else " — raise `limit` or narrow the query"
        )
    return page, note


def build_tools(client: BeetdeckClient) -> tuple[Callable[..., dict[str, Any]], ...]:
    """The read tools bound to `client`.

    Split out from `register` so tests can drive the exact callables the server
    exposes against the in-process Flask app.
    """

    def search_library(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
        """Search the library for artists, albums and tracks.

        Case-insensitive substring match over album-artist names, album titles
        and track titles, returned as three separate groups. An empty `query`
        returns an empty result set, not the whole library — use `list_albums`
        for that. Each group is truncated to `limit`, and a `notes` entry says so
        when that happens.

        The groups match **independently**: an artist name only ever lands in
        `artists`, so searching for a band returns its name and an empty `albums`
        unless the band's name is also part of an album title. `artists` entries
        are bare names — feed one to `list_albums(artist=…)` to get that artist's
        albums with their ids.
        """
        bad = _bad_window(limit, 0)
        if bad is not None:
            return bad

        response = client.get("/api/search", params={"q": query})
        if not response.ok:
            return failure(response)

        result: dict[str, Any] = {"query": query}
        notes: list[str] = []
        for group in ("artists", "albums", "tracks"):
            rows = response.data.get(group) or []
            page, note = _window(rows, limit, 0, group, pageable=False)
            result[group] = [compact(row) if isinstance(row, dict) else row for row in page]
            if note is not None:
                notes.append(note)
        if result["artists"] and not result["albums"]:
            # The dead end this tool used to leave an agent in: a name, no id,
            # and no obvious next call. Name the next call.
            names = ", ".join(f"{name!r}" for name in result["artists"][:3])
            notes.append(
                f"matched {len(result['artists'])} artist name(s) but no album title — "
                f"call list_albums(artist=…) with one of {names} to see what the library "
                "has for them"
            )
        if notes:
            result["notes"] = notes
        return result

    def list_albums(
        filter: AlbumFilter = "all",
        artist: str | None = None,
        limit: int = DEFAULT_ALBUM_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List albums as one flat row each, ordered by artist then year.

        The underlying endpoint takes **no** query parameters and returns the
        whole library grouped by artist, so filtering and paging happen in this
        tool: `limit`/`offset` bound what reaches you, not what is fetched.

        `artist` narrows the list to album artists whose name contains it
        (case-insensitive substring, so `"floyd"` finds `"Pink Floyd"`). This is
        the answer to "what does the library have by this band" — `search_library`
        matches artist names only into its own `artists` group and returns bare
        names, so pass one of those here to get the albums and their ids.

        `filter`:

        * `all` — every album.
        * `untagged` — not yet confirmed against MusicBrainz and not ignored;
          this is the work queue.
        * `ignored` — explicitly marked ignored.

        `filter` and `artist` combine: `filter="untagged", artist="Pink Floyd"`
        is that artist's share of the work queue.
        """
        bad = _bad_window(limit, offset)
        if bad is not None:
            return bad

        # Normalize once and key everything below off the result — echoing or
        # reporting the raw value would claim a filter that a whitespace-only
        # argument silently turned off.
        wanted = (artist or "").strip()
        needle = wanted.lower()

        response = client.get("/api/library")
        if not response.ok:
            return failure(response)
        groups = response.payload if isinstance(response.payload, list) else []

        rows: list[dict[str, Any]] = []
        for group in groups:
            artist_name = group.get("artist") if isinstance(group, dict) else None
            if needle and needle not in (artist_name or "").lower():
                continue
            for album in (group.get("albums") or []) if isinstance(group, dict) else []:
                tagged = bool(album.get("tagged"))
                ignored = bool(album.get("ignored"))
                if filter == "untagged" and (tagged or ignored):
                    continue
                if filter == "ignored" and not ignored:
                    continue
                rows.append(
                    compact(
                        {
                            "id": album.get("id"),
                            "artist": artist_name,
                            "album": album.get("album"),
                            "year": album.get("year"),
                            "tagged": tagged,
                            "ignored": ignored,
                            "has_cover": bool(album.get("has_cover")),
                        }
                    )
                )

        page, note = _window(rows, limit, offset, "albums")
        result: dict[str, Any] = {
            "filter": filter,
            "total": len(rows),
            "offset": offset,
            "albums": page,
        }
        if wanted:
            result["artist"] = wanted
        notes = [note] if note is not None else []
        if wanted and not rows:
            # An empty page under an artist filter is ambiguous — no such artist,
            # or none matching `filter` — and the difference decides the next call.
            notes.append(
                f"no album artist contains {wanted!r}"
                + (f" among the {filter} albums" if filter != "all" else "")
                + " — names must match as a substring; search_library(query) finds the "
                "spelling the library actually uses"
            )
        if notes:
            result["notes"] = notes
        return result

    def get_album(album_id: int, include_raw_tags: bool = False) -> dict[str, Any]:
        """One album with its full tracklist.

        Every track carries `length_sec` (raw seconds) and `filename`, which is
        what makes a tracklist comparable against a MusicBrainz release without
        further calls.

        Two fields answer "is this album done" without a second call per track:
        `cover_width`/`cover_height` are the current cover's pixel dimensions
        (null when there is none), directly comparable against what
        `preview_cover` reports for a candidate; and a track's `instrumental`
        flag means a lyrics source positively said there is nothing to fetch —
        `has_lyrics: false` with `instrumental: false` is the one that still
        needs work.

        With `include_raw_tags=true` each track additionally carries `raw_tags` —
        its complete beets row including flexible attributes — at the cost of one
        extra request per track. Leave it off unless you need to inspect or edit
        individual tags; a track that cannot be read carries `raw_tags_error`
        instead of aborting the whole call.
        """
        response = client.get(f"/api/album/{album_id}")
        if not response.ok:
            return failure(response)

        tracks = [
            compact(track) for track in response.data.get("tracks") or [] if isinstance(track, dict)
        ]
        if include_raw_tags:
            for track in tracks:
                tags = client.get(f"/api/album/{album_id}/track/{track['id']}/tags")
                if tags.ok:
                    track["raw_tags"] = tags.data
                else:
                    track["raw_tags_error"] = tags.message

        album = compact(response.data)
        # Kept even when empty: an album with no tracks is a finding, not an
        # absence, and it is exactly what a broken import looks like.
        album["tracks"] = tracks
        return album

    def list_untagged_items(limit: int = DEFAULT_ITEM_LIMIT, offset: int = 0) -> dict[str, Any]:
        """Loose tracks — items whose album row has no album artist.

        These are the input to `identify_untagged_items`; they are not part of
        any properly tagged album yet, so they do not show up in `list_albums`
        under a real artist name.
        """
        bad = _bad_window(limit, offset)
        if bad is not None:
            return bad

        response = client.get("/api/items/untagged")
        if not response.ok:
            return failure(response)
        items = response.payload if isinstance(response.payload, list) else []

        page, note = _window(items, limit, offset, "items")
        result: dict[str, Any] = {
            "total": len(items),
            "offset": offset,
            "items": [compact(item) if isinstance(item, dict) else item for item in page],
        }
        if note is not None:
            result["notes"] = [note]
        return result

    return (search_library, list_albums, get_album, list_untagged_items)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(client_for(config)):
        server.add_tool(tool)
