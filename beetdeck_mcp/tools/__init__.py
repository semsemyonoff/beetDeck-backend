"""Single registration point for every MCP tool module.

A module that is not listed in `TOOL_MODULES` is invisible to the server, and
nothing else would notice — so `tests/test_mcp_auth.py` asserts this list matches
the modules actually present in the package.

Each listed module must expose `register(server, config) -> None` and a
`TOOL_NAMES` tuple naming the tools it registers.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from beetdeck_mcp.client import BeetdeckClient

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_MODULES: tuple[str, ...] = ("read",)

#: One pooled REST client per configuration, shared by every tool module — the
#: modules are registered together and live as long as the process, so a client
#: each would mean a connection pool each for the same backend.
_CLIENTS: dict["Config", BeetdeckClient] = {}


def client_for(config: "Config") -> BeetdeckClient:
    """The REST client every tool module should use for `config`."""
    client = _CLIENTS.get(config)
    if client is None:
        client = BeetdeckClient(config)
        _CLIENTS[config] = client
    return client


def register_all(server: "MCPServer", config: "Config") -> None:
    """Register every tool module on `server`."""
    for name in TOOL_MODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        module.register(server, config)
