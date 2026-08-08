"""Environment-driven configuration for the MCP server.

Two settings are startup-fatal when empty — the bearer token table and the
MusicBrainz user agent. Neither has a safe default: an empty token table would
publish an unauthenticated write surface, and MusicBrainz rejects requests
without a real user agent. Both raise `ConfigError` rather than degrading.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

DEFAULT_API_URL = "http://backend:5000"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5100

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"", "0", "false", "no", "off"}


class ConfigError(RuntimeError):
    """The environment does not carry a usable configuration."""


@dataclass(frozen=True)
class Config:
    """Validated MCP server configuration.

    `host`/`port` are the **in-container** bind only; the host-side published
    port is a separate compose variable so `dwe test` can remap it without
    changing what the process listens on.
    """

    api_url: str
    tokens: tuple[str, ...]
    readonly: bool
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    musicbrainz_user_agent: str


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ConfigError(f"{name} must be one of 1/0/true/false/yes/no/on/off, got {raw!r}")


def _port(env: Mapping[str, str]) -> int:
    raw = env.get("BEETDECK_MCP_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ConfigError(f"BEETDECK_MCP_PORT must be an integer, got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"BEETDECK_MCP_PORT must be in 1..65535, got {port}")
    return port


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read and validate the configuration, or raise `ConfigError`."""
    env = os.environ if env is None else env

    tokens = _split(env.get("BEETDECK_MCP_TOKEN", ""))
    if not tokens:
        raise ConfigError(
            "BEETDECK_MCP_TOKEN is empty — refusing to start an unauthenticated MCP server. "
            "Set it to one or more comma-separated bearer tokens."
        )

    user_agent = env.get("MUSICBRAINZ_USER_AGENT", "").strip()
    if not user_agent:
        raise ConfigError(
            "MUSICBRAINZ_USER_AGENT is empty — MusicBrainz rejects requests without a "
            "descriptive user agent. Set it to 'app/version (contact)'."
        )

    api_url = env.get("BEETDECK_API_URL", "").strip() or DEFAULT_API_URL

    return Config(
        api_url=api_url.rstrip("/"),
        tokens=tokens,
        readonly=_flag(env, "BEETDECK_MCP_READONLY", False),
        host=env.get("BEETDECK_MCP_HOST", "").strip() or DEFAULT_HOST,
        port=_port(env),
        allowed_hosts=_split(env.get("BEETDECK_MCP_ALLOWED_HOSTS", "")),
        musicbrainz_user_agent=user_agent,
    )
