"""Exhaustive spatial-directory checks against ordinary Parquet page indexes."""

import gzip
import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from ._compact import dumps, get, loads, loads_prefix
from ._layout import (
    BOOTSTRAP_BYTES,
    BOX_FIELDS,
    CODECS,
    FENCE_FIELDS,
    MAGIC,
    PAGE_FIELDS,
    RUN_FIELDS,
    VERSION,
    covering,
    covering_cells,
    flat_columns,
    grid_zoom,
    page_boxes,
    page_cells,
    primary_schema,
    read_box_node,
    read_node,
    row_metadata,
)

PIECE_FIELDS = ('piece_first', 'piece_count', 'piece_x0', 'piece_y0', 'piece_x1', 'piece_y1')


def check_index(path, parquet, lod):
    """Return normally for a file without the spatial index; raise ValueError on
    corruption. Holds the decoded directories, then one row group's keys and
    one LOD column of it at a time."""
    size = Path(path).stat().st_size
    with open(path, 'rb') as source:
        source.seek(-8, 2)
        footer_size = int.from_bytes(source.read(4), 'little')
        footer_start = size - 8 - footer_size
        source.seek(footer_start)
        metadata = loads(source.read(footer_size))

        def within(start, length):
            """Raise unless every `[start, start + length)` of these arrays is
            a nonempty range between the leading magic and the footer."""
            start, length = np.asarray(start), np.asarray(length)
            # Bound each value on its own first: an int64 sum could wrap.
            for value in (start, length):
                if (
                    value.dtype.kind not in 'iu'
                    or (value < 0).any()
                    or (value > footer_start).any()
                ):
                    raise ValueError('index byte range outside data region')
            start, length = start.astype(np.int64), length.astype(np.int64)
            if (start < 4).any() or (length <= 0).any() or (length > footer_start - start).any():
                raise ValueError('index byte range outside data region')

        def read(start, length):
            # the same bounds for Python integers, which cannot wrap
            if not (
                type(start) is int
                and type(length) is int
                and 4 <= start
                and 0 < length <= footer_start - start
            ):
                raise ValueError('index byte range outside data region')
            source.seek(start)
            raw = source.read(length)
            if len(raw) != length:
                raise ValueError('truncated index range')
            return raw

        # Validate all standard offset indexes, including source columns.
        groups = get(metadata, 4)[1]
        starts, first, locations = [], 0, {name: [] for name in lod['lods']}
        sources = {}
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
                    or (bounds[1:] < bounds[:-1]).any()
                    or (byte[1:] < (byte + length)[:-1]).any()
                ):
                    raise ValueError(f'row group {g}: invalid offset-index page ordering')
                if len(path_parts) == 2 and path_parts[0] == lod['column']:
                    locations[path_parts[1]].append((byte, length, bounds, cm))
                if len(path_parts) == 1:
                    sources.setdefault(path_parts[0], []).append((byte, length, bounds, cm, first))
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

        trees = {}
        for name, top in directories.items():
            level, entries = read_node(top)
            if level < 1:
                raise ValueError('a directory tree must start above its leaves')
            trees[name] = _concat(walk(level, entries))

        primary = root['primary']
        index = root.get('primary_index')
        if not isinstance(index, dict):
            raise ValueError('spatial root has no primary directory')
        columns = flat_columns(metadata)
        attributes = index.get('attributes')
        if (
            not isinstance(attributes, list)
            or len(set(map(str, attributes))) != len(attributes)
            or any(type(a) is not str or a == primary or a not in columns for a in attributes)
        ):
            raise ValueError('primary directory attributes must be distinct flat source columns')
        if primary not in columns or get(columns[primary][0], 1) != 6:
            raise ValueError('the primary geometry is not a flat binary column')
        indexed = [primary, *attributes]
        if index.get('schema') != primary_schema(metadata, indexed):
            raise ValueError('primary directory schema disagrees with footer')
        codecs = index.get('codecs')
        if not isinstance(codecs, list) or len(codecs) != len(indexed):
            raise ValueError('primary directory codecs disagree with footer')
        for name, codec in zip(indexed, codecs, strict=True):
            for byte, length, bounds, cm, _ in sources[name]:
                if codec != CODECS[get(cm, 4)]:
                    raise ValueError('primary directory codecs disagree with footer')
                _check_source_pages(read, cm, byte, length, np.diff(bounds))

        def walk_boxes(level, entries):
            """The leaves under one primary directory node, in tree order."""
            if not level:
                return [entries]
            leaves = []
            within(entries['offset'], entries['length'])
            for i in range(len(entries['offset'])):
                offset = int(entries['offset'][i])
                if offset in reached:
                    raise ValueError('a directory node is referenced twice')
                reached.add(offset)
                raw = read(offset, int(entries['length'][i]))
                child_level, child = read_box_node(json.loads(gzip.decompress(raw)), attributes)
                if child_level != level - 1:
                    raise ValueError('inconsistent directory depth')
                if not len(child['offset']) or _union(child) != tuple(
                    int(entries[k][i]) for k in BOX_FIELDS
                ):
                    raise ValueError('primary directory boxes do not cover their children exactly')
                leaves += walk_boxes(child_level, child)
            return leaves

        leaves = walk_boxes(*read_box_node(index.get('directory'), attributes))
        exact = {
            k: np.concatenate([np.zeros(0, np.int64)] + [leaf[k] for leaf in leaves])
            for k in (*BOX_FIELDS, *RUN_FIELDS, *PIECE_FIELDS)
        }
        within(exact['offset'], exact['length'])
        for i, name in enumerate(attributes):
            parts = [_index_part(*chunk) for chunk in sources[name]]
            pages = {k: np.concatenate([part[k] for part in parts]) for k in RUN_FIELDS}
            for leaf in leaves:
                actual = leaf['runs'][i]
                if len(leaf['first']):
                    span = leaf['first'][0], leaf['first'][-1] + leaf['count'][-1]
                    a = np.searchsorted(pages['first'] + pages['count'], span[0], side='right')
                    b = np.searchsorted(pages['first'], span[1])
                else:
                    a = b = 0
                if any(not np.array_equal(actual[k], pages[k][a:b]) for k in RUN_FIELDS):
                    raise ValueError(f'primary directory leaf misplaces {name} pages')
        bbox_column, bbox_fields = covering(geo)
        checked_exact = 0
        # Pages compare in tree order, which must be key and row order: each
        # row group's pages follow the pages of the row groups before it.
        checked = dict.fromkeys(trees, 0)
        last = 0
        for g in range(len(groups)):
            byte, length, bounds, _, start = sources[primary][g]
            bbox = parquet.read_row_group(g, columns=[bbox_column]).column(0).combine_chunks()
            if len(bbox) != bounds[-1]:
                raise ValueError('covering length disagrees with the row group')
            cells = covering_cells(bbox, bbox_fields)
            box, shown = page_boxes(cells, bounds)
            expected = {
                **dict(zip(BOX_FIELDS, box, strict=True)),
                **_index_part(byte, length, bounds, None, start),
            }
            end = checked_exact + int(shown.sum())
            for fields, message in (
                (RUN_FIELDS, 'primary directory disagrees with the geometry page index'),
                (BOX_FIELDS, "primary directory box is not the union of its rows' covering"),
            ):
                if any(
                    not np.array_equal(exact[k][checked_exact:end], expected[k][shown])
                    for k in fields
                ):
                    raise ValueError(message)
            # pieces of this row group's pages, each the union of its rows' boxes
            a, b = np.searchsorted(exact['piece_first'], [start, start + bounds[-1]])
            first = exact['piece_first'][a:b] - start
            edges = np.union1d(bounds, first)
            pieces, filled = page_boxes(cells, edges)
            at = np.searchsorted(edges, first)
            if not filled[at].all() or any(
                not np.array_equal(exact[f'piece_{k}'][a:b], pieces[i][at])
                for i, k in enumerate(BOX_FIELDS)
            ):
                raise ValueError("primary directory piece is not the union of its rows' covering")
            checked_exact = end
            column = parquet.read_row_group(g, columns=[key['column']]).column(0)
            if not pa.types.is_integer(column.type):
                raise ValueError('Hilbert keys must be integers')
            keys = column.fill_null(sentinel).to_numpy()
            if len(keys) and (
                (keys < 0).any()
                or (keys > sentinel).any()
                or keys[0] < last
                or (keys[1:] < keys[:-1]).any()
            ):
                raise ValueError('Hilbert keys out of range or unsorted')
            last = keys[-1] if len(keys) else last
            for name, actual in trees.items():
                byte, length, bounds, cm = locations[name][g]
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
                expected = page_cells(keys, bounds, present, depth, grid_zoom(lod, name))
                shown = expected.pop('pages')
                expected['offset'], expected['length'] = byte[shown], length[shown]
                expected['first'] += starts[g]
                _compare(actual, checked[name], expected)
                checked[name] += len(shown)
        if any(checked[name] != len(actual['low']) for name, actual in trees.items()):
            raise ValueError('spatial directory omits, adds or reorders overview pages')
        if checked_exact != len(exact['offset']):
            raise ValueError('primary directory disagrees with the geometry page index')


