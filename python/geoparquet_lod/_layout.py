"""Cluster the LOD column chunks and add compact spatial page directories.

The output is still the same Parquet table, including every LOD value. No
geometry, attribute or row id is changed. LOD pages are re-encoded with zstd
at tile boundaries; every other column chunk is copied verbatim, and a box tree
indexes the primary geometry's own pages. Readers unaware of the directories
follow the relocated standard footer.
"""

from __future__ import annotations

import base64
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

VERSION = 3
BOOTSTRAP_BYTES = 16384
# reserve room for file_bytes, footer_bytes, row_metadata and the header
_ROOT_TARGET_BYTES = BOOTSTRAP_BYTES - 128
MAGIC = b'LODIDX01'
CODECS = ['UNCOMPRESSED', 'SNAPPY', 'GZIP', 'LZO', 'BROTLI', 'LZ4', 'ZSTD', 'LZ4_RAW']
BRANCH_ENTRIES = 64
PAGE_FIELDS = ('low', 'high', 'offset', 'length', 'first', 'count', 'valid')
FENCE_FIELDS = ('fence_page', 'fence_row', 'fence_cell')
# primary directory boxes are whole cells of 2**-BOX_BITS degrees
BOX_BITS = 12
BOX_FIELDS = ('x0', 'y0', 'x1', 'y1')
RUN_FIELDS = ('offset', 'length', 'first', 'count')
# A geometry page is cut in two, and each half once more, where the cut makes
# the boxes cover at most PIECE_SAVING of the tiles of PIECE_TILE cells its box
# covers.
PIECE_ROUNDS = 2
PIECE_SAVING = 0.7
PIECE_TILE = 32


def _json(value):
    return gzip.compress(json.dumps(value, separators=(',', ':')).encode(), mtime=0)


def covering(geo):
    """The primary geometry's bbox covering: its struct column and the fields
    holding xmin, ymin, xmax and ymax."""
    paths = geo['columns'][geo['primary_column']].get('covering', {}).get('bbox')
    keys = ('xmin', 'ymin', 'xmax', 'ymax')
    if (
        not isinstance(paths, dict)
        or any(not isinstance(paths.get(k), list) or len(paths[k]) != 2 for k in keys)
        or len({paths[k][0] for k in keys}) != 1
    ):
        raise ValueError('the primary geometry needs a bbox covering of one struct column')
    return paths['xmin'][0], [paths[k][1] for k in keys]


def covering_cells(bbox, fields=('xmin', 'ymin', 'xmax', 'ymax')):
    """Each row's covering box in whole cells of 2**-BOX_BITS degrees, columns
    counted from longitude -180 and rows from latitude -90, as float64 arrays
    `x0, y0, x1, y1`. A cell holds its lower edges, so the cells of a box's
    minimum and maximum contain it. A row has no extent, and NaN cells, unless
    its four bounds are finite with each minimum at most its maximum; a null
    struct or value has none. `bbox` is the covering's struct array and
    `fields` name its xmin, ymin, xmax and ymax."""
    names = [bbox.type.field(i).name for i in range(bbox.type.num_fields)]
    values = dict(zip(names, bbox.flatten(), strict=True))
    xmin, ymin, xmax, ymax = (
        values[k].to_numpy(zero_copy_only=False).astype(np.float64) for k in fields
    )
    with np.errstate(invalid='ignore'):
        extent = (
            np.isfinite(xmin)
            & np.isfinite(ymin)
            & np.isfinite(xmax)
            & np.isfinite(ymax)
            & (xmin <= xmax)
            & (ymin <= ymax)
        )
    scale = float(1 << BOX_BITS)
    cells = []
    for v, origin in ((xmin, 180.0), (ymin, 90.0), (xmax, 180.0), (ymax, 90.0)):
        cell = np.floor((v + origin) * scale)
        cell[~extent] = np.nan
        cells.append(cell)
    return cells


