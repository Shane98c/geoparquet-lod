"""Cluster existing LOD column chunks and add a compact spatial page directory.

The output is still the same Parquet table, including every LOD value. No
geometry, attribute or row id is changed. Tile alignment re-encodes LOD
pages with zstd; keep-pages mode copies every compressed data page verbatim.
Readers unaware of the directory follow the relocated standard footer.
"""

from __future__ import annotations

import gzip
import json
import os
import struct
import tempfile
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ._compact import dumps, get, loads, set_value

BOOTSTRAP_BYTES = 16384
_ROOT_TARGET_BYTES = BOOTSTRAP_BYTES - 64  # reserve room for file_bytes and the header
MAGIC = b'LODIDX01'
CODECS = ['UNCOMPRESSED', 'SNAPPY', 'GZIP', 'LZO', 'BROTLI', 'LZ4', 'ZSTD', 'LZ4_RAW']


def _json(value):
    return gzip.compress(json.dumps(value, separators=(',', ':')).encode(), mtime=0)


def _root_json(root):
    """Delta-code ordered directory locations. Each series is contiguous on
    disk. Bounds use the LOD's finest tile cells rather than full Hilbert keys.
    """
    streams = {}
    for name, sections in root['streams'].items():
        span = 4 ** max(0, root['lod']['key']['depth'] - max(root['lod']['lods'][name]['zooms']))
        streams[name] = []
        for shards in sections:
            if not shards:
                streams[name].append([0, 0, []])
                continue
            values, previous, offset = [], 0, shards[0][2]
            depth = shards[0][4] if len(shards[0]) > 4 else 0
            for shard in shards:
                if shard[2] != offset:
                    raise ValueError('directory series is not contiguous')
                low, high = shard[0] // span, shard[1] // span
                values.extend([low - previous, high - low, shard[3]])
                previous, offset = low, offset + shard[3]
            streams[name].append([shards[0][2], depth, values])
    return _json({**root, 'directory_encoding': 'delta', 'streams': streams})


def _copy(src, dst, start, size):
    src.seek(start)
    while size:
        data = src.read(min(size, 8 * 1024 * 1024))
        if not data:
            raise ValueError('truncated column chunk')
        dst.write(data)
        size -= len(data)


def _tile_pages(parquet, group, name, lod, keys, work, target, sparse_pages, page_bytes):
    """Use Arrow to encode tile-aligned pages, then concatenate its temporary
    column chunks into one ordinary chunk in the original row group. Dictionary
    encoding is disabled so every page is independently decodable. Consecutive
    all-null tiles share a run; they carry no display payload.
    """
    table = parquet.read_row_group(group, columns=[f'{lod["column"]}.{name}'])
    array = table.column(0).combine_chunks().field(name)
    present = array.is_valid().to_numpy(zero_copy_only=False)
    shift = 2 * max(0, lod['key']['depth'] - max(lod['lods'][name]['zooms']))
    cells = keys >> shift
    boundaries = np.r_[0, np.flatnonzero(cells[1:] != cells[:-1]) + 1, len(keys)]
    # A sparse overview may contain only a handful of values per 2048 source
    # rows. Give its compression pages a useful value budget while retaining
    # tile boundaries, the byte target, and a bounded decoded row span.
    page_rows = 2048
    if sparse_pages:
        page_rows = min(
            262144, max(page_rows, int(page_rows * len(present) / max(1, present.sum())))
        )
    path = work / 'pages.parquet'
    with pq.ParquetWriter(
        path,
        table.schema,
        compression='zstd',
        compression_level=9,
        use_dictionary=False,
        write_statistics=False,
        write_page_index=True,
        data_page_size=page_bytes,
        max_rows_per_page=page_rows,
        write_batch_size=128,
    ) as writer:
        null_start = None
        for first, end in zip(boundaries[:-1], boundaries[1:], strict=True):
            first, end = int(first), int(end)
            if not present[first:end].any():
                if null_start is None:
                    null_start = first
                continue
            if null_start is not None:
                writer.write_table(
                    table.slice(null_start, first - null_start), row_group_size=first - null_start
                )
                null_start = None
            writer.write_table(table.slice(first, end - first), row_group_size=end - first)
        if null_start is not None:
            writer.write_table(table.slice(null_start), row_group_size=len(keys) - null_start)
    combined, index = _append_page_chunks(path, target)
    return combined, index, present


