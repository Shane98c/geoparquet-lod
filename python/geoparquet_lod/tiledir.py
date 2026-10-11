"""Tile directory: a Parquet table beside the file mapping each tile to the pages it needs."""

from __future__ import annotations

import hashlib
import json
import struct
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

PROFILE = 'parquet-table-1'
KEY = 'lod_tile_directory'
SUFFIX = '.tiledir.parquet'
BUFFER = 64 / 4096
SCHEMA = pa.schema(
    [
        pa.field('zoom', pa.int8(), nullable=False),
        pa.field('tile', pa.int64(), nullable=False),
        pa.field(
            'pages', pa.list_(pa.field('element', pa.int64(), nullable=False)), nullable=False
        ),
    ]
)
ROW_GROUP_BYTES = 64 * 1024


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


def flatten(entries):
    """(tile, {group: [set of pages per leaf]}) items as tile ids and flat page sextuples."""
    tiles = np.fromiter((tile for tile, _ in entries), np.int64, len(entries))
    pages = []
    for _, groups in entries:
        flat = []
        for group, leaves in sorted(groups.items()):
            for leaf, located in enumerate(leaves):
                for start, end, byte_start, byte_end in sorted(located):
                    flat += (group, leaf, start, end - start, byte_start, byte_end - byte_start)
        pages.append(flat)
    return tiles, pages


def write_directory(out, chunks, metadata, row_group_bytes=ROW_GROUP_BYTES):
    """Write (zoom, tiles, pages) chunks in (zoom, tile) order, in row groups of about
    `row_group_bytes` that never span zooms: footer statistics on zoom and tile pick the
    group, and a lookup reads it whole in one range."""
    options = dict(
        compression='zstd',
        use_dictionary=False,
        # interleaved sextuples defeat delta coding; plain values compress about a third smaller
        column_encoding={
            'zoom': 'DELTA_BINARY_PACKED',
            'tile': 'DELTA_BINARY_PACKED',
            'pages.list.element': 'PLAIN',
        },
        write_statistics=['zoom', 'tile'],
        write_page_index=False,
        data_page_size=16384,
        data_page_version='1.0',
    )
    with pq.ParquetWriter(out, SCHEMA, **options) as writer:
        for zoom, tiles, pages in chunks:
            if not len(tiles):
                continue
            if np.any(np.diff(tiles) <= 0):
                raise ValueError('tile ids must increase within a zoom')
            table = pa.table(
                [
                    pa.array(np.full(len(tiles), zoom), pa.int8()),
                    pa.array(tiles),
                    pa.array(pages, SCHEMA.field('pages').type),
                ],
                schema=SCHEMA,
            )
            for a, b in _byte_groups(table, row_group_bytes, options):
                writer.write_table(table.slice(a, b - a), row_group_size=b - a)
        # serialized after the chunks are consumed, so a builder can fill it while writing
        writer.add_key_value_metadata({KEY: json.dumps(metadata, separators=(',', ':'))})
    return out


def _byte_groups(table, target, options):
    """Row slices of about `target` compressed bytes, from the compressed size of a sample."""
    values = pc.list_value_length(table['pages']).to_numpy() + 2
    # contiguous runs spread over the zoom keep the locality the real groups have
    starts = np.unique(np.linspace(0, max(0, table.num_rows - 1000), 20).astype(int))
    picked = np.unique(
        np.concatenate([np.arange(a, min(a + 1000, table.num_rows)) for a in starts])
    )
    sink = pa.BufferOutputStream()
    pq.write_table(table.take(picked), sink, **options)
    per_value = sink.getvalue().size / max(1, values[picked].sum())
    bounds = [0]
    total = 0.0
    for i, v in enumerate(values):
        total += v * per_value
        if total >= target and i + 1 < len(values):
            bounds.append(i + 1)
            total = 0.0
    bounds.append(len(values))
    return list(zip(bounds[:-1], bounds[1:], strict=True))


def sidecar_path(path):
    return Path(str(path) + SUFFIX)


def _footer(file):
    file.seek(-8, 2)
    footer_length, magic = struct.unpack('<I4s', file.read(8))
    if magic != b'PAR1':
        raise ValueError('invalid Parquet trailer')
    file.seek(-8 - footer_length, 2)
    return file.read(footer_length)


def source_binding(path):
    """The write of the data file a sidecar is bound to: size, footer length and footer hash."""
    with Path(path).open('rb') as file:
        return _binding(path, _footer(file))


def _binding(path, footer):
    return dict(
        file_size=Path(path).stat().st_size,
        footer_length=len(footer),
        footer_sha256=hashlib.sha256(footer).hexdigest(),
    )


def _page_locations(path):
    with Path(path).open('rb') as file:
        raw = _footer(file)
        footer = Compact(raw).value(12)
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
    return _binding(path, raw), indexes


def build(path, out=None, row_group_bytes=ROW_GROUP_BYTES):
    """Build the sidecar from Arrow bbox/null masks and Thrift footer/offset-index page locations."""
    path = Path(path)
    out = Path(out) if out else sidecar_path(path)
    if out.resolve() == path.resolve():
        raise ValueError('sidecar output must differ from the Parquet input')
    parquet = pq.ParquetFile(path)
    geo = json.loads(parquet.metadata.metadata[b'geo'])
    metadata = geo['lod_columns']
    if metadata['tile_size'] != 512 or not is_crs84(geo['columns'][geo['primary_column']]):
        raise ValueError('a tile directory requires 512px tiles and OGC:CRS84')
    source, indexes = _page_locations(path)

    all_paths = [parquet.schema.column(i).path for i in range(len(parquet.schema))]
    prefix = metadata.get('prefix')
    directories = []

    def chunks():
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
                raise ValueError('a tile directory supports zooms <= 24')
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
                tiles, flat = flatten(sorted(entries.pop(zoom).items()))
                directories.append(dict(lod=name, zoom=zoom, paths=paths, entries=len(tiles)))
                yield zoom, tiles, flat

    header = directory_metadata(source, directories)
    write_directory(out, chunks(), header, row_group_bytes)
    return dict(path=str(out), size=out.stat().st_size, **header)


def directory_metadata(source, directories):
    return dict(
        profile=PROFILE,
        tile_size=512,
        buffer=BUFFER,
        slop_cells=1,
        tile_order='morton',
        source=source,
        directories=directories,
    )
