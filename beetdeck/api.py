"""OpenAPI / spectree integration for the beetDeck backend.

A single module-level :class:`SpecTree` instance (``api``) is the source of truth
for the generated OpenAPI spec and the served documentation UI. Route modules
import ``api`` from here and decorate their handlers with ``@api.validate(...)``;
``create_app()`` calls ``api.register(app)`` once, after all blueprints are
registered.

Served routes (mounted by ``api.register``):
- ``GET /apidoc/openapi.json`` — the OpenAPI spec (JSON)
- ``GET /apidoc/scalar/`` — the Scalar documentation viewer (trailing slash; the
  bare ``/apidoc/scalar`` 308-redirects to it)

Notes:
- ``validation_error_status=422``: invalid request bodies/queries raise pydantic
  errors that spectree converts into an HTTP 422 response. This is set explicitly
  so the 422 contract holds regardless of backend defaults.
- Response validation is **active** (spectree's default — handlers are decorated
  without ``skip_validation``), but ``force_resp_serialize`` is left ``False``, so
  a handler's already-serialized ``jsonify(...)`` body is validated against its
  ``resp=`` model and then passed through **byte-for-byte** (never re-serialized).
  Response models therefore carry all-optional/lenient fields so real — sometimes
  partial — payloads validate unchanged; a non-conforming body would surface as a
  500. ``resp=`` also feeds the spec.

Validation-error contract (Task 11):
- Every invalid request — across every blueprint — returns **HTTP 422** with the
  uniform :class:`~beetdeck.schemas.common.ErrorResponse` body::

      {"error": "Validation failed",
       "detail": [{"loc": [...], "msg": "...", "type": "..."}, ...]}

  ``error`` is a short machine-readable message; ``detail`` carries one entry per
  offending field (mirroring pydantic's error format). spectree's default emits a
  bare JSON *list* of pydantic errors — the ``_render_validation_error`` hook below
  rewrites it into this object so the SPA has a single shape to parse.
- ``validation_error_model=ErrorResponse`` documents that same shape as the 422
  response of every validated endpoint in the OpenAPI spec.
"""

import os
from typing import Any

from pydantic import ValidationError
from spectree import SpecTree

from beetdeck.schemas.common import ErrorResponse, ValidationErrorDetail


def _render_validation_error(
    req: Any,
    resp: Any,
    req_validation_error: ValidationError | None,
    instance: Any,
    model_adapter: Any = None,
) -> None:
    """spectree ``before`` hook: rewrite the 422 body into the uniform shape.

    spectree 3 passes ``model_adapter`` as a fifth positional argument, 2.x does
    not; the default keeps the hook callable by both. A required fifth parameter
    breaks 2.x, a missing one makes every validated route a 500 under 3.x.

    spectree builds the validation-error response as ``jsonify(errors)`` — a bare
    JSON *list* of pydantic error dicts — before invoking this hook. When a
    validation error occurred we replace that body in place with the documented
    :class:`~beetdeck.schemas.common.ErrorResponse` object so every blueprint returns
    one stable shape. Non-error calls (``req_validation_error is None``) are
    left untouched.
    """
    if req_validation_error is None or resp is None:
        return
    detail = [
        ValidationErrorDetail(
            loc=list(err.get("loc", ())),
            msg=err.get("msg", ""),
            type=err.get("type", ""),
        )
        for err in req_validation_error.errors(include_context=False)
    ]
    body = ErrorResponse(error="Validation failed", detail=detail)
    resp.set_data(body.model_dump_json())
    resp.content_type = "application/json"


#: Product release version, reported as the OpenAPI ``info.version``. Baked into
#: the production image at build time (``APP_VERSION`` build-arg → env, wired from
#: the release tag by the deploy repo's ``build.sh``); defaults to ``0.0.0`` for
#: dev runs and plain ``docker build`` without the build-arg.
APP_VERSION = os.environ.get("APP_VERSION", "0.0.0")

#: Single SpecTree singleton shared across all route modules.
api = SpecTree(
    "flask",
    title="beetDeck API",
    version=APP_VERSION,
    description="Internal OpenAPI spec for the beetDeck backend.",
    validation_error_status=422,
    validation_error_model=ErrorResponse,
    before=_render_validation_error,
)
