"""Cluster the LOD column chunks and add a compact spatial page directory.

The output is still the same Parquet table, including every LOD value. No
geometry, attribute or row id is changed. LOD pages are re-encoded with zstd
at tile boundaries; every other column chunk is copied verbatim. Readers
unaware of the directory follow the relocated standard footer.
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
from .encoding import PACK_VERSION

VERSION = 2
BOOTSTRAP_BYTES = 16384
# reserve room for file_bytes, footer_bytes, row_metadata and the header
_ROOT_TARGET_BYTES = BOOTSTRAP_BYTES - 128
MAGIC = b'LODIDX01'
CODECS = ['UNCOMPRESSED', 'SNAPPY', 'GZIP', 'LZO', 'BROTLI', 'LZ4', 'ZSTD', 'LZ4_RAW']
BRANCH_ENTRIES = 64
PAGE_FIELDS = ('low', 'high', 'offset', 'length', 'first', 'count', 'valid')
FENCE_FIELDS = ('fence_page', 'fence_row', 'fence_cell')


def _json(value):
    return gzip.compress(json.dumps(value, separators=(',', ':')).encode(), mtime=0)


def grid_zoom(lod, name):
    """Zoom of the cells an LOD's directory uses: its finest, at most the key depth."""
    return min(lod['key']['depth'], max(lod['lods'][name]['zooms']))


def page_cells(keys, bounds, present, depth, zoom):
    """Directory values of one row group's pages. Page i holds rows
    `bounds[i]:bounds[i + 1]` of the sorted Hilbert `keys`, null keys stored as
    4**depth, and `present` marks the rows with a value. Pages without a value
    are left out. For the rest: their index among the row group's pages, first
    row and row count, valid (non-null) key rows, minimum and maximum cell at
    `zoom`, and their fences as flat arrays: the page, relative row and cell of
    each page's first row and of every later row whose cell differs from the
    row before.
    """
    keys = np.asarray(keys, np.uint64)
    filled = np.r_[0, np.cumsum(present, dtype=np.int64)]
    shown = filled[bounds[1:]] > filled[bounds[:-1]]
    firsts, ends = bounds[:-1][shown], bounds[1:][shown]
    cells = (keys >> np.uint64(2 * (depth - zoom))).astype(np.int64)
    starts = np.union1d(firsts, np.flatnonzero(cells[1:] != cells[:-1]) + 1)
    page = np.searchsorted(firsts, starts, side='right') - 1
    inside = page >= 0
    inside[inside] = starts[inside] < ends[page[inside]]
    starts, page = starts[inside], page[inside]
    valid = np.clip(np.searchsorted(keys, np.uint64(4**depth)) - firsts, 0, ends - firsts)
    return {
        'pages': np.flatnonzero(shown),
        'low': cells[firsts],
        'high': cells[firsts + np.maximum(valid, 1) - 1],
        'first': firsts,
        'count': ends - firsts,
        'valid': valid,
        'fence_page': page,
        'fence_row': starts - firsts[page],
        'fence_cell': cells[starts],
    }


def _node(level, low, high, offset, length):
    """A directory node's leading columns, one value per entry in key order:
    minimum cell minus the previous entry's, cell span, bytes between the
    previous entry's end and this entry's start, and compressed length. The
    first entry counts from zero."""
    low, high, offset, length = (np.asarray(v, np.int64) for v in (low, high, offset, length))
    return [
        level,
        np.diff(low, prepend=0).tolist(),
        (high - low).tolist(),
        (offset - np.r_[0, offset + length][: len(offset)]).tolist(),
        length.tolist(),
    ]


