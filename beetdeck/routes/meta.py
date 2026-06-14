"""Meta routes: app-level metadata not tied to library contents.

``GET /api/version`` reports the running beetDeck and beets versions so the SPA
can surface them (e.g. an "About" box / footer). Versions are static for the
lifetime of the process, so this is a small standalone endpoint the SPA fetches
once — deliberately kept out of ``/api/library`` (album data) to avoid resending
static metadata on every list call.
"""

import beets
from flask import Blueprint, jsonify
from spectree import Response

from beetdeck.api import APP_VERSION, api
from beetdeck.schemas.meta import VersionResponse

bp = Blueprint("meta", __name__)


@bp.route("/api/version")
@api.validate(
    resp=Response(HTTP_200=VersionResponse),
    tags=["meta"],
)
def version():
    """Return the running beetDeck and beets versions."""
    return jsonify({"beetdeck": APP_VERSION, "beets": beets.__version__})
