"""Tests for the spectree OpenAPI integration (src/api.py).

Covers:
- the served spec route (`/apidoc/openapi.json`) and Scalar viewer
  (`/apidoc/scalar/`) — paths discovered in-container (version-dependent),
- the 422 validation-error contract being configured on the singleton,
- a completeness/isolation guard: every `@api.validate`-decorated view appears
  in the generated spec, across two independent `create_app()` builds.
"""

import re

import pytest

from src import create_app
from src.api import api

# Routes mounted by api.register(app); discovered via app.url_map in-container.
SPEC_URL = "/apidoc/openapi.json"
SCALAR_URL = "/apidoc/scalar/"


def _spec_path(rule: str) -> str:
    """Convert a Flask URL rule to the OpenAPI path format.

    `<int:id>` / `<name>` → `{id}` / `{name}` — matches spectree's conversion.
    """
    return re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", rule)


def _decorated_specs(app) -> set[tuple[str, str]]:
    """Return the set of (spec_path, method) pairs for every view decorated
    with this app's `@api.validate` (spectree marks them with `_decorator`)."""
    pairs: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        view = app.view_functions.get(rule.endpoint)
        if getattr(view, "_decorator", None) is not api:
            continue
        path = _spec_path(rule.rule)
        for method in rule.methods or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            pairs.add((path, method.lower()))
    return pairs


def test_spec_route_returns_valid_json(client):
    """The OpenAPI spec route returns 200 and a parseable OpenAPI document."""
    resp = client.get(SPEC_URL)
    assert resp.status_code == 200
    spec = resp.get_json()
    assert isinstance(spec, dict)
    assert "openapi" in spec
    assert "paths" in spec
    assert spec["info"]["title"] == "beetDeck API"


def test_scalar_page_renders(client):
    """The Scalar documentation viewer renders (HTML, 200)."""
    resp = client.get(SCALAR_URL)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True).lower()
    assert "scalar" in body or "<html" in body


def test_validation_error_status_is_422():
    """The singleton is configured to emit 422 on validation errors (not 400)."""
    assert api.validation_error_status == 422


@pytest.mark.parametrize("_build", [0, 1])
def test_every_decorated_view_appears_in_spec(db_path, _build):
    """Completeness/isolation guard: build the app independently and assert each
    `@api.validate`-decorated view is present in the generated spec.

    Parametrized to build `create_app()` twice — catches spec leakage or lazy
    blueprint-import ordering issues across fresh app instances. `api.spec`
    memoizes its result on first access, so we drop the cache here to force a
    genuine re-generation against this build (otherwise the second run would
    silently re-read the first run's cached spec and assert nothing new).
    """
    if hasattr(api, "_spec"):
        del api._spec

    app = create_app(
        test_config={"LIBRARY_DB": db_path, "LIBRARY_ROOT": "", "TESTING": True}
    )
    decorated = _decorated_specs(app)

    spec = app.test_client().get(SPEC_URL).get_json()
    spec_paths = spec.get("paths", {})

    missing = [
        (path, method)
        for (path, method) in decorated
        if method not in spec_paths.get(path, {})
    ]
    assert not missing, f"decorated views missing from spec: {missing}"
