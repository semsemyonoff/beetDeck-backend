"""beetDeek Flask application factory."""

import json
import os

from flask import Flask


def create_app(test_config=None):
    """Create and configure the Flask application.

    Args:
        test_config: Optional dict of config overrides (e.g. LIBRARY_DB for tests).
    """
    app = Flask(__name__, static_folder="static", template_folder="templates")

    app.config["LIBRARY_DB"] = os.environ.get("BEETS_LIBRARY_DB", "/data/beets/library.db")
    app.config["IMPORT_DIR"] = os.environ.get("BEETS_IMPORT_DIR", "/music")
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB upload limit
    # LIBRARY_ROOT: base path for resolving relative item/album paths stored by
    # beets 2.10.0. Defaults to IMPORT_DIR — in this app's deployment convention
    # (beets config `copy: no, move: no`) the library directory always matches
    # the import directory. Override with BEETS_LIBRARY_ROOT env for exotic setups.
    app.config["LIBRARY_ROOT"] = (
        os.environ.get("BEETS_LIBRARY_ROOT", "") or app.config["IMPORT_DIR"]
    )

    if test_config:
        app.config.update(test_config)

    from .routes.albums import bp as albums_bp
    from .routes.cover import bp as cover_bp
    from .routes.genres import bp as genres_bp
    from .routes.identify import bp as identify_bp
    from .routes.items import bp as items_bp
    from .routes.library import bp as library_bp
    from .routes.lyrics import bp as lyrics_bp
    from .routes.scan import bp as scan_bp

    app.register_blueprint(library_bp)
    app.register_blueprint(albums_bp)
    app.register_blueprint(cover_bp)
    app.register_blueprint(genres_bp)
    app.register_blueprint(identify_bp)
    app.register_blueprint(items_bp)
    app.register_blueprint(lyrics_bp)
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
