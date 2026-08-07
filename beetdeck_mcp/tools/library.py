"""Library-wide operations: rescanning, BPM computation, and the ignore flag.

Four REST facts shape this module:

* **`POST /api/rescan` does not return the `run_id`** — it is only readable from
  `GET /api/rescan/status`, so `rescan_library` reads it back with a second call
  rather than leaving the agent without a handle on the run it just started.
* **A quick scan has no denominator.** The status endpoint answers `total: null`
  for it, which is *indeterminate*, not zero — reported as an explicit
  `indeterminate` flag, because a dropped `null` would read as "no work to do".
* **BPM is ~9 s of CPU per track** and always recomputes, so the request gets its
  own generous timeout instead of the client default. The plugin swallows its own
  errors, so a failure arrives as a `400` with no value, never as an exception.
* **`…/ignore` is a bodyless toggle.** There is no way to request a target state,
  so the tool is a toggle too — a `set_*` façade would need a read-modify-write
  that races the SPA for no gain.

None of these tools goes through the preview→apply contract, so
`BEETDECK_MCP_READONLY` does not disable them — it gates `apply_change` alone, by
design. They do write: a rescan re-imports files, `compute_bpm` writes a tag into
the audio file, `toggle_album_ignored` flips an album flag. Their descriptions say
so rather than letting a read-only server look inert.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.tools import client_for, compact, failure

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_NAMES = ("rescan_library", "get_rescan_status", "compute_bpm", "toggle_album_ignored")

#: librosa needs roughly 9 s per track and the route always recomputes; the 30 s
#: client default would abort a slow one while the plugin is still writing.
BPM_TIMEOUT = 300.0

#: A full rescan of a large library can add thousands of items. The counts are
#: always exact; only the listed rows are windowed.
DEFAULT_CHANGE_LIMIT = 20

RescanMode = Literal["quick", "full"]


def build_tools(
    client: BeetdeckClient,
    *,
    change_limit: int = DEFAULT_CHANGE_LIMIT,
) -> tuple[Callable[..., dict[str, Any]], ...]:
    """The library tools bound to `client`.

    Split out from `register` so tests drive the exact callables the server
    exposes against the in-process Flask app.
    """

    def rescan_library(mode: RescanMode = "quick") -> dict[str, Any]:
        """Start a library rescan and return immediately.

        `quick` (the default) imports only what beets has not seen yet; `full`
        re-imports everything, which is what picks up files whose tags changed on
        disk. The scan runs in the background and can take minutes to hours — poll
        `get_rescan_status` for progress and for the diff it produced.

        Only one scan runs at a time: when one is already in flight this returns
        an error instead of restarting or interrupting it.
        """
        response = client.post("/api/rescan", params={"mode": mode})
        if response.status_code == 409:
            return {
                "error": (
                    "a rescan is already running — poll get_rescan_status; a second scan "
                    "cannot be started until it finishes"
                )
            }
        if not response.ok:
            return failure(response)

        result: dict[str, Any] = {"status": "started", "mode": response.data.get("mode") or mode}
        # The start response carries no run_id — it exists only in the status
        # endpoint (see the module docstring), so read it back here.
        status = client.get("/api/rescan/status")
        run_id = status.data.get("run_id") if status.ok else None
        if run_id:
            result["run_id"] = run_id
        else:
            result["notes"] = [
                "the scan was started but its run_id could not be read back — "
                "call get_rescan_status for it"
            ]
        return result

    def get_rescan_status() -> dict[str, Any]:
        """Progress and result of the current or last rescan.

        `status` is `idle`, `running`, `done` or `error`. While running,
        `processed` counts albums handled so far; `total` is present only for a
        `full` scan — a quick scan has no denominator and carries
        `indeterminate: true` instead, so a missing `total` does **not** mean
        there is nothing to do.

        A finished scan carries the diff it produced: `added_count` /
        `removed_count` are exact, while `added` / `removed` list only the first
        few items, with a note when they were truncated.
        """
        response = client.get("/api/rescan/status")
        if not response.ok:
            return failure(response)
        data = response.data

        result = compact(
            {
                "status": data.get("status"),
                "phase": data.get("phase"),
                "run_id": data.get("run_id"),
                "processed": data.get("processed"),
                "current_item": data.get("current_item"),
                "returncode": data.get("returncode"),
            }
        )

        processed = data.get("processed")
        total = data.get("total")
        if isinstance(total, int):
            result["total"] = total
            if total > 0 and isinstance(processed, int):
                result["percent"] = round(processed / total * 100, 1)
        elif data.get("status") == "running":
            result["indeterminate"] = True

        notes: list[str] = []
        for key in ("added", "removed"):
            rows = data.get(key)
            if not isinstance(rows, list) or not rows:
                continue
            result[f"{key}_count"] = len(rows)
            result[key] = [compact(row) for row in rows[:change_limit] if isinstance(row, dict)]
            if len(rows) > change_limit:
                notes.append(f"{key}: listing the first {change_limit} of {len(rows)}")
        if notes:
            result["notes"] = notes
        return result

    def compute_bpm(album_id: int, item_id: int) -> dict[str, Any]:
        """Compute one track's BPM and write it into the audio file's tags.

        Both ids are required: the endpoint is nested under the album and reports
        "track not found" when the item belongs to a different one.

        This is **slow** — roughly 9 s of CPU per track — and it always
        recomputes, even for a track that already carries a BPM. Call it per
        track on request, not in a loop across an album.

        The analysis plugin swallows its own errors, so a failed computation
        comes back as an error with no value; an unreadable or unsupported audio
        file is the usual cause.
        """
        response = client.post(
            f"/api/album/{album_id}/track/{item_id}/bpm/compute",
            timeout=BPM_TIMEOUT,
        )
        if not response.ok:
            return failure(response)

        bpm = response.data.get("bpm")
        if not isinstance(bpm, (int, float)) or isinstance(bpm, bool):
            return {
                "error": (
                    f"the BPM computation for item {item_id} reported success but returned no "
                    "value — the tag was not written; retry or check the audio file"
                )
            }
        return {"album_id": album_id, "item_id": item_id, "bpm": bpm}

    def toggle_album_ignored(album_id: int) -> dict[str, Any]:
        """Flip an album's `ignored` flag and return the new state.

        This is a **toggle**, not a setter — the endpoint accepts no target
        state. Read the current flag first (`list_albums(filter="ignored")` or
        `get_album`) when you need to reach a specific one; calling this twice
        leaves the album where it started.

        Ignoring an album takes it out of the untagged work queue. It is how an
        album that should never be identified — a bootleg, a personal recording —
        is parked without tagging it.
        """
        response = client.post(f"/api/album/{album_id}/ignore")
        if not response.ok:
            return failure(response)

        ignored = response.data.get("ignored")
        if not isinstance(ignored, bool):
            return {
                "error": (
                    f"the ignore toggle for album {album_id} returned no state, so it is "
                    "unknown whether the flag changed — read the album back"
                )
            }
        return {"album_id": album_id, "ignored": ignored}

    return (rescan_library, get_rescan_status, compute_bpm, toggle_album_ignored)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(client_for(config)):
        server.add_tool(tool)
