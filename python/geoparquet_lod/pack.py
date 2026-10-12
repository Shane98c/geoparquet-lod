"""One packed blob per row per LOD.

A row's LOD value is a byte string a tile reads in one run: its geometry on
the LOD's grid, its carried attributes, and on a host row the straddler
copies it carries. Everything is an unsigned LEB128 varint except string
bytes, which follow all of a row's varints in order, so a reader walks the
varints knowing the structure and then takes string payloads in turn.

    row     := varints(own) varints(attrs) [varints(spill)] strings...
    own     := polygons: n, per polygon: rings, per ring: vertices, then
               zigzag(dx) zigzag(dy) per vertex, deltas within the ring and
               the first vertex from the origin
               lines: n parts, per part: vertices, deltas
               point: zigzag(x) zigzag(y)
    attrs   := presence bitmap (one varint, bit i for attribute i), then each
               present value: integers zigzag, float32 as its bit pattern,
               float64 as its bit pattern, bool 0/1, string as its byte length
    spill   := count, per copy: tile (zoom << 48 | x * 2^zoom + y), file row
               index, own, attrs

A row shown only to host copies has zero polygons. Empty geometry is
otherwise never written.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

FAMILY_DEPTH = {'Point': 0, 'MultiLineString': 2, 'MultiPolygon': 3}


def zigzag(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.int64)
    return ((v << 1) ^ (v >> 63)).astype(np.uint64)


def unzigzag(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


def varint_bytes(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """LEB128 bytes of unsigned `values` in order, and each value's byte count."""
    v = np.asarray(values, dtype=np.uint64)
    cols = []
    for j in range(10):
        shifted = v >> np.uint64(7 * j)
        more = (shifted >> np.uint64(7)) != 0
        cols.append(
            ((shifted & np.uint64(0x7F)) | (more.astype(np.uint64) << np.uint64(7))).astype(
                np.uint8
            )
        )
    matrix = np.stack(cols, axis=1)
    valid = np.ones_like(matrix, dtype=bool)
    for j in range(1, 10):
        valid[:, j] = (v >> np.uint64(7 * j)) != 0
    return matrix[valid], valid.sum(axis=1).astype(np.int64)


def _segment_values(parts, depth: int):
    """Depth-first varint values of ragged geometry over its rows, and the
    number of values per row. `parts` are (geom offsets, poly offsets, ring
    offsets, x, y) as quantize.render_* returns them."""
    geom_offsets, poly_offsets, ring_offsets, x, y = (np.asarray(p) for p in parts)
    n_rings = len(ring_offsets) - 1
    verts_per_ring = np.diff(ring_offsets)
    ring_len = 1 + 2 * verts_per_ring
    if depth == 3:
        rings_per_poly = np.diff(poly_offsets)
        poly_len = 1 + _group_sum(ring_len, poly_offsets)
        polys_per_row = np.diff(geom_offsets)
        row_len = 1 + _group_sum(poly_len, geom_offsets)
        # positions
        row_start = np.concatenate([[0], np.cumsum(row_len)])
        poly_start = np.repeat(row_start[:-1] + 1, polys_per_row) + _within(poly_len, geom_offsets)
        ring_start = np.repeat(poly_start + 1, rings_per_poly) + _within(ring_len, poly_offsets)
        total = int(row_start[-1])
        out = np.zeros(total, np.uint64)
        out[row_start[:-1]] = polys_per_row
        out[poly_start] = rings_per_poly
    else:
        parts_per_row = np.diff(geom_offsets)
        row_len = 1 + _group_sum(ring_len, geom_offsets)
        row_start = np.concatenate([[0], np.cumsum(row_len)])
        ring_start = np.repeat(row_start[:-1] + 1, parts_per_row) + _within(ring_len, geom_offsets)
        total = int(row_start[-1])
        out = np.zeros(total, np.uint64)
        out[row_start[:-1]] = parts_per_row
    out[ring_start] = verts_per_ring
    # vertices: ring-relative deltas, zigzag
    ring_of_vertex = np.repeat(np.arange(n_rings), verts_per_ring)
    dx = np.diff(x.astype(np.int64), prepend=np.int64(0))
    dy = np.diff(y.astype(np.int64), prepend=np.int64(0))
    starts = ring_offsets[:-1]
    dx[starts] = x[starts]
    dy[starts] = y[starts]
    k = np.arange(len(x)) - np.repeat(starts, verts_per_ring)
    vpos = ring_start[ring_of_vertex] + 1 + 2 * k
    out[vpos] = zigzag(dx)
    out[vpos + 1] = zigzag(dy)
    return out, row_len


