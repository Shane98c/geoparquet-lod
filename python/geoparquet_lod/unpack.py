"""Packed LOD values decoded a column at a time.

`unpack` reverses `pack` for every value of a column at once: all the bytes
are split into varints together, then every value's structure is walked in
step, one nesting level at a time, so the work is a few array passes per
level rather than Python per byte. A value that does not decode raises
`UnpackError` with its index. `read_overview` uses it to read one zoom of a
file as an ordinary table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

from .encoding import PACK_VERSION
from .pack import FAMILY_DEPTH

# larger than any real count; counts are clipped to it so positions stay in int64
COUNT_LIMIT = 1 << 40


class UnpackError(ValueError):
    """A packed value that does not decode; `row` is its index in the column."""

    def __init__(self, row: int, message: str):
        super().__init__(message)
        self.row = row


@dataclass
class Unpacked:
    """A column's records in packed order: each value's own geometry and
    attributes, then the copies it hosts.

    `geometry` is (x, y) for points and, for shapes, the ragged arrays
    quantize.render_* returns, (geometry, polygon and ring offsets, x, y),
    with polygon rings closed and each line part a polygon of one ring.
    """

    row: np.ndarray  # index in the column of the value holding each record
    copy: np.ndarray  # whether each record is a hosted copy
    tile: np.ndarray  # a copy's tile tag, 0 for a value's own record
    id: np.ndarray  # a copy's file row index, -1 for a value's own record
    geometry: tuple
    attrs: dict[str, pa.Array]


def carried(lod: dict) -> tuple[list[pa.Field], dict[str, list]]:
    """Carried attribute fields and string value tables of `lod_columns` metadata."""
    attributes = lod.get('attributes', [])
    fields = [pa.field(a['name'], pa.type_for_alias(a['type'])) for a in attributes]
    return fields, {a['name']: a['values'] for a in attributes if 'values' in a}


def read_overview(path: str | Path, zoom: int) -> pa.Table:
    """The rows shown at `zoom` with their overview geometry: `row`, the file
    row index; `geometry`, WKB in EPSG:3857 metres, the CRS stated in the
    field's GeoArrow metadata; and the carried attributes as that zoom's LOD
    stores them. Boundary copies are left out, as each repeats a shown row."""
    f = pq.ParquetFile(path)
    lod = json.loads((f.schema_arrow.metadata or {}).get(b'geo', b'{}')).get('lod_columns')
    if not lod or lod.get('pack_version') != PACK_VERSION:
        raise ValueError(f'{path} has no packed LOD columns of version {PACK_VERSION}')
    name = next((n for n, c in lod['lods'].items() if zoom in c['zooms']), None)
    if name is None:
        raise ValueError(f'zoom {zoom} is not one of the file zooms {lod["zooms"]}')
    child = lod['lods'][name]
    fields, dictionaries = carried(lod)
    crs = {'crs': 'EPSG:3857', 'crs_type': 'authority_code'}
    wkb = {'ARROW:extension:name': 'geoarrow.wkb', 'ARROW:extension:metadata': json.dumps(crs)}
    schema = pa.schema(
        [pa.field('row', pa.int64()), pa.field('geometry', pa.binary(), metadata=wkb), *fields]
    )
    tables, first = [], 0
    for group in range(f.metadata.num_row_groups):
        column = f.read_row_group(group, columns=[f'{lod["column"]}.{name}']).column(0)
        u = unpack(
            column.combine_chunks().field(name), child['geometry_type'], fields, dictionaries
        )
        shapes = _geometries(u, child)
        own = np.flatnonzero(~u.copy & ~shapely.is_empty(shapes))
        attrs = [u.attrs[a.name].take(own) for a in fields]
        tables.append(pa.table([first + u.row[own], shapely.to_wkb(shapes[own]), *attrs], schema))
        first += f.metadata.row_group(group).num_rows
    return pa.concat_tables(tables) if tables else schema.empty_table()


def _geometries(u: Unpacked, child: dict) -> np.ndarray:
    """Shapely geometries of every record, in EPSG:3857 metres."""
    (sx, sy), (ox, oy) = child['scale'], child['offset']
    if child['geometry_type'] == 'Point':
        return shapely.points(ox + sx * u.geometry[0], oy + sy * u.geometry[1])
    geom, poly, ring, x, y = u.geometry
    coords = np.stack([ox + sx * x, oy + sy * y], axis=1)
    if child['geometry_type'] == 'MultiPolygon':
        kind, offsets = shapely.GeometryType.MULTIPOLYGON, (ring, poly, geom)
    else:
        kind, offsets = shapely.GeometryType.MULTILINESTRING, (ring, geom)
    return shapely.from_ragged_array(kind, coords, offsets)


def unpack(
    values: pa.Array, family: str, fields: list[pa.Field], dictionaries: dict | None = None
) -> Unpacked:
    """Decode every non-null packed value of one LOD column."""
    depth = FAMILY_DEPTH[family]
    index = np.flatnonzero(values.is_valid().to_numpy(zero_copy_only=False))
    if values.null_count:
        values = values.take(pa.array(index))
    values = values.cast(pa.large_binary())
    offsets = np.frombuffer(values.buffers()[1], np.int64)[
        values.offset : values.offset + len(values) + 1
    ]
    buffer = values.buffers()[2]
    data = np.frombuffer(buffer, np.uint8) if buffer is not None else np.zeros(0, np.uint8)
    data = data[offsets[0] : offsets[-1]]
    lo, hi = offsets[:-1] - offsets[0], offsets[1:] - offsets[0]
    if (hi == lo).any():
        raise UnpackError(int(index[np.argmax(hi == lo)]), 'empty packed value')
    tok, bad, last, t1 = _varints(data, hi)
    t0 = np.zeros_like(t1)
    t0[1:] = t1[:-1]
    walk = _Walk(tok, t1, depth, len(fields))
    rows = np.arange(len(lo))
    pos = walk.attrs(walk.geometry(t0, rows), rows)
    pos = walk.each(walk.count(pos, rows, 4), pos + 1, rows, walk.copy)
    # every varint before a value's strings must be whole and fit 64 bits
    broken = np.flatnonzero(bad)
    owner = np.searchsorted(t0, broken, side='right') - 1
    broken = owner[broken < np.minimum(pos, t1)[owner]]
    if len(broken):
        walk.fail(broken, 'a varint is cut short or wider than 64 bits')
    if walk.error:
        raise UnpackError(int(index[walk.error[0]]), walk.error[1])

    # records in packed order: each value's own, then its copies
    copies = _sorted(walk.copies)
    starts = np.concatenate([t0, copies + 2])
    order = np.argsort(starts, kind='stable')
    record_start, copy = starts[order], order >= len(t0)
    value = np.searchsorted(t0, record_start, side='right') - 1
    tile = np.zeros(len(order), np.uint64)
    tile[copy] = tok[copies]
    ids = np.full(len(order), -1, np.int64)
    ids[copy] = walk.counts[copies + 1]

    attrs, lengths = _attributes(tok, _sorted(walk.bitmaps), index[value], fields, dictionaries)
    # each value's string bytes follow its varints, in the order of their lengths
    string_start = last[pos - 1] + 1
    attrs.update(_strings(data, walk.counts, lengths, value, string_start, hi, index))
    attrs = {f.name: attrs[f.name] for f in fields}
    if depth == 0:
        geometry = (_unzigzag(tok[record_start]), _unzigzag(tok[record_start + 1]))
    else:
        geometry = _shapes(tok, walk, record_start)
    return Unpacked(index[value], copy, tile, ids, geometry, attrs)


def _varints(data: np.ndarray, value_ends: np.ndarray):
    """Each value's bytes read as varints: their values, which are cut short
    or wider than 64 bits, the last byte of each, and the varint after each
    value's last. Within a value's string payload the varints mean nothing."""
    stop = data < 0x80
    stop[value_ends - 1] = True
    last = np.flatnonzero(stop)
    ends = np.zeros(len(data), bool)
    ends[value_ends - 1] = True
    value_end = np.flatnonzero(ends[last]) + 1
    first = np.zeros_like(last)
    first[1:] = last[:-1] + 1
    size = last - first + 1
    tok = (data[first] & 0x7F).astype(np.uint64)
    k = np.flatnonzero(size > 1)
    for j in range(1, 10):
        k = k[size[k] > j]
        if not len(k):
            break
        tok[k] |= (data[first[k] + j] & 0x7F).astype(np.uint64) << np.uint64(7 * j)
    # only a value's last varint can be cut short, and only one of ten bytes or more too wide
    bad = np.zeros(len(last), bool)
    bad[value_end - 1] = data[value_ends - 1] > 0x7F
    bad[k] |= (size[k] > 10) | (data[last[k]] > 1)
    return tok, bad, last, value_end


class _Walk:
    """Cursors walking packed values in step. The walk records where the
    elements it passes start, and the first value that breaks the format."""

    def __init__(self, tok, end, depth: int, n_fields: int):
        self.tok, self.end, self.depth = tok, end, depth
        self.counts = np.minimum(tok, COUNT_LIMIT).astype(np.int64)
        self.mask = np.uint64((1 << n_fields) - 1)
        self.polygons, self.rings, self.bitmaps, self.copies = [], [], [], []
        self.error = None

    def fail(self, rows: np.ndarray, message: str) -> None:
        row = int(rows.min())
        if self.error is None or row < self.error[0]:
            self.error = (row, message)

    def read(self, pos, row):
        """Positions safe to read at, and which cursors have a varint there."""
        ok = pos < self.end[row]
        if not ok.all():
            self.fail(row[~ok], 'the packed structure runs past the end of the value')
        return np.where(ok, pos, 0), ok

    def count(self, pos, row, size: int):
        """The count at `pos` of elements at least `size` varints long."""
        at, ok = self.read(pos, row)
        n = np.where(ok, self.counts[at], 0)
        over = n * size > self.end[row] - pos - 1
        if over.any():
            self.fail(row[over], 'a count runs past the end of the value')
            n[over] = 0
        return n

    def each(self, n, pos, row, step):
        """Advance each cursor past its `n` consecutive elements, which `step` walks."""
        pos = pos.copy()
        live = np.arange(len(pos))
        k = 0
        while len(live := live[n[live] > k]):
            pos[live] = step(pos[live], row[live])
            k += 1
        return pos

    def geometry(self, pos, row):
        if self.depth == 0:
            self.read(pos, row)
            self.read(pos + 1, row)
            return pos + 2
        step = self.polygon if self.depth == 3 else self.ring
        return self.each(self.count(pos, row, 1), pos + 1, row, step)

    def polygon(self, pos, row):
        self.polygons.append(pos)
        return self.each(self.count(pos, row, 1), pos + 1, row, self.ring)

    def ring(self, pos, row):
        self.rings.append(pos)
        return pos + 1 + 2 * self.count(pos, row, 2)

    def attrs(self, pos, row):
        self.bitmaps.append(pos)
        at, ok = self.read(pos, row)
        present = np.where(ok, np.bitwise_count(self.tok[at] & self.mask), 0)
        end = pos + 1 + present.astype(np.int64)
        over = end > self.end[row]
        if over.any():
            self.fail(row[over], 'the attributes run past the end of the value')
        return end

    def copy(self, pos, row):
        self.copies.append(pos)
        self.read(pos, row)
        self.read(pos + 1, row)
        return self.attrs(self.geometry(pos + 2, row), row)


def _sorted(positions: list) -> np.ndarray:
    return np.sort(np.concatenate(positions), kind='stable') if positions else np.zeros(0, np.int64)


def _unzigzag(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.uint64)
    return (v >> np.uint64(1)).astype(np.int64) ^ -(v & np.uint64(1)).astype(np.int64)


def _refuse(problem: np.ndarray, rows: np.ndarray, message: str) -> None:
    if problem.any():
        raise UnpackError(int(rows[np.argmax(problem)]), message)


def _attributes(tok, bitmaps, rows, fields, dictionaries):
    """Attribute arrays over records, and for each string attribute where
    its byte lengths are and which records have one."""
    bits = tok[bitmaps]
    slot = bitmaps + 1
    attrs, lengths = {}, {}
    for i, f in enumerate(fields):
        has = ((bits >> np.uint64(i)) & np.uint64(1)).astype(bool)
        at = np.where(has, slot, 0)
        raw = np.where(has, tok[at], 0)
        slot = slot + has
        t = f.type
        if dictionaries and f.name in dictionaries:
            table = dictionaries[f.name]
            _refuse(has & (raw >= len(table)), rows, f'{f.name} indexes past its value table')
            attrs[f.name] = pa.array(table, t).take(pa.array(raw.astype(np.int64), mask=~has))
        elif pa.types.is_integer(t):
            v = _unzigzag(raw)
            limit = np.iinfo(t.to_pandas_dtype())
            _refuse((v < limit.min) | (v > limit.max), rows, f'{f.name} is out of range for {t}')
            attrs[f.name] = pa.array(v, mask=~has).cast(t)
        elif pa.types.is_float64(t):
            attrs[f.name] = pa.array(raw.view(np.float64), mask=~has)
        elif pa.types.is_floating(t):
            _refuse(raw > 0xFFFFFFFF, rows, f'{f.name} has a float32 wider than 32 bits')
            attrs[f.name] = pa.array(raw.astype(np.uint32).view(np.float32), mask=~has).cast(t)
        elif pa.types.is_boolean(t):
            attrs[f.name] = pa.array(raw != 0, mask=~has)
        else:
            lengths[f.name] = (at, has, t)
    return attrs, lengths


def _strings(data, counts, lengths, value, string_start, value_end, index):
    """String attribute arrays read from each value's trailing payload, which
    must hold exactly the bytes its lengths claim."""
    names, none = list(lengths), np.zeros(0, np.int64)
    at = np.concatenate([lengths[n][0][lengths[n][1]] for n in names] + [none])
    owner = value[np.concatenate([np.flatnonzero(lengths[n][1]) for n in names] + [none])]
    order = np.argsort(at, kind='stable')
    size, owner = counts[at[order]], owner[order]
    used = np.bincount(owner, weights=size, minlength=len(value_end)).astype(np.int64)
    left = value_end - string_start - used
    if left.any():
        v = int(np.argmax(left != 0))
        message = 'trailing bytes' if left[v] > 0 else 'string bytes run past the end of the value'
        raise UnpackError(int(index[v]), message)
    before = np.cumsum(size) - size
    start = np.empty(len(at), np.int64)
    start[order] = string_start[owner] + before - before[np.searchsorted(owner, owner)]
    out, k = {}, 0
    for n in names:
        _, has, t = lengths[n]
        m = int(has.sum())
        out[n] = _string_array(data, start[k : k + m], counts[at[k : k + m]], has, t)
        k += m
    return out


def _string_array(data, start, size, has, t) -> pa.Array:
    offsets = np.zeros(len(has) + 1, np.int64)
    offsets[1:][has] = size
    offsets = np.cumsum(offsets)
    taken = np.repeat(start - offsets[:-1][has], size) + np.arange(offsets[-1])
    validity = pa.py_buffer(np.packbits(has, bitorder='little'))
    buffers = [validity, pa.py_buffer(offsets), pa.py_buffer(data[taken])]
    return pa.Array.from_buffers(pa.large_string(), len(has), buffers).cast(t)


def _shapes(tok, walk: _Walk, record_start):
    rings = _sorted(walk.rings)
    if walk.depth == 3:
        polygons = _sorted(walk.polygons)
        geom_offsets = np.append(np.searchsorted(polygons, record_start), len(polygons))
        poly_offsets = np.append(np.searchsorted(rings, polygons), len(rings))
    else:
        geom_offsets = np.append(np.searchsorted(rings, record_start), len(rings))
        poly_offsets = np.arange(len(rings) + 1)
    n = walk.counts[rings]
    ring_offsets = np.concatenate([[0], np.cumsum(n)])
    # each ring's coordinates are the 2n varints after its count
    run = np.zeros(len(tok) + 1, np.int8)
    run[(rings + 1)[n > 0]] = 1
    run[(rings + 1 + 2 * n)[n > 0]] = -1
    step = _unzigzag(tok[np.cumsum(run[:-1], dtype=np.int8).astype(bool)])
    x, y = _ring_sums(step[0::2], ring_offsets), _ring_sums(step[1::2], ring_offsets)
    if walk.depth == 3:
        ring_offsets, x, y = _close(ring_offsets, x, y)
    return geom_offsets, poly_offsets, ring_offsets, x, y


def _ring_sums(step: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """Running sums of steps, restarting at each ring."""
    total = np.concatenate([[0], np.cumsum(step)])
    return total[1:] - np.repeat(total[offsets[:-1]], np.diff(offsets))


def _close(offsets, x, y):
    """Rings with their first vertex repeated at the end."""
    n = np.diff(offsets)
    closed = offsets + np.concatenate([[0], np.cumsum(n > 0)])
    at = np.arange(len(x)) + np.repeat(closed[:-1] - offsets[:-1], n)
    first, end = offsets[:-1][n > 0], closed[1:][n > 0] - 1
    out = []
    for v in (x, y):
        c = np.empty(closed[-1], v.dtype)
        c[at] = v
        c[end] = v[first]
        out.append(c)
    return closed, *out
