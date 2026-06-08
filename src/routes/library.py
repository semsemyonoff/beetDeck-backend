"""Library routes: SPA index, album list, search, and artist detail."""

import os
import sqlite3

from flask import Blueprint, jsonify, render_template, request

from src.utils import _album_dirs_for_ids, _find_cover, _get_ro_conn, _resolve_path

bp = Blueprint("library", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/api/library")
def library():
    from flask import current_app

    if not os.path.isfile(current_app.config["LIBRARY_DB"]):
        return jsonify({"error": "not_initialized"}), 503
    try:
        conn = _get_ro_conn()
        try:
            rows = conn.execute(
                """
            SELECT a.id, a.albumartist, a.album, a.original_year, a.year,
                   a.artpath,
                   (SELECT value FROM album_attributes
                    WHERE entity_id = a.id AND key = 'beetdeck_tagged') AS tagged,
                   (SELECT value FROM album_attributes
                    WHERE entity_id = a.id AND key = 'beetdeck_ignored') AS ignored
            FROM albums a
            ORDER BY a.albumartist COLLATE NOCASE, a.original_year, a.year, a.album
            """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return jsonify({"error": str(e)}), 500

    item_dirs = {}
    try:
        conn2 = _get_ro_conn()
        try:
            item_dirs = _album_dirs_for_ids(conn2, [r["id"] for r in rows])
        finally:
            conn2.close()
    except sqlite3.OperationalError:
        pass

    artists = {}
    for r in rows:
        artist = r["albumartist"] or "Unknown Artist"
        yr = r["original_year"] or r["year"] or None
        albums = artists.setdefault(artist, [])
        artpath = _resolve_path(r["artpath"]) if r["artpath"] else None
        has_cover = bool(artpath and os.path.isfile(artpath))
        if not has_cover:
            parent, disc_dirs = item_dirs.get(r["id"]) or (None, [])
            has_cover = bool(_find_cover(parent, fallback_dirs=disc_dirs))
        albums.append(
            {
                "id": r["id"],
                "album": r["album"],
                "year": yr,
                "tagged": r["tagged"] == "1" if r["tagged"] else False,
                "ignored": r["ignored"] == "1" if r["ignored"] else False,
                "has_cover": has_cover,
            }
        )

    result = [
        {"artist": name, "albums": albums}
        for name, albums in sorted(artists.items(), key=lambda x: x[0].lower())
    ]
    return jsonify(result)


@bp.route("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"artists": [], "albums": [], "tracks": []})

    escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    try:
        conn = _get_ro_conn()
        try:
            artist_rows = conn.execute(
                """
            SELECT DISTINCT albumartist
            FROM albums
            WHERE ULOWER(albumartist) LIKE ? ESCAPE '\\'
            ORDER BY albumartist COLLATE NOCASE
            LIMIT 20
            """,
                (like,),
            ).fetchall()
            artists = [r["albumartist"] for r in artist_rows]

            album_rows = conn.execute(
                """
            SELECT a.id, a.album, a.albumartist, a.original_year, a.year, a.artpath
            FROM albums a
            WHERE ULOWER(a.album) LIKE ? ESCAPE '\\'
            ORDER BY a.albumartist COLLATE NOCASE, a.album COLLATE NOCASE
            LIMIT 30
            """,
                (like,),
            ).fetchall()

            search_album_ids = [r["id"] for r in album_rows]
            search_item_dirs = _album_dirs_for_ids(conn, search_album_ids)

            albums = []
            for r in album_rows:
                artpath = _resolve_path(r["artpath"]) if r["artpath"] else None
                has_cover = bool(artpath and os.path.isfile(artpath))
                if not has_cover:
                    parent, disc_dirs = search_item_dirs.get(r["id"]) or (None, [])
                    has_cover = bool(_find_cover(parent, fallback_dirs=disc_dirs))
                albums.append(
                    {
                        "id": r["id"],
                        "album": r["album"],
                        "albumartist": r["albumartist"],
                        "year": r["original_year"] or r["year"] or None,
                        "has_cover": has_cover,
                    }
                )

            track_rows = conn.execute(
                """
            SELECT i.id, i.title, i.artist, i.album_id,
                   a.album, a.albumartist
            FROM items i
            JOIN albums a ON a.id = i.album_id
            WHERE ULOWER(i.title) LIKE ? ESCAPE '\\'
            ORDER BY i.title COLLATE NOCASE, i.artist COLLATE NOCASE
            LIMIT 30
            """,
                (like,),
            ).fetchall()
            tracks = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "artist": r["artist"],
                    "album_id": r["album_id"],
                    "album": r["album"],
                    "albumartist": r["albumartist"],
                }
                for r in track_rows
            ]
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"artists": artists, "albums": albums, "tracks": tracks})


@bp.route("/api/artist")
def artist_detail():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing name"}), 400

    try:
        conn = _get_ro_conn()
        try:
            if name == "Unknown Artist":
                rows = conn.execute(
                    """
                SELECT a.id, a.album, a.albumartist, a.original_year, a.year,
                       a.artpath,
                       (SELECT value FROM album_attributes
                        WHERE entity_id = a.id AND key = 'beetdeck_tagged') AS tagged,
                       (SELECT value FROM album_attributes
                        WHERE entity_id = a.id AND key = 'beetdeck_ignored') AS ignored
                FROM albums a
                WHERE (a.albumartist IS NULL OR a.albumartist = '')
                ORDER BY a.original_year, a.year, a.album
                """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                SELECT a.id, a.album, a.albumartist, a.original_year, a.year,
                       a.artpath,
                       (SELECT value FROM album_attributes
                        WHERE entity_id = a.id AND key = 'beetdeck_tagged') AS tagged,
                       (SELECT value FROM album_attributes
                        WHERE entity_id = a.id AND key = 'beetdeck_ignored') AS ignored
                FROM albums a
                WHERE a.albumartist = ?
                ORDER BY a.original_year, a.year, a.album
                """,
                    (name,),
                ).fetchall()

            item_dirs = _album_dirs_for_ids(conn, [r["id"] for r in rows])
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return jsonify({"error": str(e)}), 500

    albums = []
    for r in rows:
        artpath = _resolve_path(r["artpath"]) if r["artpath"] else None
        has_cover = bool(artpath and os.path.isfile(artpath))
        if not has_cover:
            parent, disc_dirs = item_dirs.get(r["id"]) or (None, [])
            has_cover = bool(_find_cover(parent, fallback_dirs=disc_dirs))
        albums.append(
            {
                "id": r["id"],
                "album": r["album"],
                "year": r["original_year"] or r["year"] or None,
                "tagged": r["tagged"] == "1" if r["tagged"] else False,
                "ignored": r["ignored"] == "1" if r["ignored"] else False,
                "has_cover": has_cover,
            }
        )

    return jsonify({"artist": name, "albums": albums})
