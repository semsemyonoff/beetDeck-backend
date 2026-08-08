"""Static bearer-token authentication for the MCP server.

There is no OAuth server here: the token table comes from `BEETDECK_MCP_TOKEN`.
`AuthSettings` is still constructed because `MCPServer.__init__` raises when a
`token_verifier` is passed without it, and its `issuer_url` is only ever used to
build metadata URLs.
"""

from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from beetdeck_mcp.config import Config

#: Scope every configured token carries; `AuthSettings.required_scopes` demands it.
MCP_SCOPE = "beetdeck"


class StaticTokenVerifier:
    """`mcp.server.auth.provider.TokenVerifier` over a fixed token table.

    `verify_token` is a coroutine because the SDK protocol is async — unlike the
    tool callables, which the SDK dispatches to a worker thread and which this
    package keeps synchronous throughout.
    """

    def __init__(self, tokens: tuple[str, ...]) -> None:
        if not tokens:
            raise ValueError("StaticTokenVerifier requires at least one token")
        # The client id is positional, never the token itself: it ends up in
        # logs and in the auth context handed to tools.
        #
        # Compared as bytes: `hmac.compare_digest` raises `TypeError` on a `str`
        # holding a code point above U+007F, and the bearer value comes straight
        # off the wire (headers decode as latin-1), so a non-ASCII byte in the
        # header — or in `BEETDECK_MCP_TOKEN` — would be a 500 instead of a 401.
        self._tokens = tuple(
            (f"beetdeck-mcp-{index}", token.encode("utf-8"))
            for index, token in enumerate(tokens, 1)
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate = token.encode("utf-8")
        for client_id, known in self._tokens:
            if hmac.compare_digest(candidate, known):
                return AccessToken(token=token, client_id=client_id, scopes=[MCP_SCOPE])
        return None


def build_auth_settings(config: Config) -> AuthSettings:
    """Auth settings matching the static token table.

    Both URLs are derived from the in-container bind, which is not how a client
    reaches the server. That is harmless here: with static tokens they are only
    ever echoed in discovery metadata, never resolved or matched against a token.
    """
    base = f"http://{config.host}:{config.port}"
    return AuthSettings(
        issuer_url=base,
        resource_server_url=f"{base}/mcp",
        required_scopes=[MCP_SCOPE],
    )