def _group_sum(values: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """Sum of each group of consecutive `values` delimited by `offsets`; empty groups give 0."""
    cum = np.concatenate([[0], np.cumsum(np.asarray(values, dtype=np.int64))])
    offsets = np.asarray(offsets)
    return cum[offsets[1:]] - cum[offsets[:-1]]


def _within(seg_len: np.ndarray, group_offsets: np.ndarray) -> np.ndarray:
    """Start of each segment within its group: exclusive cumsum restarting per group."""
    cum = np.concatenate([[0], np.cumsum(np.asarray(seg_len, dtype=np.int64))])
    group_offsets = np.asarray(group_offsets)
    group_base = np.repeat(cum[group_offsets[:-1]], np.diff(group_offsets))
    return cum[:-1] - group_base


def _point_values(x: np.ndarray, y: np.ndarray):
    out = np.empty(2 * len(x), np.uint64)
    out[0::2] = zigzag(x)
    out[1::2] = zigzag(y)
    return out, np.full(len(x), 2, np.int64)


def _attr_values(fields: list[pa.Field], arrays: dict[str, pa.Array], n: int):
    """Varint values of the attribute block per row, values per row, and the
    string payload bytes per row (lengths and a flat byte buffer)."""
    present = np.zeros(n, np.uint64)
    blocks = []  # (values aligned per row where present, mask)
    str_len = np.zeros(n, np.int64)
    str_chunks: list[np.ndarray] = []
    for i, f in enumerate(fields):
        arr = arrays[f.name]
        valid = arr.is_valid().to_numpy(zero_copy_only=False)
        present |= valid.astype(np.uint64) << np.uint64(i)
        t = f.type
        if pa.types.is_integer(t):
            vals = zigzag(np.where(valid, arr.fill_null(0).to_numpy(zero_copy_only=False), 0))
        elif pa.types.is_floating(t):
            if pa.types.is_float64(t):
                vals = (
                    arr.fill_null(0.0)
                    .to_numpy(zero_copy_only=False)
                    .astype(np.float64)
                    .view(np.uint64)
                )
            else:
                vals = (
                    arr.fill_null(0.0)
                    .to_numpy(zero_copy_only=False)
                    .astype(np.float32)
                    .view(np.uint32)
                    .astype(np.uint64)
                )
        elif pa.types.is_boolean(t):
            vals = arr.fill_null(False).to_numpy(zero_copy_only=False).astype(np.uint64)
        elif pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_binary(t):
            b = arr.cast(pa.large_binary()).fill_null(b'')
            offsets = np.frombuffer(b.buffers()[1], np.int64)[b.offset : b.offset + len(b) + 1]
            data = np.frombuffer(b.buffers()[2], np.uint8)[offsets[0] : offsets[-1]]
            lengths = np.diff(offsets).astype(np.int64)
            vals = lengths.astype(np.uint64)
            str_len += lengths
            str_chunks.append((data, lengths))
        else:
            raise TypeError(f'cannot pack attribute {f.name} of type {t}')
        blocks.append((vals, valid))
    # values per row: bitmap plus one per present attribute
    count = 1 + sum(b[1].astype(np.int64) for b in blocks) if blocks else np.ones(n, np.int64)
    total = int(count.sum())
    out = np.empty(total, np.uint64)
    row_start = np.concatenate([[0], np.cumsum(count)])
    out[row_start[:-1]] = present
    slot = row_start[:-1] + 1
    for vals, valid in blocks:
        out[slot[valid]] = vals[valid]
        slot = slot + valid
    # string bytes per row, attributes in order
    strings = np.empty(int(str_len.sum()), np.uint8)
    base = np.concatenate([[0], np.cumsum(str_len)])[:-1]
    before = np.zeros(n, np.int64)
    for data, lengths in str_chunks:
        _scatter(strings, base + before, data, lengths)
        before += lengths
    return out, count, str_len, strings


def pack(
    family: str,
    parts,
    fields: list[pa.Field],
    attrs: dict[str, pa.Array],
    spill=None,
) -> pa.Array:
    """Packed blobs for one batch: `parts` the rows' geometry (ragged arrays
    for shapes, (x, y) for points), `attrs` the carried attribute arrays, and
    `spill` optionally (counts per row, tile, id, parts, attrs) for the copies
    hosted here, entries in row order. Returns a non-null binary array; the
    caller masks rows not shown."""
    depth = FAMILY_DEPTH[family]
    n = len(parts[0]) if family == 'Point' else len(parts[0]) - 1
    own, own_len = _point_values(*parts) if family == 'Point' else _segment_values(parts, depth)
    av, a_len, s_len, strings = _attr_values(fields, attrs, n)
    blocks = [(own, own_len), (av, a_len)]
    if spill is None:
        spill = (
            np.zeros(n, np.int64),
            np.zeros(0, np.int64),
            np.zeros(0, np.int64),
            _empty_parts(family),
            {f.name: pa.nulls(0, f.type) for f in fields},
        )
    if True:
        counts, tile, ids, sparts, sattrs = spill
        m = len(tile)
        sown, sown_len = (
            _point_values(*sparts) if family == 'Point' else _segment_values(sparts, depth)
        )
        sav, sa_len, ss_len, sstrings = _attr_values(fields, sattrs, m)
        # per entry: tile, id, own, attrs
        e_len = 2 + sown_len + sa_len
        e_start = np.concatenate([[0], np.cumsum(e_len)])
        ev = np.zeros(int(e_start[-1]), np.uint64)
        ev[e_start[:-1]] = tile.astype(np.uint64)
        ev[e_start[:-1] + 1] = ids.astype(np.uint64)
        _scatter(ev, e_start[:-1] + 2, sown, sown_len)
        _scatter(ev, e_start[:-1] + 2 + sown_len, sav, sa_len)
        # per row: count, then its entries
        row_entry_start = np.concatenate([[0], np.cumsum(counts)])
        sp_len = 1 + (_group_sum(e_len, row_entry_start) if m else np.zeros(n, np.int64)) * (
            counts > 0
        )
        sp_len = np.where(counts > 0, sp_len, 1)
        sp = np.zeros(int(sp_len.sum()), np.uint64)
        sp_start = np.concatenate([[0], np.cumsum(sp_len)])
        sp[sp_start[:-1]] = counts
        if m:
            entry_dest = np.repeat(sp_start[:-1] + 1, counts) + _within(e_len, row_entry_start)
            _scatter(sp, entry_dest, ev, e_len)
        blocks.append((sp, sp_len))
        s_len = s_len + (_group_sum(ss_len, row_entry_start) * (counts > 0) if m else 0)
        strings = _merge_strings(
            n,
            strings,
            s_len - (_group_sum(ss_len, row_entry_start) * (counts > 0) if m else 0),
            sstrings,
            ss_len,
            counts,
        )
    # concatenate blocks per row
    row_len = sum(b[1] for b in blocks)
    row_start = np.concatenate([[0], np.cumsum(row_len)])
    values = np.empty(int(row_start[-1]), np.uint64)
    offset = np.zeros(n, np.int64)
    for vals, lens in blocks:
        _scatter(values, row_start[:-1] + offset, vals, lens)
        offset += lens
    vb, vcount = varint_bytes(values)
    vbytes_per_row = _group_sum(vcount, row_start) if len(vcount) else np.zeros(n, np.int64)
    vbytes_per_row = np.where(row_len > 0, vbytes_per_row, 0)
    total = vbytes_per_row + s_len
    out_off = np.concatenate([[0], np.cumsum(total)])
    out = np.empty(int(out_off[-1]), np.uint8)
    _scatter(out, out_off[:-1], vb, vbytes_per_row)
    _scatter(out, out_off[:-1] + vbytes_per_row, strings, s_len)
    return pa.BinaryArray.from_buffers(
        pa.binary(), n, [None, pa.py_buffer(out_off.astype(np.int32)), pa.py_buffer(out)]
    )


def _empty_parts(family: str):
    if family == 'Point':
        return (np.zeros(0, np.int64), np.zeros(0, np.int64))
    z = np.zeros(1, np.int64)
    return (z, z, z, np.zeros(0, np.int32), np.zeros(0, np.int32))


def _scatter(dest: np.ndarray, dest_start: np.ndarray, src: np.ndarray, lens: np.ndarray) -> None:
    """Copy consecutive `src` segments of `lens` to `dest` at `dest_start`."""
    lens = np.asarray(lens, dtype=np.int64)
    total = int(lens.sum())
    if not total:
        return
    idx = np.repeat(dest_start - np.concatenate([[0], np.cumsum(lens)[:-1]]), lens) + np.arange(
        total
    )
    dest[idx] = src[:total]


def _merge_strings(n, own_strings, own_len, spill_strings, spill_len, counts):
    """Each row's string bytes: its own, then its hosted copies' in order."""
    if not len(spill_strings) and not len(own_strings):
        return np.zeros(0, np.uint8)
    row_entry_start = np.concatenate([[0], np.cumsum(counts)])
    sp_row = (
        _group_sum(spill_len, row_entry_start) if len(spill_len) else np.zeros(n, np.int64)
    ) * (counts > 0)
    total = own_len + sp_row
    out = np.empty(int(total.sum()), np.uint8)
    out_off = np.concatenate([[0], np.cumsum(total)])
    _scatter(out, out_off[:-1], own_strings, own_len)
    if len(spill_strings):
        entry_dest = np.repeat(out_off[:-1] + own_len, counts) + _within(spill_len, row_entry_start)
        _scatter(out, entry_dest, spill_strings, spill_len)
    return out


class Unpacker:
    """Decoder of one packed row, for validation and tests."""

    def __init__(self, blob: bytes, family: str, fields: list[pa.Field]):
        self.b = blob
        self.pos = 0
        self.family = family
        self.fields = fields
        self.strings: list[tuple[int, int]] = []  # (slot, length) in order

    def varint(self) -> int:
        shift = 0
        out = 0
        while True:
            byte = self.b[self.pos]
            self.pos += 1
            out |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return out
            shift += 7

    def geometry(self):
        if self.family == 'Point':
            return (unzigzag(self.varint()), unzigzag(self.varint()))
        out = []
        if self.family == 'MultiPolygon':
            for _ in range(self.varint()):
                out.append([self.ring() for _ in range(self.varint())])
        else:
            for _ in range(self.varint()):
                out.append(self.ring())
        return out

    def ring(self):
        n = self.varint()
        ring, x, y = [], 0, 0
        for _ in range(n):
            x += unzigzag(self.varint())
            y += unzigzag(self.varint())
            ring.append((x, y))
        return ring

    def attrs(self):
        present = self.varint()
        out = {}
        for i, f in enumerate(self.fields):
            if not present >> i & 1:
                out[f.name] = None
                continue
            v = self.varint()
            t = f.type
            if pa.types.is_integer(t):
                out[f.name] = unzigzag(v)
            elif pa.types.is_float64(t):
                out[f.name] = float(np.array([v], np.uint64).view(np.float64)[0])
            elif pa.types.is_floating(t):
                out[f.name] = float(np.array([v], np.uint32).view(np.float32)[0])
            elif pa.types.is_boolean(t):
                out[f.name] = bool(v)
            else:
                out[f.name] = ('string', v)
                self.strings.append(f.name)
        return out

    def row(self):
        geometry = self.geometry()
        attrs = self.attrs()
        spill = []
        if True:
            for _ in range(self.varint()):
                tile = self.varint()
                rid = self.varint()
                spill.append(
                    {'tile': tile, 'id': rid, 'geometry': self.geometry(), 'attrs': self.attrs()}
                )
        # string payloads follow, in order
        for record in [attrs, *[s['attrs'] for s in spill]]:
            for name, v in record.items():
                if isinstance(v, tuple) and v[0] == 'string':
                    n = v[1]
                    record[name] = self.b[self.pos : self.pos + n].decode('utf-8', 'replace')
                    self.pos += n
        return {'geometry': geometry, 'attrs': attrs, 'spill': spill}
