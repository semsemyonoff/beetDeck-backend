"""Genre previews — the one kind whose preview and commit use *different*
endpoints, on purpose.

The backend offers three genre routes and only two of them can be combined into
a preview→apply pair that writes what it showed:

* `POST …/genre` runs lastgenre in pretend mode and restores the old value. It
  is the suggestion, and nothing else — it stashes nothing.
* `POST …/genre/confirm` performs a **second, independent Last.fm lookup** before
  writing, so what it commits is whatever Last.fm answers at commit time, not
  what the preview showed. This tool never uses it.
* `POST …/genre/save` takes an explicit comma-separated string. That is the only
  route that can bind a previewed value, so it is the one `apply_change` sends.

Two consequences worth stating:

* **The old value is read from `GET /api/album/<id>`, not from the lookup's
  `old_genre`.** The two disagree: the lookup reads only the native `genres`
  list, while the album endpoint falls back to the legacy singular `genre`
  column when `genres` is empty — so on an album tagged before beets 2.10 the
  lookup says `""` where the album says `Rock`, and the drift check at apply
  time would refuse every such album. The previewed old value must come from the
  endpoint `apply_change` re-reads.
* **An override skips the lookup entirely.** An agent that already knows the
  genre it wants has no reason to make the backend query Last.fm, and no reason
  to be blocked when lastgenre is unavailable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.preview import PreviewStore, default_store, values_equal
from beetdeck_mcp.tools import client_for, failure

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_NAMES = ("preview_genres",)

GenreMode = Literal["replace", "merge"]


def read_path(album_id: int) -> str:
    """Where the album's current genre is read — and re-read on apply."""
    return f"/api/album/{album_id}"


def lookup_path(album_id: int) -> str:
    """The pretend-mode lastgenre lookup (no write)."""
    return f"/api/album/{album_id}/genre"


def save_path(album_id: int) -> str:
    """The value-binding write — see the module docstring on `…/genre/confirm`."""
    return f"/api/album/{album_id}/genre/save"


def _clean(genre: str) -> str:
    """A comma-separated genre string with empty entries dropped.

    Mirrors what `save_genre` does with the value (`split(",")` + `strip`), so a
    string that would reach the handler as an empty list — `" , "` — is refused
    here instead of silently clearing the album's genre.
    """
    return ", ".join(part.strip() for part in genre.split(",") if part.strip())


def build_tools(
    client: BeetdeckClient,
    store: PreviewStore,
) -> tuple[Callable[..., dict[str, Any]], ...]:
    """The genre preview tool bound to `client` and `store`."""

    def preview_genres(
        album_id: int, genre: str = "", mode: GenreMode = "replace"
    ) -> dict[str, Any]:
        """Show what setting an album's genre would change.

        Omit `genre` to have the backend look one up from Last.fm without
        writing it; pass `genre` to set a value of your own — one name or several
        as a comma-separated string (`"Rock, Post-Punk"`), written verbatim. An
        override does not query Last.fm at all, and `mode` does not apply to it.

        `mode` decides what a looked-up genre is combined with:

        * `replace` (default) — the album's genres become exactly what Last.fm
          returned.
        * `merge` — the album keeps its current genres and gains the fetched ones
          it does not already carry (case-insensitive, current ones first). Use
          this when the existing genre came from MusicBrainz or from the files
          and is worth keeping.

        The genre is written to the album and to every one of its tracks.
        Returns the old → new diff plus a single-use `preview_token`; nothing is
        written until you pass that token to `apply_change`, which re-reads the
        album's current genre first and refuses if it changed in the meantime.
        The value it writes is exactly the one shown here.

        A Last.fm lookup that could not be performed is reported as a failure,
        never as "this album has no genre" — the two are different facts.
        """
        override = _clean(genre)
        if genre and not override:
            return {
                "error": (
                    f"genre {genre!r} has no genre names in it — pass one name, or several "
                    "separated by commas; to look one up instead, omit the argument. "
                    "Nothing was previewed."
                )
            }

        current = client.get(read_path(album_id))
        if not current.ok:
            return failure(current)
        old = current.data.get("genre") or ""

        fetched = ""
        if override:
            new, source = override, "override"
        else:
            lookup = client.post(lookup_path(album_id), params={"mode": mode})
            # A 502 here is the backend saying the lookup never happened; passing
            # it through as a failure is the whole point of that status.
            if not lookup.ok:
                return failure(lookup)
            new = lookup.data.get("new_genre") or ""
            fetched = lookup.data.get("fetched_genre") or ""
            source = "lastgenre"
            if not new:
                return {
                    "error": (
                        f"Last.fm returned no genre for album {album_id} — nothing was "
                        "previewed; pass the genre argument to set one yourself"
                    )
                }

        if values_equal(old, new):
            return {
                "error": (
                    f"album {album_id} already carries the genre {new!r} — there is nothing "
                    "to change, so no preview was created"
                )
            }

        preview = store.mint(
            "genres",
            target=f"album {album_id}",
            method="POST",
            endpoint=save_path(album_id),
            params={"genre": new},
            old_values={"genre": old},
            reread_endpoint=read_path(album_id),
            summary={
                "album_id": album_id,
                "genre": {"old": old, "new": new},
                "source": source,
                # In merge mode `new` is old+fetched, so without these two the
                # agent cannot see what Last.fm actually contributed.
                **({"mode": mode} if not override else {}),
                **({"fetched": fetched} if fetched and fetched != new else {}),
            },
        )
        return store.describe(preview)

    return (preview_genres,)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(client_for(config), default_store()):
        server.add_tool(tool)
