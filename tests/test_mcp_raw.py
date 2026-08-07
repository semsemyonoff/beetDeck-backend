"""The read-only escape hatch and the two MCP resources.

Both are driven against the real Flask app in-process: the tool forwards real
`GET`s, and the OpenAPI resource reads the spec the running app actually
generates. Only the unreachable-backend cases use a stub transport.
"""

import asyncio
import json

import httpx
import pytest

import beetdeck_mcp.tools as tools_pkg
from beetdeck_mcp.client import BeetdeckClient
from beetdeck_mcp.resources import (
    OPENAPI_PATH,
    OPENAPI_URI,
    RESOURCE_URIS,
    WORKFLOW_URI,
    read_openapi,
)
from beetdeck_mcp.server import create_server
from beetdeck_mcp.tools.raw import ALLOWED_PREFIX, TOOL_NAMES, build_tools
from tests.conftest import insert_album, insert_item


@pytest.fixture()
def tools(beetdeck_client):
    """The registered escape-hatch tool, bound to the in-process app."""
    return {tool.__name__: tool for tool in build_tools(beetdeck_client)}


@pytest.fixture()
def refusing_tools(mcp_config):
    """The tool bound to a transport that fails the test if it is used at all."""

    def handler(request):
        raise AssertionError(f"a rejected path must not be requested: {request.url}")

    client = BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))
    return {tool.__name__: tool for tool in build_tools(client)}


@pytest.fixture()
def dead_tools(mcp_config):
    """The tool bound to a backend that refuses every connection."""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))
    return {tool.__name__: tool for tool in build_tools(client)}


@pytest.fixture()
def resource_server(mcp_config, beetdeck_client, monkeypatch):
    """A server whose resources read through the in-process app, not the network."""
    monkeypatch.setitem(tools_pkg._CLIENTS, mcp_config, beetdeck_client)
    return create_server(mcp_config)


def read_resource(server, uri):
    """The single content block a resource read produces."""
    contents = list(asyncio.run(server.read_resource(uri)))
    assert len(contents) == 1
    return contents[0]


class TestBuildTools:
    def test_exposes_exactly_the_declared_tool_names(self, tools):
        assert tuple(tools) == TOOL_NAMES


# ---------------------------------------------------------------------------
# beetdeck_api_call — path policy
# ---------------------------------------------------------------------------


class TestPathPolicy:
    @pytest.mark.parametrize(
        "path",
        [
            "http://backend:5000/api/library",
            "https://example.org/api/library",
            "//example.org/api/library",
        ],
    )
    def test_rejects_an_absolute_url(self, refusing_tools, path):
        """The tool talks to the configured API, not to arbitrary hosts."""
        result = refusing_tools["beetdeck_api_call"](path)
        assert result["status"] == "failed"
        assert "relative" in result["error"]

    @pytest.mark.parametrize(
        "path",
        ["/apidoc/openapi.json", "/", "api/library", "/apis/library", "/health"],
    )
    def test_rejects_a_path_outside_the_api_prefix(self, refusing_tools, path):
        result = refusing_tools["beetdeck_api_call"](path)
        assert result["status"] == "failed"
        assert ALLOWED_PREFIX in result["error"]

    @pytest.mark.parametrize(
        "path",
        ["/api/../apidoc/openapi.json", "/api/album/../../etc/passwd", "/api/%2e%2e/apidoc"],
    )
    def test_rejects_a_traversal(self, refusing_tools, path):
        """Percent-encoded segments are decoded before the check, not after."""
        result = refusing_tools["beetdeck_api_call"](path)
        assert result["status"] == "failed"
        assert ".." in result["error"]

    def test_a_rejection_names_the_path_it_refused(self, refusing_tools):
        result = refusing_tools["beetdeck_api_call"]("/etc/passwd")
        assert result["path"] == "/etc/passwd"
        assert "status_code" not in result


# ---------------------------------------------------------------------------
# beetdeck_api_call — round trips
# ---------------------------------------------------------------------------


