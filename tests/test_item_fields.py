"""Unit tests for beetdeck.item_fields."""

import pytest

from beetdeck.item_fields import (
    CLEAR,
    build_field_catalog,
    coerce_value,
    is_album_level,
    is_editable,
)

# ---------------------------------------------------------------------------
# is_editable
# ---------------------------------------------------------------------------


class TestIsEditable:
    def test_readonly_fields_not_editable(self):
        for field in (
            "id",
            "path",
            "album_id",
            "length",
            "bitrate",
            "format",
            "samplerate",
            "bitdepth",
            "channels",
            "mtime",
            "added",
            "bpm",
        ):
            assert is_editable(field) is False, f"{field} should not be editable"

    def test_rg_fields_not_editable(self):
        for field in (
            "rg_track_gain",
            "rg_track_peak",
            "rg_album_gain",
            "rg_album_peak",
            "r128_track_gain",
            "r128_album_gain",
        ):
            assert is_editable(field) is False

    def test_acoustid_fields_not_editable(self):
        assert is_editable("acoustid_fingerprint") is False
        assert is_editable("acoustid_id") is False

    def test_encoder_not_editable(self):
        assert is_editable("encoder") is False

    def test_mb_prefix_not_editable(self):
        for field in (
            "mb_trackid",
            "mb_albumid",
            "mb_artistid",
            "mb_albumartistid",
            "mb_releasegroupid",
            "mb_workid",
            "mb_releasetrackid",
            "mb_albumartistids",
            "mb_artistids",
        ):
            assert is_editable(field) is False, f"{field} should not be editable"

    def test_normal_fields_editable(self):
        for field in (
            "title",
            "artist",
            "album",
            "albumartist",
            "year",
            "track",
            "disc",
            "genres",
            "composer",
            "label",
            "comments",
            "lyrics",
            "comp",
        ):
            assert is_editable(field) is True, f"{field} should be editable"


# ---------------------------------------------------------------------------
# is_album_level
# ---------------------------------------------------------------------------


class TestIsAlbumLevel:
    def test_album_level_fields(self):
        for field in ("album", "albumartist", "year", "genres"):
            assert is_album_level(field) is True, f"{field} should be album_level"

    def test_genre_not_album_level(self):
        assert is_album_level("genre") is False

    def test_non_album_level_fields(self):
        for field in ("title", "artist", "track", "disc", "composer", "label"):
            assert is_album_level(field) is False


# ---------------------------------------------------------------------------
# build_field_catalog
# ---------------------------------------------------------------------------


class TestBuildFieldCatalog:
    def setup_method(self):
        self.catalog = build_field_catalog()
        self.by_name = {e["name"]: e for e in self.catalog}

    def test_returns_list_of_dicts(self):
        assert isinstance(self.catalog, list)
        assert len(self.catalog) > 0
        for entry in self.catalog:
            assert {"name", "type", "editable", "album_level"} == set(entry.keys())

    def test_editable_field_present_and_flagged(self):
        title = self.by_name.get("title")
        assert title is not None
        assert title["editable"] is True
        assert title["album_level"] is False

    def test_readonly_field_flagged(self):
        for name in ("length", "bitrate", "format"):
            entry = self.by_name.get(name)
            assert entry is not None
            assert entry["editable"] is False

    def test_mb_trackid_not_editable(self):
        entry = self.by_name.get("mb_trackid")
        assert entry is not None
        assert entry["editable"] is False

    def test_album_has_album_level_true(self):
        entry = self.by_name.get("album")
        assert entry is not None
        assert entry["album_level"] is True
        assert entry["editable"] is True

    def test_albumartist_has_album_level_true(self):
        entry = self.by_name.get("albumartist")
        assert entry is not None
        assert entry["album_level"] is True

    def test_year_has_album_level_true(self):
        entry = self.by_name.get("year")
        assert entry is not None
        assert entry["album_level"] is True

    def test_genres_album_level_and_list_typed(self):
        entry = self.by_name.get("genres")
        assert entry is not None
        assert entry["album_level"] is True
        assert entry["type"] == "list"

    def test_genre_not_in_catalog(self):
        # 'genre' is not a real Item._fields entry in beets 2.12.0
        assert "genre" not in self.by_name

    def test_comp_bool_typed(self):
        entry = self.by_name.get("comp")
        assert entry is not None
        assert entry["type"] == "bool"

    def test_track_int_typed(self):
        entry = self.by_name.get("track")
        assert entry is not None
        assert entry["type"] == "int"

    def test_catalog_is_sorted(self):
        names = [e["name"] for e in self.catalog]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# coerce_value
# ---------------------------------------------------------------------------


class TestCoerceValue:
    def test_empty_string_returns_clear(self):
        assert coerce_value("title", "") is CLEAR

    def test_none_returns_clear(self):
        assert coerce_value("title", None) is CLEAR

    def test_str_passthrough(self):
        assert coerce_value("title", "Hello World") == "Hello World"

    def test_composer_str(self):
        assert coerce_value("composer", "Bach") == "Bach"

    # int coercion
    def test_int_valid(self):
        assert coerce_value("track", "5") == 5
        assert coerce_value("year", 2020) == 2020

    def test_int_invalid_raises(self):
        with pytest.raises(ValueError, match="invalid int"):
            coerce_value("track", "not-a-number")

    def test_year_int_coercion(self):
        assert coerce_value("year", "1999") == 1999

    # float coercion
    def test_float_valid(self):
        result = coerce_value("length", "180.5")
        assert isinstance(result, float)
        assert abs(result - 180.5) < 1e-9

    def test_float_invalid_raises(self):
        with pytest.raises(ValueError, match="invalid float"):
            coerce_value("length", "xyz")

    # list coercion (genres = DelimitedString)
    def test_genres_comma_string_to_list(self):
        result = coerce_value("genres", "Rock, Pop, Jazz")
        assert result == ["Rock", "Pop", "Jazz"]

    def test_genres_null_byte_delimiter_treated_as_string(self):
        # raw from track_tags may use \x00; we treat the whole thing as a
        # comma-split string — caller should normalize before sending
        result = coerce_value("genres", "Rock\x00Pop")
        assert isinstance(result, list)

    def test_genres_already_list(self):
        result = coerce_value("genres", ["Rock", "Pop"])
        assert result == ["Rock", "Pop"]

    def test_genres_strips_whitespace(self):
        result = coerce_value("genres", " Rock , Pop ")
        assert result == ["Rock", "Pop"]

    def test_genres_filters_empty(self):
        result = coerce_value("genres", "Rock,,Pop,")
        assert result == ["Rock", "Pop"]

    # bool coercion (comp = Boolean)
    def test_bool_true_strings(self):
        for val in ("true", "True", "TRUE", "1", "yes"):
            assert coerce_value("comp", val) is True, f"expected True for {val!r}"

    def test_bool_false_strings(self):
        for val in ("false", "False", "FALSE", "0", "no"):
            assert coerce_value("comp", val) is False, f"expected False for {val!r}"

    def test_bool_python_bool(self):
        assert coerce_value("comp", True) is True
        assert coerce_value("comp", False) is False

    def test_bool_invalid_raises(self):
        with pytest.raises(ValueError, match="invalid bool"):
            coerce_value("comp", "maybe")

    # unknown flexible field (not in Item._fields)
    def test_unknown_field_str_passthrough(self):
        result = coerce_value("my_flex_attr", "some value")
        assert result == "some value"

    def test_unknown_field_empty_returns_clear(self):
        assert coerce_value("my_flex_attr", "") is CLEAR
