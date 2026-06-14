"""Pydantic models for the ``library`` blueprint's OpenAPI spec.

Covers ``GET /api/library``, ``GET /api/search`` and ``GET /api/artist``.

Query models keep their single parameter **optional** (default empty string) so
missing/empty queries preserve the existing behavior (``search`` → empty result
set; ``artist`` → in-handler ``400 Missing name``) rather than turning into a
spectree 422. Marking them required would break empty-search calls from the SPA.

Response models document the success bodies only — response validation stays OFF
(``resp=`` feeds the spec), so handlers keep returning their raw ``jsonify(...)``
output. ``GET /api/library`` returns a **bare top-level array**, modeled with a
``RootModel`` rather than an object wrapper.
"""

from pydantic import BaseModel, ConfigDict, Field, RootModel

from src.schemas.common import AlbumSummary, ArtistGroup


class SearchQuery(BaseModel):
    """Query params for ``GET /api/search``.

    ``q`` is optional; empty/whitespace is handled in the handler and returns an
    empty result set (not an error). ``extra="forbid"`` makes an unknown query
    param a 422 — the one intentional stricter-validation behavior change.
    """

    model_config = ConfigDict(extra="forbid")

    q: str = Field(default="", description="Search term; empty → empty result set.")


class ArtistQuery(BaseModel):
    """Query params for ``GET /api/artist``.

    ``name`` is optional; the handler still returns ``400 Missing name`` when it
    is empty (behavior preserved). ``extra="forbid"`` makes an unknown query
    param a 422.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        default="", description="Album artist name; empty → 400 Missing name."
    )


class LibraryResponse(RootModel[list[ArtistGroup]]):
    """``GET /api/library`` — a bare top-level array of artist groups."""


class SearchAlbum(BaseModel):
    """An album hit in ``GET /api/search`` results."""

    id: int = Field(description="Album id.")
    album: str | None = Field(default=None, description="Album title.")
    albumartist: str | None = Field(default=None, description="Album artist.")
    year: int | None = Field(default=None, description="Original or release year.")
    has_cover: bool = Field(description="Whether cover art is available.")


class SearchTrack(BaseModel):
    """A track hit in ``GET /api/search`` results."""

    id: int = Field(description="Item (track) id.")
    title: str | None = Field(default=None, description="Track title.")
    artist: str | None = Field(default=None, description="Track artist.")
    album_id: int = Field(description="Id of the album the track belongs to.")
    album: str | None = Field(default=None, description="Album title.")
    albumartist: str | None = Field(default=None, description="Album artist.")


class SearchResponse(BaseModel):
    """``GET /api/search`` — grouped artist/album/track hits."""

    artists: list[str] = Field(
        default_factory=list, description="Matching album-artist names."
    )
    albums: list[SearchAlbum] = Field(
        default_factory=list, description="Matching albums."
    )
    tracks: list[SearchTrack] = Field(
        default_factory=list, description="Matching tracks."
    )


class ArtistResponse(BaseModel):
    """``GET /api/artist`` — one artist with their albums."""

    artist: str = Field(description="The queried album-artist name.")
    albums: list[AlbumSummary] = Field(
        default_factory=list, description="Albums by this artist."
    )
