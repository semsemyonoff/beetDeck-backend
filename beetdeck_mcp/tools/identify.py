"""Identification tools — the workflow this whole server exists for.

`identify_album` starts the backend's autotag task and polls it to completion;
`preview_match` turns one candidate into a diff plus a preview token, and
`apply_change` (in `apply.py`) commits it.

Three REST facts shape this module:

* **Starting an identification for an album that already has one running is a
  `409` carrying the running `task_id`.** Restarting would throw away work and
  bump the generation under a concurrent preview, so the `409` is *adopted*: the
  tool keeps polling the task that is already in flight.
* **Identification is a background thread**, so there is nothing to return
  synchronously — the tool polls `…/identify/status` and only comes back with
  candidates. A poll that runs out of time is reported as `running`, not as a
  failure: the task keeps going and a second call adopts it.
* **The generation is read before the diff, never after.** `…/apply` renders the
  diff from whatever candidates are current; if a re-identify lands between the
  two calls, reading the generation first means the token carries the *old* one
  and the confirm is rejected. Reading it afterwards would pair a stale diff with
  a fresh generation and the confirm would silently succeed on the wrong match.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from beetdeck_mcp.client import ApiResponse, BeetdeckClient
from beetdeck_mcp.preview import PreviewStore, default_store
from beetdeck_mcp.tools import client_for, compact, failure

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_NAMES = ("identify_album", "preview_match")

#: The backend polls MusicBrainz per candidate; 0.5 s is what the SPA uses.
POLL_INTERVAL = 0.5
#: An autotag run on a large album with several sources takes tens of seconds.
POLL_TIMEOUT = 60.0

#: Candidate fields worth an agent's context. `tracks` (the per-track proposal)
#: is deliberately left out — `preview_match` shows the full diff, and repeating
#: every tracklist for five candidates buries the decision it should support.
_CANDIDATE_FIELDS = (
    "index",
    "artist",
    "album",
    "year",
    "label",
    "media",
    "data_source",
    "mb_albumid",
    "distance",
    "track_count",
)


def _candidates(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        compact({field: row.get(field) for field in _CANDIDATE_FIELDS})
        for row in data.get("candidates") or []
        if isinstance(row, dict)
    ]


def _task_summary(album_id: int, data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "album_id": album_id,
        "task_id": data.get("task_id"),
        "status": data.get("status"),
        "match_generation": data.get("match_generation"),
        "matches_available": bool(data.get("matches_available")),
    }


def build_tools(
    client: BeetdeckClient,
    store: PreviewStore,
    *,
    poll_interval: float = POLL_INTERVAL,
    poll_timeout: float = POLL_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Callable[..., dict[str, Any]], ...]:
    """The identification tools bound to `client` and `store`.

    The polling knobs are arguments so the tests can drive the real flow without
    waiting a minute for the timeout path.
    """

    def _poll(album_id: int) -> tuple[dict[str, Any] | ApiResponse, str | None]:
        """Poll until the task settles; the note is set when time ran out."""
        deadline = clock() + poll_timeout
        while True:
            response = client.get(f"/api/album/{album_id}/identify/status")
            if not response.ok:
                return response, None
            data = dict(response.data)
            if data.get("status") in ("done", "error"):
                return data, None
            if clock() >= deadline:
                return data, (
                    f"still identifying after {poll_timeout:g}s — the task keeps running; "
                    "call identify_album again for this album to keep waiting (it adopts the "
                    "running task instead of restarting it)"
                )
            sleep(poll_interval)

    def identify_album(
        album_id: int,
        artist: str = "",
        album: str = "",
        mb_release_id: str = "",
    ) -> dict[str, Any]:
        """Run MusicBrainz identification for an album and return its candidates.

        Blocks until the background task finishes (up to ~60 s). If it is still
        running when that runs out, the result carries `status: "running"` — call
        this tool again for the same album to keep waiting; it adopts the running
        task rather than restarting it.

        `artist` and `album` are free-text search hints for a mistagged album.

        **`mb_release_id` is the way to override a wrong match.** An explicit
        release id is routed straight to the source and always yields a candidate
        with a real track mapping, regardless of how far it scores from the
        current tags — no distance or recommendation filter applies to it. When
        autotag's own candidates are all wrong, find the right release with
        `musicbrainz_search_release` and pass its id here.

        Each candidate carries `distance` (0 = exact) and `data_source`; the
        per-track proposal is not repeated here — use `preview_match` for that.
        """
        body = {"artist": artist, "album": album, "search_id": mb_release_id}
        started = client.post(f"/api/album/{album_id}/identify", json=body)

        adopted = started.status_code == 409
        if not started.ok and not adopted:
            return failure(started)

        settled, timeout_note = _poll(album_id)
        if isinstance(settled, ApiResponse):
            return failure(settled)

        result = _task_summary(album_id, settled)
        notes: list[str] = []
        if adopted:
            # Said out loud: the hints and mb_release_id above were *not* used —
            # they belong to the run that was already in flight.
            notes.append(
                "an identification was already running for this album and was adopted; "
                "any search hints passed to this call were not applied"
            )
        if timeout_note is not None:
            notes.append(timeout_note)
        if notes:
            result["notes"] = notes
        if settled.get("status") == "error":
            result["error"] = settled.get("error") or "identification failed"
            return result

        for key in ("current_artist", "current_album"):
            if settled.get(key):
                result[key] = settled[key]
        if settled.get("status") == "done":
            result["candidates"] = _candidates(settled)
        return result

    def preview_match(album_id: int, candidate_index: int = 0) -> dict[str, Any]:
        """Show what applying one identification candidate would change.

        Returns the album- and track-level diff plus a single-use
        `preview_token`; nothing is written until you pass that token to
        `apply_change`.

        This is the strongest of the preview guarantees: the token carries the
        identification generation read at preview time, and the backend rejects
        the confirm outright if identification was re-run or already applied in
        the meantime. Run `identify_album` first — the candidate list only exists
        while a finished task holds it.
        """
        status = client.get(f"/api/album/{album_id}/identify/status")
        if not status.ok:
            return failure(status)
        task = status.data

        if task.get("status") != "done" or not task.get("matches_available"):
            return {
                "error": (
                    f"no confirmable identification results for album {album_id} "
                    f"(task status {task.get('status')!r}) — run identify_album first"
                )
            }
        generation = task.get("match_generation")
        if not isinstance(generation, int):
            return {
                "error": (
                    f"the identification task for album {album_id} carries no "
                    "match_generation — re-run identify_album"
                )
            }

        diff = client.post(
            f"/api/album/{album_id}/apply", json={"candidate_index": candidate_index}
        )
        if not diff.ok:
            return failure(diff)

        preview = store.mint(
            "identify",
            target=f"album {album_id}",
            method="POST",
            endpoint=f"/api/album/{album_id}/confirm",
            params={"candidate_index": candidate_index},
            expected_generation=generation,
            summary={
                "album_id": album_id,
                "candidate_index": candidate_index,
                "album": diff.data.get("album") or {},
                "tracks": diff.data.get("tracks") or [],
            },
        )
        return store.describe(preview)

    return (identify_album, preview_match)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(client_for(config), default_store()):
        server.add_tool(tool)
