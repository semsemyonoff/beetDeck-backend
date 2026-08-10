"""Pydantic models for the ``albums`` blueprint's OpenAPI spec.

Covers ``GET /api/album/<id>`` (album detail with tracks) and
``GET /api/album/<id>/track/<id>/tags`` (raw track tags).

Both endpoints are GETs whose only inputs are integer path params
(``<int:album_id>`` / ``<int:item_id>``) — spectree documents those from the
Flask URL rule, so there is no query/body model here, only response models for
the spec. Response validation is active (spectree's default), but with
``force_resp_serialize`` left ``False`` the handlers' raw ``jsonify(...)`` bytes
pass through unchanged; all-optional fields keep real payloads conformant.

``GET .../tags`` returns a **bare object of arbitrary tag keys** (the column /
attribute names vary per track), modeled with a ``RootModel`` over a free-form
mapping rather than a fixed field set.
"""

from typing import Any

from pydantic import BaseModel, Field, RootModel


class TrackSummary(BaseModel):
    """A single track row in ``GET /api/album/<id>`` ``tracks``."""

    id: int = Field(description="Item (track) id.")
    title: str | None = Field(default=None, description="Track title.")
    artist: str | None = Field(default=None, description="Track artist.")
    track: int | None = Field(default=None, description="Track number.")
    disc: int = Field(description="Disc number (defaults to 1 when null).")
    length: str = Field(description="Formatted duration ``m:ss`` ('' when unknown).")
    length_sec: float | None = Field(
        default=None,
        description="Raw duration in seconds; null when unknown or zero.",
    )
    filename: str = Field(description="Basename of the track's file ('' when no path).")
    format: str | None = Field(default=None, description="Audio format (e.g. FLAC).")
    bitrate: int | None = Field(default=None, description="Bitrate in bits per second.")
    samplerate: int | None = Field(default=None, description="Sample rate in Hz.")
    has_lrc: bool = Field(description="Whether a sidecar .lrc lyrics file exists.")
    has_lyrics: bool = Field(
        description="Whether the track has lyrics (embedded or readable non-empty sidecar .lrc)."
    )
    instrumental: bool = Field(
        default=False,
        description="Whether a lyrics source positively reported this track as instrumental "
        "(beets `lyrics_instrumental`). Pairs with has_lyrics=false but means 'checked, "
        "there is nothing to fetch' — without it that is indistinguishable from 'never "
        "looked up'.",
    )
    has_bpm: bool = Field(description="Whether the track has a BPM value stored.")
    bpm: int | None = Field(
        default=None,
        description="Stored BPM (rounded integer tempo); null when unset or zero.",
    )


class DiscSummary(BaseModel):
    """A disc grouping in ``GET /api/album/<id>`` ``discs``."""

    disc: int = Field(description="Disc number.")
    dir: str | None = Field(default=None, description="Filesystem directory for the disc.")
    track_count: int = Field(description="Number of tracks on the disc.")
    duration_sec: int = Field(description="Total disc duration in seconds.")


class AlbumDetailResponse(BaseModel):
    """``GET /api/album/<id>`` — album metadata plus its tracks and discs."""

    id: int = Field(description="Album id.")
    album: str | None = Field(default=None, description="Album title.")
    albumartist: str | None = Field(default=None, description="Album artist.")
    year: int | None = Field(default=None, description="Original or release year.")
    genre: str = Field(description="Formatted genre string ('' when none).")
    label: str = Field(description="Record label ('' when none).")
    mb_albumid: str = Field(description="MusicBrainz album id ('' when none).")
    path: str = Field(description="Album directory path ('' when unresolved).")
    multi_disc: bool = Field(description="Whether the album spans more than one disc.")
    discs: list[DiscSummary] = Field(default_factory=list, description="Per-disc groupings.")
    has_cover: bool = Field(description="Whether cover art is available.")
    cover_width: int | None = Field(
        default=None,
        description="Pixel width of the current cover; null when there is none or it "
        "cannot be read as an image. Comparable against the dimensions a cover fetch "
        "reports, so 'is this candidate an upgrade' needs no external tooling.",
    )
    cover_height: int | None = Field(
        default=None, description="Pixel height of the current cover (see cover_width)."
    )
    tagged: bool = Field(description="Whether the album is marked beetdeck_tagged.")
    ignored: bool = Field(description="Whether the album is marked beetdeck_ignored.")
    tracks: list[TrackSummary] = Field(
        default_factory=list, description="Tracks on the album, ordered by disc/track."
    )


class TrackTagsResponse(RootModel[dict[str, Any]]):
    """``GET /api/album/<id>/track/<id>/tags`` — a bare object of arbitrary tag
    keys/values (item columns + item attributes; field set varies per track)."""
