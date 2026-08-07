"""Cover art and lyrics — the two previews nothing but the clock defends.

Both endpoints work the same way on the backend: a fetch stashes its result in
`state.identify_tasks` under `cover_<album_id>` / `lyrics_<item_id>`, and a later
confirm writes whatever is stashed. That stash carries **no identity and no read
endpoint**, so from outside the process there is no way to tell whether the thing
about to be committed is still the thing that was previewed — a second fetch (a
click in the web UI, another agent) silently overwrites it and wins. That is why
these kinds are `TTL_ONLY` with a 5-minute lifetime, and why both tool
descriptions say it outright instead of implying the guarantee the identify and
tag previews give.

Two consequences shape the code:

* **Only ids this fetch actually stashed go into the token.** A track the sources
  had nothing for leaves any *earlier* stash in place, so requesting it on
  confirm would commit a result from a previous fetch that was never previewed.
* **Lyrics always commit through the album-wide confirm**, even for a single
  track. `…/track/<id>/lyrics/confirm` answers `{"status", "has_lrc"}` — a shape
  with no per-id partial signal — while the album confirm reports
  `written_item_ids`, `failed` and `retained_lrc_item_ids`, which is what
  `client._normalize_lyrics` maps, including the ids it silently skipped. One
  write path, one normalizer.

An instrumental track is a **third state**, not a miss: the fetch answers
`found: false` together with `new_instrumental: true` and still stashes the
result, so confirming it records beets' `lyrics_instrumental` flag and clears the
file's lyrics tag. Reporting that as "nothing found" would throw away a positive
answer from the source.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.preview import PreviewStore, default_store
from beetdeck_mcp.tools import client_for, compact, failure

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_NAMES = ("preview_cover", "preview_lyrics")

#: How much of each lyrics text to show. Enough to tell "this is the right song"
#: from "this is a different song with the same title"; a full album's lyrics
#: would be several thousand tokens of context for a judgement that needs a line.
EXCERPT_CHARS = 160

#: Both fetch endpoints do one remote lookup per track, sequentially — the lyrics
#: handler loops `find_lyrics(item)` over the whole album, the cover handler walks
#: the art sources. The 30 s client default aborts a normal-length album while the
#: backend keeps going and stashes the result, leaving the agent no token for work
#: that did happen. Same reasoning as `library.BPM_TIMEOUT`.
FETCH_TIMEOUT = 300.0

RACE_NOTE = (
    "The backend keeps this fetch in an anonymous stash that cannot be read back, so a "
    "competing fetch (the web UI, another agent) overwrites it and wins without either side "
    "noticing. Apply within the token's lifetime or re-run the preview."
)


def _excerpt(text: str) -> str:
    """The first line-ish of `text`, collapsed onto one line."""
    flat = " ".join(text.split())
    if len(flat) <= EXCERPT_CHARS:
        return flat
    return flat[:EXCERPT_CHARS].rstrip() + "…"


def _fetched(row: Mapping[str, Any]) -> bool:
    """Whether this fetch stashed something for the track.

    `found` is "there is text"; an instrumental verdict is a hit with no text.
    The backend stashes on either, and stashes on neither otherwise.
    """
    return bool(row.get("found")) or bool(row.get("new_instrumental"))


def _track_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """One previewed track, trimmed to what a decision needs."""
    text = row.get("new_lyrics") or ""
    current = row.get("current_lyrics") or ""
    return compact(
        {
            "item_id": row.get("item_id"),
            "track": row.get("track"),
            "title": row.get("title"),
            "instrumental": bool(row.get("new_instrumental")),
            "synced": bool(row.get("new_synced")),
            "backend": row.get("new_backend"),
            "chars": len(text) or None,
            "excerpt": _excerpt(text),
            # The one fact that turns this from an addition into an overwrite.
            "replaces_existing": True if current else None,
            "current_source": row.get("current_source"),
        }
    )


def build_tools(
    client: BeetdeckClient,
    store: PreviewStore,
) -> tuple[Callable[..., dict[str, Any]], ...]:
    """The cover and lyrics preview tools bound to `client` and `store`."""

    def preview_cover(album_id: int) -> dict[str, Any]:
        """Fetch cover art for an album from online sources, without saving it.

        Returns what the backend found — the source it came from and a
        `preview_url` on the beetDeck API that serves the candidate image — plus
        a single-use `preview_token`. Nothing is written until you pass that
        token to `apply_change`, which saves the image into the album directory
        and embeds it into every track. Image dimensions are not part of the
        API's answer and are not fetched here.

        **This preview is guarded by its 5-minute lifetime alone.** The fetched
        image sits in an anonymous stash on the backend with no way to read it
        back, so a cover fetched afterwards from the web UI replaces it and would
        be what `apply_change` writes. Apply promptly, or re-run this preview.
        """
        response = client.post(f"/api/album/{album_id}/cover/fetch", timeout=FETCH_TIMEOUT)
        if not response.ok:
            return failure(response)

        data = response.data
        if not data.get("found"):
            return {
                "error": (
                    f"no cover art was found online for album {album_id} — nothing was stashed "
                    "and no preview was created; upload an image from the web UI, or fix the "
                    "album's identification first so the art sources can find it"
                )
            }

        preview = store.mint(
            "cover",
            target=f"album {album_id}",
            method="POST",
            endpoint=f"/api/album/{album_id}/cover/confirm",
            summary=compact(
                {
                    "album_id": album_id,
                    "source": data.get("source"),
                    "preview_url": data.get("preview_url"),
                }
            ),
        )
        described = store.describe(preview)
        described["notes"] = [RACE_NOTE]
        return described

    def preview_lyrics(album_id: int, item_id: int | None = None) -> dict[str, Any]:
        """Fetch lyrics for a whole album, or for one track, without saving them.

        Pass `item_id` for a single track; omit it to fetch every track of the
        album in one call. Returns a per-track summary — source backend, whether
        the lyrics are time-synced, a short excerpt, and whether the track
        already has lyrics that would be replaced — plus a single-use
        `preview_token`. Nothing is written until you pass that token to
        `apply_change`.

        A track reported as `instrumental` is a **result, not a miss**: the
        source positively says there are no lyrics, and applying it records that
        verdict and clears any lyrics tag on the file. Tracks the sources had
        nothing at all for are named in `notes` and are left out of the change.

        **This preview is guarded by its 5-minute lifetime alone.** The fetched
        lyrics sit in an anonymous stash on the backend with no way to read them
        back, so a fetch afterwards from the web UI replaces them and would be
        what `apply_change` writes. Apply promptly, or re-run this preview.
        """
        if item_id is None:
            response = client.post(f"/api/album/{album_id}/lyrics/fetch", timeout=FETCH_TIMEOUT)
            if not response.ok:
                return failure(response)
            rows = [row for row in response.data.get("tracks") or [] if isinstance(row, dict)]
            target = f"album {album_id}"
            subject = f"album {album_id}"
        else:
            response = client.post(
                f"/api/album/{album_id}/track/{item_id}/lyrics/fetch", timeout=FETCH_TIMEOUT
            )
            if not response.ok:
                return failure(response)
            # The single-track fetch answers about the track it was asked for and
            # does not echo its id; the album-wide one carries `item_id` per row.
            rows = [{**response.data, "item_id": item_id}]
            target = f"item {item_id}"
            subject = f"track {item_id} of album {album_id}"

        fetched = [row for row in rows if _fetched(row)]
        missing = [row for row in rows if not _fetched(row)]

        if not fetched:
            return {
                "error": (
                    f"no lyrics were found for {subject} ({len(rows)} track(s) searched) — "
                    "nothing was stashed and no preview was created; the configured sources "
                    "have nothing for these tracks, so check the artist and title tags first"
                )
            }

        item_ids = [row["item_id"] for row in fetched]
        preview = store.mint(
            "lyrics",
            target=target,
            method="POST",
            # Always the album-wide confirm — see the module docstring.
            endpoint=f"/api/album/{album_id}/lyrics/confirm",
            params={"item_ids": item_ids},
            requested_item_ids=item_ids,
            summary={
                "album_id": album_id,
                "tracks": [_track_summary(row) for row in fetched],
            },
        )
        described = store.describe(preview)
        notes = [RACE_NOTE]
        if missing:
            named = ", ".join(
                f"{row.get('title') or 'untitled'} (item {row['item_id']})" for row in missing
            )
            notes.append(
                f"no lyrics found for {len(missing)} track(s), left out of this change: {named}"
            )
        described["notes"] = notes
        return described

    return (preview_cover, preview_lyrics)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(client_for(config), default_store()):
        server.add_tool(tool)
