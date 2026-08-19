"""Unit tests for beetdeck.mbsync — the pure mapping/snapshot/diff helpers."""

from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.library import Album, Item

from beetdeck.mbsync import (
    build_item_info_pairs,
    check_drift,
    diff_against,
    resolve_data_source,
    restore_fields,
    snapshot_album,
    snapshot_items,
    strip_genre_fields,
)


def make_item(**fields):
    return Item(**fields)


def make_album(**fields):
    return Album(**fields)


def make_track_info(**fields):
    return TrackInfo(**fields)


def make_album_info(tracks, **fields):
    return AlbumInfo(tracks=tracks, **fields)


# ---------------------------------------------------------------------------
# resolve_data_source
# ---------------------------------------------------------------------------


class TestResolveDataSource:
    def test_prefers_album_data_source(self):
        album = make_album(id=1, data_source="Deezer")
        assert resolve_data_source(album) == "Deezer"

    def test_falls_back_to_first_item(self, monkeypatch):
        album = make_album(id=1, data_source="")
        item = make_item(id=1, data_source="Discogs")
        monkeypatch.setattr(Album, "items", lambda self: [item])
        assert resolve_data_source(album) == "Discogs"

    def test_falls_back_to_musicbrainz(self, monkeypatch):
        album = make_album(id=1, data_source="")
        item = make_item(id=1)
        monkeypatch.setattr(Album, "items", lambda self: [item])
        assert resolve_data_source(album) == "MusicBrainz"

    def test_an_album_with_no_items_falls_back_rather_than_raising(self, monkeypatch):
        # A route reaches this with whatever album id the URL carried; an
        # IndexError here surfaces as a 500 reading "list index out of range".
        album = make_album(id=1, data_source="")
        monkeypatch.setattr(Album, "items", lambda self: [])
        assert resolve_data_source(album) == "MusicBrainz"


# ---------------------------------------------------------------------------
# strip_genre_fields
# ---------------------------------------------------------------------------


class TestStripGenreFields:
    def test_blanks_album_and_track_genres(self):
        track = make_track_info(title="A", genres=["Rock"])
        info = make_album_info([track], genres=["Pop"])

        strip_genre_fields(info)

        assert info.genres is None
        assert track.genres is None

    def test_noop_when_no_genres_present(self):
        track = make_track_info(title="A")
        info = make_album_info([track])

        strip_genre_fields(info)

        assert info.genres is None
        assert track.genres is None


# ---------------------------------------------------------------------------
# build_item_info_pairs
# ---------------------------------------------------------------------------


class TestBuildItemInfoPairs:
    def test_release_track_id_wins(self):
        rt = make_track_info(title="A", release_track_id="rt1", track_id="rec1")
        info = make_album_info([rt])
        item = make_item(id=1, mb_releasetrackid="rt1", mb_trackid="rec1", track=1, disc=1)

        pairs, unmapped = build_item_info_pairs(info, [item])

        assert pairs == {item: rt}
        assert unmapped == []

    def test_recording_id_fallback(self):
        rt = make_track_info(title="A", release_track_id="rt1", track_id="rec1")
        info = make_album_info([rt])
        item = make_item(id=1, mb_releasetrackid="", mb_trackid="rec1", track=1, disc=1)

        pairs, unmapped = build_item_info_pairs(info, [item])

        assert pairs == {item: rt}
        assert unmapped == []

    def test_duplicate_recording_disambiguated_by_disc_and_track(self):
        rt1 = make_track_info(
            title="A", release_track_id="rt1", track_id="rec1", medium=1, medium_index=1
        )
        rt2 = make_track_info(
            title="A (bonus)",
            release_track_id="rt2",
            track_id="rec1",
            medium=2,
            medium_index=1,
        )
        info = make_album_info([rt1, rt2])
        item = make_item(id=1, mb_releasetrackid="", mb_trackid="rec1", track=1, disc=2)

        pairs, unmapped = build_item_info_pairs(info, [item])

        assert pairs == {item: rt2}
        assert unmapped == []

    def test_item_matching_nothing_is_unmapped(self):
        rt = make_track_info(title="A", release_track_id="rt1", track_id="rec1")
        info = make_album_info([rt])
        item = make_item(id=1, mb_releasetrackid="", mb_trackid="", track=1, disc=1)

        pairs, unmapped = build_item_info_pairs(info, [item])

        assert pairs == {}
        assert unmapped == [item]

    def test_duplicate_recording_no_disambiguation_match_is_unmapped(self):
        rt1 = make_track_info(
            title="A", release_track_id="rt1", track_id="rec1", medium=1, medium_index=1
        )
        rt2 = make_track_info(
            title="A (bonus)",
            release_track_id="rt2",
            track_id="rec1",
            medium=2,
            medium_index=1,
        )
        info = make_album_info([rt1, rt2])
        item = make_item(id=1, mb_releasetrackid="", mb_trackid="rec1", track=9, disc=9)

        pairs, unmapped = build_item_info_pairs(info, [item])

        assert pairs == {}
        assert unmapped == [item]


# ---------------------------------------------------------------------------
# snapshot / diff / restore / drift
# ---------------------------------------------------------------------------


