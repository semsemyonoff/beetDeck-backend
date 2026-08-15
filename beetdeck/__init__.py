"""beetDeek Flask application factory."""

import atexit
import json
import logging
import os
import tempfile

from flask import Flask

#: The identity CAA/archive.org sees. Not a refuse-to-start invariant — unlike
#: MusicBrainz `ws/2`, CAA serves anonymous clients (see `beetdeck.coverart`).
DEFAULT_ARTWORK_USER_AGENT = "beetDeck/0.3 ( https://git.horn/beetDeck )"


def _int_env(name, default):
    """An integer env var, falling back to `default` on anything unparseable.

    A typo in a cache budget must not take the whole app down at construction:
    the gallery degrades to the default and says so in the log.
    """
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        # `beetdeck.utils` imports this package, so its `log` cannot be imported
        # here — this is the same logger by name.
        logging.getLogger("beetdeck").warning(
            "%s=%r is not an integer, falling back to %s", name, raw, default
        )
        return default


def create_app(test_config=None):
    """Create and configure the Flask application.

    Args:
        test_config: Optional dict of config overrides (e.g. LIBRARY_DB for tests).
    """
    app = Flask(__name__, static_folder="static", template_folder="templates")

    app.config["LIBRARY_DB"] = os.environ.get("BEETS_LIBRARY_DB", "/data/beets/library.db")
    app.config["IMPORT_DIR"] = os.environ.get("BEETS_IMPORT_DIR", "/music")
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB upload limit
    # LIBRARY_ROOT: base path for resolving the relative item/album paths beets
    # stores in the DB. Defaults to IMPORT_DIR — in this app's deployment convention
    # (beets config `copy: no, move: no`) the library directory always matches
    # the import directory. Override with BEETS_LIBRARY_ROOT env for exotic setups.
    app.config["LIBRARY_ROOT"] = (
        os.environ.get("BEETS_LIBRARY_ROOT", "") or app.config["IMPORT_DIR"]
    )
    # COVER_CACHE_DIR: where generated WebP cover thumbnails are cached. Defaults
    # to a subdir of the system temp dir (always writable); override with
    # BEETS_COVER_CACHE_DIR to point at a persistent volume.
    app.config["COVER_CACHE_DIR"] = os.environ.get("BEETS_COVER_CACHE_DIR", "") or os.path.join(
        tempfile.gettempdir(), "beetdeck-cover-cache"
    )

    # ARTWORK_STORAGE_DIR is opt-in: unset or empty means there is no permanent
    # mirror and everything the gallery touches lands in the bounded cache
    # instead. Both roots share one on-disk layout, so flipping the mode never
    # orphans what is already written.
    app.config["ARTWORK_STORAGE_DIR"] = (
        os.environ.get("BEETS_ARTWORK_STORAGE_DIR", "").strip() or None
    )
    app.config["ARTWORK_CACHE_DIR"] = os.environ.get(
        "BEETS_ARTWORK_CACHE_DIR", ""
    ).strip() or os.path.join(tempfile.gettempdir(), "beetdeck-artwork-cache")
    app.config["ARTWORK_CACHE_MAX_BYTES"] = _int_env(
        "BEETS_ARTWORK_CACHE_MAX_BYTES", 512 * 1024 * 1024
    )
    app.config["ARTWORK_CACHE_TTL"] = _int_env("BEETS_ARTWORK_CACHE_TTL", 7 * 24 * 60 * 60)
    app.config["ARTWORK_META_TTL"] = _int_env("BEETS_ARTWORK_META_TTL", 24 * 60 * 60)
    app.config["ARTWORK_USER_AGENT"] = (
        os.environ.get("BEETS_ARTWORK_USER_AGENT", "").strip() or DEFAULT_ARTWORK_USER_AGENT
    )
    # Test-only injection seam: an httpx transport handed to the CAA client, so
    # tests drive real request construction through create_app(test_config=…)
    # instead of monkeypatching a module global.
    app.config["ARTWORK_CAA_TRANSPORT"] = None

    if test_config:
        app.config.update(test_config)

    from .artwork_store import ArtworkStore
    from .coverart import CaaClient

    # One store and one client per **app**, never per request. A factory called
    # per request hands every thread a fresh RateGate with a clean timestamp, so
    # concurrent gallery loads all reach CAA at once and the spacing contract
    # silently does nothing.
    app.extensions["beetdeck_artwork_store"] = ArtworkStore(
        app.config["ARTWORK_STORAGE_DIR"],
        app.config["ARTWORK_CACHE_DIR"],
        meta_ttl=app.config["ARTWORK_META_TTL"],
        cache_ttl=app.config["ARTWORK_CACHE_TTL"],
        max_bytes=app.config["ARTWORK_CACHE_MAX_BYTES"],
    )
    caa_client = CaaClient(
        user_agent=app.config["ARTWORK_USER_AGENT"],
        transport=app.config["ARTWORK_CAA_TRANSPORT"],
    )
    app.extensions["beetdeck_caa"] = caa_client
    # atexit, not teardown_appcontext/teardown_request: Flask has no app-shutdown
    # hook and both of those fire **per request**, so the second gallery load
    # would raise "Cannot send a request, as the client has been closed".
    atexit.register(caa_client.close)

    from .routes.albums import bp as albums_bp
    from .routes.bpm import bp as bpm_bp
    from .routes.cover import bp as cover_bp
    from .routes.genres import bp as genres_bp
    from .routes.identify import bp as identify_bp
    from .routes.items import bp as items_bp
    from .routes.library import bp as library_bp
    from .routes.lyrics import bp as lyrics_bp
    from .routes.meta import bp as meta_bp
    from .routes.scan import bp as scan_bp

    app.register_blueprint(library_bp)
    app.register_blueprint(albums_bp)
    app.register_blueprint(bpm_bp)
    app.register_blueprint(cover_bp)
    app.register_blueprint(genres_bp)
    app.register_blueprint(identify_bp)
    app.register_blueprint(items_bp)
    app.register_blueprint(lyrics_bp)
    app.register_blueprint(meta_bp)
    app.register_blueprint(scan_bp)

    # Mount the OpenAPI spec (/apidoc/openapi.json) and Scalar viewer
    # (/apidoc/scalar) after all blueprints so spectree sees every decorated view.
    from .api import api

    api.register(app)

    @app.context_processor
    def _inject_vite_assets():
        manifest_path = os.path.join(app.static_folder, "dist", ".vite", "manifest.json")
        entry = None
        css = []
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
            index_entry = manifest.get("index.html", {})
            entry = index_entry.get("file")
            css = index_entry.get("css", []) or []
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return {"vite_entry": entry, "vite_css": css}

    return app
