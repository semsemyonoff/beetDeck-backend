"""Identification tools — the workflow this whole server exists for.

`identify_album` starts the backend's autotag task and polls it to completion;
`preview_match` turns one candidate into a diff plus a preview token, and
`apply_change` (in `apply.py`) commits it. `identify_untagged_items` /
`preview_items_match` are the same two steps for loose items that belong to no
album yet — a separate namespace rather than a flag, because those tasks are
keyed by a generated `task_id`, not by an album id (see below).

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

The untagged-items flow differs from the album flow in three ways that are
visible to an agent:

* it is addressed by `task_id`, so the id must be carried from
  `identify_untagged_items` into `preview_items_match`;
* **starting a run purges every completed items task**, so a token minted
  against an earlier task is dead the moment a second run starts. The backend
  answers that confirm with `409` (a missing task counts as a generation
  mismatch), which lands as the same staleness refusal as a re-identify;
* its status route answers `{"status": "idle"}` for a task that is gone — a
  terminal answer, not something to keep polling.
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

TOOL_NAMES = (
    "identify_album",
    "preview_match",
    "identify_untagged_items",
    "preview_items_match",
)

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


def _task_summary(data: Mapping[str, Any], **subject: Any) -> dict[str, Any]:
    """The task fields worth an agent's context, `subject` naming what was identified.

    A `task_id` in `subject` is the fallback the items flow needs: an `idle`
    status body (the task was purged) carries none of its own.
    """
    return {
        **subject,
        "task_id": data.get("task_id") or subject.get("task_id"),
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

    def _poll(
        status_path: str,
        retry_hint: str,
        *,
        terminal: tuple[str, ...] = ("done", "error"),
    ) -> tuple[dict[str, Any] | ApiResponse, str | None]:
        """Poll until the task settles; the note is set when time ran out."""
        deadline = clock() + poll_timeout
        while True:
            response = client.get(status_path)
            if not response.ok:
                return response, None
            data = dict(response.data)
            if data.get("status") in terminal:
                return data, None
            if clock() >= deadline:
                return data, (
                    f"still identifying after {poll_timeout:g}s — the task keeps running; "
                    f"{retry_hint}"
                )
            sleep(poll_interval)

    def _mint_preview(
        kind: str,
        *,
        target: str,
        status_path: str,
        apply_path: str,
        confirm_path: str,
        candidate_index: int,
        rerun_hint: str,
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Status → diff → token, in that order (see the module docstring).

        Shared by both preview tools: the album and the untagged-items flows
        differ only in their paths and in what identifies the subject, so the
        ordering guarantee lives in one place rather than twice.
        """
        status = client.get(status_path)
        if not status.ok:
            return failure(status)
        task = status.data

        if task.get("status") != "done" or not task.get("matches_available"):
            return {
                "error": (
                    f"no confirmable identification results for {target} "
                    f"(task status {task.get('status')!r}) — {rerun_hint}"
                )
            }
        generation = task.get("match_generation")
        if not isinstance(generation, int):
            return {
                "error": (
                    f"the identification task for {target} carries no "
                    f"match_generation — {rerun_hint}"
                )
            }

        diff = client.post(apply_path, json={"candidate_index": candidate_index})
        if not diff.ok:
            return failure(diff)

        preview = store.mint(
            kind,
            target=target,
            method="POST",
            endpoint=confirm_path,
            params={"candidate_index": candidate_index},
            expected_generation=generation,
            summary={
                **summary,
                "candidate_index": candidate_index,
                "album": diff.data.get("album") or {},
                "tracks": diff.data.get("tracks") or [],
            },
        )
        return store.describe(preview)

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

        settled, timeout_note = _poll(
            f"/api/album/{album_id}/identify/status",
            "call identify_album again for this album to keep waiting (it adopts the "
            "running task instead of restarting it)",
        )
        if isinstance(settled, ApiResponse):
            return failure(settled)

        result = _task_summary(settled, album_id=album_id)
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
        return _mint_preview(
            "identify",
            target=f"album {album_id}",
            status_path=f"/api/album/{album_id}/identify/status",
            apply_path=f"/api/album/{album_id}/apply",
            confirm_path=f"/api/album/{album_id}/confirm",
            candidate_index=candidate_index,
            rerun_hint="run identify_album first",
            summary={"album_id": album_id},
        )

    def identify_untagged_items(
        item_ids: list[int],
        artist: str = "",
        album: str = "",
    ) -> dict[str, Any]:
        """Identify loose items that belong to no album yet, as one group.

        Use this for the tracks `list_untagged_items` returns: they are matched
        together against a single release, and confirming creates a new album
        from them. Pass the ids that really belong to the same release — a
        stray track drags the whole match off.

        Blocks until the background task finishes (up to ~60 s) and returns the
        candidates plus the `task_id` you need for `preview_items_match`.
        `artist` and `album` are free-text search hints.

        **Starting a run discards the results of every finished untagged-items
        identification**, including preview tokens minted from them — those
        applies are then refused as stale. Finish one group before starting the
        next.
        """
        ids = list(item_ids)
        body = {"item_ids": ids, "search_artist": artist, "search_album": album}
        started = client.post("/api/items/identify", json=body)
        if not started.ok:
            return failure(started)

        task_id = started.data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return {
                "error": (
                    "the backend accepted the identification but returned no task_id, "
                    "so its result cannot be addressed — retry"
                )
            }

        settled, timeout_note = _poll(
            f"/api/items/identify/{task_id}/status",
            f"call preview_items_match with task_id {task_id!r} once it settles; do not "
            "re-run identify_untagged_items, which starts a second identification",
            # "idle" means the task is gone — a later run purged it. Terminal:
            # polling a task that no longer exists never settles.
            terminal=("done", "error", "idle"),
        )
        if isinstance(settled, ApiResponse):
            return failure(settled)

        result = _task_summary(settled, task_id=task_id, item_ids=ids)
        if timeout_note is not None:
            result["notes"] = [timeout_note]
        if settled.get("status") == "idle":
            result["error"] = (
                f"identification task {task_id} disappeared before it finished — another "
                "untagged-items identification was started and purged it; re-run this one"
            )
            return result
        if settled.get("status") == "error":
            result["error"] = settled.get("error") or "identification failed"
            return result
        if settled.get("status") == "done":
            result["candidates"] = _candidates(settled)
        return result

    def preview_items_match(task_id: str, candidate_index: int = 0) -> dict[str, Any]:
        """Show what creating an album from one items candidate would change.

        Takes the `task_id` returned by `identify_untagged_items`. Returns the
        album- and track-level diff plus a single-use `preview_token`; nothing is
        written until you pass that token to `apply_change`, which then creates
        the album and writes the tags.

        The token carries the identification generation, so the confirm is
        rejected outright if the identification was re-run, already applied, or
        purged by a later `identify_untagged_items` call.
        """
        return _mint_preview(
            "items_identify",
            target=f"untagged items task {task_id}",
            status_path=f"/api/items/identify/{task_id}/status",
            apply_path=f"/api/items/identify/{task_id}/apply",
            confirm_path=f"/api/items/identify/{task_id}/confirm",
            candidate_index=candidate_index,
            rerun_hint="run identify_untagged_items first",
            summary={"task_id": task_id},
        )

    return (identify_album, preview_match, identify_untagged_items, preview_items_match)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(client_for(config), default_store()):
        server.add_tool(tool)
