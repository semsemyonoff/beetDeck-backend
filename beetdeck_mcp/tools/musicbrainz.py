"""MusicBrainz tools — how the agent finds the release autotag missed.

The workflow these serve: `get_album(include_raw_tags=…)` shows what is on disk,
`musicbrainz_search_release` finds plausible releases, `musicbrainz_get_release`
compares tracklist and durations against the album's `length_sec`, and the
winning `mbid` goes back into `identify_album(album_id, mb_release_id=…)`, which
always yields a candidate with a real track mapping.

**Raw MusicBrainz JSON never reaches the agent.** A single release lookup with
recordings is tens of kilobytes of relationships, aliases and ids; every response
here is trimmed to the fields that decide "is this the same album?" — credit,
date, country, label, catalogue number, media, and per-track lengths.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from beetdeck_mcp.musicbrainz import MusicBrainzClient
from beetdeck_mcp.tools import compact, failure, musicbrainz_client_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_NAMES = ("musicbrainz_search_release", "musicbrainz_get_release")

DEFAULT_SEARCH_LIMIT = 5
#: Search rows are the cheap part of the workflow, but 25 of them is already more
#: than any release comparison needs.
MAX_SEARCH_LIMIT = 25


def artist_credit(rows: Any) -> str:
    """A credit list rendered the way it is printed — `"A feat. B"`.

    The join phrases carry the difference between a collaboration and a
    compilation, so they are kept rather than replaced with a separator.
    """
    parts: list[str] = []
    for entry in rows if isinstance(rows, list) else []:
        if not isinstance(entry, dict):
            continue
        artist = entry.get("artist")
        name = entry.get("name") or (artist.get("name") if isinstance(artist, dict) else None)
        if name:
            parts.append(str(name))
        join = entry.get("joinphrase")
        if join:
            parts.append(str(join))
    return "".join(parts)


def _label_info(rows: Any) -> tuple[str, str]:
    """The first label name and catalogue number, as one flat pair."""
    for entry in rows if isinstance(rows, list) else []:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        name = label.get("name") if isinstance(label, dict) else None
        catalog = entry.get("catalog-number")
        if name or catalog:
            return str(name or ""), str(catalog or "")
    return "", ""


def _media_formats(media: Any) -> str:
    """`"CD"`, `"2×CD"`, `"CD + DVD"` — the physical shape in one string."""
    rows = media if isinstance(media, list) else []
    formats = [
        str(medium.get("format"))
        for medium in rows
        if isinstance(medium, dict) and medium.get("format")
    ]
    if not formats:
        return ""
    if len(formats) > 1 and len(set(formats)) == 1:
        return f"{len(formats)}×{formats[0]}"
    return " + ".join(formats)


def _length_sec(track: Mapping[str, Any]) -> float | None:
    """Track length in seconds, falling back to the recording's own length."""
    recording = track.get("recording")
    raw = track.get("length")
    if raw is None and isinstance(recording, dict):
        raw = recording.get("length")
    if not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return round(raw / 1000, 1)


