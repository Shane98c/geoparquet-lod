"""Arrow shapes of an LOD column.

Each LOD is a nullable struct: null means the row is not shown there. Its
`geometry` child is COGP's `quantized_geoarrow` grid in GeoArrow's
interleaved coordinate layout: nested lists of `fixed_size_list<int32>[2]`
(x then y) on the LOD's grid as a list of exactly two, `MultiPolygon` three
lists deep and `MultiLineString` two. Each pair holds the zigzag-coded
difference from the ring's previous vertex, the ring's first vertex being
the difference from the origin, so a small shape is a few small integers.
Interleaving makes a geometry one Parquet leaf, so a tile reads one page run
and one page index for it rather than two. `Point` is the bare coordinate
pair, our extension of that encoding to point families. Every other child is
a carried attribute in its own Arrow type.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

ENCODING = 'quantized_geoarrow'
COORDINATES = 'interleaved'
DELTAS = 'ring'
# a list of exactly two, as Parquet stores a fixed-size list; a fixed-size Arrow type does not
# survive the round trip under a null struct in pyarrow
COORDINATE = pa.list_(pa.field('xy', pa.int32(), nullable=False))
GEOMETRY_TYPES = {'point': 'Point', 'line': 'MultiLineString', 'polygon': 'MultiPolygon'}
LIST_DEPTH = {'Point': 0, 'MultiLineString': 2, 'MultiPolygon': 3}


def _list(item: pa.DataType) -> pa.DataType:
    return pa.list_(pa.field('element', item, nullable=False))


def geometry_type(name: str) -> pa.DataType:
    t = COORDINATE
    for _ in range(LIST_DEPTH[name]):
        t = _list(t)
    return t


def lod_type(name: str, attributes: list[pa.Field]) -> pa.DataType:
    return pa.struct([pa.field('geometry', geometry_type(name), nullable=False), *attributes])


def infer_attributes(schema: pa.Schema, carry) -> list[pa.Field]:
    """`carry` is a list of column names or (name, Arrow type alias) pairs. Only
    scalar types are carried: each attribute is one Parquet leaf a tile can read."""
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
        if pa.types.is_nested(f.type):
            raise ValueError(f'cannot carry {f.name}: {f.type} is not a scalar type')
    return out


def round_away(v: np.ndarray) -> np.ndarray:
    """Round half away from zero, as the encoding requires."""
    v = np.nan_to_num(np.asarray(v, dtype=np.float64))
    return (np.sign(v) * np.floor(np.abs(v) + 0.5)).astype(np.int32)


def zigzag(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.int32)
    return ((v << 1) ^ (v >> 31)).astype(np.int32)


def unzigzag(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.int32)
    return ((v >> 1) ^ -(v & 1)).astype(np.int32)


def ring_deltas(v: np.ndarray, ring_offsets: np.ndarray) -> np.ndarray:
    """Each value less the one before it in its ring; a ring's first value stays."""
    v = np.asarray(v, dtype=np.int32)
    d = np.diff(v, prepend=np.int32(0))
    starts = np.asarray(ring_offsets)[:-1]
    d[starts] = v[starts]
    return d


def _pairs(x: np.ndarray, y: np.ndarray, ring_offsets) -> pa.Array:
    xy = np.empty(2 * len(x), dtype=np.int32)
    xy[0::2] = zigzag(ring_deltas(x, ring_offsets))
    xy[1::2] = zigzag(ring_deltas(y, ring_offsets))
    offsets = pa.array(np.arange(0, 2 * len(x) + 1, 2, dtype=np.int32))
    return pa.ListArray.from_arrays(offsets, pa.array(xy)).cast(COORDINATE)


def point_array(cx: np.ndarray, cy: np.ndarray, scale: float, offset) -> pa.Array:
    singles = np.arange(len(cx) + 1)
    return _pairs(
        round_away((cx - offset[0]) / scale), round_away((cy - offset[1]) / scale), singles
    )


def nested_array(parts, depth: int) -> pa.Array:
    """Quantized geometry column from the ragged arrays of quantize.render_*."""
    geom_offsets, poly_offsets, ring_offsets, x, y = parts
    arr = _pairs(x, y, ring_offsets)
    # lines have one ring per part, so the part level is the geometry level
    offsets = (
        [ring_offsets, poly_offsets, geom_offsets] if depth == 3 else [ring_offsets, geom_offsets]
    )
    for o in offsets:
        arr = pa.ListArray.from_arrays(pa.array(o, pa.int32()), arr)
    t = COORDINATE
    for _ in range(depth):
        t = _list(t)
    return arr.cast(t)


def lod_array(geometry: pa.Array, attributes: dict[str, pa.Array], mask: np.ndarray) -> pa.Array:
    """One LOD column: the struct is null where `mask` is False."""
    fields = [pa.field('geometry', geometry.type, nullable=False)]
    arrays = [geometry]
    for name, arr in attributes.items():
        fields.append(pa.field(name, arr.type, nullable=True))
        arrays.append(arr)
    return pa.StructArray.from_arrays(arrays, fields=fields, mask=pa.array(~np.asarray(mask)))


def geometry_rows(column: pa.Array) -> list:
    """Python lists of absolute integer coordinate tuples for a sample, for validation."""
    out = []
    for v in column.to_pylist():
        if v is None:
            out.append(None)
            continue
        out.append(_tuples(v))
    return out


def _tuples(v):
    if len(v) == 2 and isinstance(v[0], int):
        return (int(unzigzag(v[0])), int(unzigzag(v[1])))
    if v and len(v[0]) == 2 and isinstance(v[0][0], int):
        ring, x, y = [], 0, 0
        for pair in v:
            x += int(unzigzag(pair[0]))
            y += int(unzigzag(pair[1]))
            ring.append((x, y))
        return ring
    return [_tuples(u) for u in v]