def page_boxes(cells, bounds):
    """For page i holding rows `bounds[i]:bounds[i + 1]` of `cells`, the union
    of its rows' boxes as int64 `x0, y0, x1, y1`, and which pages have any row
    with an extent; the others' boxes are meaningless."""
    starts = np.asarray(bounds[:-1], np.int64)
    if not len(starts):
        empty = np.zeros(0, np.int64)
        return (empty,) * 4, np.zeros(0, bool)
    with np.errstate(invalid='ignore'):
        union = [
            np.fmin.reduceat(cells[0], starts),
            np.fmin.reduceat(cells[1], starts),
            np.fmax.reduceat(cells[2], starts),
            np.fmax.reduceat(cells[3], starts),
        ]
    shown = ~np.isnan(union[0])
    return tuple(np.where(shown, u, 0).astype(np.int64) for u in union), shown


def _within(ufunc, values, piece, span):
    """`ufunc` accumulated over each run of equal, ascending `piece` from the
    run's first value. Values lie in [0, span), and shifting each run past the
    ones before keeps one accumulation from mixing them."""
    shift = piece * span if ufunc is np.maximum else -piece * span
    return ufunc.accumulate(values + shift) - shift


def page_pieces(cells, bounds, rounds=PIECE_ROUNDS, saving=PIECE_SAVING, tile=PIECE_TILE):
    """Where pages split into pieces, runs of consecutive rows each boxed on
    its own. A Hilbert curve leaving the data and re-entering it far away
    puts rows of distant places in one page, whose box then spans the empty
    space between them. Each round cuts every piece once more, at the row
    minimizing its halves' cost `(width + tile) * (height + tile)`, roughly
    the tiles a box overlaps, when that cost is at most `saving` times the
    piece's. Returns the cut rows, each the first row of a piece after its
    page's first."""
    rows = np.flatnonzero(~np.isnan(cells[0]))
    cuts = np.zeros(0, np.int64)
    if len(rows) < 2:
        return cuts
    # rows with an extent, offset by piece so that one accumulation runs
    # through every piece without mixing them
    origin = np.nanmin(cells[0]), np.nanmin(cells[1])
    values = [c[rows] - origin[i % 2] for i, c in enumerate(cells)]
    span = max(float(np.max(v)) for v in values) + 1
    page = np.searchsorted(bounds, rows, side='right') - 1
    starts = np.r_[True, page[1:] != page[:-1]]
    union = (np.minimum, np.minimum, np.maximum, np.maximum)
    for _ in range(rounds):
        piece = np.cumsum(starts) - 1
        last = np.r_[starts[1:], True]
        head = [_within(f, v, piece, span) for f, v in zip(union, values, strict=True)]
        tail = [
            _within(f, v[::-1], piece[-1] - piece[::-1], span)[::-1]
            for f, v in zip(union, values, strict=True)
        ]
        left = (head[2] - head[0] + tile) * (head[3] - head[1] + tile)
        right = (tail[2] - tail[0] + tile) * (tail[3] - tail[1] + tile)
        whole = left[last][piece]
        both = left[:-1] + right[1:]
        inside = np.flatnonzero(~last[:-1] & (both <= saving * whole[:-1]))
        inside = inside[np.lexsort((both[inside], piece[inside]))]
        best = (
            inside[np.r_[True, piece[inside][1:] != piece[inside][:-1]]] if len(inside) else inside
        )
        if not len(best):
            break
        starts[best + 1] = True
        cuts = np.union1d(cuts, rows[best + 1])
    return cuts


def _pieces(cells, bounds, cuts, box):
    """Per page holding rows `bounds[i]:bounds[i + 1]`, its pieces split at
    `cuts` as a flat list: each piece's rows, then its box's minimum cell
    column and row minus the page box's, width and height; empty for a page
    of one piece. `box` holds the pages' boxes."""
    edges = np.union1d(bounds, cuts)
    (px0, py0, px1, py1), _ = page_boxes(cells, edges)
    page = np.searchsorted(bounds, edges[:-1], side='right') - 1
    out = [[] for _ in range(len(bounds) - 1)]
    for i in np.unique(page[np.searchsorted(edges, cuts)]):
        at = np.flatnonzero(page == i)
        flat = np.column_stack(
            (
                edges[at + 1] - edges[at],
                px0[at] - box[0][i],
                py0[at] - box[1][i],
                px1[at] - px0[at],
                py1[at] - py0[at],
            )
        )
        out[i] = flat.ravel().tolist()
    return out