class TestSnapshotDiffRestoreDrift:
    def test_diff_reports_changed_track_field(self):
        item = make_item(id=1, title="Intro", track=3)
        snapshot = snapshot_items([item])

        item.title = "Introduction"

        album_snapshot = snapshot_album(make_album(id=1))
        album_fields, track_fields = diff_against(album_snapshot, snapshot, [item])

        assert album_fields == []
        title_entry = next(f for f in track_fields if f["field"] == "title")
        assert title_entry["changes"] == [
            {"item_id": 1, "track": 3, "old": "Intro", "new": "Introduction"}
        ]

    def test_diff_reports_no_change_when_nothing_moved(self):
        item = make_item(id=1, title="Intro", track=3)
        snapshot = snapshot_items([item])

        album_snapshot = snapshot_album(make_album(id=1))
        album_fields, track_fields = diff_against(album_snapshot, snapshot, [item])

        assert album_fields == []
        assert track_fields == []

    def test_diff_reports_album_row_change_from_item_keys(self):
        # `label` is one of `library.Album.item_keys`.
        item = make_item(id=1, label="")
        snapshot = snapshot_items([item])

        item.label = "4AD"

        album = make_album(id=1, label="")
        album_snapshot = snapshot_album(album)
        album_fields, _ = diff_against(album_snapshot, snapshot, [item])

        assert album_fields == [{"field": "label", "old": "", "new": "4AD"}]

    def test_album_row_disagreeing_with_item_on_unchanged_key_is_not_reported(self):
        # The album row disagrees with item[0] on `label`, but the item's
        # value never moved from its own snapshot — nothing to copy.
        item = make_item(id=1, label="Old Label")
        snapshot = snapshot_items([item])

        album = make_album(id=1, label="Different Label")
        album_snapshot = snapshot_album(album)
        album_fields, _ = diff_against(album_snapshot, snapshot, [item])

        assert album_fields == []

    def test_album_only_fields_stay_out_of_the_item_snapshot(self):
        # `album.items()` hands back items with `_cached_album` populated, and
        # beets' `Item.keys()` unions the album's fields by default — `artpath`
        # is an album-only column no item row carries.
        item = make_item(id=1, title="Intro")
        item._cached_album = make_album(id=1, artpath=b"/music/a/cover.jpg")

        snapshot = snapshot_items([item])

        assert "artpath" not in snapshot[1]

    def test_an_album_only_field_changing_is_not_a_track_change(self):
        # A cover write between preview and confirm moves `album.artpath`. Read
        # through the item, that would mark *every* track changed and make the
        # confirm rewrite every file in the album, unmapped ones included.
        item = make_item(id=1, title="Intro")
        item._cached_album = make_album(id=1, artpath=b"/music/a/old.jpg")
        snapshot = snapshot_items([item])

        item._cached_album = make_album(id=1, artpath=b"/music/a/new.jpg")

        album_snapshot = snapshot_album(make_album(id=1))
        album_fields, track_fields = diff_against(album_snapshot, snapshot, [item])

        assert album_fields == []
        assert track_fields == []

    def test_mtime_stays_out_of_the_item_snapshot(self):
        item = make_item(id=1, title="Intro", mtime=1780935555.0)

        snapshot = snapshot_items([item])

        assert "mtime" not in snapshot[1]

    def test_mtime_reset_by_a_tag_write_is_not_reported_as_a_change(self):
        # `Item.__setitem__` zeroes `mtime` whenever a field in
        # `MediaFile.fields()` changes, so a release that corrects one title
        # would otherwise show the user an `mtime: … -> 0` row with a
        # checkbox next to it.
        item = make_item(id=1, title="Intro", track=3, mtime=1780935555.0)
        snapshot = snapshot_items([item])

        item.title = "Introduction"
        assert item.mtime == 0

        album_snapshot = snapshot_album(make_album(id=1))
        _, track_fields = diff_against(album_snapshot, snapshot, [item])

        assert [f["field"] for f in track_fields] == ["title"]

    def test_restore_field_present_in_snapshot(self):
        item = make_item(id=1, title="Intro")
        snapshot = snapshot_items([item])
        item.title = "Introduction"

        restore_fields([item], snapshot, ["title"])

        assert item.title == "Intro"

    def test_restore_field_absent_in_snapshot_deletes_rather_than_blanks(self):
        # A flexible attribute never set on the item is genuinely absent —
        # unlike a fixed field, which always has a null value and can never
        # be missing from `keys()`.
        item = make_item(id=1)
        snapshot = snapshot_items([item])
        assert "custom_tag" not in snapshot[1]

        item["custom_tag"] = "fetched from release"
        restore_fields([item], snapshot, ["custom_tag"])

        assert "custom_tag" not in item

    def test_check_drift_on_item_field(self):
        item = make_item(id=1, title="Intro")
        snapshot = snapshot_items([item])[1]

        assert check_drift(snapshot, item, ["title"]) is None

        item.title = "Changed elsewhere"
        assert check_drift(snapshot, item, ["title"]) == "title"

    def test_check_drift_on_album_row(self):
        album = make_album(id=1, label="4AD")
        snapshot = snapshot_album(album)

        assert check_drift(snapshot, album, ["label"]) is None

        album.label = "Changed elsewhere"
        assert check_drift(snapshot, album, ["label"]) == "label"

    def test_check_drift_checks_fields_in_order_and_returns_first(self):
        item = make_item(id=1, title="Intro", artist="A")
        snapshot = snapshot_items([item])[1]

        item.title = "Changed"
        item.artist = "Also changed"

        assert check_drift(snapshot, item, ["title", "artist"]) == "title"
        assert check_drift(snapshot, item, ["artist", "title"]) == "artist"
