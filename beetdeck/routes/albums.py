"""Album and track-tag routes."""

import os
import sqlite3

from flask import Blueprint, jsonify
from spectree import Response

from beetdeck.api import api
from beetdeck.schemas.albums import AlbumDetailResponse, TrackTagsResponse
from beetdeck.schemas.common import ErrorResponse
from beetdeck.utils import (
    _album_dir_from_items,
    _decode_path,
    _detect_disc_subdirs,
    _find_cover,
    _find_lrc_file,
    _format_genre,
    _get_ro_conn,
    _read_lrc_file,
    _resolve_path,
)

bp = Blueprint("albums", __name__)


def _fmt_length(seconds):
    if not seconds:
        return ""
    m, sec = divmod(int(seconds), 60)
    return f"{m}:{sec:02d}"


@bp.route("/api/album/<int:album_id>")
@api.validate(
    resp=Response(
        HTTP_200=AlbumDetailResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["albums"],
)
def album_detail(album_id):
    try:
        conn = _get_ro_conn()
        try:
            a = conn.execute(
                """
                SELECT a.*,
                       (SELECT value FROM album_attributes
                        WHERE entity_id = a.id AND key = 'beetdeck_tagged') AS tagged,
                       (SELECT value FROM album_attributes
                        WHERE entity_id = a.id AND key = 'beetdeck_ignored') AS ignored
                FROM albums a WHERE a.id = ?
                """,
                (album_id,),
            ).fetchone()
            if not a:
                return jsonify({"error": "Album not found"}), 404

            items = conn.execute(
                """
                SELECT id, title, artist, track, disc, length, format, bitrate,
                       samplerate, path, lyrics, bpm
                FROM items WHERE album_id = ? ORDER BY disc, track
                """,
                (album_id,),
            ).fetchall()

            album_dir = _album_dir_from_items(conn, album_id)
            discs = _detect_disc_subdirs(items)
            disc_dirs = [d["dir"] for d in discs if d["dir"]]
            artpath = _resolve_path(a["artpath"]) if a["artpath"] else None
            has_cover = bool(artpath and os.path.isfile(artpath)) or bool(
                _find_cover(album_dir, fallback_dirs=disc_dirs)
            )
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return jsonify({"error": str(e)}), 500

    tracks = []
    for it in items:
        item_path = _resolve_path(it["path"]) if it["path"] else None
        lrc_path = _find_lrc_file(item_path)
        tracks.append(
            {
                "id": it["id"],
                "title": it["title"],
                "artist": it["artist"],
                "track": it["track"],
                "disc": it["disc"] or 1,
                "length": _fmt_length(it["length"]),
                "format": it["format"],
                "bitrate": it["bitrate"],
                "samplerate": it["samplerate"],
                "has_lrc": bool(lrc_path),
                "has_lyrics": bool(it["lyrics"]) or bool(lrc_path and _read_lrc_file(lrc_path)),
                "has_bpm": bool(it["bpm"]),
            }
        )

    genre = ""
    if "genres" in a.keys():
        genre = a["genres"] or ""
    if not genre and "genre" in a.keys():
        genre = a["genre"] or ""
    genre = _format_genre(genre)

    return jsonify(
        {
            "id": a["id"],
            "album": a["album"],
            "albumartist": a["albumartist"],
            "year": a["original_year"] or a["year"] or None,
            "genre": genre,
            "label": a["label"] or "",
            "mb_albumid": a["mb_albumid"] or "",
            "path": album_dir or "",
            "multi_disc": len(discs) > 1,
            "discs": discs,
            "has_cover": has_cover,
            "tagged": a["tagged"] == "1" if a["tagged"] else False,
            "ignored": a["ignored"] == "1" if a["ignored"] else False,
            "tracks": tracks,
        }
    )


@bp.route("/api/album/<int:album_id>/track/<int:item_id>/tags")
@api.validate(
    resp=Response(
        HTTP_200=TrackTagsResponse,
        HTTP_404=ErrorResponse,
        HTTP_500=ErrorResponse,
    ),
    tags=["albums"],
)
def track_tags(album_id, item_id):
    try:
        conn = _get_ro_conn()
        try:
            row = conn.execute(
                "SELECT * FROM items WHERE id = ? AND album_id = ?", (item_id, album_id)
            ).fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404

            attrs = conn.execute(
                "SELECT key, value FROM item_attributes WHERE entity_id = ?", (item_id,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return jsonify({"error": str(e)}), 500

    skip = {"id", "album_id", "path"}
    tags = {}
    for key in row.keys():
        if key in skip:
            continue
        val = row[key]
        if isinstance(val, bytes):
            val = _decode_path(val)
        if val is not None and val != "" and val != 0 and val != 0.0:
            tags[key] = val

    for attr in attrs:
        val = attr["value"]
        if isinstance(val, bytes):
            val = val.decode("utf-8", errors="replace")
        tags[attr["key"]] = val

    return jsonify(tags)