class TestApiCall:
    def test_forwards_a_get_and_returns_the_body_verbatim(self, tools, db_path):
        album_id = insert_album(db_path, album="Kind of Blue", albumartist="Miles Davis")
        insert_item(db_path, album_id, title="So What")

        result = tools["beetdeck_api_call"](f"/api/album/{album_id}")
        assert result["status"] == "ok"
        assert result["status_code"] == 200
        assert result["data"]["album"] == "Kind of Blue"
        # Verbatim: the typed tools compact and window, this one must not.
        assert [track["title"] for track in result["data"]["tracks"]] == ["So What"]

    def test_passes_a_query_string_through(self, tools, db_path):
        insert_album(db_path, album="Bitches Brew", albumartist="Miles Davis")

        result = tools["beetdeck_api_call"]("/api/search?q=Bitches")
        assert result["status"] == "ok"
        assert [album["album"] for album in result["data"]["albums"]] == ["Bitches Brew"]

    def test_a_list_body_is_returned_as_a_list(self, tools):
        result = tools["beetdeck_api_call"]("/api/library")
        assert result["status"] == "ok"
        assert isinstance(result["data"], list)

    def test_an_unknown_endpoint_fails_instead_of_raising(self, tools):
        result = tools["beetdeck_api_call"]("/api/no-such-endpoint")
        assert result["status"] == "failed"
        assert result["status_code"] == 404
        assert "data" not in result

    def test_a_missing_album_fails_with_the_backend_message(self, tools):
        result = tools["beetdeck_api_call"]("/api/album/999999")
        assert result["status"] == "failed"
        assert result["status_code"] == 404
        assert "not found" in result["error"].lower()

    def test_an_unreachable_backend_fails_with_no_status_code(self, dead_tools):
        result = dead_tools["beetdeck_api_call"]("/api/library")
        assert result["status"] == "failed"
        assert "status_code" not in result
        assert "connection refused" in result["error"]


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------


class TestOpenApiResource:
    def test_reads_the_live_spec(self, beetdeck_client):
        spec = json.loads(read_openapi(beetdeck_client))
        assert spec["openapi"].startswith("3.")
        assert "/api/library" in spec["paths"]

    def test_an_unreachable_backend_becomes_a_json_error_not_a_raise(self, mcp_config):
        """The SDK strips the reason off a raised error, so it is returned as content."""

        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        client = BeetdeckClient(mcp_config, transport=httpx.MockTransport(handler))
        payload = json.loads(read_openapi(client))
        assert OPENAPI_PATH in payload["error"]
        assert "connection refused" in payload["error"]


class TestResourceRegistration:
    def test_both_resources_are_listed(self, resource_server):
        listed = {
            str(resource.uri): resource
            for resource in asyncio.run(resource_server.list_resources())
        }
        assert set(RESOURCE_URIS) <= set(listed)
        assert listed[OPENAPI_URI].mime_type == "application/json"
        assert listed[WORKFLOW_URI].mime_type == "text/markdown"
        assert listed[WORKFLOW_URI].description

    def test_the_openapi_resource_is_readable(self, resource_server):
        content = read_resource(resource_server, OPENAPI_URI)
        assert content.mime_type == "application/json"
        assert "/api/library" in json.loads(content.content)["paths"]

    def test_the_workflow_guide_is_readable(self, resource_server):
        content = read_resource(resource_server, WORKFLOW_URI)
        assert content.mime_type == "text/markdown"
        text = content.content
        # The three things a tool description cannot carry on its own.
        assert "apply_change" in text
        assert "TTL only" in text
        assert "mb_release_id" in text

    def test_resources_are_registered_outside_register_all(self, mcp_config, beetdeck_client):
        """`register_all` is the tool registry; resources are wired in `server.py`."""
        from mcp.server import MCPServer

        bare = MCPServer(name="bare")
        tools_pkg.register_all(bare, mcp_config)
        assert asyncio.run(bare.list_resources()) == []