def primary_schema(metadata, columns):
    """The footer `metadata`'s schema cut down to the flat top-level `columns`
    as a Thrift FileMetaData without rows, base64 encoded, from which a
    reader decodes their pages without the footer."""
    elements = get(metadata, 2)[1]
    flat = flat_columns(metadata)
    root = dict(elements[0])
    set_value(root, 5, len(columns))
    schema = {
        1: metadata[1],
        2: (9, (12, [root, *(flat[name][0] for name in columns)])),
        3: (6, 0),
        4: (9, (12, [])),
    }
    return base64.b64encode(dumps(schema)).decode()


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


def _runs(offset, length, first, count):
    """Byte ranges and rows of consecutive pages: each one's bytes after the
    previous one's end, compressed length, first row after the previous one's
    end row, and rows. The first counts from zero."""
    offset, length, first, count = (np.asarray(v, np.int64) for v in (offset, length, first, count))
    return [
        (offset - np.r_[0, offset + length][: len(offset)]).tolist(),
        length.tolist(),
        (first - np.r_[0, first + count][: len(first)]).tolist(),
        count.tolist(),
    ]


def _read_runs(columns):
    """The pages `_runs` stores, as arrays, in byte and row order."""
    gaps, length, rows, count = (_column(values) for values in columns)
    if not len(gaps) == len(length) == len(rows) == len(count):
        raise ValueError('malformed page run')
    if (gaps < 0).any() or (length <= 0).any() or (rows < 0).any() or (count <= 0).any():
        raise ValueError('page runs overlap or are empty')
    end, last = np.cumsum(gaps + length), np.cumsum(rows + count)
    return {'offset': end - length, 'length': length, 'first': last - count, 'count': count}


def _box_node(level, x0, y0, x1, y1, offset, length):
    """A primary directory node's leading columns, one value per entry in row
    order: minimum column and row cell minus the previous entry's, width and
    height in cells, bytes between the previous entry's end and this entry's
    start, and compressed length. The first entry counts from zero."""
    x0, y0, x1, y1, offset, length = (
        np.asarray(v, np.int64) for v in (x0, y0, x1, y1, offset, length)
    )
    return [
        level,
        np.diff(x0, prepend=0).tolist(),
        np.diff(y0, prepend=0).tolist(),
        (x1 - x0).tolist(),
        (y1 - y0).tolist(),
        (offset - np.r_[0, offset + length][: len(offset)]).tolist(),
        length.tolist(),
    ]


def _box_leaf(pages, start, end, columns):
    """A level-zero primary node over geometry pages `start:end`, adding per
    page its first global row minus the previous page's end, its rows and its
    pieces, then, for each source column in `columns`, the runs of that
    column's pages overlapping the leaf's rows."""
    first, count = pages['first'][start:end], pages['count'][start:end]
    runs = []
    for column in columns:
        if start < end:
            a = np.searchsorted(column['first'] + column['count'], first[0], side='right')
            b = np.searchsorted(column['first'], first[-1] + count[-1])
        else:
            a = b = 0
        runs.append(_runs(*(column[k][a:b] for k in RUN_FIELDS)))
    head = _box_node(0, *(pages[k][start:end] for k in (*BOX_FIELDS, 'offset', 'length')))
    return [
        *head,
        *_runs(pages['offset'][start:end], pages['length'][start:end], first, count)[2:],
        pages['pieces'][start:end],
        runs,
    ]


