"""MCP server skeleton: config validation, bearer auth, /health, tool registration."""

import asyncio
import importlib
import json
import pkgutil

import pytest
from starlette.testclient import TestClient

import beetdeck_mcp.server as server_module
import beetdeck_mcp.tools as tools_pkg
from beetdeck_mcp.__main__ import main
from beetdeck_mcp.auth import MCP_SCOPE, StaticTokenVerifier, build_auth_settings
from beetdeck_mcp.config import (
    DEFAULT_API_URL,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ConfigError,
    load_config,
)
from beetdeck_mcp.server import build_transport_security, create_http_app
from tests.conftest import MCP_TEST_ENV, MCP_TEST_TOKEN, MCP_TEST_TOKEN_ALT

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "beetdeck-tests", "version": "0"},
    },
}

TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def _rpc_result(response):
    """Pull the JSON-RPC payload out of an SSE-framed streamable-http response."""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    raise AssertionError(f"no SSE data frame in response: {response.text!r}")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_reads_every_setting(self):
        config = load_config(
            {
                "BEETDECK_API_URL": "http://backend:5000/",
                "BEETDECK_MCP_TOKEN": " a , b ,, ",
                "BEETDECK_MCP_READONLY": "1",
                "BEETDECK_MCP_HOST": "127.0.0.1",
                "BEETDECK_MCP_PORT": "6000",
                "BEETDECK_MCP_ALLOWED_HOSTS": "mcp.example.org, localhost",
                "MUSICBRAINZ_USER_AGENT": "beetdeck/1.0 (me@example.org)",
            }
        )
        # The trailing slash is stripped so callers can join paths naively.
        assert config.api_url == "http://backend:5000"
        assert config.tokens == ("a", "b")
        assert config.readonly is True
        assert config.host == "127.0.0.1"
        assert config.port == 6000
        assert config.allowed_hosts == ("mcp.example.org", "localhost")
        assert config.musicbrainz_user_agent == "beetdeck/1.0 (me@example.org)"

    def test_applies_defaults(self):
        config = load_config(
            {"BEETDECK_MCP_TOKEN": "t", "MUSICBRAINZ_USER_AGENT": "ua/1 (me@example.org)"}
        )
        assert config.api_url == DEFAULT_API_URL
        assert config.host == DEFAULT_HOST
        assert config.port == DEFAULT_PORT
        assert config.readonly is False
        assert config.allowed_hosts == ()

    @pytest.mark.parametrize("token", ["", "   ", ",, ,"])
    def test_refuses_to_start_without_a_token(self, token):
        with pytest.raises(ConfigError, match="BEETDECK_MCP_TOKEN"):
            load_config({"BEETDECK_MCP_TOKEN": token, "MUSICBRAINZ_USER_AGENT": "ua/1 (x@y.z)"})

    def test_refuses_to_start_without_a_musicbrainz_user_agent(self):
        with pytest.raises(ConfigError, match="MUSICBRAINZ_USER_AGENT"):
            load_config({"BEETDECK_MCP_TOKEN": "t", "MUSICBRAINZ_USER_AGENT": "   "})

    def test_missing_user_agent_key_is_also_fatal(self):
        with pytest.raises(ConfigError, match="MUSICBRAINZ_USER_AGENT"):
            load_config({"BEETDECK_MCP_TOKEN": "t"})

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("", False)],
    )
    def test_readonly_flag_parsing(self, raw, expected):
        config = load_config({**MCP_TEST_ENV, "BEETDECK_MCP_READONLY": raw})
        assert config.readonly is expected

    def test_rejects_an_unparseable_readonly_flag(self):
        with pytest.raises(ConfigError, match="BEETDECK_MCP_READONLY"):
            load_config({**MCP_TEST_ENV, "BEETDECK_MCP_READONLY": "maybe"})

    @pytest.mark.parametrize("raw", ["nope", "0", "70000", "-1"])
    def test_rejects_an_invalid_port(self, raw):
        with pytest.raises(ConfigError, match="BEETDECK_MCP_PORT"):
            load_config({**MCP_TEST_ENV, "BEETDECK_MCP_PORT": raw})

    def test_reads_the_process_environment_by_default(self, monkeypatch):
        monkeypatch.setenv("BEETDECK_MCP_TOKEN", "from-environ")
        monkeypatch.setenv("MUSICBRAINZ_USER_AGENT", "ua/1 (x@y.z)")
        monkeypatch.delenv("BEETDECK_MCP_READONLY", raising=False)
        assert load_config().tokens == ("from-environ",)


# ---------------------------------------------------------------------------
# token verifier
# ---------------------------------------------------------------------------


class TestStaticTokenVerifier:
    def test_accepts_every_configured_token(self):
        verifier = StaticTokenVerifier(("first", "second"))
        for token in ("first", "second"):
            access = asyncio.run(verifier.verify_token(token))
            assert access is not None
            assert access.token == token
            assert access.scopes == [MCP_SCOPE]
        # Client ids are positional, so they never leak the token itself.
        first = asyncio.run(verifier.verify_token("first"))
        second = asyncio.run(verifier.verify_token("second"))
        assert (first.client_id, second.client_id) == ("beetdeck-mcp-1", "beetdeck-mcp-2")

    @pytest.mark.parametrize("token", ["", "wrong", "firs", "first-and-more"])
    def test_rejects_anything_else(self, token):
        verifier = StaticTokenVerifier(("first", "second"))
        assert asyncio.run(verifier.verify_token(token)) is None

    def test_requires_at_least_one_token(self):
        with pytest.raises(ValueError):
            StaticTokenVerifier(())

    def test_auth_settings_require_the_scope_the_verifier_grants(self, mcp_config):
        settings = build_auth_settings(mcp_config)
        assert settings.required_scopes == [MCP_SCOPE]
        assert str(settings.issuer_url).startswith(f"http://{mcp_config.host}:{mcp_config.port}")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