def _append_page_chunks(path, target):
    pages, row, combined = [], 0, None
    with path.open('rb') as encoded:
        encoded.seek(-8, 2)
        size = int.from_bytes(encoded.read(4), 'little')
        encoded.seek(-8 - size, 2)
        metadata = loads(encoded.read(size))
        for group in get(metadata, 4)[1]:
            chunk = get(group, 1)[1][0]
            cm = get(chunk, 3)
            old_start, new_start = get(cm, 9), target.tell()
            _copy(encoded, target, old_start, get(cm, 7))
            encoded.seek(get(chunk, 4))
            index = loads(encoded.read(get(chunk, 5)))
            for page in get(index, 1)[1]:
                set_value(page, 1, get(page, 1) + new_start - old_start)
                set_value(page, 3, get(page, 3) + row)
                pages.append(page)
            if combined is None:
                combined = cm
                set_value(combined, 9, new_start)
                for optional in (12, 13, 16):
                    combined.pop(optional, None)
            else:
                for field in (5, 6, 7):
                    set_value(combined, field, get(combined, field) + get(cm, field))
            row += get(group, 3)
    path.unlink()
    return combined, {1: (9, (12, pages))}


def _companion_pages(parquet, group, column, geometry_index, work, target):
    table = parquet.read_row_group(group, columns=[column])
    pages = get(geometry_index, 1)[1]
    path = work / 'attributes.parquet'
    # A companion page covers exactly the same source rows as its geometry page.
    # One write per row span and an oversized byte target prevent splitting a
    # list-valued host record across pages. Ordinary offset indexes remain valid.
    with pq.ParquetWriter(
        path,
        table.schema,
        compression='zstd',
        compression_level=9,
        use_dictionary=False,
        write_statistics=False,
        write_page_index=True,
        data_page_size=2**31 - 1,
        max_rows_per_page=2**31 - 1,
    ) as writer:
        for i, page in enumerate(pages):
            first = get(page, 3)
            end = get(pages[i + 1], 3) if i + 1 < len(pages) else len(table)
            writer.write_table(table.slice(first, end - first), row_group_size=end - first)
    combined, index = _append_page_chunks(path, target)
    actual = get(index, 1)[1]
    if [get(p, 3) for p in actual] != [get(p, 3) for p in pages]:
        raise ValueError('companion pages did not align with geometry pages')
    return combined, index


