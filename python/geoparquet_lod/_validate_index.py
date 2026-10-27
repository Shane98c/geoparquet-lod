"""Exhaustive spatial-directory checks against ordinary Parquet page indexes."""

import gzip
import json
import tempfile
from pathlib import Path

import numpy as np
import pyarrow as pa

from ._compact import dumps, get, loads
from ._layout import BOOTSTRAP_BYTES, MAGIC, row_metadata


def check_index(path, parquet, lod):
    """Return normally for a file without the spatial index; raise ValueError on corruption."""
    size = Path(path).stat().st_size
    with open(path, 'rb') as source, tempfile.TemporaryFile() as key_store:
        source.seek(-8, 2)
        footer_size = int.from_bytes(source.read(4), 'little')
        footer_start = size - 8 - footer_size
        source.seek(footer_start)
        metadata = loads(source.read(footer_size))

        def read(start, length):
            if (
                not isinstance(start, int)
                or not isinstance(length, int)
                or start < 4
                or length <= 0
                or start + length > footer_start
            ):
                raise ValueError('index byte range outside data region')
            source.seek(start)
            raw = source.read(length)
            if len(raw) != length:
                raise ValueError('truncated index range')
            return raw

        # Validate all standard offset indexes, including source columns.
        starts, first, pages_by_lod = [], 0, {name: {} for name in lod['lods']}
        for g, group in enumerate(get(metadata, 4)[1]):
            starts.append(first)
            count = get(group, 3)
            for chunk in get(group, 1)[1]:
                cm = get(chunk, 3)
                path_parts = [part.decode() for part in get(cm, 3)[1]]
                offset, length = get(chunk, 4), get(chunk, 5)
                pages = get(loads(read(offset, length)), 1)[1]
                previous_row, previous_end = -1, -1
                for p, page in enumerate(pages):
                    byte, length, row = get(page, 1), get(page, 2), get(page, 3)
                    read(byte, length)
                    end = get(pages[p + 1], 3) if p + 1 < len(pages) else count
                    if (
                        row < previous_row
                        or end < row
                        or end > count
                        or byte < previous_end
                        or (p == 0 and row != 0)
                    ):
                        raise ValueError(f'row group {g}: invalid offset-index page ordering')
                    previous_row, previous_end = row, byte + length
                    if len(path_parts) == 2 and path_parts[0] == lod['column']:
                        pages_by_lod[path_parts[1]][byte] = (length, first + row, end - row, g)
            first += count

        source.seek(4)
        magic = source.read(8)
        advertised = b'lod_spatial_index' in (parquet.schema_arrow.metadata or {})
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
        key_row, last = 0, None
        for g in range(parquet.metadata.num_row_groups):
            values = parquet.read_row_group(g, columns=[key['column']]).column(0).to_pylist()
            for value in values:
                value = sentinel if value is None else value
                if (
                    not isinstance(value, int)
                    or not 0 <= value <= sentinel
                    or (last is not None and value < last)
                ):
                    raise ValueError('Hilbert keys out of range or unsorted')
                keys[key_row] = value
                key_row += 1
                last = value
        seen_ranges = set()
        for name, sections in root['streams'].items():
            span = 4 ** max(0, depth - max(lod['lods'][name]['zooms']))
            seen_pages = set()
            present_pages = set()
            for g in range(parquet.metadata.num_row_groups):
                values = (
                    parquet.read_row_group(g, columns=[f'{lod["column"]}.{name}'])
                    .column(0)
                    .combine_chunks()
                    .field(name)
                )
                for byte, (_, first, count, group) in pages_by_lod[name].items():
                    if group == g and values.slice(first - starts[g], count).null_count < count:
                        present_pages.add(byte)

            def visit(entries, level, bounds=None, *, name=name, span=span, seen_pages=seen_pages):
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
                    if len(entry) != 8:
                        raise ValueError('invalid leaf entry shape')
                    expected = pages_by_lod[name].get(byte)
                    if (
                        expected is None
                        or (length, entry[4], entry[5]) != expected[:3]
                        or byte in seen_pages
                    ):
                        raise ValueError('spatial leaf disagrees with Parquet page index')
                    codec = (
                        parquet.metadata.row_group(expected[3])
                        .column(
                            next(
                                i
                                for i in range(parquet.metadata.row_group(expected[3]).num_columns)
                                if parquet.metadata.row_group(expected[3]).column(i).path_in_schema
                                == f'{lod["column"]}.{name}'
                            )
                        )
                        .compression
                    )
                    if root.get('codecs', {}).get(name) != codec:
                        raise ValueError('spatial root codec disagrees with footer')
                    seen_pages.add(byte)
                    first, count, fences, valid = entry[4:8]
                    page_keys = keys[first : first + count]
                    actual_valid = sum(k < sentinel for k in page_keys)
                    if (
                        valid != actual_valid
                        or not valid
                        or (low, high) != (page_keys[0], page_keys[valid - 1])
                    ):
                        raise ValueError('spatial leaf key bounds or valid-row count incorrect')
                    expected_fences = []
                    last_cell = None
                    for row, k in enumerate(page_keys):
                        cell = k // span
                        if cell != last_cell:
                            expected_fences.extend([cell, row])
                            last_cell = cell
                    if fences != expected_fences:
                        raise ValueError('spatial leaf fences disagree with source keys')

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
            if seen_pages != present_pages:
                raise ValueError('spatial directory omits or adds overview pages')
