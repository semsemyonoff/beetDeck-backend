"""MCP resources: the live OpenAPI spec and the workflow guide.

Resources are wired in `server.py`, **not** in `tools.register_all` — they are a
separate MCP capability with a separate contract (a URI a client reads on demand,
not a callable), and keeping the split visible is why `register_all` stays a
tool-only loop.

Two resources, for two things a tool description cannot carry:

* `beetdeck://openapi.json` — the spec is fetched from the running backend on
  every read, so it describes the API that is actually deployed rather than a
  copy that drifts. It is the map for `beetdeck_api_call`.
* `beetdeck://guide/workflow` — the preview→apply contract, the per-kind
  guarantee table and the ambiguity playbook in one place. Each tool states its
  own guarantee, but an agent that has not called a `preview_*` tool yet has no
  way to *find* that out, and the identification playbook spans five tools.

The spec read is the one place in this package where a failed round trip becomes
content rather than a raise: the SDK converts an exception into an opaque
`ResourceError` with the reason stripped, so a failure returned as JSON is the
only shape that tells the agent what actually went wrong.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.tools import client_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

OPENAPI_URI = "beetdeck://openapi.json"
WORKFLOW_URI = "beetdeck://guide/workflow"

RESOURCE_URIS = (OPENAPI_URI, WORKFLOW_URI)

#: Served by spectree from the running app; not under `/api/`, which is why
#: `beetdeck_api_call` cannot reach it and this resource exists.
OPENAPI_PATH = "/apidoc/openapi.json"

WORKFLOW_GUIDE = """\
# Working on a beets library through this server

## Every write is two-phase

No tool writes as a side effect of showing you something. A `preview_*` tool
returns the diff it *would* apply plus a single-use `preview_token`, and
`apply_change(preview_token)` commits exactly that diff. A token is burned on a
successful apply and expires on its own (15 minutes; 5 for `cover` and
`lyrics`).

The preview tools are `preview_match`, `preview_items_match`,
`preview_tags_edit`, `preview_cover`, `preview_lyrics` and `preview_genres`.

`BEETDECK_MCP_READONLY` disables `apply_change` alone. It does **not** make the
server inert: `rescan_library` re-imports files, `compute_bpm` writes a BPM tag
into the audio file and `toggle_album_ignored` flips an album flag. None of the
three goes through a preview.

## How strongly a token is protected — per kind

These writes land in real audio files, and the protection is not uniform.

| kind | produced by | guarantee |
|---|---|---|
| `identify`, `items_identify` | `preview_match`, `preview_items_match` | **Enforced by the backend.** The token carries the identification generation read at preview time; the confirm is rejected inside the backend's own lock if identification was re-run or already applied. |
| `tags`, `genres` | `preview_tags_edit`, `preview_genres` | **Best effort.** The previewed old values are re-read immediately before writing and the apply is refused on drift. A change landing between that re-read and the write is not caught. |
| `cover`, `lyrics` | `preview_cover`, `preview_lyrics` | **TTL only.** The backend stash has no identity and no read endpoint, so a competing fetch from the web UI overwrites it and wins silently. Apply promptly. |

A refused apply says which of the three failed — expiry, value drift, or the
backend precondition — so re-run the matching preview rather than retrying the
same token.

Two consequences worth knowing:

* Genres commit through the backend's `save` endpoint, not its `confirm`
  endpoint, because `confirm` performs a second, independent lookup that can
  write a genre different from the one you were shown.
* `apply_change` answers `ok`, `partial` or `failed`. `partial` is real: the
  database was written but at least one audio file was not. The `detail` list
  names what was missed.

## When identification returns the wrong album

`identify_album` runs the backend's autotagger and returns up to five candidates.
When they are all wrong, do not pick the least-bad one — find the right release
and force it:

1. `get_album(album_id, include_raw_tags=true)` — the tracklist with per-track
   `length_sec` and `filename`, which is what makes a release comparable.
2. `musicbrainz_search_release(...)` — search by artist and title; narrow with
   the track count.
3. `musicbrainz_get_release(mbid)` — compare the track count and the per-track
   durations against step 1 before committing to a release.
4. `identify_album(album_id, mb_release_id=<mbid>)` — an explicit release id
   bypasses the autotagger's distance filter entirely and always yields a
   candidate with a real track mapping.
5. `preview_match(album_id, 0)` → read the diff → `apply_change(token)`.

An album that should never be identified — a bootleg, a personal recording — is
parked with `toggle_album_ignored`, which takes it out of the untagged queue.

## Reaching an endpoint with no tool

`beetdeck_api_call(path)` forwards a `GET` to any `/api/…` path and returns the
body unchanged; `beetdeck://openapi.json` is the endpoint list. It is read-only
by design — there is no raw write path, because that would bypass everything
above.
"""


def read_openapi(client: BeetdeckClient) -> str:
    """The backend's live OpenAPI spec as JSON text.

    A failed round trip comes back as a JSON `{"error": …}` body rather than as a
    raise — see the module docstring.
    """
    response = client.get(OPENAPI_PATH)
    if not response.ok:
        return json.dumps(
            {
                "error": (
                    f"the beetDeck OpenAPI spec could not be read from {OPENAPI_PATH}: "
                    f"{response.message}"
                )
            },
            indent=2,
        )
    return json.dumps(response.payload, indent=2)


def register(server: "MCPServer", config: "Config") -> None:
    """Register both resources on `server`."""
    client = client_for(config)

    @server.resource(
        OPENAPI_URI,
        name="beetdeck_openapi",
        title="beetDeck REST API (OpenAPI 3)",
        description=(
            "The live OpenAPI specification of the beetDeck REST API, fetched from the "
            "running backend. The endpoint list for beetdeck_api_call."
        ),
        mime_type="application/json",
    )
    def beetdeck_openapi() -> str:
        return read_openapi(client)

    @server.resource(
        WORKFLOW_URI,
        name="beetdeck_workflow_guide",
        title="beetDeck workflow guide",
        description=(
            "How to work on the library through this server: the preview→apply contract, "
            "how strongly each kind of write is protected, and what to do when "
            "identification returns the wrong album."
        ),
        mime_type="text/markdown",
    )
    def beetdeck_workflow_guide() -> str:
        return WORKFLOW_GUIDE
