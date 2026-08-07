"""`beetdeck_api_call` — the read-only escape hatch onto the REST API.

The typed tools cover the workflows this server was built for, but the backend
has ~40 endpoints and the OpenAPI resource describes all of them. Rather than
grow a tool per leftover endpoint, one tool forwards a `GET` and returns the
body unchanged.

Two limits are deliberate and not configurable:

* **`GET` only.** A write escape would bypass the preview→apply contract that is
  this server's whole guarantee, so there is none — `BEETDECK_MCP_RAW_WRITES` was
  considered and rejected. Every write goes through a `preview_*` tool and
  `apply_change`.
* **`/api/` paths only, relative.** An absolute URL would turn the tool into a
  general-purpose HTTP client running inside the container network, and a
  traversal (`/api/../…`) would reach the SPA shell and the `/apidoc/` routes.
  Both are rejected before the request is made, not sanitized into something
  else.

Payloads are passed through verbatim: unlike the typed tools this one cannot know
which fields matter, so it does not compact or window the response.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.tools import client_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_NAMES = ("beetdeck_api_call",)

#: The only prefix this tool will request. Everything the API exposes lives
#: under it; `/apidoc/` (the spec) is reachable as an MCP resource instead.
ALLOWED_PREFIX = "/api/"


def _reject(path: str) -> str | None:
    """Why `path` may not be requested, or `None` when it may.

    Checked against the **percent-decoded** path so `%2e%2e` is treated as the
    `..` it becomes on the wire.
    """
    split = urlsplit(path)
    if split.scheme or split.netloc:
        return (
            f"path must be relative and start with {ALLOWED_PREFIX!r}, got {path!r} — "
            "this tool talks to the configured beetDeck API only, not to arbitrary URLs"
        )
    if not path.startswith(ALLOWED_PREFIX):
        return (
            f"path must start with {ALLOWED_PREFIX!r}, got {path!r} — only the REST API is "
            "reachable through this tool"
        )
    if ".." in unquote(split.path).split("/"):
        return (
            f"path must not contain a '..' segment, got {path!r} — it would escape "
            f"{ALLOWED_PREFIX!r}"
        )
    return None


def build_tools(client: BeetdeckClient) -> tuple[Callable[..., dict[str, Any]], ...]:
    """The escape-hatch tool bound to `client`.

    Split out from `register` so tests drive the exact callable the server
    exposes against the in-process Flask app.
    """

    def beetdeck_api_call(path: str) -> dict[str, Any]:
        """`GET` any beetDeck REST endpoint and return its response verbatim.

        The escape hatch for endpoints with no dedicated tool. Read
        `beetdeck://openapi.json` (an MCP resource on this server) for the full
        endpoint list, then pass a path relative to the API root, query string
        included — for example `/api/album/12/tags` or `/api/search?q=miles`.

        **Read-only.** This tool cannot write: `GET` is the only method, and
        there is no raw write mode. Every change goes through a `preview_*` tool
        followed by `apply_change`, so that a diff is seen before it lands in
        real audio files. The path must be relative and start with `/api/`;
        absolute URLs and `..` segments are refused.

        The response body is returned unchanged, including its size — prefer the
        typed tools where one exists, since they trim and page what they return.
        """
        rejection = _reject(path)
        if rejection is not None:
            return {"path": path, "status": "failed", "error": rejection}

        response = client.get(path)
        if not response.ok:
            result = {"path": path, "status": "failed", "error": response.message}
            if response.status_code is not None:
                result["status_code"] = response.status_code
            return result
        return {
            "path": path,
            "status": "ok",
            "status_code": response.status_code,
            "data": response.payload,
        }

    return (beetdeck_api_call,)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(client_for(config)):
        server.add_tool(tool)
