"""Pydantic models for the ``meta`` blueprint's OpenAPI spec.

Covers ``GET /api/version``.

Like the other response models these are validated against the handler output
(spectree's default) but pass through unchanged (``force_resp_serialize`` is
``False``); the fields are plain required strings because the handler always
emits both versions.
"""

from pydantic import BaseModel, Field


class VersionResponse(BaseModel):
    """``GET /api/version`` — running beetDeck and beets versions."""

    beetdeck: str = Field(description="beetDeck release version (OpenAPI info.version).")
    beets: str = Field(description="Installed beets version (beets.__version__).")
    beetdeck_url: str | None = Field(
        default=None,
        description=(
            "GitHub release-notes URL for the beetDeck version, or null when the "
            "version is the dev sentinel (0.0.0) with no corresponding release."
        ),
    )
    beets_url: str | None = Field(
        default=None,
        description="GitHub release-notes URL for the installed beets version.",
    )