def _union(entries):
    """The box holding every entry's box."""
    x0, y0, x1, y1 = (entries[k] for k in BOX_FIELDS)
    return int(x0.min()), int(y0.min()), int(x1.max()), int(y1.max())


def _index_part(byte, length, bounds, _cm, start):
    """A chunk's pages from its offset index, rows counted from `start`."""
    if (np.diff(bounds) <= 0).any():
        raise ValueError('an indexed column has an empty page')
    return {
        'offset': byte,
        'length': length,
        'first': bounds[:-1] + start,
        'count': np.diff(bounds),
    }


def _check_source_pages(read, cm, byte, length, rows):
    """Check that a primary or attribute chunk holds only the pages its offset
    index lists, back to back from its first data page to its end, each a
    data page, not dictionary encoded, of as many rows as the index says."""
    start = get(cm, 9)
    if (
        11 in cm
        or not len(byte)
        or byte[0] != start
        or (byte[1:] != (byte + length)[:-1]).any()
        or byte[-1] + length[-1] != start + get(cm, 7)
    ):
        raise ValueError('an indexed column chunk holds pages its offset index omits')
    for offset, size, count in zip(byte.tolist(), length.tolist(), rows.tolist(), strict=True):
        try:
            header, header_size = loads_prefix(read(offset, min(size, 512)))
        except (ValueError, IndexError):
            header, header_size = loads_prefix(read(offset, size))
        kind, v1, v2 = get(header, 1), get(header, 5), get(header, 8)
        if kind == 0 and v1 is not None:
            values, encoding = get(v1, 1), get(v1, 2)
        elif kind == 3 and v2 is not None:
            values, encoding = get(v2, 3), get(v2, 4)
        else:
            raise ValueError('indexed column pages must be data pages')
        if encoding in (2, 8):
            raise ValueError('indexed column pages must not be dictionary encoded')
        if values != count or header_size + get(header, 3) != size:
            raise ValueError('indexed column page header disagrees with its offset index')


def _compare(actual, start, expected):
    """Check the directory pages `actual` from index `start` against the next
    row group's `expected` pages, whose fences number its pages from zero."""
    end = start + len(expected['low'])
    if not np.array_equal(actual['offset'][start:end], expected['offset']):
        raise ValueError('spatial directory omits, adds or reorders overview pages')
    if not expected['valid'].all():
        raise ValueError('a displayed row has a null Hilbert key')
    first, last = np.searchsorted(actual['fence_page'], [start, end])
    found = {k: actual[k][start:end] for k in PAGE_FIELDS}
    found.update({k: actual[k][first:last] for k in FENCE_FIELDS})
    found['fence_page'] = found['fence_page'] - start
    for fields, message in (
        (('length', 'first', 'count'), 'spatial leaf disagrees with Parquet page index'),
        (('low', 'high', 'valid'), 'spatial leaf key bounds or valid-row count incorrect'),
        (FENCE_FIELDS, 'spatial leaf fences disagree with source keys'),
    ):
        if any(not np.array_equal(found[k], expected[k]) for k in fields):
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
