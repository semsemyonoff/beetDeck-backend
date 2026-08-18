"""Pure helpers behind the MusicBrainz sync preview/confirm routes.

Nothing here touches the database, a lock, or the network — every function
takes plain beets ``Item``/``Album``/``AlbumInfo``/``TrackInfo`` objects (or
snapshots of them) and returns data. The routes in ``routes/mbsync.py`` own
the plugin call, the locking, and ``store()``; this module owns the mapping
between a release and the local items, and the diff/restore/drift machinery
built on top of it.

``build_item_info_pairs`` reproduces the track-mapping half of
``beetsplug/mbsync.py`` (release-track id first, recording id — a list,
since a recording can appear more than once on a release — disambiguated by
disc/track second) because that plugin's ``albums()`` inlines mapping,
applying and writing as one method with no seam a preview can call into.
"""

from __future__ import annotations

from collections import defaultdict

from beets import library


def resolve_data_source(album):
    """Reproduce ``beetsplug/mbsync.py``'s album-level ``data_source`` lookup.

    An album's own ``data_source`` wins; a loose/legacy album that never got
    one falls back to its first item's, and then to ``"MusicBrainz"``. An album
    with no items at all falls straight through to the default rather than
    raising ``IndexError`` — upstream indexes blindly, but there the album came
    out of a query that guarantees tracks, and here it came out of a URL.
    """
    own = album.get("data_source")
    if own:
        return own
    items = list(album.items())
    if not items:
        return "MusicBrainz"
    return items[0].get("data_source", "MusicBrainz")


def strip_genre_fields(album_info):
    """Blank ``genres``/``genre`` on ``album_info`` and every one of its tracks.

    Insurance against a deploy config with ``musicbrainz.genres: yes``: this
    app's own genre route never sets that, but nothing stops an operator's
    config from doing so, and the sync must never let a MusicBrainz genre
    ride in through the metadata diff — genres are a dedicated flow.
    """
    for name in ("genre", "genres"):
        if hasattr(album_info, name):
            setattr(album_info, name, None)
    for track_info in getattr(album_info, "tracks", None) or []:
        for name in ("genre", "genres"):
            if hasattr(track_info, name):
                setattr(track_info, name, None)


def build_item_info_pairs(album_info, items):
    """Map each item to its ``TrackInfo`` on ``album_info``.

    Returns ``(pairs, unmapped)`` where ``pairs`` is ``{item: track_info}``
    and ``unmapped`` is the items that matched nothing — extra local tracks,
    or tracks the release does not (yet) cover. An unmapped item is not an
    error: it is left untouched by the sync, same as upstream.
    """
    releasetrack_index = {}
    track_index = defaultdict(list)
    for track_info in album_info.tracks:
        releasetrack_index[track_info.release_track_id] = track_info
        track_index[track_info.track_id].append(track_info)

    pairs = {}
    unmapped = []
    for item in items:
        if item.mb_releasetrackid and item.mb_releasetrackid in releasetrack_index:
            pairs[item] = releasetrack_index[item.mb_releasetrackid]
            continue

        candidates = track_index[item.mb_trackid]
        if len(candidates) == 1:
            pairs[item] = candidates[0]
            continue

        matched = None
        for candidate in candidates:
            if candidate.medium_index == item.track and candidate.medium == item.disc:
                matched = candidate
                break

        if matched is not None:
            pairs[item] = matched
        else:
            unmapped.append(item)

    return pairs, unmapped


def snapshot_items(items):
    """``{item.id: {field: value}}`` over every field each item currently holds."""
    return {item.id: {field: item[field] for field in item.keys()} for item in items}


def snapshot_album(album):
    """``{field: value}`` over every field the album row currently holds."""
    return {field: album[field] for field in album.keys()}


def diff_against(album_snapshot, item_snapshot, items):
    """Diff the current (in-memory) state of ``items`` against snapshots taken before.

    ``track_fields`` is one entry per field name that changed on at least one
    item, each carrying every item's ``old``/``new`` pair for that field.

    ``album_fields`` diffs the **album row** (``album_snapshot``) against
    ``library.Album.item_keys`` — the fields ``mbsync`` upstream copies from
    an item onto the album — but only for a field that actually changed on
    at least one item; an album row that already disagreed with its items
    before the sync ran is not this sync's business to report or to fix
    (*Review corrections* #4). The value copied is the one the sync itself
    produced, taken off the first item the field changed on.
    """
    track_changes = {}
    for item in items:
        old_row = item_snapshot.get(item.id, {})
        current_keys = set(item.keys())
        for field in set(old_row) | current_keys:
            old_val = old_row.get(field)
            new_val = item[field] if field in current_keys else None
            if new_val != old_val:
                track_changes.setdefault(field, []).append(
                    {
                        "item_id": item.id,
                        "track": item.track,
                        "old": old_val,
                        "new": new_val,
                    }
                )

    track_fields = [
        {"field": field, "changes": changes} for field, changes in track_changes.items()
    ]

    album_fields = []
    for field in library.Album.item_keys:
        changes = track_changes.get(field)
        if not changes:
            continue
        new_val = changes[0]["new"]
        old_val = album_snapshot.get(field)
        if new_val != old_val:
            album_fields.append({"field": field, "old": old_val, "new": new_val})

    return album_fields, track_fields


def restore_fields(items, snapshot, field_names):
    """Restore ``field_names`` on every item to its snapshotted value.

    A field absent from the snapshot means it was never set on that item —
    restoring it is ``del item[name]``, which resets a fixed field to its
    null value and drops a flexible attribute entirely. Writing ``""``
    instead would conflate "absent" with "empty" (*Review corrections* #6).
    """
    for item in items:
        old_row = snapshot.get(item.id, {})
        for field in field_names:
            if field in old_row:
                item[field] = old_row[field]
            else:
                try:
                    del item[field]
                except KeyError:
                    pass


def check_drift(snapshot, obj, fields):
    """Return the first field name whose current value on ``obj`` disagrees
    with ``snapshot``, or ``None`` if every field still matches.

    ``obj`` is an ``Item`` or an ``Album`` — both expose ``keys()``/``__getitem__``,
    so the same check serves the confirm route for items and for the album row
    alike.
    """
    for field in fields:
        current = obj[field] if field in obj.keys() else None
        expected = snapshot.get(field)
        if current != expected:
            return field
    return None
