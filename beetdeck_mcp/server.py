"""MCP server construction: auth, transport security, `/health`, tool wiring."""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from beetdeck_mcp import resources
from beetdeck_mcp.auth import StaticTokenVerifier, build_auth_settings
from beetdeck_mcp.config import Config
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
    """DNS-rebinding allowlist, or `None` when none is configured.

    `None` disables the SDK's protection outright — `TransportSecurityMiddleware`
    falls back to `enable_dns_rebinding_protection=False`, it does not narrow to
    localhost. Since this process binds `0.0.0.0` and its port is published, an
    empty `BEETDECK_MCP_ALLOWED_HOSTS` means no `Host`/`Origin` check at all.

    Each configured entry is registered both bare and as the SDK's `host:*`
    port wildcard: the `Host` header carries the port the client connected to, so
    a bare `beetdeck.example` would answer `421` to a client reaching the server
    on any non-default port. An entry that already names a port is left alone.

    Origins are derived from the hosts rather than configured separately: the
    only browser origin that can legitimately reach this server is one of the
    hostnames it is served under.
    """
    if not config.allowed_hosts:
        return None
    hosts: list[str] = []
    for host in config.allowed_hosts:
        hosts.append(host)
        if ":" not in host:
            hosts.append(f"{host}:*")
    return TransportSecuritySettings(
        allowed_hosts=hosts,
        allowed_origins=[f"{scheme}://{host}" for host in hosts for scheme in ("http", "https")],
    )


def create_server(config: Config) -> MCPServer:
    """Build the MCP server with auth, `/health` and every registered tool.

    `config` is required rather than loaded here as a fallback: `__main__.main`
    loads it precisely so a `ConfigError` becomes exit 2 with a readable message,
    and a second load inside server construction would bypass that handling and
    could diverge from the one the process reported.
    """
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
    # Resources are registered here rather than through `register_all`: that loop
    # is the tool registry (one module per tool group, each with `TOOL_NAMES`),
    # and resources are a different capability with a different contract.
    resources.register(server, config)
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
