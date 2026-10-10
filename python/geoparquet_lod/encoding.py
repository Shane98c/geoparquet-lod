"""Payload encodings: a declared attribute block followed by geometry.

Every payload value is self-contained: the attributes a renderer needs for one
row, in the declared order and widths, then the row's geometry at the zoom
LOD's resolution. Fixed-width attributes are packed with numpy; variable
width ones (`utf8`, arrays) fall back to a row loop.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

POINT_GEOMETRY = 'point_f32'
POLYGON_GEOMETRY = 'polygon_varint'
LINE_GEOMETRY = 'line_varint'
NONE_GEOMETRY = 'none'

FIXED_DTYPES = {
    'f32': '<f4',
    'f64': '<f8',
    'i32': '<i4',
    'u32': '<u4',
    'i16': '<i2',
    'u16': '<u2',
    'u8': 'u1',
    'i8': 'i1',
}
VARIABLE_TYPES = {'utf8', 'f32[]', 'i32[]', 'u8[]'}


@dataclass(frozen=True)
class Attribute:
    """One carried column: `name` in the source table, `type` on the wire."""

    name: str
    type: str

    def __post_init__(self):
        if self.type not in FIXED_DTYPES and self.type not in VARIABLE_TYPES:
            raise ValueError(f'unknown attribute type {self.type!r} for {self.name}')


def attribute_dtype(attributes: list[Attribute]) -> np.dtype | None:
    """Structured dtype of the fixed-width attribute block, or None when any attribute varies."""
    if any(a.type in VARIABLE_TYPES for a in attributes):
        return None
    return np.dtype([(a.name, FIXED_DTYPES[a.type]) for a in attributes])


def pack_attributes(table: pa.Table, attributes: list[Attribute]) -> tuple[bytes, np.ndarray]:
    """Return (buffer, byte offsets per row) of the attribute block for every row."""
    n = table.num_rows
    dtype = attribute_dtype(attributes)
    if dtype is not None:
        block = np.empty(n, dtype=dtype)
        for a in attributes:
            column = table.column(a.name)
            if dtype[a.name].kind == 'f':
                block[a.name] = column.to_numpy(zero_copy_only=False)
            else:
                # integer nulls have no sentinel on the wire: they are written as 0
                block[a.name] = column.fill_null(0).to_numpy(zero_copy_only=False)
        width = dtype.itemsize
        return block.tobytes(), np.arange(n + 1, dtype=np.int64) * width
    out = bytearray()
    offsets = np.zeros(n + 1, dtype=np.int64)
    columns = {a.name: table.column(a.name).to_pylist() for a in attributes}
    for i in range(n):
        for a in attributes:
            v = columns[a.name][i]
            if a.type in FIXED_DTYPES:
                out += np.array([0 if v is None else v], dtype=FIXED_DTYPES[a.type]).tobytes()
            elif a.type == 'utf8':
                b = (v or '').encode()
                if len(b) > 255:
                    raise ValueError(f'{a.name} longer than 255 bytes')
                out += bytes([len(b)]) + b
            else:
                items = np.asarray(v if v is not None else [], dtype=FIXED_DTYPES[a.type[:-2]])
                if len(items) > 255:
                    raise ValueError(f'{a.name} has more than 255 items')
                out += bytes([len(items)]) + items.tobytes()
        offsets[i + 1] = len(out)
    return bytes(out), offsets


def zigzag(v: np.ndarray) -> np.ndarray:
    v = v.astype(np.int64)
    return ((v << 1) ^ (v >> 63)).astype(np.uint64)


def varint_bytes(values: np.ndarray, row_offsets: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """LEB128-encode a flat uint64 stream; return (bytes, byte offset per row)."""
    values = values.astype(np.uint64)
    nbytes = np.maximum(1, ((np.floor(np.log2(np.maximum(values, 1))).astype(np.int64)) // 7) + 1)
    out = np.zeros(int(nbytes.sum()), dtype=np.uint8)
    starts = np.concatenate([[0], np.cumsum(nbytes)[:-1]])
    v = values.copy()
    for k in range(10):
        active = nbytes > k
        if not active.any():
            break
        idx = starts[active] + k
        chunk = (v[active] & np.uint64(0x7F)).astype(np.uint8)
        more = nbytes[active] > k + 1
        out[idx] = chunk | (more.astype(np.uint8) << 7)
        v[active] >>= np.uint64(7)
    if len(values):
        per_row = np.add.reduceat(nbytes, row_offsets[:-1])
        per_row = np.where(row_offsets[1:] > row_offsets[:-1], per_row, 0)
    else:
        per_row = np.zeros(len(row_offsets) - 1, dtype=np.int64)
    return out, np.concatenate([[0], np.cumsum(per_row)])


def polygon_tree_bytes(parts) -> tuple[np.ndarray, np.ndarray]:
    """Varint polygon tree per row: n_polys, [n_rings, [n_verts, zigzag deltas of
    x,y with the first vertex absolute]]. `parts` are the ragged int arrays from
    quantize.render_polygons. Returns (bytes, byte offset per row)."""
    geom_offsets, poly_offsets, ring_offsets, x, y = parts
    n = len(geom_offsets) - 1
    dx = np.diff(x, prepend=0).astype(np.int64)
    dy = np.diff(y, prepend=0).astype(np.int64)
    ring_starts = ring_offsets[:-1]
    dx[ring_starts] = x[ring_starts]
    dy[ring_starts] = y[ring_starts]
    coords = np.empty(2 * len(x), dtype=np.uint64)
    coords[0::2] = zigzag(dx)
    coords[1::2] = zigzag(dy)
    n_polys = np.diff(geom_offsets)
    n_rings = np.diff(poly_offsets)
    n_verts = np.diff(ring_offsets)
    ring_blocks = np.concatenate([[0], np.cumsum(1 + 2 * n_verts)])
    ring_stream = np.empty(ring_blocks[-1], dtype=np.uint64)
    ring_stream[ring_blocks[:-1]] = n_verts
    mask = np.ones(ring_blocks[-1], dtype=bool)
    mask[ring_blocks[:-1]] = False
    ring_stream[mask] = coords
    poly_ring_len = (
        np.add.reduceat(1 + 2 * n_verts, poly_offsets[:-1])
        if len(n_rings)
        else np.zeros(0, np.int64)
    )
    poly_ring_len = np.where(n_rings > 0, poly_ring_len, 0)
    poly_len = 1 + poly_ring_len
    row_poly_len = (
        np.add.reduceat(poly_len, geom_offsets[:-1]) if len(n_polys) else np.zeros(n, np.int64)
    )
    row_poly_len = np.where(n_polys > 0, row_poly_len, 0)
    row_len = 1 + row_poly_len
    row_offsets = np.concatenate([[0], np.cumsum(row_len)])
    stream = np.empty(row_offsets[-1], dtype=np.uint64)
    stream[row_offsets[:-1]] = n_polys
    poly_pos = np.repeat(row_offsets[:-1] + 1, n_polys) + (
        np.concatenate([[0], np.cumsum(poly_len)[:-1]])
        - np.repeat(np.concatenate([[0], np.cumsum(row_poly_len)[:-1]]), n_polys)
    )
    stream[poly_pos] = n_rings
    ring_pos = np.repeat(poly_pos + 1, n_rings) + (
        ring_blocks[:-1] - np.repeat(np.concatenate([[0], np.cumsum(poly_ring_len)[:-1]]), n_rings)
    )
    idx = np.repeat(ring_pos, 1 + 2 * n_verts) + (
        np.arange(ring_blocks[-1]) - np.repeat(ring_blocks[:-1], 1 + 2 * n_verts)
    )
    stream[idx] = ring_stream
    return varint_bytes(stream, row_offsets)


def point_bytes(x: np.ndarray, y: np.ndarray) -> tuple[bytes, np.ndarray]:
    data = np.empty((len(x), 2), dtype='<f4')
    data[:, 0] = x
    data[:, 1] = y
    return data.tobytes(), np.arange(len(x) + 1, dtype=np.int64) * 8


def interleave(
    n: int,
    blocks: list[tuple[bytes | np.ndarray, np.ndarray]],
    mask: np.ndarray | None = None,
) -> pa.Array:
    """Concatenate per-row slices of several (buffer, offsets) blocks into one
    binary value per row; rows where `mask` is False become null."""
    keep = np.ones(n, dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    lengths = [np.diff(offsets) for _, offsets in blocks]
    row_len = np.sum(lengths, axis=0) if blocks else np.zeros(n, dtype=np.int64)
    row_len = np.where(keep, row_len, 0)
    out_offsets = np.concatenate([[0], np.cumsum(row_len)]).astype(np.int64)
    out = np.empty(out_offsets[-1], dtype=np.uint8)
    cursor = out_offsets[:-1].copy()
    for (buf, offsets), length in zip(blocks, lengths, strict=True):
        src = np.frombuffer(buf, dtype=np.uint8) if isinstance(buf, (bytes, bytearray)) else buf
        take = np.where(keep, length, 0)
        total = int(take.sum())
        if total:
            dst = np.repeat(cursor, take) + (
                np.arange(total) - np.repeat(np.concatenate([[0], np.cumsum(take)[:-1]]), take)
            )
            src_idx = np.repeat(offsets[:-1], take) + (
                np.arange(total) - np.repeat(np.concatenate([[0], np.cumsum(take)[:-1]]), take)
            )
            out[dst] = src[src_idx]
        cursor = cursor + take
    buffers = [
        None if mask is None else pa.array(keep).buffers()[1],
        pa.py_buffer(out_offsets.astype(np.int32)),
        pa.py_buffer(out),
    ]
    return pa.BinaryArray.from_buffers(
        pa.binary(), n, buffers, null_count=0 if mask is None else int(n - keep.sum())
    )


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError('truncated varint')
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def unzigzag(u: int) -> int:
    return (u >> 1) ^ -(u & 1)


def decode_attributes(buf: bytes, attributes: list[Attribute]) -> tuple[dict, int]:
    """Decode the attribute block; return (values, byte position after it)."""
    pos = 0
    out = {}
    for a in attributes:
        if a.type in FIXED_DTYPES:
            dt = np.dtype(FIXED_DTYPES[a.type])
            out[a.name] = np.frombuffer(buf, dt, 1, pos)[0].item()
            pos += dt.itemsize
        elif a.type == 'utf8':
            n = buf[pos]
            out[a.name] = buf[pos + 1 : pos + 1 + n].decode()
            pos += 1 + n
        else:
            dt = np.dtype(FIXED_DTYPES[a.type[:-2]])
            n = buf[pos]
            out[a.name] = np.frombuffer(buf, dt, n, pos + 1).tolist()
            pos += 1 + n * dt.itemsize
    return out, pos


def decode_polygon_tree(buf: bytes, pos: int) -> tuple[list, int]:
    """Decode the varint polygon tree into integer rings; return (polygons, end position)."""
    n_polys, pos = read_varint(buf, pos)
    polygons = []
    for _ in range(n_polys):
        n_rings, pos = read_varint(buf, pos)
        rings = []
        for _ in range(n_rings):
            n_verts, pos = read_varint(buf, pos)
            ring, x, y = [], 0, 0
            for k in range(n_verts):
                ux, pos = read_varint(buf, pos)
                uy, pos = read_varint(buf, pos)
                dx, dy = unzigzag(ux), unzigzag(uy)
                x, y = (dx, dy) if k == 0 else (x + dx, y + dy)
                ring.append((x, y))
            rings.append(ring)
        polygons.append(rings)
    return polygons, pos
