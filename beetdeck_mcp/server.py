"""MCP server construction: auth, transport security, `/health`, tool wiring."""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from beetdeck_mcp.auth import StaticTokenVerifier, build_auth_settings
from beetdeck_mcp.config import Config, load_config
from beetdeck_mcp.tools import register_all

SERVER_NAME = "beetdeck"

INSTRUCTIONS = """\
Manage a beets music library: browse albums, identify them against MusicBrainz,
fetch cover art and lyrics, edit tags.

Every mutating capability is two-phase: a `preview_*` tool returns the diff plus
a single-use `preview_token`, and `apply_change(preview_token)` commits it. How
strongly a token is protected against a concurrent change differs by kind — each
preview tool states its own guarantee.
"""


def build_transport_security(config: Config) -> TransportSecuritySettings | None:
    """DNS-rebinding allowlist, or `None` to keep the SDK's localhost default.

    Origins are derived from the hosts rather than configured separately: the
    only browser origin that can legitimately reach this server is one of the
    hostnames it is served under.
    """
    if not config.allowed_hosts:
        return None
    return TransportSecuritySettings(
        allowed_hosts=list(config.allowed_hosts),
        allowed_origins=[
            f"{scheme}://{host}" for host in config.allowed_hosts for scheme in ("http", "https")
        ],
    )


def create_server(config: Config | None = None) -> MCPServer:
    """Build the MCP server with auth, `/health` and every registered tool."""
    config = load_config() if config is None else config

    server = MCPServer(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        token_verifier=StaticTokenVerifier(config.tokens),
        auth=build_auth_settings(config),
    )

    # Custom routes are appended after the MCP endpoints are wrapped in
    # RequireAuthMiddleware, so this one stays reachable without a token — which
    # is what the container healthcheck needs.
    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "readonly": config.readonly})

    register_all(server, config)
    return server


def create_http_app(config: Config, server: MCPServer | None = None) -> Starlette:
    """The streamable-http ASGI app, as `run()` would build it."""
    server = create_server(config) if server is None else server
    return server.streamable_http_app(
        transport_security=build_transport_security(config),
        host=config.host,
    )


def run(config: Config) -> None:
    """Serve streamable-http on the configured in-container host/port."""
    create_server(config).run(
        "streamable-http",
        host=config.host,
        port=config.port,
        transport_security=build_transport_security(config),
    )