def read_box_node(node, columns):
    """A primary directory node's level and its entries as arrays: boxes
    `x0, y0, x1, y1` in cells, byte offsets and lengths, and for a leaf each
    page's first row and rows plus, under `runs`, each of the `columns`
    source columns' pages over the leaf's rows. Pages are in byte and row
    order."""
    if not isinstance(node, list) or len(node) < 7:
        raise ValueError('malformed primary directory node')
    level = node[0]
    if type(level) is not int or not 0 <= level <= 32:
        raise ValueError('invalid directory level')
    if len(node) != (7 if level else 11):
        raise ValueError('malformed primary directory node')
    west, south, width, height, gaps, length = (_column(values) for values in node[1:7])
    if not len(west) == len(south) == len(width) == len(height) == len(gaps) == len(length):
        raise ValueError('malformed primary directory node')
    if (width < 0).any() or (height < 0).any():
        raise ValueError('primary directory box with a negative extent')
    if (gaps < 0).any() or (length <= 0).any():
        raise ValueError('directory byte ranges overlap or are empty')
    x0, y0, end = np.cumsum(west), np.cumsum(south), np.cumsum(gaps + length)
    entries = {
        'x0': x0,
        'y0': y0,
        'x1': x0 + width,
        'y1': y0 + height,
        'offset': end - length,
        'length': length,
    }
    if level:
        return level, entries
    rows, count = _column(node[7]), _column(node[8])
    if len(rows) != len(x0) or len(count) != len(x0) or (rows < 0).any() or (count <= 0).any():
        raise ValueError('malformed primary directory leaf')
    entries['first'] = np.cumsum(rows + count) - count
    entries['count'] = count
    entries.update(_read_pieces(node[9], entries))
    runs = node[10]
    if not isinstance(runs, list) or len(runs) != len(columns):
        raise ValueError('primary directory leaf disagrees with its columns')
    if not all(isinstance(r, list) and len(r) == 4 for r in runs):
        raise ValueError('malformed page run')
    entries['runs'] = [_read_runs(r) for r in runs]
    return 0, entries


