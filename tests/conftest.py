"""Shared test fixtures for beetDeek tests."""

import sqlite3

import httpx
import pytest
from starlette.testclient import TestClient

import beetdeck.utils as beetdeck_utils
from beetdeck import create_app, state
from beetdeck_mcp.config import load_config
from beetdeck_mcp.server import create_http_app, create_server

BEETS_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    added REAL,
    artpath BLOB,
    albumartist TEXT,
    albumartist_sort TEXT,
    albumartist_credit TEXT,
    album TEXT,
    genre TEXT,
    genres TEXT,
    year INTEGER,
    month INTEGER,
    day INTEGER,
    disctotal INTEGER,
    comp INTEGER,
    mb_albumid TEXT,
    mb_albumartistid TEXT,
    albumtype TEXT,
    label TEXT,
    mb_releasegroupid TEXT,
    asin TEXT,
    catalognum TEXT,
    script TEXT,
    language TEXT,
    country TEXT,
    albumstatus TEXT,
    media TEXT,
    albumdisambig TEXT,
    original_year INTEGER,
    original_month INTEGER,
    original_day INTEGER
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    path BLOB,
    album_id INTEGER,
    title TEXT,
    artist TEXT,
    artist_sort TEXT,
    artist_credit TEXT,
    album TEXT,
    albumartist TEXT,
    genre TEXT,
    genres TEXT,
    year INTEGER,
    month INTEGER,
    day INTEGER,
    track INTEGER,
    tracktotal INTEGER,
    disc INTEGER,
    disctotal INTEGER,
    lyrics TEXT,
    comments TEXT,
    bpm INTEGER,
    length REAL,
    bitrate INTEGER,
    format TEXT,
    samplerate INTEGER,
    bitdepth INTEGER,
    channels INTEGER,
    mtime REAL,
    added REAL,
    mb_trackid TEXT,
    mb_albumid TEXT,
    mb_artistid TEXT,
    mb_albumartistid TEXT,
    comp INTEGER,
    mb_releasegroupid TEXT,
    original_year INTEGER,
    original_month INTEGER,
    original_day INTEGER
);

CREATE TABLE IF NOT EXISTS album_attributes (
    entity_id INTEGER REFERENCES albums(id),
    key TEXT,
    value TEXT,
    PRIMARY KEY (entity_id, key)
);

CREATE TABLE IF NOT EXISTS item_attributes (
    entity_id INTEGER REFERENCES items(id),
    key TEXT,
    value TEXT,
    PRIMARY KEY (entity_id, key)
);
"""


def seed_db(db_path: str) -> None:
    """Create beets schema tables in the given SQLite file."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(BEETS_SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


def insert_album(db_path: str, **kwargs) -> int:
    """Insert a test album row and return its id."""
    defaults = {
        "album": "Test Album",
        "albumartist": "Test Artist",
        "genre": "Rock",
        "year": 2020,
        "artpath": None,
    }
    defaults.update(kwargs)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO albums ({cols}) VALUES ({placeholders})",
            list(defaults.values()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def insert_item(db_path: str, album_id: int, **kwargs) -> int:
    """Insert a test item (track) row and return its id."""
    defaults = {
        "album_id": album_id,
        "title": "Test Track",
        "artist": "Test Artist",
        "path": b"/music/test/track.mp3",
        "length": 180.0,
        "track": 1,
        "format": "MP3",
    }
    defaults.update(kwargs)
    defaults["album_id"] = album_id
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO items ({cols}) VALUES ({placeholders})",
            list(defaults.values()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _reset_rescan_state():
    state.rescan_proc = None
    state.rescan_snapshot = None
    state.rescan_run_id = None
    state.rescan_log_path = None
    state.rescan_phase = "idle"
    state.rescan_result = None
    state.rescan_thread = None
    state.rescan_processed = 0
    state.rescan_total = None
    state.rescan_current_item = None


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset shared in-memory state before each test to prevent test pollution."""
    state.identify_tasks.clear()
    _reset_rescan_state()
    beetdeck_utils._beets_initialized = False
    yield
    state.identify_tasks.clear()
    _reset_rescan_state()
    beetdeck_utils._beets_initialized = False


@pytest.fixture()
def db_path(tmp_path):
    """A temporary SQLite file seeded with the beets schema."""
    path = tmp_path / "library.db"
    seed_db(str(path))
    return str(path)


@pytest.fixture()
def app(db_path, tmp_path):
    """Flask test app with a temporary library DB."""
    application = create_app(
        test_config={
            "LIBRARY_DB": db_path,
            "IMPORT_DIR": "/tmp/music_import",
            # Empty string: test paths are absolute; no library root resolution needed
            "LIBRARY_ROOT": "",
            "COVER_CACHE_DIR": str(tmp_path / "cover-cache"),
            "TESTING": True,
        }
    )
    return application


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()


# --- MCP server fixtures ----------------------------------------------------
#
# The MCP tools are exercised against the real Flask app in-process: an
# httpx.WSGITransport around the `app` fixture above, so the MCP↔REST contract
# is tested for real instead of against hand-written mocks. This is why
# everything in `beetdeck_mcp` is synchronous — an async httpx client cannot use
# a WSGI transport.

MCP_TEST_TOKEN = "test-token-primary"
MCP_TEST_TOKEN_ALT = "test-token-secondary"

MCP_TEST_ENV = {
    "BEETDECK_MCP_TOKEN": f"{MCP_TEST_TOKEN},{MCP_TEST_TOKEN_ALT}",
    "MUSICBRAINZ_USER_AGENT": "beetdeck-tests/0.0 (tests@example.invalid)",
    "BEETDECK_API_URL": "http://backend.test",
}


@pytest.fixture()
def mcp_config():
    """Validated MCP config for tests, independent of the real environment."""
    return load_config(MCP_TEST_ENV)


@pytest.fixture()
def api_transport(app):
    """httpx transport dispatching straight into the Flask app, no sockets."""
    return httpx.WSGITransport(app=app)


@pytest.fixture()
def api_client(mcp_config, api_transport):
    """Blocking httpx client bound to the in-process Flask app."""
    with httpx.Client(transport=api_transport, base_url=mcp_config.api_url) as http_client:
        yield http_client


@pytest.fixture()
def mcp_server(mcp_config):
    """The MCP server instance with every tool registered."""
    return create_server(mcp_config)


@pytest.fixture()
def mcp_http_client(mcp_config, mcp_server):
    """Starlette test client over the streamable-http app (lifespan running)."""
    with TestClient(create_http_app(mcp_config, server=mcp_server)) as http_client:
        yield http_client
