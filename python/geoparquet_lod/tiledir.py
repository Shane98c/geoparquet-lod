"""Experimental v1 tile directory sidecars; not part of the LOD specification."""

from __future__ import annotations

import gzip
import json
import struct
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

MAGIC = b'LODTDIR1'
OPEN_BYTES = 65536
BUFFER = 64 / 4096


class Compact:
    """The Thrift compact types used by Parquet metadata and offset indexes."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, n):
        result = self.data[self.pos : self.pos + n]
        if len(result) != n:
            raise ValueError('truncated Thrift')
        self.pos += n
        return result

    def uint(self):
        value = shift = 0
        while True:
            byte = self.take(1)[0]
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
            if shift > 70:
                raise ValueError('invalid Thrift varint')

    def value(self, kind):
        if kind in (1, 2):
            return kind == 1
        if kind == 3:
            return self.take(1)[0]
        if kind in (4, 5, 6):
            n = self.uint()
            return (n >> 1) ^ -(n & 1)
        if kind == 7:
            return struct.unpack('<d', self.take(8))[0]
        if kind == 8:
            return self.take(self.uint())
        if kind in (9, 10):
            head = self.take(1)[0]
            count = head >> 4
            if count == 15:
                count = self.uint()
            item = head & 15
            return [
                self.take(1)[0] == 1 if item in (1, 2) else self.value(item) for _ in range(count)
            ]
        if kind == 12:
            result = {}
            field = 0
            while head := self.take(1)[0]:
                field = field + (head >> 4) if head >> 4 else self.value(4)
                result[field] = self.value(head & 15)
            return result
        raise ValueError(f'unsupported Thrift compact type {kind}')


def varint(value):
    if value < 0:
        raise ValueError('negative unsigned varint')
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return out


def is_crs84(column):
    """GeoParquet: an omitted `crs` is OGC:CRS84, an explicit null is unknown."""
    if 'crs' not in column:
        return True
    crs = column['crs']
    if not isinstance(crs, dict):
        return False
    ident = crs.get('id') or {}
    authority, code = str(ident.get('authority', '')).upper(), str(ident.get('code', ''))
    return (authority, code) in (('OGC', 'CRS84'), ('EPSG', '4326'))


def morton(x, y, z):
    return sum(((x >> i) & 1) << (2 * i) | ((y >> i) & 1) << (2 * i + 1) for i in range(z))


def tile_spans(bounds, zoom, scale):
    """Inclusive tile rectangles covering each bbox row, padded by the buffer and one cell."""
    n = 2**zoom
    bounds = np.asarray(bounds, dtype=np.float64)
    sx, sy = map(abs, scale)
    limit = 85.0511287798066

    def ty(lat):
        lat = np.radians(np.clip(lat, -limit, limit))
        return (1 - np.arcsinh(np.tan(lat)) / np.pi) / 2 * n

    # ceil(left - 1) retains exact boundary contacts on both sides.
    left = np.ceil((bounds[:, 0] - sx + 180) / 360 * n - BUFFER - 1)
    right = np.floor((bounds[:, 2] + sx + 180) / 360 * n + BUFFER)
    top = np.ceil(ty(bounds[:, 3] + sy) - BUFFER - 1)
    bottom = np.floor(ty(bounds[:, 1] - sy) + BUFFER)
    return np.column_stack(
        [
            np.maximum(0, left),
            np.maximum(0, top),
            np.minimum(n - 1, right),
            np.minimum(n - 1, bottom),
        ]
    ).astype(np.int64)


def tile_span(bbox, zoom, scale):
    return tuple(int(v) for v in tile_spans([bbox], zoom, scale)[0])


def encode_entries(entries, leaf_count):
    out = bytearray(varint(len(entries)))
    previous = 0
    for tile, groups in entries:
        out += varint(tile - previous)
        previous = tile
        out += varint(len(groups))
        for group, leaves in sorted(groups.items()):
            out += varint(group)
            for leaf in range(leaf_count):
                pages = sorted(leaves[leaf])
                out += varint(len(pages))
                row = offset = 0
                for start, end, byte_start, byte_end in pages:
                    out += varint(start - row) + varint(end - start)
                    out += varint(byte_start - offset) + varint(byte_end - byte_start)
                    row, offset = start, byte_start
    return bytes(out)


def append_blocks(items, leaf_count, payload, blocks):
    encoded = encode_entries(items, leaf_count)
    compressed = gzip.compress(encoded, mtime=0)
    if len(compressed) > 20 * 1024 and len(items) > 1:
        middle = len(items) // 2
        append_blocks(items[:middle], leaf_count, payload, blocks)
        append_blocks(items[middle:], leaf_count, payload, blocks)
        return
    blocks.append((items[0][0], items[-1][0], len(payload), len(compressed), len(encoded)))
    payload.extend(compressed)


def build(path, out=None):
    """Build from Arrow bbox/null masks and Thrift footer/offset-index page locations."""
    path = Path(path)
    out = Path(out) if out else Path(str(path) + '.tiledir')
    if out.resolve() == path.resolve():
        raise ValueError('sidecar output must differ from the Parquet input')
    parquet = pq.ParquetFile(path)
    geo = json.loads(parquet.metadata.metadata[b'geo'])
    metadata = geo['lod_columns']
    if metadata['tile_size'] != 512 or not is_crs84(geo['columns'][geo['primary_column']]):
        raise ValueError('experimental directory requires 512px tiles and OGC:CRS84')
    with path.open('rb') as file:
        file.seek(-8, 2)
        footer_length, magic = struct.unpack('<I4s', file.read(8))
        if magic != b'PAR1':
            raise ValueError('invalid Parquet trailer')
        file.seek(-8 - footer_length, 2)
        footer = Compact(file.read(footer_length)).value(12)
        indexes = []
        for group in footer[4]:
            leaves = {}
            for chunk in group[1]:
                name = b'.'.join(chunk[3][3]).decode()
                if 4 not in chunk or 5 not in chunk:
                    raise ValueError(f'missing offset index: {name}')
                file.seek(chunk[4])
                locations = Compact(file.read(chunk[5])).value(12)[1]
                pages = [
                    (
                        p[3],
                        locations[i + 1][3] if i + 1 < len(locations) else group[3],
                        p[1],
                        p[1] + p[2],
                    )
                    for i, p in enumerate(locations)
                ]
                if any(p[0] >= p[1] for p in pages):
                    raise ValueError('pages splitting a repeated row are unsupported')
                leaves[name] = pages
            indexes.append(leaves)

    lods = []
    payload = bytearray()
    root = bytearray()
    all_paths = [parquet.schema.column(i).path for i in range(len(parquet.schema))]
    prefix = metadata.get('prefix')
    for name, lod in metadata['lods'].items():
        column = f'{metadata["column"]}.{name}.'
        geometry = [p for p in all_paths if p.startswith(column + 'geometry.')]
        paths = geometry + [column + a['name'] for a in metadata['attributes']]
        if len(geometry) != 1:
            raise ValueError('expected one interleaved geometry leaf')
        # the prefix row groups already serve the coarse zooms in one or two reads
        zooms = [z for z in lod['zooms'] if not prefix or z > prefix['zoom']]
        if not zooms:
            continue
        if max(zooms) > 24:
            raise ValueError('experimental directory supports finest zoom <= 24')
        entries = {z: defaultdict(dict) for z in zooms}
        for g in range(parquet.num_row_groups):
            table = parquet.read_row_group(g, columns=['bbox', f'{metadata["column"]}.{name}'])
            bbox = table['bbox'].combine_chunks()
            bounds = np.column_stack(
                [
                    bbox.field(k).to_numpy(zero_copy_only=False)
                    for k in ('xmin', 'ymin', 'xmax', 'ymax')
                ]
            )
            shown = (
                table[metadata['column']]
                .combine_chunks()
                .field(name)
                .is_valid()
                .to_numpy(zero_copy_only=False)
            )
            rows = np.flatnonzero(shown & np.isfinite(bounds).all(axis=1))
            pages = [indexes[g][p] for p in paths]
            page_ids = [np.searchsorted([p[1] for p in ps], rows, side='right') for ps in pages]
            spans = {z: tile_spans(bounds[rows], z, lod['scale']) for z in zooms}
            for i in range(len(rows)):
                selected = [ps[ids[i]] for ps, ids in zip(pages, page_ids, strict=True)]
                for z in zooms:
                    x0, y0, x1, y1 = spans[z][i]
                    for x in range(x0, x1 + 1):
                        for y in range(y0, y1 + 1):
                            group = entries[z][morton(x, y, z)].setdefault(
                                g, [set() for _ in paths]
                            )
                            for leaf, page in zip(group, selected, strict=True):
                                leaf.add(page)
        for zoom in zooms:
            ordered = sorted(entries[zoom].items())
            blocks = []
            if ordered:
                append_blocks(ordered, len(paths), payload, blocks)
            root_start = len(root)
            root += varint(len(blocks))
            tile = offset = 0
            for first, last, start, size, _ in blocks:
                root += varint(first - tile) + varint(last - first)
                root += varint(start - offset) + varint(size)
                tile, offset = first, start
            lods.append(
                dict(
                    name=name,
                    zoom=zoom,
                    paths=paths,
                    entries=len(ordered),
                    root_offset=root_start,
                    compressed_bytes=sum(b[3] for b in blocks),
                    raw_bytes=sum(b[4] for b in blocks),
                    blocks=len(blocks),
                )
            )
    header = dict(
        experimental=True,
        version=1,
        file_size=path.stat().st_size,
        footer_length=footer_length,
        tile_size=512,
        buffer=BUFFER,
        slop_cells=1,
        codec='gzip',
        tile_order='morton',
        lods=lods,
        root_bytes=len(root),
    )
    encoded_header = json.dumps(header, separators=(',', ':')).encode()
    used = 16 + len(encoded_header) + len(root)
    if used > OPEN_BYTES:
        raise ValueError(f'header + root needs {used} bytes; experimental limit is {OPEN_BYTES}')
    with out.open('wb') as file:
        file.write(MAGIC + struct.pack('<II', len(encoded_header), len(root)))
        file.write(encoded_header)
        file.write(root)
        file.write(bytes(OPEN_BYTES - used))
        file.write(payload)
    return dict(
        path=str(out),
        size=out.stat().st_size,
        open_bytes=OPEN_BYTES,
        header_bytes=len(encoded_header),
        **header,
    )
