"""Pydantic request/response models for the beetDeck OpenAPI spec.

One module per blueprint, plus this shared ``common`` module for the error
contract and reusable nested types. Route modules import the models they need
and reference them in ``@api.validate(...)`` decorators (see ``beetdeck/api.py``).
"""

from beetdeck.schemas.common import (
    AlbumSummary,
    ArtistGroup,
    ErrorResponse,
    ValidationErrorDetail,
)

__all__ = [
    "AlbumSummary",
    "ArtistGroup",
    "ErrorResponse",
    "ValidationErrorDetail",
]
