"""OpenAPI / spectree integration for the beetDeck backend.

A single module-level :class:`SpecTree` instance (``api``) is the source of truth
for the generated OpenAPI spec and the served documentation UI. Route modules
import ``api`` from here and decorate their handlers with ``@api.validate(...)``;
``create_app()`` calls ``api.register(app)`` once, after all blueprints are
registered.

Served routes (mounted by ``api.register``):
- ``GET /apidoc/openapi.json`` — the OpenAPI spec (JSON)
- ``GET /apidoc/scalar`` — the Scalar documentation viewer

Notes:
- ``validation_error_status=422``: invalid request bodies/queries raise pydantic
  errors that spectree converts into an HTTP 422 response. This is set explicitly
  so the 422 contract holds regardless of backend defaults.
- Response validation stays disabled (``mode="normal"``). ``resp=`` on handlers
  only feeds the spec; handlers keep returning their existing raw ``jsonify(...)``
  bodies untouched.
"""

from spectree import SpecTree

#: Single SpecTree singleton shared across all route modules.
api = SpecTree(
    "flask",
    title="beetDeck API",
    version="0.0.0",
    description="Internal OpenAPI spec for the beetDeck backend.",
    validation_error_status=422,
)
