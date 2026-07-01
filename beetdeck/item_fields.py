"""Field-classification module — single source of truth for editable/album-level
field logic shared by the catalog endpoint and the PATCH write handler.
"""

from beets.library import Item

READONLY_FIELDS: frozenset[str] = frozenset(
    {
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
        "rg_track_gain",
        "rg_track_peak",
        "rg_album_gain",
        "rg_album_peak",
        "r128_track_gain",
        "r128_album_gain",
        "acoustid_fingerprint",
        "acoustid_id",
        "encoder",
    }
)

ALBUM_LEVEL_FIELDS: frozenset[str] = frozenset({"album", "albumartist", "year", "genres"})

_CLEAR_SENTINEL = object()


def is_editable(name: str) -> bool:
    """Return True when the field may be written via the per-track PATCH endpoint."""
    if name in READONLY_FIELDS:
        return False
    if name.startswith("mb_"):
        return False
    return True


def is_album_level(name: str) -> bool:
    return name in ALBUM_LEVEL_FIELDS


def is_known_field(name: str) -> bool:
    """Return True when ``name`` is a beets Item fixed (DB column) field.

    Unknown names are only writable when they already exist as a flexible attr
    on the specific item being patched — that membership check belongs to the
    handler, which has the loaded item. The catalog/dropdown only ever offers
    known fields, so arbitrary new flexible keys must be rejected at the API.
    """
    return name in Item._fields


def build_field_catalog() -> list[dict]:
    """Return one entry per beets Item fixed field: {name, type, editable, album_level}."""
    catalog = []
    for name, field in Item._fields.items():
        model_type_name = field.model_type.__name__
        catalog.append(
            {
                "name": name,
                "type": model_type_name,
                "editable": is_editable(name),
                "album_level": is_album_level(name),
            }
        )
    catalog.sort(key=lambda e: e["name"])
    return catalog


def coerce_value(name: str, raw):
    """Coerce raw string value for a beets field.

    Returns the coerced value, or a clear sentinel (``CLEAR``) when raw is
    an empty string — callers must ``del item[name]`` in that case.

    Raises ValueError on bad numeric/bool value (caller maps to 400).
    """
    if raw == "" or raw is None:
        return _CLEAR_SENTINEL

    fields = Item._fields
    if name not in fields:
        return str(raw)

    model_type = fields[name].model_type
    type_name = model_type.__name__

    if type_name == "int":
        try:
            return int(raw)
        except (ValueError, TypeError):
            raise ValueError(f"invalid int for field '{name}': {raw!r}")

    if type_name == "float":
        try:
            return float(raw)
        except (ValueError, TypeError):
            raise ValueError(f"invalid float for field '{name}': {raw!r}")

    if type_name == "list":
        if isinstance(raw, list):
            return [str(s).strip() for s in raw if str(s).strip()]
        return [g.strip() for g in str(raw).split(",") if g.strip()]

    if type_name == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
        raise ValueError(f"invalid bool for field '{name}': {raw!r}")

    return str(raw)


CLEAR = _CLEAR_SENTINEL
