"""Single registration point for every MCP tool module.

A module that is not listed in `TOOL_MODULES` is invisible to the server, and
nothing else would notice — so `tests/test_mcp_auth.py` asserts this list matches
the modules actually present in the package.

Each listed module must expose `register(server, config) -> None` and a
`TOOL_NAMES` tuple naming the tools it registers.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.musicbrainz import MusicBrainzClient

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_MODULES: tuple[str, ...] = (
    "apply",
    "genres",
    "identify",
    "library",
    "media",
    "musicbrainz",
    "raw",
    "read",
    "tags",
)

#: One pooled REST client per configuration, shared by every tool module — the
#: modules are registered together and live as long as the process, so a client
#: each would mean a connection pool each for the same backend.
_CLIENTS: dict["Config", BeetdeckClient] = {}

#: Same reasoning for MusicBrainz, plus a harder requirement: the ≤1 req/s gate
#: lives on the client, so a second instance would double the request rate.
_MB_CLIENTS: dict["Config", MusicBrainzClient] = {}


class FailedResponse(Protocol):
    """What `failure()` needs — `client.ApiResponse` and `musicbrainz.MbResponse`."""

    @property
    def message(self) -> str: ...  # pragma: no cover - structural typing only


def compact(row: Mapping[str, Any]) -> dict[str, Any]:
    """Drop absent values — `None`, `""` and empty containers.

    Every payload lands in an agent's context window, so absent fields are not
    worth their tokens. Booleans stay: `tagged: false` is a fact, not an absence.
    """
    return {
        key: value
        for key, value in row.items()
        if value is not None
        and value != ""
        and not (isinstance(value, (list, dict, tuple)) and not value)
    }


def failure(response: FailedResponse) -> dict[str, Any]:
    """A failed round trip as a tool result.

    Deliberately carries no result key: an unreachable backend must not read as
    an empty library.
    """
    return {"error": response.message}


def client_for(config: "Config") -> BeetdeckClient:
    """The REST client every tool module should use for `config`."""
    client = _CLIENTS.get(config)
    if client is None:
        client = BeetdeckClient(config)
        _CLIENTS[config] = client
    return client


def musicbrainz_client_for(config: "Config") -> MusicBrainzClient:
    """The MusicBrainz client every tool module should use for `config`."""
    client = _MB_CLIENTS.get(config)
    if client is None:
        client = MusicBrainzClient(config.musicbrainz_user_agent)
        _MB_CLIENTS[config] = client
    return client


def register_all(server: "MCPServer", config: "Config") -> None:
    """Register every tool module on `server`."""
    for name in TOOL_MODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        module.register(server, config)