def optimize(
    src, out, *, leaf_pages=1024, tile_pages=True, sparse_pages=True, page_bytes=65536, log=print
):
    """Write a separate optimized copy, atomically. Working memory is bounded by
    one row group of LOD values plus the file's page directory and indexes; keys
    are staged in a disk mmap. Encrypted/external chunks and bloom filters are
    unsupported; keep-pages mode also rejects dictionary-encoded LODs.
    """
    src, out = Path(src), Path(out)
    if src.resolve() == out.resolve():
        raise ValueError('optimize requires a separate output path')
    if leaf_pages < 1:
        raise ValueError('leaf_pages must be positive')
    if page_bytes < 1:
        raise ValueError('page_bytes must be positive')
    parquet = pq.ParquetFile(src)
    geo = json.loads(parquet.schema_arrow.metadata[b'geo'])
    lod = geo['lod_columns']
    if lod.get('encoding') not in ('packed', 'packed_geometry') or lod.get('pack_version') != 1:
        raise ValueError('optimize requires packed LOD version 1')
    key_column = lod['key']['column']
    if not 0 <= lod['key']['depth'] <= 26:
        raise ValueError('Hilbert depth must fit exact JavaScript integers (0..26)')
    root = {'version': 1, 'lod': lod, 'primary': geo['primary_column'], 'streams': {}, 'codecs': {}}
    root['columns'] = [
        f.name
        for f in parquet.schema_arrow
        if f.name != geo['primary_column'] and not pa.types.is_nested(f.type)
    ]
    bounds = [180.0, 90.0, -180.0, -90.0]
    for g in range(parquet.metadata.num_row_groups):
        group = parquet.metadata.row_group(g)
        columns = {
            group.column(c).path_in_schema: group.column(c) for c in range(group.num_columns)
        }
        for i, leaf in enumerate(('bbox.xmin', 'bbox.ymin', 'bbox.xmax', 'bbox.ymax')):
            stats = columns[leaf].statistics
            if stats and stats.has_min_max:
                value = stats.min if i < 2 else stats.max
                if np.isfinite(value):
                    bounds[i] = min(bounds[i], value) if i < 2 else max(bounds[i], value)
    root['bounds'] = bounds if bounds[0] <= bounds[2] else [-180, -85.051129, 180, 85.051129]
    prefix_end = lod['prefix']['row_group_end'] if lod.get('prefix') else -1
    names = list(lod['lods'])
    companion_columns = {
        column: (name, i)
        for name, columns in lod.get('attribute_columns', {}).items()
        for i, attribute in enumerate(lod['attributes'])
        for column in [columns[attribute['name']]]
    }
    if companion_columns and not tile_pages:
        raise ValueError('separated attributes require --tile-pages')
    with src.open('rb') as original:
        if original.read(4) != b'PAR1':
            raise ValueError('not an unencrypted Parquet file')
        original.seek(-8, 2)
        tail = original.read(8)
        if tail[4:] != b'PAR1':
            raise ValueError('not an unencrypted Parquet file')
        original.seek(-8 - int.from_bytes(tail[:4], 'little'), 2)
        metadata = loads(original.read(int.from_bytes(tail[:4], 'little')))
        if 8 in metadata or 9 in metadata:
            raise ValueError('encrypted Parquet is unsupported')
        groups = get(metadata, 4)[1]
        # Reject layouts the fast decoder cannot faithfully reconstruct.
        lod_type = parquet.schema_arrow.field(lod['column'])
        if lod_type.nullable:
            raise ValueError('LOD parent must be required')
        for name in names:
            field = lod_type.type.field(name)
            if not pa.types.is_binary(field.type) or not field.nullable:
                raise ValueError('LOD children must be nullable binary')
        for group in groups:
            for chunk in get(group, 1)[1]:
                if 1 in chunk or 8 in chunk or 9 in chunk:
                    raise ValueError('external or encrypted column chunks are unsupported')
                cm = get(chunk, 3)
                if 14 in cm or 10 in cm:
                    raise ValueError('bloom filters and legacy index pages are unsupported')
        with tempfile.TemporaryDirectory(prefix='.lod-optimize-', dir=out.parent) as work:
            work = Path(work)
            total_rows = parquet.metadata.num_rows
            if not total_rows:
                raise ValueError('cannot optimize an empty file')
            keys = np.memmap(work / 'keys', mode='w+', dtype=np.uint64, shape=(total_rows,))
            starts, start = [], 0
            last = None
            for g, _group in enumerate(groups):
                values = parquet.read_row_group(g, columns=[key_column]).column(0)
                # Null/empty source geometries remain analytical rows at the end
                # of the main section, but are absent from every overview.
                values = values.fill_null(4 ** lod['key']['depth']).to_numpy()
                if g == prefix_end + 1:
                    last = None
                if np.any(values[1:] < values[:-1]) or (last is not None and values[0] < last):
                    raise ValueError('keys are not sorted within each section')
                last = values[-1]
                starts.append(start)
                keys[start : start + len(values)] = values
                start += len(values)
            keys.flush()
            # Keep all original chunks, sorted physically by column. Their logical
            # row-group/column positions in the standard footer remain unchanged.
            columns = len(get(groups[0], 1)[1])
            pending_indexes = []
            geometry_indexes, directory_pages = {}, {}
            directory = {name: [[], []] for name in names}
            output = work / 'output.parquet'
            with output.open('w+b') as target:
                target.write(b'PAR1' + bytes(BOOTSTRAP_BYTES - 4))
                for c in range(columns):
                    for g, group in enumerate(groups):
                        chunk = get(group, 1)[1][c]
                        cm = get(chunk, 3)
                        path = [p.decode() for p in get(cm, 3)[1]]
                        name = path[1] if len(path) == 2 and path[0] == lod['column'] else None
                        present = None
                        companion = companion_columns.get(path[0])
                        if companion:
                            lod_name, _ = companion
                            cm, index = _companion_pages(
                                parquet, g, path[0], geometry_indexes[g, lod_name], work, target
                            )
                            set_value(chunk, 3, cm)
                            set_value(chunk, 2, 0)
                            chunk.pop(6, None)
                            chunk.pop(7, None)
                        elif name and tile_pages:
                            local_keys = keys[starts[g] : starts[g] + get(group, 3)]
                            cm, index, present = _tile_pages(
                                parquet,
                                g,
                                name,
                                lod,
                                local_keys,
                                work,
                                target,
                                sparse_pages,
                                page_bytes,
                            )
                            set_value(chunk, 3, cm)
                            set_value(chunk, 2, 0)
                            chunk.pop(6, None)
                            chunk.pop(7, None)
                        else:
                            old_start = min(get(cm, 9), get(cm, 11, get(cm, 9)))
                            delta = target.tell() - old_start
                            _copy(original, target, old_start, get(cm, 7))
                            for field_id in (9, 11):
                                if field_id in cm:
                                    set_value(cm, field_id, get(cm, field_id) + delta)
                            set_value(chunk, 2, 0)
                            index = None
                            if 4 in chunk:
                                original.seek(get(chunk, 4))
                                index = loads(original.read(get(chunk, 5)))
                                for page in get(index, 1)[1]:
                                    set_value(page, 1, get(page, 1) + delta)
                            if 6 in chunk:
                                original.seek(get(chunk, 6))
                                pending_indexes.append((chunk, 6, 7, original.read(get(chunk, 7))))
                            if name:
                                array = (
                                    parquet.read_row_group(g, columns=[f'{lod["column"]}.{name}'])
                                    .column(0)
                                    .combine_chunks()
                                    .field(name)
                                )
                                present = array.is_valid().to_numpy(zero_copy_only=False)
                        if index is not None:
                            pages = get(index, 1)[1]
                            if companion:
                                lod_name, attribute_index = companion
                                for page in pages:
                                    entry = directory_pages.get((g, lod_name, get(page, 3)))
                                    if entry is not None:
                                        entry[8][attribute_index] = [get(page, 1), get(page, 2)]
                            if name:
                                geometry_indexes[g, name] = index
                                for p, page in enumerate(pages):
                                    local_first = get(page, 3)
                                    local_end = (
                                        get(pages[p + 1], 3)
                                        if p + 1 < len(pages)
                                        else get(group, 3)
                                    )
                                    if not present[local_first:local_end].any():
                                        continue
                                    first, end = starts[g] + local_first, starts[g] + local_end
                                    valid_rows = int(
                                        np.searchsorted(keys[first:end], 4 ** lod['key']['depth'])
                                    )
                                    if not valid_rows:
                                        raise ValueError('a displayed row has a null Hilbert key')
                                    zoom = max(lod['lods'][name]['zooms'])
                                    shift = 2 * max(0, lod['key']['depth'] - zoom)
                                    cells = keys[first:end] >> shift
                                    changes = np.r_[0, np.flatnonzero(cells[1:] != cells[:-1]) + 1]
                                    fences = (
                                        np.column_stack((cells[changes], changes)).ravel().tolist()
                                    )
                                    entry = [
                                        int(keys[first]),
                                        int(keys[first + valid_rows - 1]),
                                        get(page, 1),
                                        get(page, 2),
                                        first,
                                        end - first,
                                        fences,
                                        valid_rows,
                                    ]
                                    if lod.get('encoding') == 'packed_geometry':
                                        entry.append([None] * len(lod['attributes']))
                                    directory[name][int(g > prefix_end)].append(entry)
                                    directory_pages[g, name, local_first] = entry
                            pending_indexes.append((chunk, 4, 5, dumps(index)))
                        elif name:
                            raise ValueError('LOD column has no offset index')
                        if name:
                            if 11 in cm:
                                raise ValueError('dictionary-encoded LODs are unsupported')
                            codec = CODECS[get(cm, 4)]
                            if name in root['codecs'] and root['codecs'][name] != codec:
                                raise ValueError('LOD codec must agree across row groups')
                            root['codecs'][name] = codec
                    log(f'  clustered column {c + 1}/{columns}')
                for chunk, offset_field, length_field, raw in pending_indexes:
                    set_value(chunk, offset_field, target.tell())
                    set_value(chunk, length_field, len(raw))
                    target.write(raw)
                directory_bytes = 0
                for name, sections in directory.items():
                    root['streams'][name] = [[], []]
                    for section, pages in enumerate(sections):
                        for first in range(0, len(pages), leaf_pages):
                            batch = pages[first : first + leaf_pages]
                            raw = _json(batch)
                            root['streams'][name][section].append(
                                [
                                    batch[0][0],
                                    batch[-1][1],
                                    target.tell(),
                                    len(raw),
                                ]
                            )
                            target.write(raw)
                            directory_bytes += len(raw)
                # Keep the first request bounded even for very large datasets.
                # Extra directory levels are fetched lazily, then shared by a pan.
                while len(_root_json(root)) > _ROOT_TARGET_BYTES:
                    reduced = False
                    for sections in root['streams'].values():
                        for section, shards in enumerate(sections):
                            if len(shards) <= 1:
                                continue
                            parents = []
                            for first in range(0, len(shards), 64):
                                batch = shards[first : first + 64]
                                raw = _json(batch)
                                depth = (batch[0][4] if len(batch[0]) > 4 else 0) + 1
                                parents.append(
                                    [batch[0][0], batch[-1][1], target.tell(), len(raw), depth]
                                )
                                target.write(raw)
                                directory_bytes += len(raw)
                            sections[section] = parents
                            reduced = True
                    if not reduced:
                        raise ValueError('LOD metadata exceeds the 16 KiB bootstrap')
                for group in groups:
                    chunks = get(group, 1)[1]
                    set_value(group, 2, sum(get(get(c, 3), 6) for c in chunks))
                    if 6 in group:
                        set_value(group, 6, sum(get(get(c, 3), 7) for c in chunks))
                # RowGroup.file_offset is optional and must describe the new placement.
                for group in groups:
                    if 5 in group:
                        offsets = [get(get(c, 3), 11, get(get(c, 3), 9)) for c in get(group, 1)[1]]
                        set_value(group, 5, min(offsets))
                # Discoverable to analytical tooling without changing the GeoParquet contract.
                kv = get(metadata, 5, (12, []))[1]
                kv = [v for v in kv if get(v, 1) != b'lod_spatial_index']
                kv.append(
                    {
                        1: (8, b'lod_spatial_index'),
                        2: (8, b'{"version":1,"offset":4,"length":16380}'),
                    }
                )
                metadata[5] = (9, (12, kv))
                footer = dumps(metadata)
                target.write(footer)
                target.write(struct.pack('<I', len(footer)) + b'PAR1')
                root['file_bytes'] = target.tell()
                packed = _root_json(root)
                if len(packed) + 16 > BOOTSTRAP_BYTES:
                    raise ValueError('root exceeds 16 KiB; increase leaf_pages')
                target.seek(4)
                target.write(MAGIC + struct.pack('<I', len(packed)) + packed)
                target.flush()
                os.fsync(target.fileno())
            del keys
            # Read the relocated footer before publishing the completed file.
            check = pq.ParquetFile(output)
            if check.metadata.num_rows != total_rows or check.schema_arrow != parquet.schema_arrow:
                raise ValueError('relocated table schema or row count changed')
            os.replace(output, out)
    log(f'  {directory_bytes:,} directory bytes, {len(packed):,} root bytes -> {out}')
    return out
