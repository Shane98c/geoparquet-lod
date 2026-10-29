"""Exhaustive spatial-directory checks against ordinary Parquet page indexes."""

import gzip
import json
import tempfile
from pathlib import Path

import numpy as np
import pyarrow as pa

from ._compact import dumps, get, loads, loads_prefix
from ._layout import (
    BOOTSTRAP_BYTES,
    CODECS,
    FENCE_FIELDS,
    MAGIC,
    PAGE_FIELDS,
    VERSION,
    grid_zoom,
    page_cells,
    read_node,
    row_metadata,
)


def check_index(path, parquet, lod):
    """Return normally for a file without the spatial index; raise ValueError on corruption."""
    size = Path(path).stat().st_size
    with open(path, 'rb') as source, tempfile.TemporaryFile() as key_store:
        source.seek(-8, 2)
        footer_size = int.from_bytes(source.read(4), 'little')
        footer_start = size - 8 - footer_size
        source.seek(footer_start)
        metadata = loads(source.read(footer_size))

        def within(start, length):
            start, length = np.asarray(start), np.asarray(length)
            if (
                start.dtype.kind not in 'iu'
                or length.dtype.kind not in 'iu'
                or (start < 4).any()
                or (length <= 0).any()
                or (start + length > footer_start).any()
            ):
                raise ValueError('index byte range outside data region')

        def read(start, length):
            within(start, length)
            source.seek(start)
            raw = source.read(length)
            if len(raw) != length:
                raise ValueError('truncated index range')
            return raw

        # Validate all standard offset indexes, including source columns.
        groups = get(metadata, 4)[1]
        starts, first, locations = [], 0, {name: [] for name in lod['lods']}
        for g, group in enumerate(groups):
            starts.append(first)
            count = get(group, 3)
            for chunk in get(group, 1)[1]:
                cm = get(chunk, 3)
                path_parts = [part.decode() for part in get(cm, 3)[1]]
                pages = get(loads(read(get(chunk, 4), get(chunk, 5))), 1)[1]
                byte, length, row = (
                    np.array([get(page, k) for page in pages], np.int64) for k in (1, 2, 3)
                )
                within(byte, length)
                # page i holds rows bounds[i]:bounds[i + 1]
                bounds = np.r_[row, count]
                if (
                    bounds[0] != 0
                    or (np.diff(bounds) < 0).any()
                    or (byte[1:] < (byte + length)[:-1]).any()
                ):
                    raise ValueError(f'row group {g}: invalid offset-index page ordering')
                if len(path_parts) == 2 and path_parts[0] == lod['column']:
                    locations[path_parts[1]].append((byte, length, bounds, cm))
            first += count

        source.seek(4)
        magic = source.read(8)
        # The footer's key/value metadata, which Arrow's schema metadata need not include.
        advertised = (parquet.metadata.metadata or {}).get(b'lod_spatial_index')
        if magic != MAGIC:
            if advertised is not None:
                raise ValueError('spatial index advertised but bootstrap magic missing')
            return
        if advertised is None or json.loads(advertised) != {
            'version': VERSION,
            'offset': 4,
            'length': BOOTSTRAP_BYTES - 4,
        }:
            raise ValueError('spatial index advertisement missing or of another version')
        root_length = int.from_bytes(source.read(4), 'little')
        if not 0 < root_length <= BOOTSTRAP_BYTES - 16:
            raise ValueError('invalid spatial root length')
        root = json.loads(gzip.decompress(read(16, root_length)))
        if root.get('version') != VERSION or root.get('file_bytes') != size:
            raise ValueError('spatial root version or file length disagrees with footer')
        if root.get('lod') != lod:
            raise ValueError('spatial root metadata disagrees with footer')
        geo = json.loads(parquet.schema_arrow.metadata[b'geo'])
        if root.get('primary') != geo.get('primary_column', 'geometry'):
            raise ValueError('spatial root primary disagrees with footer')
        if root.get('footer_bytes', footer_size) != footer_size:
            raise ValueError('spatial root footer_bytes disagrees with footer')
        if 'row_metadata' in root:
            offset, length = root['row_metadata']
            flat = [f.name for f in parquet.schema_arrow if not pa.types.is_nested(f.type)]
            if gzip.decompress(read(offset, length)) != dumps(row_metadata(metadata, flat)):
                raise ValueError('row metadata disagrees with footer')
        directories = root.get('directories')
        if not isinstance(directories, dict) or set(directories) != set(lod['lods']):
            raise ValueError('spatial directories disagree with declared LODs')
        key = lod['key']
        depth = key['depth']
        if not isinstance(depth, int) or not 0 <= depth <= 26:
            raise ValueError('invalid Hilbert depth')
        sentinel = 4**depth
        keys = np.memmap(key_store, dtype=np.uint64, mode='w+', shape=(parquet.metadata.num_rows,))
        last = 0
        for g in range(len(groups)):
            column = parquet.read_row_group(g, columns=[key['column']]).column(0)
            if not pa.types.is_integer(column.type):
                raise ValueError('Hilbert keys must be integers')
            values = column.fill_null(sentinel).to_numpy()
            if len(values) and (
                (values < 0).any()
                or (values > sentinel).any()
                or values[0] < last
                or (values[1:] < values[:-1]).any()
            ):
                raise ValueError('Hilbert keys out of range or unsorted')
            keys[starts[g] : starts[g] + len(values)] = values
            last = values[-1] if len(values) else last

        # Every node of every tree, by offset: a tree reaches each node once.
        reached = set()

        def walk(level, entries):
            """The leaf entries under one node, in tree order."""
            if not level:
                return [entries]
            leaves = []
            within(entries['offset'], entries['length'])
            for i in range(len(entries['low'])):
                offset, low, high = (int(entries[k][i]) for k in ('offset', 'low', 'high'))
                if offset in reached:
                    raise ValueError('a directory node is referenced twice')
                reached.add(offset)
                raw = read(offset, int(entries['length'][i]))
                child_level, child = read_node(json.loads(gzip.decompress(raw)))
                if child_level != level - 1:
                    raise ValueError('inconsistent directory depth')
                if (
                    not len(child['low'])
                    or (child['low'][0], child['high'][-1]) != (low, high)
                    or (child['low'] < low).any()
                    or (child['high'] > high).any()
                ):
                    raise ValueError('directory bounds do not cover children exactly')
                leaves += walk(child_level, child)
            return leaves

        for name, top in directories.items():
            level, entries = read_node(top)
            if level < 1:
                raise ValueError('a directory tree must start above its leaves')
            actual = _concat(walk(level, entries))
            expected, zoom = [], grid_zoom(lod, name)
            for g, (byte, length, bounds, cm) in enumerate(locations[name]):
                if root.get('codecs', {}).get(name) != CODECS[get(cm, 4)]:
                    raise ValueError('spatial root codec disagrees with footer')
                _check_pages(read, cm, byte, length, np.diff(bounds))
                values = (
                    parquet.read_row_group(g, columns=[f'{lod["column"]}.{name}'])
                    .column(0)
                    .combine_chunks()
                    .field(name)
                )
                present = values.is_valid().to_numpy(zero_copy_only=False)
                found = page_cells(
                    keys[starts[g] : starts[g] + len(present)], bounds, present, depth, zoom
                )
                shown = found.pop('pages')
                found['offset'], found['length'] = byte[shown], length[shown]
                found['first'] += starts[g]
                expected.append(found)
            expected = _concat(expected)
            # Pages compare in tree order, which must be key and row order.
            if not np.array_equal(actual['offset'], expected['offset']):
                raise ValueError('spatial directory omits, adds or reorders overview pages')
            if not expected['valid'].all():
                raise ValueError('a displayed row has a null Hilbert key')
            for fields, message in (
                (('length', 'first', 'count'), 'spatial leaf disagrees with Parquet page index'),
                (('low', 'high', 'valid'), 'spatial leaf key bounds or valid-row count incorrect'),
                (FENCE_FIELDS, 'spatial leaf fences disagree with source keys'),
            ):
                if any(not np.array_equal(actual[k], expected[k]) for k in fields):
                    raise ValueError(message)


