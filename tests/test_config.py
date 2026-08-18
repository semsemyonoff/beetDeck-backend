"""App-factory configuration — the artwork keys and the app-scoped clients.

These tests build their own apps rather than using the `app` fixture: the point
is what `create_app` reads from the environment, and the fixture overrides it.
"""

import os
import tempfile

import httpx
import pytest
from flask import current_app

from beetdeck import DEFAULT_ARTWORK_USER_AGENT, create_app

ARTWORK_ENV_KEYS = (
    "BEETS_ARTWORK_STORAGE_DIR",
    "BEETS_ARTWORK_CACHE_DIR",
    "BEETS_ARTWORK_CACHE_MAX_BYTES",
    "BEETS_ARTWORK_CACHE_TTL",
    "BEETS_ARTWORK_META_TTL",
    "BEETS_ARTWORK_USER_AGENT",
)

CAA_PAYLOAD = {
    "release": "https://musicbrainz.org/release/mbid",
    "images": [
        {
            "id": 1234567890,
            "types": ["Front"],
            "front": True,
            "back": False,
            "approved": True,
            "comment": "",
            "image": "https://coverartarchive.org/release/mbid/1234567890.jpg",
            "thumbnails": {"250": "https://caa/250.jpg", "500": "https://caa/500.jpg"},
        }
    ],
}


@pytest.fixture(autouse=True)
def clean_artwork_env(monkeypatch):
    """No BEETS_ARTWORK_* leaks in from the container's own environment."""
    for key in ARTWORK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _app(**overrides):
    """An app with the artwork roots pinned, plus any config override."""
    return create_app(test_config={"TESTING": True, **overrides})


# --- defaults ---------------------------------------------------------------


def test_artwork_defaults_when_no_env_is_set(tmp_path):
    app = _app(ARTWORK_CACHE_DIR=str(tmp_path / "cache"))

    assert app.config["ARTWORK_STORAGE_DIR"] is None
    assert app.config["ARTWORK_CACHE_MAX_BYTES"] == 536870912
    assert app.config["ARTWORK_CACHE_TTL"] == 604800
    assert app.config["ARTWORK_META_TTL"] == 86400
    assert app.config["ARTWORK_USER_AGENT"] == DEFAULT_ARTWORK_USER_AGENT
    assert app.config["ARTWORK_CAA_TRANSPORT"] is None


def test_cache_dir_defaults_under_the_system_temp_dir():
    app = create_app(test_config={"TESTING": True})

    assert app.config["ARTWORK_CACHE_DIR"] == os.path.join(
        tempfile.gettempdir(), "beetdeck-artwork-cache"
    )


def test_storage_is_off_by_default_so_the_store_writes_to_the_cache(tmp_path):
    app = _app(ARTWORK_CACHE_DIR=str(tmp_path / "cache"))
    store = app.extensions["beetdeck_artwork_store"]

    assert store.storage_enabled is False
    assert store.write_root == (str(tmp_path / "cache"), "cache")


# --- env overrides ----------------------------------------------------------


def test_env_overrides_are_applied(monkeypatch, tmp_path):
    monkeypatch.setenv("BEETS_ARTWORK_STORAGE_DIR", str(tmp_path / "mirror"))
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_MAX_BYTES", "1024")
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_TTL", "60")
    monkeypatch.setenv("BEETS_ARTWORK_META_TTL", "30")
    monkeypatch.setenv("BEETS_ARTWORK_USER_AGENT", "beetDeck-tests/0.0 ( tests@example.invalid )")

    app = create_app(test_config={"TESTING": True})

    assert app.config["ARTWORK_STORAGE_DIR"] == str(tmp_path / "mirror")
    assert app.config["ARTWORK_CACHE_DIR"] == str(tmp_path / "cache")
    assert app.config["ARTWORK_CACHE_MAX_BYTES"] == 1024
    assert app.config["ARTWORK_CACHE_TTL"] == 60
    assert app.config["ARTWORK_META_TTL"] == 30
    assert app.config["ARTWORK_USER_AGENT"] == "beetDeck-tests/0.0 ( tests@example.invalid )"


def test_env_values_reach_the_store(monkeypatch, tmp_path):
    monkeypatch.setenv("BEETS_ARTWORK_STORAGE_DIR", str(tmp_path / "mirror"))
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_MAX_BYTES", "4096")
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_TTL", "60")
    monkeypatch.setenv("BEETS_ARTWORK_META_TTL", "30")

    store = create_app(test_config={"TESTING": True}).extensions["beetdeck_artwork_store"]

    assert store.storage_enabled is True
    assert store.write_root == (str(tmp_path / "mirror"), "storage")
    assert (store.meta_ttl, store.cache_ttl, store.max_bytes) == (30, 60, 4096)


