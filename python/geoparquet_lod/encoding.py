"""Arrow shape of an LOD column.

Each LOD is a nullable `binary` child of the `lod` struct: null means the row
is not shown there, and a value is the row packed as `pack` describes, so a
tile reads one leaf per LOD. Every carried attribute is inside the blob.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

ENCODING = 'packed'
PACK_VERSION = 1
GEOMETRY_TYPES = {'point': 'Point', 'line': 'MultiLineString', 'polygon': 'MultiPolygon'}


def lod_type() -> pa.DataType:
    return pa.binary()


def infer_attributes(schema: pa.Schema, carry) -> list[pa.Field]:
    """`carry` is a list of column names or (name, Arrow type alias) pairs. Only
    scalar types are carried: integers, floats, booleans and strings."""
    out = []
    for item in carry:
        if isinstance(item, pa.Field):
            out.append(pa.field(item.name, item.type, nullable=True))
        elif isinstance(item, tuple):
            name, alias = item
            out.append(pa.field(name, pa.type_for_alias(alias), nullable=True))
        else:
            out.append(pa.field(item, schema.field(item).type, nullable=True))
    for f in out:
        t = f.type
        ok = (
            pa.types.is_integer(t)
            or pa.types.is_floating(t)
            or pa.types.is_boolean(t)
            or pa.types.is_string(t)
            or pa.types.is_large_string(t)
        )
        if not ok:
            raise ValueError(f'cannot carry {f.name}: {t} is not an integer, float, bool or string')
    return out


def round_away(v: np.ndarray) -> np.ndarray:
    """Round half away from zero, as the encoding requires."""
    v = np.nan_to_num(np.asarray(v, dtype=np.float64))
    return (np.sign(v) * np.floor(np.abs(v) + 0.5)).astype(np.int32)


def point_parts(cx: np.ndarray, cy: np.ndarray, scale: float, offset) -> tuple:
    """Grid coordinates of points, as `pack` takes them."""
    return (round_away((cx - offset[0]) / scale), round_away((cy - offset[1]) / scale))
