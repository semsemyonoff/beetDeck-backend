"""Tag editing — the generic write path into one track's beets row.

`preview_tags_edit` produces the diff and a token; `apply_change` sends the
`PATCH`. Three REST facts shape it:

* **Which names are writable is decided in two places, not one.**
  `/api/items/fields` is the catalog of beets' *fixed* fields and says which of
  them are read-only (`mb_*`, `length`, the ReplayGain columns, …). Everything
  else — an arbitrary name — is writable exactly when the item already carries
  it as a flexible attribute, which only the write handler can tell, per item.
  So this tool rejects what the catalog **knows** to be non-editable and passes
  everything else through: pre-filtering against the catalog would make every
  legitimate flex-attr edit impossible. A name that is neither known nor
  currently on the item gets a *note*, not a refusal — the note is free, and
  refusing would block the one case where the read is misleading (a flex attr
  holding an empty string is not returned by the read endpoint at all).
* **An item's current tags are readable only under its album**
  (`/api/album/<album_id>/track/<item_id>/tags`), while the write is addressed
  per item. That is why this tool takes both ids: without the album there is
  nothing to diff against and nothing to re-read at apply time. Both discovery
  paths hand the agent an album id — `get_album` is album-scoped and
  `list_untagged_items` carries `album_id` per row.
* **An empty value clears the field** (`del item[name]`, uniform for fixed and
  flexible attrs), so `""` and "absent" are one state throughout: the diff, the
  drift check and the write all agree on it.

Only fields that actually change are written. A no-op field in the request would
otherwise widen the drift check — and hence the ways to be refused — without
changing anything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.preview import PreviewStore, default_store, values_equal
from beetdeck_mcp.tools import client_for, failure

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_NAMES = ("preview_tags_edit",)

#: The catalog of beets' fixed Item fields, with an `editable` flag each.
CATALOG_PATH = "/api/items/fields"


def read_path(album_id: int, item_id: int) -> str:
    """Where a track's current tags are read — album-scoped, see module docstring."""
    return f"/api/album/{album_id}/track/{item_id}/tags"


def _catalog(rows: Any) -> tuple[frozenset[str], frozenset[str]]:
    """`(known, non_editable)` field names from the `/api/items/fields` payload."""
    entries = [row for row in rows if isinstance(row, dict) and row.get("name")]
    return (
        frozenset(row["name"] for row in entries),
        frozenset(row["name"] for row in entries if not row.get("editable")),
    )


def _rejected(fields: Mapping[str, Any], non_editable: frozenset[str]) -> list[str]:
    """The requested names the backend is guaranteed to refuse.

    `mb_*` is checked by prefix as well as by catalog membership: the rule in
    `item_fields.is_editable` is the prefix, so an `mb_` name that is not a fixed
    field (and therefore not in the catalog) is refused just the same.
    """
    return sorted(name for name in fields if name in non_editable or name.startswith("mb_"))


def build_tools(
    client: BeetdeckClient,
    store: PreviewStore,
) -> tuple[Callable[..., dict[str, Any]], ...]:
    """The tag-editing preview tool bound to `client` and `store`."""

    def preview_tags_edit(
        album_id: int,
        item_id: int,
        fields: dict[str, str],
    ) -> dict[str, Any]:
        """Show what editing one track's tags would change.

        `fields` maps beets field names to their new values, as strings — the
        backend coerces them to the field's type (`track: "5"`, `comp: "true"`,
        a list field such as `genres` from a comma-separated string). **An empty
        string clears the field.** Pass only the fields you want to change;
        anything already equal to its new value is left out of the write.

        Read-only fields (`mb_*`, `length`, `bpm`, the ReplayGain columns, …) are
        refused here. Any other name is accepted: beets' own fields always, and
        an arbitrary name when the track already carries it as a flexible
        attribute — which is checked when the change is applied, so an unknown
        name that the track does not carry comes back as a failed `apply_change`
        with a note about it here.

        Returns the per-field diff plus a single-use `preview_token`; nothing is
        written until you pass that token to `apply_change`, which re-reads these
        same fields first and refuses if any of them changed in the meantime.

        `album_id` is the album the track belongs to — a track's current tags are
        only readable under its album, and both `get_album` and
        `list_untagged_items` give you the pair.
        """
        if not fields:
            return {
                "error": (
                    "fields is empty — pass at least one field name → new value pair "
                    "(an empty string clears that field)"
                )
            }

        catalog = client.get(CATALOG_PATH)
        if not catalog.ok:
            return failure(catalog)
        known, non_editable = _catalog(catalog.payload if isinstance(catalog.payload, list) else [])

        rejected = _rejected(fields, non_editable)
        if rejected:
            return {
                "error": (
                    f"these fields are not editable and nothing was previewed: "
                    f"{', '.join(rejected)} — they are derived from the audio file or from "
                    "MusicBrainz identification; use identify_album to change identification "
                    "data"
                )
            }

        current = client.get(read_path(album_id, item_id))
        if not current.ok:
            return failure(current)
        tags = current.data

        changes: dict[str, dict[str, Any]] = {}
        unchanged: list[str] = []
        for name, value in fields.items():
            old = tags.get(name)
            if values_equal(old, value):
                unchanged.append(name)
                continue
            change: dict[str, Any] = {"old": old, "new": value}
            if value == "":
                change["clears"] = True
            changes[name] = change

        if not changes:
            return {
                "error": (
                    f"item {item_id} already carries these values ({', '.join(unchanged)}) — "
                    "there is nothing to change, so no preview was created"
                )
            }

        notes: list[str] = []
        if unchanged:
            notes.append(
                f"already at the requested value, left out of this edit: {', '.join(unchanged)}"
            )
        unverifiable = [name for name in changes if name not in known and name not in tags]
        if unverifiable:
            notes.append(
                f"not beets fields and not currently on this track: {', '.join(unverifiable)} "
                "— the write is rejected unless the track already carries them as flexible "
                "attributes, which cannot be checked from here"
            )

        preview = store.mint(
            "tags",
            target=f"item {item_id}",
            method="PATCH",
            endpoint=f"/api/items/{item_id}/tags",
            params={"fields": {name: change["new"] for name, change in changes.items()}},
            old_values={name: change["old"] for name, change in changes.items()},
            reread_endpoint=read_path(album_id, item_id),
            summary={"album_id": album_id, "item_id": item_id, "fields": changes},
        )
        described = store.describe(preview)
        if notes:
            described["notes"] = notes
        return described

    return (preview_tags_edit,)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(client_for(config), default_store()):
        server.add_tool(tool)