class TestMain:
    def test_exits_nonzero_without_serving_on_a_bad_config(self, monkeypatch, capsys):
        monkeypatch.delenv("BEETDECK_MCP_TOKEN", raising=False)
        monkeypatch.setenv("MUSICBRAINZ_USER_AGENT", "ua/1 (x@y.z)")
        monkeypatch.setattr(
            server_module,
            "run",
            lambda config: pytest.fail("must not serve with an invalid config"),
        )
        assert main() == 2
        assert "BEETDECK_MCP_TOKEN" in capsys.readouterr().err

    def test_serves_on_the_configured_bind(self, monkeypatch):
        monkeypatch.setenv("BEETDECK_MCP_TOKEN", MCP_TEST_TOKEN)
        monkeypatch.setenv("MUSICBRAINZ_USER_AGENT", "ua/1 (x@y.z)")
        monkeypatch.setenv("BEETDECK_MCP_PORT", "5199")
        served = []
        monkeypatch.setattr(server_module, "run", served.append)
        assert main() == 0
        assert (served[0].port, served[0].tokens) == (5199, (MCP_TEST_TOKEN,))


# ---------------------------------------------------------------------------
# transport security
# ---------------------------------------------------------------------------


class TestTransportSecurity:
    def test_is_unset_without_an_allowlist(self, mcp_config):
        assert build_transport_security(mcp_config) is None

    def test_derives_origins_from_the_allowed_hosts(self):
        config = load_config({**MCP_TEST_ENV, "BEETDECK_MCP_ALLOWED_HOSTS": "mcp.example.org"})
        settings = build_transport_security(config)
        assert settings.allowed_hosts == ["mcp.example.org"]
        assert settings.allowed_origins == [
            "http://mcp.example.org",
            "https://mcp.example.org",
        ]

    def test_a_host_outside_the_allowlist_is_misdirected(self):
        config = load_config({**MCP_TEST_ENV, "BEETDECK_MCP_ALLOWED_HOSTS": "mcp.example.org"})
        with TestClient(create_http_app(config)) as http_client:
            resp = http_client.post(
                "/mcp",
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Authorization": f"Bearer {MCP_TEST_TOKEN}"},
            )
        # TestClient sends Host: testserver, which is not in the allowlist.
        assert resp.status_code == 421


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


class TestHealthRoute:
    def test_answers_without_a_token(self, mcp_http_client):
        resp = mcp_http_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "readonly": False}

    def test_reports_readonly_mode(self):
        config = load_config({**MCP_TEST_ENV, "BEETDECK_MCP_READONLY": "1"})
        with TestClient(create_http_app(config)) as http_client:
            assert http_client.get("/health").json()["readonly"] is True


class TestBearerAuth:
    def test_rejects_a_request_without_a_token(self, mcp_http_client):
        resp = mcp_http_client.post("/mcp", json=TOOLS_LIST, headers=MCP_HEADERS)
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "header",
        ["Bearer wrong-token", "Bearer ", MCP_TEST_TOKEN, f"Basic {MCP_TEST_TOKEN}"],
    )
    def test_rejects_a_bad_authorization_header(self, mcp_http_client, header):
        resp = mcp_http_client.post(
            "/mcp", json=TOOLS_LIST, headers={**MCP_HEADERS, "Authorization": header}
        )
        assert resp.status_code == 401

    @pytest.mark.parametrize("token", [MCP_TEST_TOKEN, MCP_TEST_TOKEN_ALT])
    def test_accepts_every_configured_token(self, mcp_http_client, token):
        resp = mcp_http_client.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        result = _rpc_result(resp)["result"]
        assert result["serverInfo"]["name"] == "beetdeck"
        assert resp.headers["mcp-session-id"]


# ---------------------------------------------------------------------------
# tool registration
# ---------------------------------------------------------------------------


def _package_modules() -> set[str]:
    return {info.name for info in pkgutil.iter_modules(tools_pkg.__path__)}


class TestToolRegistration:
    def test_tool_modules_list_matches_the_package_contents(self):
        """A tool module that exists but is not listed would register nothing."""
        assert _package_modules() == set(tools_pkg.TOOL_MODULES)

    def test_every_listed_module_exposes_the_registration_contract(self):
        for name in tools_pkg.TOOL_MODULES:
            module = importlib.import_module(f"{tools_pkg.__name__}.{name}")
            assert callable(getattr(module, "register", None)), f"{name}.register missing"
            assert isinstance(getattr(module, "TOOL_NAMES", None), tuple), (
                f"{name}.TOOL_NAMES missing"
            )

    def test_register_all_registers_every_declared_tool(self, mcp_server):
        expected = {
            tool_name
            for module_name in tools_pkg.TOOL_MODULES
            for tool_name in importlib.import_module(
                f"{tools_pkg.__name__}.{module_name}"
            ).TOOL_NAMES
        }
        registered = {tool.name for tool in asyncio.run(mcp_server.list_tools())}
        assert expected <= registered

    def test_registered_tools_are_reachable_over_http(self, mcp_server, mcp_http_client):
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {MCP_TEST_TOKEN}"}
        init = mcp_http_client.post("/mcp", json=INITIALIZE, headers=headers)
        session_headers = {**headers, "mcp-session-id": init.headers["mcp-session-id"]}
        mcp_http_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )
        resp = mcp_http_client.post("/mcp", json=TOOLS_LIST, headers=session_headers)
        assert resp.status_code == 200
        listed = {tool["name"] for tool in _rpc_result(resp)["result"]["tools"]}
        assert {tool.name for tool in asyncio.run(mcp_server.list_tools())} == listed
