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

#: GitHub "releases/tag" bases for the two versioned components. A concrete
#: release-notes URL is ``<base>/v<version>`` (both projects tag as ``vX.Y.Z``).
BEETDECK_RELEASES = "https://github.com/semsemyonoff/beetDeck/releases/tag"
BEETS_RELEASES = "https://github.com/beetbox/beets/releases/tag"


def _release_url(base: str, version: str) -> str | None:
    """Build the GitHub release-notes URL for ``version`` under ``base``.

    Returns ``None`` for the dev sentinel (``0.0.0``, ``APP_VERSION``'s default
    outside a tagged build) or a missing version, since no such release exists —
    the SPA renders the number as plain text rather than a broken link.
    """
    if not version or version == "0.0.0":
        return None
    return f"{base}/v{version}"


@bp.route("/api/version")
@api.validate(
    resp=Response(HTTP_200=VersionResponse),
    tags=["meta"],
)
def version():
    """Return the running beetDeck and beets versions with release-notes links."""
    return jsonify(
        {
            "beetdeck": APP_VERSION,
            "beets": beets.__version__,
            "beetdeck_url": _release_url(BEETDECK_RELEASES, APP_VERSION),
            "beets_url": _release_url(BEETS_RELEASES, beets.__version__),
        }
    )
