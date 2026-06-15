"""Tests for the meta blueprint (GET /api/version)."""

import beets

from beetdeck.api import APP_VERSION


def test_version_returns_200(client):
    resp = client.get("/api/version")
    assert resp.status_code == 200


def test_version_reports_both_versions(client):
    """Body is the flat {beetdeck, beets} shape with the running versions."""
    body = client.get("/api/version").get_json()
    assert body == {"beetdeck": APP_VERSION, "beets": beets.__version__}


def test_version_beets_matches_installed(client):
    """The beets version is read live from the installed package."""
    body = client.get("/api/version").get_json()
    assert body["beets"] == beets.__version__
    # Sanity: a real semantic-ish version string, not empty.
    assert body["beets"]
    assert body["beets"][0].isdigit()