def _read_pieces(stored, pages):
    """A leaf's pieces as flat arrays in row order: each piece's page, first
    global row, rows and box. Each page holds none or two or more pieces,
    whose rows add up to the page's and whose boxes lie within its box."""
    if not isinstance(stored, list) or len(stored) != len(pages['count']):
        raise ValueError('malformed primary directory pieces')
    if not all(isinstance(p, list) and len(p) % 5 == 0 and len(p) != 5 for p in stored):
        raise ValueError('malformed primary directory pieces')
    flat = _column([v for p in stored for v in p]).reshape(-1, 5)
    page = np.repeat(np.arange(len(stored)), [len(p) // 5 for p in stored])
    count, dx, dy, width, height = flat.T
    if (count <= 0).any() or (flat[:, 1:] < 0).any():
        raise ValueError('malformed primary directory pieces')
    x0, y0 = pages['x0'][page] + dx, pages['y0'][page] + dy
    x1, y1 = x0 + width, y0 + height
    if (x1 > pages['x1'][page]).any() or (y1 > pages['y1'][page]).any():
        raise ValueError('primary directory piece outside its page box')
    total = np.bincount(page, count, len(stored))
    split = np.bincount(page, minlength=len(stored)) > 0
    if (total[split] != pages['count'][split]).any():
        raise ValueError('primary directory pieces disagree with their page rows')
    end = np.cumsum(count)
    start = end - count - np.r_[0, end][np.searchsorted(page, page)] + pages['first'][page]
    return {
        'piece_page': page,
        'piece_first': start,
        'piece_count': count,
        'piece_x0': x0,
        'piece_y0': y0,
        'piece_x1': x1,
        'piece_y1': y1,
    }


def _index_runs(index, rows, start):
    """The pages a chunk's offset index lists, for a chunk of `rows` rows whose
    first global row is `start`, and the chunk's page bounds: page i holds its
    rows `bounds[i]:bounds[i + 1]`."""
    locations = get(index, 1)[1]
    bounds = np.array([get(p, 3) for p in locations] + [rows], np.int64)
    if not len(locations) or bounds[0] != 0 or (np.diff(bounds) <= 0).any():
        raise ValueError('an indexed column has an invalid offset index')
    part = {
        'offset': np.array([get(p, 1) for p in locations], np.int64),
        'length': np.array([get(p, 2) for p in locations], np.int64),
        'first': bounds[:-1] + start,
        'count': np.diff(bounds),
    }
    return part, bounds


def _lod_top(level, entries):
    return _node(level, *(entries[k] for k in ('low', 'high', 'offset', 'length')))


def _box_top(level, entries):
    return _box_node(level, *(entries[k] for k in (*BOX_FIELDS, 'offset', 'length')))


def _lod_union(batch):
    return {'low': batch['low'][0], 'high': batch['high'][-1]}


def _box_union(batch):
    return {
        'x0': min(batch['x0']),
        'y0': min(batch['y0']),
        'x1': max(batch['x1']),
        'y1': max(batch['y1']),
    }


def _branches(target, level, entries, top, union):
    """Write nodes of up to BRANCH_ENTRIES of a tree's top `entries` at
    `level`, and return the next level's entries."""
    parents = {k: [] for k in entries}
    for first in range(0, len(entries['offset']), BRANCH_ENTRIES):
        batch = {k: v[first : first + BRANCH_ENTRIES] for k, v in entries.items()}
        raw = _json(top(level, batch))
        for k, v in union(batch).items():
            parents[k].append(v)
        parents['offset'].append(target.tell())
        parents['length'].append(len(raw))
        target.write(raw)
    return level + 1, parents


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


def flat_columns(metadata):
    """The flat (non-nested, non-repeated) top-level columns of the footer
    `metadata`, in schema order: name to schema element and column chunk
    position."""
    elements = get(metadata, 2)[1]
    flat, i, leaf = {}, 1, 0
    while i < len(elements):
        size, count = _subtree(elements, i)
        element = elements[i]
        if 5 not in element and get(element, 3) != 2:
            flat[get(element, 4).decode()] = (element, leaf)
        i, leaf = i + size, leaf + count
    return flat


def row_metadata(metadata, columns):
    """The footer `metadata` cut down to the flat top-level `columns`: their
    schema, their chunks in every row group without statistics or column
    indexes, and the `geo` key/value, so a reader can read rows of those
    columns without the full footer."""
    elements = get(metadata, 2)[1]
    flat = flat_columns(metadata)
    if not all(name in flat for name in columns):
        raise ValueError('row metadata takes only flat top-level columns')
    kept = [flat[name][0] for name in flat if name in columns]
    leaves = [flat[name][1] for name in flat if name in columns]
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
    column chunks into one ordinary chunk in the original row group. Pages are
    v1 data pages of PLAIN values without a dictionary, as the profile
    requires, so every page is independently decodable. Consecutive all-null
    tiles share a run; they carry no overview payload.
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
        data_page_version='1.0',
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
    primary = geo['primary_column']
    root = {'version': VERSION, 'lod': lod, 'primary': primary, 'codecs': {}}
    flat = [f.name for f in parquet.schema_arrow if not pa.types.is_nested(f.type)]
    root['columns'] = [name for name in flat if name != primary]
    bbox_column, bbox_fields = covering(geo)
    bounds = [180.0, 90.0, -180.0, -90.0]
    for g in range(parquet.metadata.num_row_groups):
        group = parquet.metadata.row_group(g)
        columns = {
            group.column(c).path_in_schema: group.column(c) for c in range(group.num_columns)
        }
        for i, leaf in enumerate(f'{bbox_column}.{field}' for field in bbox_fields):
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
        # The primary directory locates pages of the primary geometry and of the
        # carried attributes' source columns that decode on their own.
        flat_chunks = flat_columns(metadata)
        if primary not in flat_chunks or get(flat_chunks[primary][0], 1) != 6:
            raise ValueError('the primary geometry must be a flat binary column')

        def standalone(name):
            leaf = flat_chunks[name][1]
            return all(
                4 in chunk and 11 not in get(chunk, 3)
                for chunk in (get(group, 1)[1][leaf] for group in groups)
            )

        def codecs(name):
            leaf = flat_chunks[name][1]
            return {get(get(get(group, 1)[1][leaf], 3), 4) for group in groups}

        if not standalone(primary):
            raise ValueError('the primary geometry needs offset indexes and no dictionary')
        if len(codecs(primary)) != 1:
            raise ValueError('the primary geometry codec must agree across row groups')
        attributes = [
            a['name']
            for a in lod['attributes']
            if a['name'] in flat_chunks
            and a['name'] != primary
            and standalone(a['name'])
            and len(codecs(a['name'])) == 1
        ]
        tracked = {flat_chunks[name][1]: name for name in [primary, *attributes]}
        root['primary_index'] = {
            'attributes': attributes,
            'codecs': [CODECS[next(iter(codecs(name)))] for name in [primary, *attributes]],
            'schema': primary_schema(metadata, [primary, *attributes]),
        }
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
            runs = {name: [] for name in tracked.values()}
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
                            if c in tracked:
                                part, bounds = _index_runs(index, get(group, 3), starts[g])
                                if tracked[c] == primary:
                                    bbox = parquet.read_row_group(g, columns=[bbox_column])
                                    cells = covering_cells(
                                        bbox.column(0).combine_chunks(), bbox_fields
                                    )
                                    box, shown = page_boxes(cells, bounds)
                                    pieces = _pieces(cells, bounds, page_pieces(cells, bounds), box)
                                    part = dict(zip(BOX_FIELDS, box, strict=True)) | part
                                    part = {k: v[shown] for k, v in part.items()}
                                    part['pieces'] = [pieces[i] for i in np.flatnonzero(shown)]
                                runs[tracked[c]].append(part)
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
                # too large, group the largest top node's entries under a new
                # level of branches, fetched lazily and then shared by a pan.
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
                    trees[name] = (1, shards, _lod_top, _lod_union)
                # The primary tree's single leaf stays in the root while it fits.
                exact = {
                    k: np.concatenate([part[k] for part in runs[primary]])
                    for k in (*BOX_FIELDS, *RUN_FIELDS)
                }
                exact['pieces'] = [p for part in runs[primary] for p in part['pieces']]
                columns_runs = [
                    {k: np.concatenate([part[k] for part in runs[name]]) for k in RUN_FIELDS}
                    for name in attributes
                ]
                count = len(exact['offset'])
                leaves = [
                    (_box_leaf(exact, first, min(first + leaf_pages, count), columns_runs), first)
                    for first in range(0, count, leaf_pages)
                ] or [(_box_leaf(exact, 0, 0, columns_runs), 0)]

                def write_leaves():
                    boxes = {k: [] for k in (*BOX_FIELDS, 'offset', 'length')}
                    for leaf, first in leaves:
                        raw = _json(leaf)
                        batch = {k: exact[k][first : first + leaf_pages] for k in BOX_FIELDS}
                        for k, v in _box_union(batch).items():
                            boxes[k].append(v)
                        boxes['offset'].append(target.tell())
                        boxes['length'].append(len(raw))
                        target.write(raw)
                    return 1, boxes

                exact_tree = None if len(leaves) == 1 else write_leaves()
                while True:
                    root['directories'] = {
                        name: top(level, shards) for name, (level, shards, top, _) in trees.items()
                    }
                    exact_top = leaves[0][0] if exact_tree is None else _box_top(*exact_tree)
                    root['primary_index']['directory'] = exact_top
                    if len(_json(root)) <= _ROOT_TARGET_BYTES:
                        break
                    # the top nodes that can shrink, the primary directory's under None
                    tops = {
                        name: root['directories'][name]
                        for name, (_, shards, _, _) in trees.items()
                        if len(shards['offset']) > 1
                    }
                    if len(exact_tree[1]['offset']) > 1 if exact_tree else count:
                        tops[None] = exact_top
                    if not tops:
                        raise ValueError('LOD metadata exceeds the 16 KiB bootstrap')
                    largest = max(tops, key=lambda name: len(_json(tops[name])))
                    if largest is not None:
                        level, shards, top, union = trees[largest]
                        trees[largest] = (*_branches(target, level, shards, top, union), top, union)
                    elif exact_tree is None:
                        exact_tree = write_leaves()
                    else:
                        exact_tree = _branches(target, *exact_tree, _box_top, _box_union)
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