def _concat(parts):
    """Page tables in order, as one table with fences numbered across it."""
    pages = {
        k: np.concatenate([np.zeros(0, np.int64)] + [p[k] for p in parts]) for k in PAGE_FIELDS
    }
    counts = np.cumsum([0] + [len(p['low']) for p in parts])
    for k in FENCE_FIELDS:
        pages[k] = np.concatenate(
            [np.zeros(0, np.int64)]
            + [p[k] + (counts[i] if k == 'fence_page' else 0) for i, p in enumerate(parts)]
        )
    return pages


def _check_pages(read, cm, byte, length, rows):
    """Check that an LOD chunk holds only the pages its offset index lists,
    back to back from its first data page to its end, each a v1 data page of
    PLAIN values with RLE definition levels and as many values as rows."""
    start = get(cm, 9)
    if (
        11 in cm
        or not len(byte)
        or byte[0] != start
        or (byte[1:] != (byte + length)[:-1]).any()
        or byte[-1] + length[-1] != start + get(cm, 7)
    ):
        raise ValueError('LOD chunk holds pages its offset index omits')
    for offset, size, count in zip(byte.tolist(), length.tolist(), rows.tolist(), strict=True):
        header, header_size = loads_prefix(read(offset, size))
        page = get(header, 5)
        if get(header, 1) != 0 or page is None or get(page, 2) != 0 or get(page, 3) != 3:
            raise ValueError('LOD pages must be v1 data pages of PLAIN values')
        if get(page, 1) != count or header_size + get(header, 3) != size:
            raise ValueError('LOD page header disagrees with its offset index')