def trim_search_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One search hit, cut down to what distinguishes it from its neighbours."""
    label, catalog = _label_info(row.get("label-info"))
    return compact(
        {
            "mbid": row.get("id"),
            "title": row.get("title"),
            "artist": artist_credit(row.get("artist-credit")),
            "date": row.get("date"),
            "country": row.get("country"),
            "label": label,
            "catalog_number": catalog,
            "track_count": row.get("track-count"),
            "media": _media_formats(row.get("media")),
            "status": row.get("status"),
            "disambiguation": row.get("disambiguation"),
            "score": row.get("score"),
        }
    )


def trim_release(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A release lookup, cut down to the tracklist comparison it exists for."""
    label, catalog = _label_info(payload.get("label-info"))
    credit = artist_credit(payload.get("artist-credit"))
    media_rows = payload.get("media") if isinstance(payload.get("media"), list) else []

    media: list[dict[str, Any]] = []
    total_tracks = 0
    for position, medium in enumerate(media_rows, start=1):
        if not isinstance(medium, dict):
            continue
        tracks: list[dict[str, Any]] = []
        for track in medium.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            track_credit = artist_credit(track.get("artist-credit"))
            tracks.append(
                compact(
                    {
                        "position": track.get("position"),
                        "number": track.get("number"),
                        "title": track.get("title"),
                        "length_sec": _length_sec(track),
                        # Only when it differs from the release credit: on a
                        # compilation that difference is the whole point, on a
                        # normal album it would repeat once per track.
                        "artist": track_credit if track_credit != credit else "",
                    }
                )
            )
        count = medium.get("track-count")
        total_tracks += count if isinstance(count, int) else len(tracks)
        media.append(
            compact(
                {
                    "position": medium.get("position") or position,
                    "format": medium.get("format"),
                    "track_count": count,
                    "tracks": tracks,
                }
            )
        )

    release_group = payload.get("release-group")
    return compact(
        {
            "mbid": payload.get("id"),
            "title": payload.get("title"),
            "artist": credit,
            "date": payload.get("date"),
            "country": payload.get("country"),
            "status": payload.get("status"),
            "disambiguation": payload.get("disambiguation"),
            "barcode": payload.get("barcode"),
            "label": label,
            "catalog_number": catalog,
            # Only when there is a tracklist to count: a bare `0` on a lookup
            # that returned no media reads like an empty release.
            "track_count": total_tracks if media else None,
            "primary_type": (
                release_group.get("primary-type") if isinstance(release_group, dict) else None
            ),
            "media": media,
        }
    )


def build_tools(client: MusicBrainzClient) -> tuple[Callable[..., dict[str, Any]], ...]:
    """The MusicBrainz tools bound to `client`."""

    def musicbrainz_search_release(
        artist: str = "",
        album: str = "",
        track_count: int = 0,
        year: int = 0,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> dict[str, Any]:
        """Search MusicBrainz for a release. At least one of `artist`/`album`.

        Use this when autotag's candidates are all wrong: find the right release
        here, then pass its `mbid` to `identify_album(album_id,
        mb_release_id=…)`, which always produces a candidate with a real track
        mapping regardless of distance.

        `track_count` and `year` narrow the search — they are the cheapest way to
        tell editions of the same album apart (`year` matches any release date in
        that year). Leave them at `0` to omit them.

        Every hit carries `score` (MusicBrainz' own 0-100 confidence),
        `track_count`, `date`, `country`, `label` and `media`, but no tracklist —
        call `musicbrainz_get_release` for that. Requests are gated to one per
        second, shared with everything else this server does.
        """
        if limit < 1 or limit > MAX_SEARCH_LIMIT:
            return {"error": f"limit must be between 1 and {MAX_SEARCH_LIMIT}, got {limit}"}

        response = client.search_releases(
            artist=artist,
            album=album,
            track_count=track_count or None,
            year=year or None,
            limit=limit,
        )
        if not response.ok:
            return failure(response)

        rows = response.data.get("releases")
        releases = [trim_search_row(row) for row in rows if isinstance(row, dict)] if rows else []
        result: dict[str, Any] = {
            "count": response.data.get("count", len(releases)),
            "releases": releases,
        }
        if not releases:
            # An empty result is a fact, not a failure — but a silent `[]` reads
            # like a broken call, so it says what was searched for.
            result["notes"] = [
                "no MusicBrainz release matched — try fewer constraints "
                "(drop `track_count`/`year`) or a different spelling of the artist or album"
            ]
        return result

    def musicbrainz_get_release(mbid: str) -> dict[str, Any]:
        """One MusicBrainz release with its full tracklist.

        Returns per-medium tracklists with `length_sec` per track, so the
        durations can be compared directly against the `length_sec` values
        `get_album` reports for the local files — the reliable way to tell two
        editions of the same album apart when the titles are identical.

        A track carries its own `artist` only when it differs from the release
        credit, which is what marks a compilation.
        """
        response = client.get_release(mbid)
        if not response.ok:
            return failure(response)
        return trim_release(response.data)

    return (musicbrainz_search_release, musicbrainz_get_release)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(musicbrainz_client_for(config)):
        server.add_tool(tool)