def test_env_user_agent_reaches_the_caa_client(monkeypatch, tmp_path):
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BEETS_ARTWORK_USER_AGENT", "beetDeck-tests/0.0 ( tests@example.invalid )")

    client = create_app(test_config={"TESTING": True}).extensions["beetdeck_caa"]

    assert client.user_agent == "beetDeck-tests/0.0 ( tests@example.invalid )"


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_storage_dir_leaves_storage_disabled(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("BEETS_ARTWORK_STORAGE_DIR", raw)
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_DIR", str(tmp_path / "cache"))

    app = create_app(test_config={"TESTING": True})

    assert app.config["ARTWORK_STORAGE_DIR"] is None
    assert app.extensions["beetdeck_artwork_store"].storage_enabled is False


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_cache_dir_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_DIR", raw)

    app = create_app(test_config={"TESTING": True})

    assert app.config["ARTWORK_CACHE_DIR"] == os.path.join(
        tempfile.gettempdir(), "beetdeck-artwork-cache"
    )


def test_test_config_wins_over_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("BEETS_ARTWORK_STORAGE_DIR", str(tmp_path / "from-env"))

    app = _app(
        ARTWORK_STORAGE_DIR=str(tmp_path / "from-config"),
        ARTWORK_CACHE_DIR=str(tmp_path / "cache"),
    )

    assert app.config["ARTWORK_STORAGE_DIR"] == str(tmp_path / "from-config")
    assert app.extensions["beetdeck_artwork_store"].write_root == (
        str(tmp_path / "from-config"),
        "storage",
    )


# --- defensive numeric parsing ----------------------------------------------


@pytest.mark.parametrize(
    ("env_key", "config_key", "default"),
    [
        ("BEETS_ARTWORK_CACHE_MAX_BYTES", "ARTWORK_CACHE_MAX_BYTES", 536870912),
        ("BEETS_ARTWORK_CACHE_TTL", "ARTWORK_CACHE_TTL", 604800),
        ("BEETS_ARTWORK_META_TTL", "ARTWORK_META_TTL", 86400),
    ],
)
def test_garbage_numeric_env_falls_back_to_the_default(
    monkeypatch, tmp_path, caplog, env_key, config_key, default
):
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv(env_key, "512MB")

    with caplog.at_level("WARNING", logger="beetdeck"):
        app = create_app(test_config={"TESTING": True})

    assert app.config[config_key] == default
    assert any(env_key in record.getMessage() for record in caplog.records)


def test_negative_and_zero_numeric_env_are_passed_through(monkeypatch, tmp_path):
    """`0` is a real setting — the store reads it as "that half of the sweep off"."""
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_TTL", "0")
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_MAX_BYTES", "-1")

    app = create_app(test_config={"TESTING": True})

    assert app.config["ARTWORK_CACHE_TTL"] == 0
    assert app.config["ARTWORK_CACHE_MAX_BYTES"] == -1


def test_blank_numeric_env_uses_the_default_without_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("BEETS_ARTWORK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BEETS_ARTWORK_META_TTL", "  ")

    with caplog.at_level("WARNING", logger="beetdeck"):
        app = create_app(test_config={"TESTING": True})

    assert app.config["ARTWORK_META_TTL"] == 86400
    assert not [r for r in caplog.records if "BEETS_ARTWORK_META_TTL" in str(r.msg)]


# --- the app-scoped CAA client ----------------------------------------------


def test_injected_transport_reaches_the_client(tmp_path):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=CAA_PAYLOAD)

    app = _app(
        ARTWORK_CACHE_DIR=str(tmp_path / "cache"),
        ARTWORK_CAA_TRANSPORT=httpx.MockTransport(handler),
    )

    result = app.extensions["beetdeck_caa"].fetch_release("mbid")

    assert seen == ["https://coverartarchive.org/release/mbid"]
    assert result.ok
    assert [image["image_id"] for image in result.images] == ["1234567890"]


def test_two_requests_share_one_client_instance(tmp_path):
    """A per-request client would hand each thread a fresh, useless RateGate."""
    app = _app(ARTWORK_CACHE_DIR=str(tmp_path / "cache"))
    seen = []

    @app.route("/_test/artwork-extensions")
    def _artwork_extensions():
        seen.append(
            (
                current_app.extensions["beetdeck_caa"],
                current_app.extensions["beetdeck_artwork_store"],
            )
        )
        return {"ok": True}

    client = app.test_client()
    assert client.get("/_test/artwork-extensions").status_code == 200
    assert client.get("/_test/artwork-extensions").status_code == 200

    (first_caa, first_store), (second_caa, second_store) = seen
    assert first_caa is second_caa is app.extensions["beetdeck_caa"]
    assert first_store is second_store is app.extensions["beetdeck_artwork_store"]
    # Same instance means one gate — the object that actually does the spacing.
    assert first_caa._gate is second_caa._gate


def test_the_client_still_works_after_a_request(tmp_path):
    """Regression: closing it in a teardown hook would break the second load."""
    app = _app(
        ARTWORK_CACHE_DIR=str(tmp_path / "cache"),
        ARTWORK_CAA_TRANSPORT=httpx.MockTransport(lambda request: httpx.Response(404)),
    )

    @app.route("/_test/ping")
    def _ping():
        return {"ok": True}

    client = app.test_client()
    client.get("/_test/ping")
    client.get("/_test/ping")

    result = app.extensions["beetdeck_caa"].fetch_release("mbid")

    assert result.not_found is True
    assert result.ok is True


def test_the_client_transport_is_closed_at_exit(monkeypatch, tmp_path):
    registered = []
    monkeypatch.setattr("beetdeck.atexit.register", lambda fn: registered.append(fn) or fn)

    app = _app(ARTWORK_CACHE_DIR=str(tmp_path / "cache"))

    assert app.extensions["beetdeck_caa"].close in registered


def test_each_app_gets_its_own_client_and_store(tmp_path):
    first = _app(ARTWORK_CACHE_DIR=str(tmp_path / "cache"))
    second = _app(ARTWORK_CACHE_DIR=str(tmp_path / "cache"))

    assert first.extensions["beetdeck_caa"] is not second.extensions["beetdeck_caa"]
    assert (
        first.extensions["beetdeck_artwork_store"]
        is not second.extensions["beetdeck_artwork_store"]
    )
