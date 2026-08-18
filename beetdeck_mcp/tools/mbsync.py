"""MusicBrainz re-sync — one preview, applying the whole diff.

`POST /api/album/<id>/mbsync` re-fetches the album's own release and diffs it
against what is stored; `POST /api/album/<id>/mbsync/confirm` writes the result
the preview stashed, never looking the release up again. Both facts carry over
from the REST route unchanged (`beetdeck/routes/mbsync.py`), including the
stash's `stash_generation`, which is why this kind sits in the same
`BACKEND_PRECONDITION` group as `identify`/`cover`/`lyrics`: the confirm is
rejected outright if the stash was consumed, replaced or re-run since this
preview.

**v1 applies the whole diff.** The REST confirm accepts `excluded_fields` so the
web UI can uncheck individual fields before writing; this tool does not expose
that argument and never sends it, so an apply always writes everything the
preview showed. That is a deliberate asymmetry with the REST surface, not an
oversight — naming it here means it is documented rather than silently
discovered the first time an agent wants to exclude one field.
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

TOOL_NAMES = ("preview_mbsync",)


def _stash_generation(data: Mapping[str, Any], subject: str) -> int | dict[str, Any]:
    """The preview's `stash_generation`, or an error dict explaining its absence.

    Minting without it would produce a token described as backend-enforced that
    nothing enforces, so a missing value fails the preview rather than
    weakening it silently.
    """
    generation = data.get("stash_generation")
    if isinstance(generation, int) and not isinstance(generation, bool):
        return generation
    return {
        "error": (
            f"the backend's sync preview for {subject} returned no stash generation, so a "
            "confirm could not be made conditional on it and this preview would not be safe "
            "to apply — nothing was previewed; check that the beetDeck API is up to date"
        )
    }


def build_tools(
    client: BeetdeckClient,
    store: PreviewStore,
) -> tuple[Callable[..., dict[str, Any]], ...]:
    """The mbsync preview tool bound to `client` and `store`."""

    def preview_mbsync(album_id: int) -> dict[str, Any]:
        """Show what re-syncing an album against its own MusicBrainz release
        would change, without writing anything.

        Re-fetches the release the album's `mb_albumid` already points to —
        picking up a fix an editor made after the album was first identified —
        and diffs it field by field: `album_fields` for the album row (label,
        albumartist, …), `track_fields` for per-track fields grouped by field
        name, each with the affected tracks' old/new values. `unmapped` lists
        local tracks the release's track list could not be matched to; those
        are left untouched by design, not silently dropped.

        Returns that diff plus a single-use `preview_token`. Applying it writes
        **every** field the diff shows — there is no way to exclude individual
        fields from this tool, unlike the web UI's sync dialog; if you only want
        some of the changes, apply the fields you want through
        `preview_tags_edit` instead.

        The album must already carry an `mb_albumid` and be identified against
        MusicBrainz (not another metadata source) — `get_album` shows both. A
        release MusicBrainz no longer has, or that could not be reached, is
        reported as a failure, never as "nothing to change".
        """
        response = client.post(f"/api/album/{album_id}/mbsync")
        if not response.ok:
            return failure(response)

        data = response.data
        if not data.get("changed"):
            return {
                "error": (
                    f"the MusicBrainz release for album {album_id} proposes no change — "
                    "nothing was previewed"
                )
            }

        subject = f"album {album_id}"
        generation = _stash_generation(data, subject)
        if isinstance(generation, dict):
            return generation

        unmapped = [row for row in data.get("unmapped") or [] if isinstance(row, dict)]

        preview = store.mint(
            "mbsync",
            target=subject,
            method="POST",
            endpoint=f"/api/album/{album_id}/mbsync/confirm",
            expected_generation=generation,
            summary=compact(
                {
                    "album_id": album_id,
                    "mb_albumid": data.get("mb_albumid"),
                    "data_source": data.get("data_source"),
                    "album_fields": data.get("album_fields") or [],
                    "track_fields": data.get("track_fields") or [],
                    "unmapped": unmapped,
                }
            ),
        )
        described = store.describe(preview)
        notes = [
            "this preview applies the whole diff on apply_change — individual fields cannot "
            "be excluded from this tool"
        ]
        if unmapped:
            named = ", ".join(
                f"{row.get('title') or 'untitled'} (item {row.get('item_id')})" for row in unmapped
            )
            notes.append(
                f"{len(unmapped)} local track(s) could not be matched to the release and are "
                f"left as is: {named}"
            )
        described["notes"] = notes
        return described

    return (preview_mbsync,)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(client_for(config), default_store()):
        server.add_tool(tool)