def _leaf(pages, start, end):
    """A level-zero node over pages `start:end`, adding per page its first
    global row minus the previous page's end, row count, fences after the
    implied [minimum cell, 0] as pairs minus the pair before, and rows with a
    null key."""
    first, count = pages['first'][start:end], pages['count'][start:end]
    a, b = np.searchsorted(pages['fence_page'], [start, end])
    page, row, cell = (pages[k][a:b] for k in FENCE_FIELDS)
    later = row > 0
    pairs = np.column_stack((np.diff(cell, prepend=0), np.diff(row, prepend=0)))[later]
    fences = [[] for _ in range(end - start)]
    for p, pair in zip((page[later] - start).tolist(), pairs.tolist(), strict=True):
        fences[p].extend(pair)
    return [
        *_node(0, *(pages[k][start:end] for k in ('low', 'high', 'offset', 'length'))),
        (first - np.r_[0, first + count][: len(first)]).tolist(),
        count.tolist(),
        fences,
        (count - pages['valid'][start:end]).tolist(),
    ]


def read_node(node):
    """A directory node's level and its entries as arrays: cell bounds, byte
    offsets and lengths, and for a leaf the page fields and fences that
    `page_cells` returns. Entries must be in key order, each starting at or
    after the previous one's maximum cell."""
    if not isinstance(node, list) or len(node) < 5:
        raise ValueError('malformed directory node')
    level = node[0]
    if type(level) is not int or not 0 <= level <= 32:
        raise ValueError('invalid directory level')
    if len(node) != (5 if level else 9):
        raise ValueError('malformed directory node')
    cells, spans, gaps, length = (_column(values) for values in node[1:5])
    if not len(cells) == len(spans) == len(gaps) == len(length) or (spans < 0).any():
        raise ValueError('malformed directory node')
    if len(cells) and (cells[0] < 0 or (cells[1:] < spans[:-1]).any()):
        raise ValueError('directory entries out of key order')
    if (gaps < 0).any() or (length <= 0).any():
        raise ValueError('directory byte ranges overlap or are empty')
    low = np.cumsum(cells)
    end = np.cumsum(gaps + length)
    entries = {'low': low, 'high': low + spans, 'offset': end - length, 'length': length}
    if level:
        return level, entries
    rows, count, nulls = (_column(node[i]) for i in (5, 6, 8))
    fences = node[7]
    if (
        not isinstance(fences, list)
        or not len(low) == len(rows) == len(count) == len(fences) == len(nulls)
        or not all(isinstance(f, list) and len(f) % 2 == 0 for f in fences)
        or (rows < 0).any()
    ):
        raise ValueError('malformed directory leaf')
    per_page = np.array([len(f) // 2 + 1 for f in fences], np.int64)
    heads = np.cumsum(per_page) - per_page
    delta = np.zeros((int(per_page.sum()), 2), np.int64)
    later = np.ones(len(delta), bool)
    later[heads] = False
    delta[later] = _column([v for f in fences for v in f]).reshape(-1, 2)
    total = np.cumsum(delta, axis=0)
    fence = total - np.repeat(total[heads], per_page, axis=0)
    page = np.repeat(np.arange(len(fences)), per_page)
    return 0, {
        **entries,
        'first': np.cumsum(rows + count) - count,
        'count': count,
        'valid': count - nulls,
        'fence_page': page,
        'fence_row': fence[:, 1],
        'fence_cell': fence[:, 0] + low[page],
    }


def _column(values):
    array = np.asarray(values)
    if not isinstance(values, list) or array.ndim != 1 or (array.size and array.dtype.kind != 'i'):
        raise ValueError('directory columns hold integers')
    return array.astype(np.int64)


def _copy(src, dst, start, size):
    src.seek(start)
    while size:
        data = src.read(min(size, 8 * 1024 * 1024))
        if not data:
            raise ValueError('truncated column chunk')
        dst.write(data)
        size -= len(data)


def _subtree(elements, i):
    """Schema elements and leaves in the subtree at flat schema index `i`."""
    size, leaves = 1, 0 if 5 in elements[i] else 1
    for _ in range(get(elements[i], 5, 0)):
        child_size, child_leaves = _subtree(elements, i + size)
        size, leaves = size + child_size, leaves + child_leaves
    return size, leaves


def row_metadata(metadata, columns):
    """The footer `metadata` cut down to the flat top-level `columns`: their
    schema, their chunks in every row group without statistics or column
    indexes, and the `geo` key/value, so a reader can read rows of those
    columns without the full footer."""
    elements = get(metadata, 2)[1]
    kept, leaves, i, leaf = [], [], 1, 0
    while i < len(elements):
        size, count = _subtree(elements, i)
        element = elements[i]
        if 5 not in element and get(element, 3) != 2 and get(element, 4).decode() in columns:
            kept.append(element)
            leaves.append(leaf)
        i, leaf = i + size, leaf + count
    if len(kept) != len(columns):
        raise ValueError('row metadata takes only flat top-level columns')
    root = dict(elements[0])
    set_value(root, 5, len(kept))
    groups = []
    for group in get(metadata, 4)[1]:
        chunks = []
        for leaf in leaves:
            chunk = get(group, 1)[1][leaf]
            cm = {
                k: v for k, v in sorted(get(chunk, 3).items()) if k in (1, 2, 3, 4, 5, 6, 7, 9, 11)
            }
            trimmed = {k: v for k, v in sorted(chunk.items()) if k in (2, 4, 5)}
            trimmed[3] = (chunk[3][0], cm)
            chunks.append(dict(sorted(trimmed.items())))
        total = sum(get(get(c, 3), 6) for c in chunks)
        trimmed = {1: (9, (12, chunks)), 2: (group[2][0], total), 3: group[3]}
        if 7 in group:
            trimmed[7] = group[7]
        groups.append(trimmed)
    kv = [v for v in get(metadata, 5, (12, []))[1] if get(v, 1) == b'geo']
    return {
        1: metadata[1],
        2: (9, (12, [root, *kept])),
        3: metadata[3],
        4: (9, (12, groups)),
        5: (9, (12, kv)),
    }


def _tile_pages(parquet, group, name, lod, keys, work, target, page_bytes, compression_level):
    """Use Arrow to encode tile-aligned pages, then concatenate its temporary
    column chunks into one ordinary chunk in the original row group. Dictionary
    encoding is disabled so every page is independently decodable. Consecutive
    all-null tiles share a run; they carry no overview payload.
    """
    table = parquet.read_row_group(group, columns=[f'{lod["column"]}.{name}'])
    array = table.column(0).combine_chunks().field(name)
    present = array.is_valid().to_numpy(zero_copy_only=False)
    cells = keys >> np.uint64(2 * (lod['key']['depth'] - grid_zoom(lod, name)))
    boundaries = np.r_[0, np.flatnonzero(cells[1:] != cells[:-1]) + 1, len(keys)]
    # A sparse overview may contain only a handful of values per 2048 source
    # rows. Give its compression pages a useful value budget while retaining
    # tile boundaries, the byte target, and a bounded decoded row span.
    page_rows = min(262144, max(2048, int(2048 * len(present) / max(1, present.sum()))))
    path = work / 'pages.parquet'
    with pq.ParquetWriter(
        path,
        table.schema,
        compression='zstd',
        compression_level=compression_level,
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


def finalize_layout(src, out, *, leaf_pages=1024, page_bytes=65536, compression_level=9, log=print):
    """Write a separate optimized copy, atomically. Working memory is bounded by
    one row group of LOD values plus the file's page directory and indexes; keys
    are staged in a disk mmap. Encrypted/external chunks and bloom filters are
    unsupported.
    """
    src, out = Path(src), Path(out)
    if src.resolve() == out.resolve():
        raise ValueError('layout finalization requires a separate output path')
    if leaf_pages < 1:
        raise ValueError('leaf_pages must be positive')
    if page_bytes < 1:
        raise ValueError('page_bytes must be positive')
    parquet = pq.ParquetFile(src)
    geo = json.loads(parquet.schema_arrow.metadata[b'geo'])
    lod = geo['lod_columns']
    if lod.get('encoding') != 'packed' or lod.get('pack_version') != PACK_VERSION:
        raise ValueError(f'layout finalization requires packed LOD version {PACK_VERSION}')
    key_column = lod['key']['column']
    if not 0 <= lod['key']['depth'] <= 26:
        raise ValueError('Hilbert depth must fit exact JavaScript integers (0..26)')
    root = {'version': VERSION, 'lod': lod, 'primary': geo['primary_column'], 'codecs': {}}
    flat = [f.name for f in parquet.schema_arrow if not pa.types.is_nested(f.type)]
    root['columns'] = [name for name in flat if name != geo['primary_column']]
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
    names = list(lod['lods'])
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
        with tempfile.TemporaryDirectory(prefix='.lod-layout-', dir=out.parent) as work:
            work = Path(work)
            total_rows = parquet.metadata.num_rows
            if not total_rows:
                raise ValueError('cannot index an empty file')
            keys = np.memmap(work / 'keys', mode='w+', dtype=np.uint64, shape=(total_rows,))
            starts, start = [], 0
            last = None
            for g, _group in enumerate(groups):
                values = parquet.read_row_group(g, columns=[key_column]).column(0)
                # Null/empty source geometries remain analytical rows at the end
                # of the file, but are absent from every overview.
                values = values.fill_null(4 ** lod['key']['depth']).to_numpy()
                if np.any(values[1:] < values[:-1]) or (last is not None and values[0] < last):
                    raise ValueError('keys are not sorted')
                last = values[-1]
                starts.append(start)
                keys[start : start + len(values)] = values
                start += len(values)
            keys.flush()
            # Keep all original chunks, sorted physically by column. Their logical
            # row-group/column positions in the standard footer remain unchanged.
            columns = len(get(groups[0], 1)[1])
            pending_indexes = []
            pages = {name: [] for name in names}
            depth = lod['key']['depth']
            output = work / 'output.parquet'
            with output.open('w+b') as target:
                target.write(b'PAR1' + bytes(BOOTSTRAP_BYTES - 4))
                for c in range(columns):
                    for g, group in enumerate(groups):
                        chunk = get(group, 1)[1][c]
                        cm = get(chunk, 3)
                        path = [p.decode() for p in get(cm, 3)[1]]
                        name = path[1] if len(path) == 2 and path[0] == lod['column'] else None
                        if name:
                            group_rows = get(group, 3)
                            local_keys = keys[starts[g] : starts[g] + group_rows]
                            cm, index, present = _tile_pages(
                                parquet,
                                g,
                                name,
                                lod,
                                local_keys,
                                work,
                                target,
                                page_bytes,
                                compression_level,
                            )
                            set_value(chunk, 3, cm)
                            chunk.pop(6, None)
                            chunk.pop(7, None)
                            locations = get(index, 1)[1]
                            bounds = np.array(
                                [get(p, 3) for p in locations] + [group_rows], np.int64
                            )
                            found = page_cells(
                                local_keys, bounds, present, depth, grid_zoom(lod, name)
                            )
                            if not found['valid'].all():
                                raise ValueError('a displayed row has a null Hilbert key')
                            shown = found.pop('pages')
                            found['offset'] = np.array(
                                [get(locations[i], 1) for i in shown], np.int64
                            )
                            found['length'] = np.array(
                                [get(locations[i], 2) for i in shown], np.int64
                            )
                            found['first'] += starts[g]
                            found['fence_page'] += sum(len(part['low']) for part in pages[name])
                            pages[name].append(found)
                            codec = CODECS[get(cm, 4)]
                            if name in root['codecs'] and root['codecs'][name] != codec:
                                raise ValueError('LOD codec must agree across row groups')
                            root['codecs'][name] = codec
                        else:
                            old_start = min(get(cm, 9), get(cm, 11, get(cm, 9)))
                            delta = target.tell() - old_start
                            _copy(original, target, old_start, get(cm, 7))
                            for field_id in (9, 11):
                                if field_id in cm:
                                    set_value(cm, field_id, get(cm, field_id) + delta)
                            index = None
                            if 4 in chunk:
                                original.seek(get(chunk, 4))
                                index = loads(original.read(get(chunk, 5)))
                                for page in get(index, 1)[1]:
                                    set_value(page, 1, get(page, 1) + delta)
                            if 6 in chunk:
                                original.seek(get(chunk, 6))
                                raw = original.read(get(chunk, 7))
                                pending_indexes.append(((6, g, c), chunk, 6, 7, raw))
                        set_value(chunk, 2, 0)
                        if index is not None:
                            pending_indexes.append(((4, g, c), chunk, 4, 5, dumps(index)))
                    log(f'  clustered column {c + 1}/{columns}')
                # Offset indexes, then column indexes, each by row group, so one
                # row's offset indexes across columns are read together.
                pending_indexes.sort(key=lambda p: p[0])
                for _, chunk, offset_field, length_field, raw in pending_indexes:
                    set_value(chunk, offset_field, target.tell())
                    set_value(chunk, length_field, len(raw))
                    target.write(raw)
                directory_start = target.tell()
                # Each LOD's tree starts as one level of leaves. Keep the first
                # request bounded even for very large datasets: while the root is
                # too large, group a tree's top entries under a new level of
                # branches, fetched lazily and then shared by a pan.
                trees = {}
                for name, parts in pages.items():
                    table = {k: np.concatenate([part[k] for part in parts]) for k in parts[0]}
                    shards = {k: [] for k in ('low', 'high', 'offset', 'length')}
                    for first in range(0, len(table['low']), leaf_pages):
                        last = min(first + leaf_pages, len(table['low']))
                        raw = _json(_leaf(table, first, last))
                        shards['low'].append(table['low'][first])
                        shards['high'].append(table['high'][last - 1])
                        shards['offset'].append(target.tell())
                        shards['length'].append(len(raw))
                        target.write(raw)
                    trees[name] = (1, shards)
                while True:
                    root['directories'] = {
                        name: _node(level, **shards) for name, (level, shards) in trees.items()
                    }
                    if len(_json(root)) <= _ROOT_TARGET_BYTES:
                        break
                    reduced = False
                    for name, (level, shards) in trees.items():
                        if len(shards['low']) <= 1:
                            continue
                        parents = {k: [] for k in shards}
                        for first in range(0, len(shards['low']), BRANCH_ENTRIES):
                            batch = {
                                k: v[first : first + BRANCH_ENTRIES] for k, v in shards.items()
                            }
                            raw = _json(_node(level, **batch))
                            parents['low'].append(batch['low'][0])
                            parents['high'].append(batch['high'][-1])
                            parents['offset'].append(target.tell())
                            parents['length'].append(len(raw))
                            target.write(raw)
                        trees[name] = (level + 1, parents)
                        reduced = True
                    if not reduced:
                        raise ValueError('LOD metadata exceeds the 16 KiB bootstrap')
                directory_bytes = target.tell() - directory_start
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
                        2: (8, f'{{"version":{VERSION},"offset":4,"length":16380}}'.encode()),
                    }
                )
                metadata[5] = (9, (12, kv))
                rows = gzip.compress(dumps(row_metadata(metadata, flat)), mtime=0)
                root['row_metadata'] = [target.tell(), len(rows)]
                target.write(rows)
                footer = dumps(metadata)
                target.write(footer)
                target.write(struct.pack('<I', len(footer)) + b'PAR1')
                root['file_bytes'] = target.tell()
                root['footer_bytes'] = len(footer)
                packed = _json(root)
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
