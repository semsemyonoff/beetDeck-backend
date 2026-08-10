"""Cover art and lyrics — the two previews that commit a *fetched remote result*.

Both endpoints work the same way on the backend: a fetch stashes its result in
`state.identify_tasks` under `cover_<album_id>` / `lyrics_<item_id>`, and a later
confirm writes whatever is stashed. There is one slot per target, so a second
fetch (a click in the web UI, another agent) replaces what an earlier preview
saw. The backend stamps every fetch with a `stash_generation` and rejects a
confirm carrying a different one, so that replacement is a `409` rather than a
silent substitution — which is what puts these kinds in the same
`BACKEND_PRECONDITION` group as the identify previews. Threading that generation
from the fetch response into the token is this module's job; dropping it would
downgrade the guarantee without changing a word of any description.

Three consequences shape the code:

* **Only ids this fetch actually stashed go into the token.** A track the sources
  had nothing for leaves any *earlier* stash in place, so requesting it on
  confirm would commit a result from a previous fetch that was never previewed —
  and one carrying an older generation, which the backend now refuses outright.
* **Lyrics always commit through the album-wide confirm**, even for a single
  track. `…/track/<id>/lyrics/confirm` answers `{"status", "has_lrc"}` — a shape
  with no per-id partial signal — while the album confirm reports
  `written_item_ids`, `failed` and `retained_lrc_item_ids`, which is what
  `client._normalize_lyrics` maps, including the ids it silently skipped. One
  write path, one normalizer.
* **A fetch response with no usable generation is an error, not a weaker token.**
  `PreviewStore.mint` would raise on it, and minting one anyway is precisely the
  silent downgrade the precondition exists to prevent.

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
    "The backend keeps this fetch in a single slot per target, so a competing fetch (the web "
    "UI, another agent) replaces it. Applying then fails with a stale-state refusal instead "
    "of writing the other result — re-run the preview if that happens."
)


def _stash_generation(data: Mapping[str, Any], subject: str) -> int | dict[str, Any]:
    """The fetch's `stash_generation`, or an error dict explaining its absence.

    Minting without it would produce a token described as backend-enforced that
    nothing enforces, so a missing value fails the preview rather than weakening
    it silently.
    """
    generation = data.get("stash_generation")
    if isinstance(generation, int) and not isinstance(generation, bool):
        return generation
    return {
        "error": (
            f"the backend's fetch for {subject} returned no stash generation, so a confirm "
            "could not be made conditional on it and this preview would not be safe to "
            "apply — nothing was previewed; check that the beetDeck API is up to date"
        )
    }


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

        Returns what the backend found — the source it came from, the
        candidate's pixel `width` and `height`, and a `preview_url` on the
        beetDeck API that serves the image — plus a single-use `preview_token`.
        Nothing is written until you pass that token to `apply_change`, which
        saves the image into the album directory and embeds it into every track.

        Compare `width`/`height` against `cover_width`/`cover_height` from
        `get_album` to decide whether the candidate is an upgrade over the cover
        the album already has; both come from the API, so this needs no download
        and no external image tool.

        The fetched image sits in a single stash slot per album, so a cover
        fetched afterwards from the web UI replaces it — but this token carries
        the generation of the slot it went into, and applying it after that
        happens is refused rather than writing the other image.
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

        generation = _stash_generation(data, f"album {album_id}")
        if isinstance(generation, dict):
            return generation

        preview = store.mint(
            "cover",
            target=f"album {album_id}",
            method="POST",
            endpoint=f"/api/album/{album_id}/cover/confirm",
            expected_generation=generation,
            summary=compact(
                {
                    "album_id": album_id,
                    "source": data.get("source"),
                    "preview_url": data.get("preview_url"),
                    "width": data.get("width"),
                    "height": data.get("height"),
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
        Applied instrumental verdicts show up as `instrumental: true` on the
        track in `get_album` — that is how "checked, nothing to fetch" stays
        distinguishable from "never looked up".

        The fetched lyrics sit in a single stash slot per track, so a fetch
        afterwards from the web UI replaces them — but this token carries the
        generation of that fetch, and applying it after a replacement is refused
        (as a whole, for every track in the selection) rather than writing the
        other result.
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

        generation = _stash_generation(response.data, subject)
        if isinstance(generation, dict):
            return generation

        item_ids = [row["item_id"] for row in fetched]
        preview = store.mint(
            "lyrics",
            target=target,
            method="POST",
            # Always the album-wide confirm — see the module docstring.
            endpoint=f"/api/album/{album_id}/lyrics/confirm",
            params={"item_ids": item_ids},
            expected_generation=generation,
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
