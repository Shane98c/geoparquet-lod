"""Exhaustive spatial-directory checks against ordinary Parquet page indexes."""

import gzip
import json
import tempfile
from pathlib import Path

import numpy as np
import pyarrow as pa

from ._compact import dumps, get, loads
from ._layout import (
    BOOTSTRAP_BYTES,
    CODECS,
    FENCE_FIELDS,
    MAGIC,
    PAGE_FIELDS,
    grid_zoom,
    page_cells,
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
                    locations[path_parts[1]].append((byte, length, bounds, CODECS[get(cm, 4)]))
            first += count

        source.seek(4)
        magic = source.read(8)
        # The footer's key/value metadata, which Arrow's schema metadata need not include.
        advertised = b'lod_spatial_index' in (parquet.metadata.metadata or {})
        if magic != MAGIC:
            if advertised:
                raise ValueError('spatial index advertised but bootstrap magic missing')
            return
        root_length = int.from_bytes(source.read(4), 'little')
        if not 0 < root_length <= BOOTSTRAP_BYTES - 16:
            raise ValueError('invalid spatial root length')
        root = json.loads(gzip.decompress(read(16, root_length)))
        if root.get('version') != 1 or root.get('file_bytes') != size or root.get('lod') != lod:
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
        if set(root.get('streams', {})) != set(lod['lods']):
            raise ValueError('spatial streams disagree with declared LODs')
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
        seen_ranges = set()
        for name, sections in root['streams'].items():
            span = 4 ** max(0, depth - max(lod['lods'][name]['zooms']))
            records = []

            def visit(entries, level, bounds=None, *, records=records):
                if not isinstance(level, int) or not 0 <= level <= 32:
                    raise ValueError('invalid directory depth')
                previous = -1
                for entry in entries:
                    low, high, byte, length = entry[:4]
                    if low > high or low < previous or low < 0 or high >= sentinel:
                        raise ValueError('invalid directory key bounds or ordering')
                    if bounds and (low < bounds[0] or high > bounds[1]):
                        raise ValueError('child directory bounds escape parent')
                    previous = low
                    if level:
                        if len(entry) > 4 and entry[4] != level - 1:
                            raise ValueError('inconsistent directory depth')
                        ref = (byte, length)
                        if ref in seen_ranges:
                            raise ValueError('duplicate or cyclic directory reference')
                        seen_ranges.add(ref)
                        child = json.loads(gzip.decompress(read(byte, length)))
                        if not child or child[0][0] != low or child[-1][1] != high:
                            raise ValueError('directory bounds do not cover children exactly')
                        visit(child, level - 1, (low, high))
                        continue
                    if len(entry) != 8 or len(entry[6]) % 2:
                        raise ValueError('invalid leaf entry shape')
                    records.append(entry)

            if not sections:
                raise ValueError('missing directory series')
            for encoded in sections:
                if root.get('directory_encoding') == 'delta':
                    offset, level, triples = encoded
                    if len(triples) % 3:
                        raise ValueError('malformed delta directory')
                    entries, previous = [], 0
                    for i in range(0, len(triples), 3):
                        low = previous + triples[i]
                        length = triples[i + 2]
                        entries.append(
                            [low * span, (low + triples[i + 1] + 1) * span - 1, offset, length]
                        )
                        previous, offset = low, offset + length
                    # Root delta bounds are rounded to tile cells, unlike exact child bounds.
                    for entry in entries:
                        child = json.loads(gzip.decompress(read(entry[2], entry[3])))
                        if not child or child[0][0] < entry[0] or child[-1][1] > entry[1]:
                            raise ValueError('root bounds do not cover directory children')
                        visit(child, level, (entry[0], entry[1]))
                elif root.get('directory_encoding') is None:
                    for low, high, byte, length, *rest in encoded:
                        child = json.loads(gzip.decompress(read(byte, length)))
                        visit(child, rest[0] if rest else 0, (low, high))
                else:
                    raise ValueError('unknown directory encoding')
            actual = _records(records)
            order = np.argsort(actual['first'], kind='stable')
            position = np.empty_like(order)
            position[order] = np.arange(len(order))
            actual = {k: actual[k][order] for k in PAGE_FIELDS} | _fences(actual, position)
            expected, zoom = [], grid_zoom(lod, name)
            for g, (byte, length, bounds, codec) in enumerate(locations[name]):
                if root.get('codecs', {}).get(name) != codec:
                    raise ValueError('spatial root codec disagrees with footer')
                values = (
                    parquet.read_row_group(g, columns=[f'{lod["column"]}.{name}'])
                    .column(0)
                    .combine_chunks()
                    .field(name)
                )
                present = values.is_valid().to_numpy(zero_copy_only=False)
                local = keys[starts[g] : starts[g] + len(present)]
                found = page_cells(local, bounds, present, depth, zoom)
                shown = found.pop('pages')
                found['offset'], found['length'] = byte[shown], length[shown]
                last_valid = found['first'] + np.maximum(found['valid'], 1) - 1
                found['low'] = local[found['first']].astype(np.int64)
                found['high'] = local[last_valid].astype(np.int64)
                found['first'] += starts[g]
                expected.append(found)
            expected = _concat(expected)
            if len(actual['low']) != len(expected['low']) or not np.array_equal(
                actual['offset'], expected['offset']
            ):
                raise ValueError('spatial directory omits or adds overview pages')
            if not expected['valid'].all():
                raise ValueError('a displayed row has a null Hilbert key')
            for fields, message in (
                (('length', 'first', 'count'), 'spatial leaf disagrees with Parquet page index'),
                (('low', 'high', 'valid'), 'spatial leaf key bounds or valid-row count incorrect'),
                (FENCE_FIELDS, 'spatial leaf fences disagree with source keys'),
            ):
                if any(not np.array_equal(actual[k], expected[k]) for k in fields):
                    raise ValueError(message)


def _records(records):
    """Leaf page records as one table with flat fences."""
    fences = [entry[6] for entry in records]
    flat = np.array([value for fence in fences for value in fence], np.int64)
    return {
        **{
            k: np.array([entry[i] for entry in records], np.int64)
            for i, k in zip((0, 1, 2, 3, 4, 5, 7), PAGE_FIELDS, strict=True)
        },
        'fence_page': np.repeat(np.arange(len(fences)), [len(f) // 2 for f in fences]),
        'fence_row': flat[1::2],
        'fence_cell': flat[0::2],
    }


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


def _fences(pages, position):
    """Fences renumbered for pages moved to `position`, in page and row order."""
    page = position[pages['fence_page']]
    order = np.lexsort((pages['fence_row'], page))
    return {
        'fence_page': page[order],
        'fence_row': pages['fence_row'][order],
        'fence_cell': pages['fence_cell'][order],
    }
