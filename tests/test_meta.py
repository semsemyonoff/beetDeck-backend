"""Tests for the meta blueprint (GET /api/version)."""

import beets

from beetdeck.api import APP_VERSION
from beetdeck.routes.meta import _release_url


def test_version_returns_200(client):
    resp = client.get("/api/version")
    assert resp.status_code == 200


def test_version_reports_both_versions(client):
    """Body carries the running versions plus their release-notes URLs."""
    body = client.get("/api/version").get_json()
    assert body["beetdeck"] == APP_VERSION
    assert body["beets"] == beets.__version__
    assert set(body) == {"beetdeck", "beets", "beetdeck_url", "beets_url"}


def test_version_beets_matches_installed(client):
    """The beets version is read live from the installed package."""
    body = client.get("/api/version").get_json()
    assert body["beets"] == beets.__version__
    # Sanity: a real semantic-ish version string, not empty.
    assert body["beets"]
    assert body["beets"][0].isdigit()


def test_version_beets_url_points_at_the_release_tag(client):
    """The beets URL is the GitHub release-notes page for the installed version."""
    body = client.get("/api/version").get_json()
    assert body["beets_url"] == (
        f"https://github.com/beetbox/beets/releases/tag/v{beets.__version__}"
    )


def test_release_url_builds_tag_url():
    assert (
        _release_url("https://github.com/o/r/releases/tag", "1.2.3")
        == "https://github.com/o/r/releases/tag/v1.2.3"
    )


def test_release_url_is_none_for_dev_sentinel_and_empty():
    """No release exists for the 0.0.0 dev default or a missing version."""
    assert _release_url("https://github.com/o/r/releases/tag", "0.0.0") is None
    assert _release_url("https://github.com/o/r/releases/tag", "") is None
