"""Exhaustive validation and logical source projection against independent Arrow values."""

import gzip
import json
import re
import struct
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
import shapely
from click.testing import CliRunner
from geoparquet_lod import _validate_index, validate, write
from geoparquet_lod._compact import dumps, get, loads, loads_prefix, set_value
from geoparquet_lod._layout import (
    _node,
    finalize_layout,
    primary_leaf,
    primary_schema,
    read_node,
    read_primary_node,
    row_metadata,
)
from geoparquet_lod.cli import cli
from geoparquet_lod.validate import BOUNDS, ValidationError, _outward

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def indexed(tmp_path_factory):
    work = tmp_path_factory.mktemp('validation')
    src, out = work / 'source.parquet', work / 'indexed.parquet'
    n = 240
    table = pa.table(
        {
            'id': range(n),
            'odd.name': range(n),
            'bracket[0]': range(n),
            'details': pa.array(
                [
                    None if i % 5 == 0 else {'height': None if i % 7 == 0 else i, 'list': i + 1}
                    for i in range(n)
                ],
                type=pa.struct([('height', pa.int32()), ('list', pa.int32())]),
            ),
            'floors': pa.array(
                [
                    None
                    if i % 5 == 0
                    else []
                    if i % 7 == 0
                    else [None, {'name': f'a{i}'}, {'name': None}]
                    for i in range(n)
                ],
                type=pa.list_(pa.struct([('name', pa.string())])),
            ),
            'nums': pa.array(
                [None if i % 5 == 0 else [] if i % 7 == 0 else [i, None, i + 1] for i in range(n)],
                type=pa.list_(pa.int32()),
            ),
            'tags': pa.array(
                [
                    None
                    if i % 5 == 0
                    else []
                    if i % 7 == 0
                    else [('name', f't{i}'), ('nullable', None)]
                    for i in range(n)
                ],
                type=pa.map_(pa.string(), pa.string()),
            ),
            'geometry': shapely.to_wkb(
                [
                    shapely.box(
                        -105 + i % 20 * 0.06,
                        39.7 + i // 20 * 0.06,
                        -104.92 + i % 20 * 0.06,
                        39.78 + i // 20 * 0.06,
                    )
                    for i in range(n)
                ]
            ),
        }
    ).replace_schema_metadata(
        {
            b'geo': json.dumps(
                {
                    'version': '1.1.0',
                    'primary_column': 'geometry',
                    'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
                }
            ).encode()
        }
    )
    pq.write_table(table, src)
    write(
        src,
        out,
        zooms=[6, 8],
        complete_from=8,
        row_group_rows=64,
        page_rows=16,
        workers=1,
        log=lambda *_: None,
    )
    return out


def test_logical_projection(indexed):
    expected = []
    for row in pq.read_table(indexed).to_pylist():
        details, floors, nums, tags = [row[n] for n in ('details', 'floors', 'nums', 'tags')]
        tags = dict(tags) if tags is not None else None
        expected.append(
            {
                'id': row['id'],
                'odd.name': row['odd.name'],
                'bracket[0]': row['bracket[0]'],
                'details': details,
                'details.height': details['height'] if details else None,
                'details.list': details['list'] if details else None,
                'floors': floors,
                'floors[1].name': floors[1]['name'] if floors and floors[1] else None,
                'floors[9].name': None,
                'nums': nums,
                'nums[2]': nums[2] if nums else None,
                'tags': tags,
                'tags.name': tags.get('name') if tags else None,
                'tags.missing': None,
            }
        )
    path = indexed.parent / 'expected.json'
    path.write_text(json.dumps(expected))
    subprocess.run(
        ['node', str(ROOT / 'js/test/projection.test.mjs'), str(indexed), str(path)], check=True
    )


def test_full_and_cli(indexed):
    quick, full = validate(indexed, sample_rows=3), validate(indexed, full=True)
    assert quick['mode'] == 'quick' and full['mode'] == 'full'
    assert sum(full['checked_non_null'].values()) > sum(quick['checked_non_null'].values())
    result = CliRunner().invoke(cli, ['validate', str(indexed), '--full'])
    assert result.exit_code == 0, result.output
    assert '"mode": "full"' in result.output
    with pytest.raises(ValueError):
        validate(indexed, sample_rows=-1)


def _root(path):
    raw = path.read_bytes()
    length = int.from_bytes(raw[12:16], 'little')
    return raw, json.loads(gzip.decompress(raw[16 : 16 + length]))


def _write_root(path, raw, root):
    encoded = gzip.compress(json.dumps(root, separators=(',', ':')).encode(), mtime=0)
    assert len(encoded) <= 16368
    path.write_bytes(
        raw[:12]
        + struct.pack('<I', len(encoded))
        + encoded
        + bytes(16384 - 16 - len(encoded))
        + raw[16384:]
    )


@pytest.fixture(scope='module')
def leafy(indexed, tmp_path_factory):
    """`indexed` laid out again with one page per leaf."""
    out = tmp_path_factory.mktemp('leafy') / 'leafy.parquet'
    return finalize_layout(indexed, out, leaf_pages=1, log=lambda *_: None)


class _Appended:
    """Directory nodes added before a file's unchanged footer."""

    def __init__(self, raw):
        self.raw = raw
        self.footer = len(raw) - 8 - int.from_bytes(raw[-8:-4], 'little')
        self.added = bytearray()

    def __call__(self, node):
        """Store `node`, gzip bytes or JSON, and return its offset and length."""
        if not isinstance(node, bytes):
            node = gzip.compress(json.dumps(node).encode(), mtime=0)
        self.added.extend(node)
        return self.footer + len(self.added) - len(node), len(node)

    def write(self, path, root):
        raw = self.raw[: self.footer] + bytes(self.added) + self.raw[self.footer :]
        root['file_bytes'] = len(raw)
        _write_root(path, raw, root)


@pytest.mark.parametrize('arrangement', ['branches', 'same_cell', 'added', 'omitted'])
def test_directory_lists_pages_in_key_order(leafy, tmp_path, arrangement):
    """Leaves copied into another order, under nodes whose bounds match their
    first and last entries, still list every page once, as do leaves with the
    last one repeated or left out. A tile would lose pages, read a cell's
    pages out of row order, or read a page twice."""
    raw, root = _root(leafy)
    name = max(root['directories'], key=lambda n: len(root['directories'][n][1]))
    level, entries = read_node(root['directories'][name])
    low, high = entries['low'], entries['high']
    n = len(low)
    assert level == 1 and n > 4 and (low == high).all() and low[1] == low[2] < low[-1]
    store = _Appended(raw)

    def leaves(order):
        """A level-one node over copies of the leaves in `order`."""
        copies = [
            store(raw[entries['offset'][i] : entries['offset'][i] + entries['length'][i]])
            for i in order
        ]
        return _node(1, low[order], high[order], *zip(*copies, strict=True))

    if arrangement == 'branches':
        parts = [[0, n - 1, 1], list(range(2, n - 1))]
        branches = [store(leaves(part)) for part in parts]
        lows, highs = [low[p[0]] for p in parts], [high[p[-1]] for p in parts]
        root['directories'][name] = _node(2, lows, highs, *zip(*branches, strict=True))
        message = 'out of key order'
    elif arrangement == 'same_cell':
        root['directories'][name] = leaves([0, 2, 1, *range(3, n)])
        message = 'reorders'
    elif arrangement == 'added':
        # every row group matches its pages before the copy is reached
        root['directories'][name] = leaves([*range(n), n - 1])
        message = 'adds'
    else:
        root['directories'][name] = leaves(list(range(n - 1)))
        message = 'omits'
    bad = tmp_path / 'reordered.parquet'
    store.write(bad, root)
    with pytest.raises(ValidationError, match=message):
        validate(bad, full=True)


def test_directory_nodes_are_reached_once(leafy, tmp_path, monkeypatch):
    """A node shared by two parents fails before its subtree is read again,
    however deep the sharing goes."""
    raw, root = _root(leafy)
    name, top = next(iter(root['directories'].items()))
    _, entries = read_node(top)
    cell = int(entries['low'][0])
    assert entries['high'][0] == cell, 'equal one-cell entries are in key order'
    start, length = int(entries['offset'][0]), int(entries['length'][0])
    store = _Appended(raw)
    node = raw[start : start + length]
    for level in range(1, 13):
        # two copies of the node below, both referring to its children
        node = _node(level, [cell] * 2, [cell] * 2, *zip(store(node), store(node), strict=True))
    root['directories'][name] = node
    bad = tmp_path / 'shared.parquet'
    store.write(bad, root)
    decoded = []
    monkeypatch.setattr(
        _validate_index, 'read_node', lambda node: decoded.append(node) or read_node(node)
    )
    with pytest.raises(ValidationError, match='referenced twice'):
        validate(bad, full=True)
    assert len(decoded) < 20


@pytest.fixture(scope='module')
def carried(tmp_path_factory):
    """A file carrying an attribute, so primary leaves locate its pages too,
    and the same laid out again with one page per leaf."""
    work = tmp_path_factory.mktemp('carried')
    n = 240
    boxes = [
        shapely.box(x, y, x + 0.08, y + 0.08)
        for x, y in ((-105 + i % 20 * 0.06, 39.7 + i // 20 * 0.06) for i in range(n))
    ]
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table(
        {
            'height': pa.array([i % 37 for i in range(n)], pa.int32()),
            'geometry': shapely.to_wkb(boxes),
        }
    )
    src = work / 'source.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    out = write(
        src,
        work / 'carried.parquet',
        zooms=[6, 8],
        complete_from=8,
        carry=['height'],
        row_group_rows=64,
        page_rows=16,
        workers=1,
        log=lambda *_: None,
    )
    return out, finalize_layout(out, work / 'leafy.parquet', leaf_pages=1, log=lambda *_: None)


@pytest.mark.parametrize(
    'mutation, message',
    [
        ('box', "run box is not the union of its rows' covering"),
        ('rows', "runs do not add up to their page's rows"),
        ('offset', 'run outside the geometry pages'),
        ('sources', 'misplaces height pages'),
        ('codecs', 'codecs disagree with footer'),
        ('schema', 'schema disagrees with footer'),
        ('attributes', 'distinct flat source columns'),
        ('level', 'malformed directory node'),
        ('top', 'must start above its leaves'),
        ('missing', 'no primary directory'),
    ],
)
def test_primary_directory_corruption(carried, tmp_path, mutation, message):
    """A primary leaf lists geometry pages as runs of their rows, each boxed
    by the union of its rows' coverings, and each attribute's pages over
    those rows; the root holds a node above the leaves."""
    raw, root = _root(carried[0])
    index = root['primary_index']
    level, top = read_node(index['directory'])
    assert level == 1 and len(top['low']) == 1 and index['attributes'] == ['height'], 'one leaf'
    start, length = int(top['offset'][0]), int(top['length'][0])
    leaf = json.loads(gzip.decompress(raw[start : start + length]))
    if mutation == 'box':
        leaf[3][1] += 1
    elif mutation == 'rows':
        leaf[8][-1] -= 1
    elif mutation == 'offset':
        leaf[5][max(i for i, length in enumerate(leaf[6]) if length)] += 1
    elif mutation == 'sources':
        leaf[9][0][1][0] += 1
    elif mutation == 'codecs':
        index['codecs'][0] = 'SNAPPY'
    elif mutation == 'schema':
        footer = len(raw) - 8 - int.from_bytes(raw[-8:-4], 'little')
        index['schema'] = primary_schema(loads(raw[footer:-8]), [root['primary']])
    elif mutation == 'attributes':
        index['attributes'] = ['bbox']
    elif mutation == 'level':
        leaf[0] = 1
    store = _Appended(raw)
    offset, size = store(leaf)
    index['directory'] = _node(1, top['low'], top['high'], [offset], [size])
    if mutation == 'top':
        index['directory'] = leaf
    elif mutation == 'missing':
        del root['primary_index']
    bad = tmp_path / 'bad.parquet'
    store.write(bad, root)
    validate(bad, sample_rows=3)
    with pytest.raises(ValidationError, match=message):
        validate(bad, full=True)


def _leaves(raw, index):
    """The top node's entries and the leaves they locate, decoded."""
    level, entries = read_node(index['directory'])
    assert level == 1
    return entries, [
        read_primary_node(json.loads(gzip.decompress(raw[o : o + n])), index['attributes'])[1]
        for o, n in zip(entries['offset'], entries['length'], strict=True)
    ]


@pytest.mark.parametrize(
    'mutation, message',
    [
        ('omitted', 'leaves out a page whose rows reach it'),
        ('cut', 'cut a page differently'),
        ('cells', 'do not hold every cell once'),
        ('shared', 'referenced twice'),
    ],
)
def test_primary_leaves_hold_every_cell_and_reaching_page(carried, tmp_path, mutation, message):
    """Leaves hold every cell of the directory's zoom once, and each lists
    every page holding a row whose box reaches its cells, cut into the same
    runs wherever it is listed; a leaf is located once."""
    raw, root = _root(carried[1])
    index = root['primary_index']
    entries, leaves = _leaves(raw, index)
    low, high = entries['low'], entries['high']
    first = [set(leaf['first'][leaf['length'] > 0].tolist()) for leaf in leaves]
    # a page two leaves list, and the second of them
    i = next(i for i in range(1, len(leaves)) if first[i] & first[i - 1])
    page = min(first[i] & first[i - 1])
    leaf = leaves[i]
    keep = leaf['offset'] != leaf['offset'][leaf['first'] == page][0]
    if mutation == 'omitted':
        leaf = {k: v[keep] for k, v in leaf.items() if k != 'sources'}
    elif mutation == 'cut':
        run = int(np.flatnonzero(~keep)[0])
        assert leaf['count'][run] > 1, 'the fixture lists a page of several rows'
        leaf = {k: np.insert(v, run + 1, v[run]) for k, v in leaf.items() if k != 'sources'}
        leaf['count'][run], leaf['count'][run + 1] = 1, leaf['count'][run] - 1
        leaf['first'][run + 1] += 1
    else:
        leaf = None
    store = _Appended(raw)
    copies = [
        store(primary_leaf(leaf, leaves[i]['sources']) if j == i and leaf else raw[o : o + n])
        for j, (o, n) in enumerate(zip(entries['offset'], entries['length'], strict=True))
    ]
    if mutation == 'cells':
        low[next(j for j in range(1, len(low)) if high[j] > low[j])] += 1
    if mutation == 'shared':
        # the second level-one node's first entry locates the first one's last leaf
        refs = [*copies[:i], copies[i - 1], *copies[i + 1 :]]
        parts = [
            store(_node(1, low[a:b], high[a:b], *zip(*refs[a:b], strict=True)))
            for a, b in ((0, i), (i, len(low)))
        ]
        node = _node(2, [low[0], low[i]], [high[i - 1], high[-1]], *zip(*parts, strict=True))
    else:
        node = _node(1, low, high, *zip(*copies, strict=True))
    index['directory'] = node
    bad = tmp_path / 'bad.parquet'
    store.write(bad, root)
    with pytest.raises(ValidationError, match=message):
        validate(bad, full=True)


@pytest.mark.parametrize(
    'field, values, message',
    [
        # the first page starts at the int64 maximum, so its end wraps negative
        (1, [2**63 - 1], 'outside data region'),
        # first rows whose differences wrap to nondecreasing values
        (3, [0, 2**63 - 1, -5], 'page ordering'),
    ],
)
def test_offset_index_sums_cannot_wrap(indexed, tmp_path, field, values, message):
    """Offset index values near the int64 limit fail validation, however
    their sums wrap."""
    raw, root = _root(indexed)
    footer = len(raw) - 8 - int.from_bytes(raw[-8:-4], 'little')
    metadata = loads(raw[footer:-8])
    chunk = next(
        c for c in get(get(metadata, 4)[1][0], 1)[1] if get(get(c, 3), 3)[1] == [b'geometry']
    )
    start = get(chunk, 4)
    index = loads(raw[start : start + get(chunk, 5)])
    pages = get(index, 1)[1]
    assert len(pages) > len(values)
    for i, value in enumerate(values):
        set_value(pages[i], field, value)
    moved = dumps(index)
    set_value(chunk, 4, footer)
    set_value(chunk, 5, len(moved))
    flat = [f.name for f in pq.ParquetFile(indexed).schema_arrow if not pa.types.is_nested(f.type)]
    rows = gzip.compress(dumps(row_metadata(metadata, flat)), mtime=0)
    root['row_metadata'] = [footer + len(moved), len(rows)]
    encoded = dumps(metadata)
    root['footer_bytes'] = len(encoded)
    raw = raw[:footer] + moved + rows + encoded + struct.pack('<I', len(encoded)) + b'PAR1'
    root['file_bytes'] = len(raw)
    bad = tmp_path / 'wrapped.parquet'
    _write_root(bad, raw, root)
    with pytest.raises(ValidationError, match=message):
        validate(bad, full=True)


def test_overview_pages_are_v1_plain(indexed, tmp_path, monkeypatch):
    """Readers scan the profile's overview pages directly, so other page
    kinds fail validation."""
    writer = pq.ParquetWriter
    monkeypatch.setattr(
        pq, 'ParquetWriter', lambda *a, **k: writer(*a, **{**k, 'data_page_version': '2.0'})
    )
    out = finalize_layout(indexed, tmp_path / 'v2.parquet', log=lambda *_: None)
    monkeypatch.undo()
    with pytest.raises(ValidationError, match='v1 data pages'):
        validate(out, full=True)


def test_v2_source_pages_are_indexed(carried, tmp_path):
    """Source chunks are copied byte for byte, so the primary directory
    locates v2 geometry and attribute pages as it does v1 ones."""
    table = pq.read_table(carried[0])
    metadata = dict(table.schema.metadata)
    metadata.pop(b'lod_spatial_index', None)
    packed = tmp_path / 'packed.parquet'
    pq.write_table(
        table.replace_schema_metadata(metadata),
        packed,
        row_group_size=64,
        max_rows_per_page=16,
        use_dictionary=False,
        data_page_version='2.0',
        write_page_index=True,
    )
    out = finalize_layout(packed, tmp_path / 'v2.parquet', log=lambda *_: None)
    raw = out.read_bytes()
    group = pq.ParquetFile(out).metadata.row_group(0)
    for name in ('geometry', 'height'):
        chunk = next(
            group.column(i)
            for i in range(group.num_columns)
            if group.column(i).path_in_schema == name
        )
        header, _ = loads_prefix(raw[chunk.data_page_offset :])
        assert get(header, 1) == 3, f'{name} pages are DATA_PAGE_V2'
    assert _root(out)[1]['primary_index']['attributes'] == ['height']
    validate(out, full=True)


@pytest.mark.parametrize(
    'kind, values, nulls, rows, ok',
    [
        (3, 3, 0, 3, True),
        (3, 3, 1, 3, True),
        (0, 3, None, None, True),
        # hyparquet sizes a v2 page's level and value arrays by its values
        (3, 500_000_000, 0, 3, False),
        (3, 3, 0, 4, False),
        (3, 3, -1, 3, False),
        (3, 3, 4, 3, False),
        (0, 500_000_000, None, None, False),
    ],
)
def test_source_page_headers_hold_a_value_or_null_per_row(kind, values, nulls, rows, ok):
    """An indexed column is flat, so a page's header counts as many values as
    its offset index gives it rows, a v2 header as many rows too, and at
    most that many nulls."""
    body = bytes(8)
    if kind == 0:
        page = {1: (5, values), 2: (5, 0), 3: (5, 3), 4: (5, 3)}
    else:
        page = {1: (5, values), 2: (5, nulls), 3: (5, rows), 4: (5, 0), 5: (5, 0), 6: (5, 0)}
    header = {1: (5, kind), 2: (5, len(body)), 3: (5, len(body)), 5 if kind == 0 else 8: (12, page)}
    raw = dumps(header) + body
    chunk = {7: (6, len(raw)), 9: (6, 4)}

    def read(offset, length):
        return raw[offset - 4 : offset - 4 + length]

    check = (read, chunk, np.array([4]), np.array([len(raw)]), np.array([3]))
    if ok:
        _validate_index._check_source_pages(*check)
        return
    with pytest.raises(ValueError, match='indexed column page header disagrees'):
        _validate_index._check_source_pages(*check)


@pytest.mark.parametrize(
    'mutation',
    [
        'root_bounds',
        'level',
        'leaf_rows',
        'leaf_fences',
        'leaf_offset',
        'leaf_nulls',
        'codec',
        'primary',
        'footer_bytes',
        'row_metadata',
        'magic',
    ],
)
def test_spatial_index_corruption(indexed, tmp_path, mutation):
    raw, root = _root(indexed)
    bad = tmp_path / 'bad.parquet'
    if mutation in ('codec', 'primary', 'footer_bytes'):
        if mutation == 'codec':
            root['codecs'][next(iter(root['codecs']))] = 'SNAPPY'
        elif mutation == 'primary':
            root['primary'] = 'wrong_geometry'
        else:
            root['footer_bytes'] += 1
    elif mutation == 'row_metadata':
        # a stale copy: the row metadata of a file with one more row
        offset, length = root['row_metadata']
        rows = loads(gzip.decompress(raw[offset : offset + length]))
        set_value(rows, 3, get(rows, 3) + 1)
        encoded = gzip.compress(dumps(rows), mtime=0)
        footer = len(raw) - 8 - int.from_bytes(raw[-8:-4], 'little')
        raw = raw[:footer] + encoded + raw[footer:]
        root['row_metadata'] = [footer, len(encoded)]
        root['file_bytes'] = len(raw)
    elif mutation in ('root_bounds', 'level'):
        # [level, cells, spans, gaps, lengths] of one LOD's top node
        node = next(node for node in root['directories'].values() if node[1])
        if mutation == 'root_bounds':
            node[1][0] += 1000000
        else:
            node[0] += 1
    elif mutation == 'magic':
        raw = raw[:4] + bytes(8) + raw[12:]
    else:
        # Append one mutated leaf before the unchanged footer and point the last
        # entry of a level-one top node at it; all page offsets stay valid.
        node = next(n for n in root['directories'].values() if n[0] == 1 and n[1])
        start = sum(node[3]) + sum(node[4][:-1])
        leaf = json.loads(gzip.decompress(raw[start : start + node[4][-1]]))
        if mutation == 'leaf_rows':
            leaf[6][0] += 1
        elif mutation == 'leaf_fences':
            leaf[7][0] = [1, 1]
        elif mutation == 'leaf_nulls':
            leaf[8][0] = 1
        else:
            leaf[3][0] += 1
        encoded = gzip.compress(json.dumps(leaf).encode(), mtime=0)
        footer = len(raw) - 8 - int.from_bytes(raw[-8:-4], 'little')
        node[3][-1] += footer - start
        node[4][-1] = len(encoded)
        raw = raw[:footer] + encoded + raw[footer:]
        root['file_bytes'] = len(raw)
    _write_root(bad, raw, root)
    validate(bad, sample_rows=3)  # quick mode intentionally does not traverse directories
    with pytest.raises(ValidationError, match='spatial index'):
        validate(bad, full=True)


def test_corrupt_packed_row_beyond_sample(indexed, tmp_path, monkeypatch):
    table = pq.read_table(indexed)
    lod = table['lod'].combine_chunks()
    name = lod.type[0].name
    children = [lod.field(i).to_pylist() for i in range(lod.type.num_fields)]
    target = next(i for i in range(len(table) - 1, 64, -1) if children[0][i] is not None)
    children[0][target] += b'\x00'
    replacement = pa.StructArray.from_arrays(
        [pa.array(values, type=pa.binary()) for values in children], fields=list(lod.type)
    )
    table = table.set_column(
        table.schema.get_field_index('lod'), table.schema.field('lod'), replacement
    )
    metadata = dict(table.schema.metadata)
    metadata.pop(b'lod_spatial_index', None)
    table = table.replace_schema_metadata(metadata)
    bad = tmp_path / 'late.parquet'
    pq.write_table(table, bad, row_group_size=64, write_page_index=True)
    validate(bad, sample_rows=3)
    with pytest.raises(ValidationError, match='trailing bytes'):
        validate(bad, full=True)
    # batches of a few values each; the row still counts from the row group's first
    monkeypatch.setattr('geoparquet_lod.unpack.BATCH_BYTES', 64)
    where = f'row group {target // 64}, row {target % 64}, lod {name}: invalid packed row'
    with pytest.raises(ValidationError, match=where):
        validate(bad, full=True)
    assert name in validate(indexed, full=True)['lods']


def varints(*values):
    out = bytearray()
    for value in values:
        while value >= 128:
            out.append((value & 127) | 128)
            value >>= 7
        out.append(value)
    return bytes(out)


@pytest.mark.parametrize(
    'value, message',
    [
        # one polygon of one ring storing two vertices, no attributes, no copies
        (varints(1, 1, 2, 0, 0, 2, 0, 0, 0), 'polygon ring with fewer than three vertices'),
        (varints(0, 0, 0), 'empty geometry on a shown row'),
        (varints(1, 1, 3, 2**33, 0, 2, 0, 0, 2, 0, 0), 'a grid coordinate outside int32'),
    ],
)
def test_invalid_geometry_names_its_row(indexed, tmp_path, monkeypatch, value, message):
    table = pq.read_table(indexed)
    lod = table['lod'].combine_chunks()
    children = [lod.field(i).to_pylist() for i in range(lod.type.num_fields)]
    target = next(i for i in range(len(table) - 1, 64, -1) if children[0][i] is not None)
    children[0][target] = value
    replacement = pa.StructArray.from_arrays(
        [pa.array(values, type=pa.binary()) for values in children], fields=list(lod.type)
    )
    table = table.set_column(
        table.schema.get_field_index('lod'), table.schema.field('lod'), replacement
    )
    metadata = dict(table.schema.metadata)
    metadata.pop(b'lod_spatial_index', None)
    bad = tmp_path / 'geometry.parquet'
    table = table.replace_schema_metadata(metadata)
    pq.write_table(table, bad, row_group_size=64, write_page_index=True)
    validate(bad, sample_rows=3)
    where = f'row group {target // 64}, row {target % 64}, lod {lod.type[0].name}: {message}'
    with pytest.raises(ValidationError, match=where):
        validate(bad, full=True)
    monkeypatch.setattr('geoparquet_lod.unpack.BATCH_BYTES', 64)
    with pytest.raises(ValidationError, match=where):
        validate(bad, full=True)


@pytest.mark.parametrize('mutation', ['tile', 'id'])
def test_invalid_spill_identifiers(indexed, tmp_path, mutation):
    from unpacker import Unpacker

    table = pq.read_table(indexed)
    lod = table['lod'].combine_chunks()
    geo = json.loads(table.schema.metadata[b'geo'])
    children = [lod.field(i).to_pylist() for i in range(lod.type.num_fields)]
    found = False
    for child_index, field in enumerate(lod.type):
        family = geo['lod_columns']['lods'][field.name]['geometry_type']
        for index, blob in enumerate(children[child_index]):
            if blob is None or index < 64:
                continue
            u = Unpacker(blob, family, [], {})
            u.geometry()
            u.attrs()
            if not u.varint():
                continue
            start = u.pos
            u.varint()  # tile
            if mutation == 'id':
                start = u.pos
                u.varint()
            end = u.pos
            value = (99 << 48) if mutation == 'tile' else len(table) + 1
            encoded = bytearray()
            while value >= 128:
                encoded.append((value & 127) | 128)
                value >>= 7
            encoded.append(value)
            children[child_index][index] = blob[:start] + encoded + blob[end:]
            found = True
            break
        if found:
            break
    assert found, 'fixture exercises spill copies outside the quick sample'
    replacement = pa.StructArray.from_arrays(
        [pa.array(values, type=pa.binary()) for values in children], fields=list(lod.type)
    )
    table = table.set_column(
        table.schema.get_field_index('lod'), table.schema.field('lod'), replacement
    )
    metadata = dict(table.schema.metadata)
    metadata.pop(b'lod_spatial_index', None)
    table = table.replace_schema_metadata(metadata)
    bad = tmp_path / f'spill-{mutation}.parquet'
    pq.write_table(table, bad, row_group_size=64, write_page_index=True)
    validate(bad, sample_rows=3)
    with pytest.raises(ValidationError, match='spill'):
        validate(bad, full=True)


def _rewritten(indexed, path, change):
    """`indexed` with `change(table, geo)` applied to its rows and geo
    metadata, written without its spatial index."""
    table = pq.read_table(indexed)
    metadata = dict(table.schema.metadata)
    geo = json.loads(metadata[b'geo'])
    table = change(table, geo)
    metadata[b'geo'] = json.dumps(geo).encode()
    metadata.pop(b'lod_spatial_index', None)
    table = table.replace_schema_metadata(metadata)
    pq.write_table(table, path, row_group_size=64, write_page_index=True)
    return path


def _replace(table, name, column, nullable=True):
    field = pa.field(name, column.type, nullable=nullable)
    return table.set_column(table.schema.get_field_index(name), field, column)


def _null_at(column, row):
    """`column` with `row` null. A struct's required children keep their
    values under it, as Arrow's Parquet writer requires."""
    column = column.combine_chunks()
    mask = column.is_null().to_numpy(zero_copy_only=False)
    mask[row] = True
    if pa.types.is_struct(column.type):
        children = [column.field(i) for i in range(column.type.num_fields)]
        return pa.StructArray.from_arrays(children, fields=list(column.type), mask=pa.array(mask))
    return pc.if_else(pa.array(mask), pa.scalar(None, column.type), column)


@pytest.mark.parametrize(
    'mutation, message',
    [
        ('struct', 'bbox must be a struct column, not float'),
        ('required', 'bbox must be nullable'),
        ('names', 'bbox fields must be xmin, ymin, xmax and ymax, not minx, ymin, xmax, ymax'),
        ('double', 'bbox.xmin must be float32, not double'),
        ('nullable', 'bbox.ymin must be required, not nullable'),
        (
            'path',
            'geo.columns.geometry.covering.bbox.xmax must be ["bbox", "xmax"], not ["bbox", "maxx"]',
        ),
        ('declared', 'geo.columns.geometry.covering.bbox must declare xmin, ymin, xmax and ymax'),
        ('geometry', 'primary geometry column geometry missing'),
        ('wkt', 'primary geometry column geometry must be WKB binary, not string'),
        ('key', 'key column hilbert missing'),
    ],
)
def test_covering_geometry_and_key_columns_are_checked(indexed, tmp_path, mutation, message):
    """The covering is a nullable struct of four required float32 bounds
    declared at their own paths, beside a WKB primary geometry and the key
    column. The footer shows each, so a sample of no rows finds them."""

    def change(table, geo):
        bbox = table['bbox'].combine_chunks()
        fields = [pa.field(bound, pa.float32(), nullable=False) for bound in BOUNDS]
        declared = geo['columns']['geometry']['covering']['bbox']
        if mutation == 'struct':
            return _replace(table, 'bbox', bbox.field('xmin'))
        if mutation == 'required':
            return _replace(table, 'bbox', bbox, nullable=False)
        if mutation == 'names':
            fields[0] = fields[0].with_name('minx')
        elif mutation == 'double':
            fields[0] = fields[0].with_type(pa.float64())
        elif mutation == 'nullable':
            fields[1] = fields[1].with_nullable(True)
        elif mutation == 'path':
            declared['xmax'] = ['bbox', 'maxx']
        elif mutation == 'declared':
            del declared['ymax']
        elif mutation == 'wkt':
            wkt = shapely.to_wkt(shapely.from_wkb(table['geometry'].to_numpy()))
            return _replace(table, 'geometry', pa.array(wkt, pa.string()))
        elif mutation == 'geometry':
            return table.drop_columns(['geometry'])
        else:
            return table.drop_columns(['hilbert'])
        bounds = [bbox.field(i).cast(f.type) for i, f in enumerate(fields)]
        return _replace(table, 'bbox', pa.StructArray.from_arrays(bounds, fields=fields))

    bad = _rewritten(indexed, tmp_path / f'{mutation}.parquet', change)
    with pytest.raises(ValidationError, match=re.escape(message)):
        validate(bad, sample_rows=0)


@pytest.mark.parametrize(
    'kind, message',
    [
        ('halffloat', None),
        ('large_string', None),
        ('date32', 'attribute id: type "date32" is not an integer, float, bool or string type'),
        ('decimal', 'attribute id: type "decimal" is not an integer, float, bool or string type'),
        (None, 'attribute id: type null is not an integer, float, bool or string type'),
    ],
)
def test_carried_attribute_types_are_checked(indexed, tmp_path, kind, message):
    """A carried attribute declares the Arrow name of a type the packer writes."""

    def change(table, geo):
        geo['lod_columns']['attributes'] = [{'name': 'id', 'type': kind}]
        return table

    bad = _rewritten(indexed, tmp_path / 'attributes.parquet', change)
    if message is None:
        validate(bad, full=True)
        return
    with pytest.raises(ValidationError, match=re.escape(message)):
        validate(bad, sample_rows=0)


@pytest.mark.parametrize(
    'mutation, message',
    [
        ('covering', 'null bbox covering on a geometry with finite bounds'),
        ('null', 'bbox covering on a null geometry'),
        ('empty', 'bbox covering on an empty geometry'),
        ('nan', 'bbox covering on a geometry without finite bounds'),
        ('infinite', 'bbox covering on a geometry without finite bounds'),
        ('invalid', 'primary geometry is not valid WKB'),
    ],
)
def test_sampled_coverings_follow_their_geometry(indexed, tmp_path, mutation, message):
    """A sampled row's covering is null exactly where its primary geometry
    has no finite bounds, in either mode; quick mode leaves a row past the
    sample unchecked."""
    row = 1

    def change(table, geo):
        if mutation == 'covering':
            return _replace(table, 'bbox', _null_at(table['bbox'], row))
        wkb = table['geometry'].to_pylist()
        wkb[row] = {
            'null': None,
            'empty': shapely.to_wkb(shapely.Polygon()),
            'nan': shapely.to_wkb(shapely.LineString([(np.nan, 39.7), (np.nan, 39.8)])),
            'infinite': shapely.to_wkb(shapely.LineString([(np.inf, 39.7), (-105, 39.8)])),
        }.get(mutation, b'\x01\x03\x00')
        return _replace(table, 'geometry', pa.array(wkb, pa.binary()))

    bad = _rewritten(indexed, tmp_path / f'{mutation}.parquet', change)
    validate(bad, sample_rows=row)
    for full in (False, True):
        with pytest.raises(ValidationError, match=f'row group 0, row {row}: {message}'):
            validate(bad, sample_rows=row + 1, full=full)


@pytest.mark.parametrize('bound, outward', [('xmin', True), ('ymax', True), ('xmax', False)])
@pytest.mark.parametrize('group, row', [(0, 1), (2, 5)])
def test_coverings_are_their_geometry_bounds(indexed, tmp_path, bound, outward, group, row):
    """A covering is its geometry's bounds rounded outward to float32, so one
    a float32 step wider or narrower is refused: in the sample in either
    mode, and past it in full mode."""

    def change(table, geo):
        bbox = table['bbox'].combine_chunks()
        values = {k: bbox.field(k).to_numpy(zero_copy_only=False).copy() for k in BOUNDS}
        away = -np.inf if (bound in ('xmin', 'ymin')) == outward else np.inf
        at = group * 64 + row
        values[bound][at] = np.nextafter(values[bound][at], np.float32(away))
        children = [pa.array(values[k], pa.float32()) for k in BOUNDS]
        moved = pa.StructArray.from_arrays(children, fields=list(bbox.type), mask=bbox.is_null())
        return _replace(table, 'bbox', moved)

    bad = _rewritten(indexed, tmp_path / 'moved.parquet', change)
    message = f'row group {group}, row {row}: bbox.{bound} is '
    validate(bad, sample_rows=row if group == 0 else 2000)
    if group == 0:
        with pytest.raises(ValidationError, match=message):
            validate(bad, sample_rows=row + 1)
    with pytest.raises(ValidationError, match=message):
        validate(bad, full=True)


def _among(tmp_path, kind, odd, n=60):
    """A source of `n` small `kind` geometries near Denver with `odd`, unless
    None, at row 25."""
    x, y = -105 + np.arange(n) % 10 * 0.01, 39.7 + np.arange(n) // 10 * 0.01
    corners = np.stack([np.column_stack([x, y]), np.column_stack([x, y]) + 0.004], axis=1)
    if kind == 'Polygon':
        shapes = shapely.box(x, y, x + 0.004, y + 0.004)
    elif kind == 'LineString':
        shapes = shapely.linestrings(corners)
    else:
        shapes = shapely.multipoints(corners)
    wkb = shapely.to_wkb(shapes).tolist()
    if odd is not None:
        wkb.insert(25, shapely.to_wkb(odd))
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': [kind]}},
    }
    table = pa.table({'id': range(len(wkb)), 'geometry': pa.array(wkb, pa.binary())})
    src = tmp_path / f'{kind}.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    return src


@pytest.mark.parametrize(
    'kind, odd, bounds',
    [
        # a hole outside its shell reaches past the shell
        (
            'Polygon',
            shapely.Polygon(
                [(-105, 39.7), (-104.99, 39.7), (-104.99, 39.71), (-105, 39.71)],
                [[(-104.9, 39.8), (-104.895, 39.8), (-104.895, 39.805)]],
            ),
            (-105, 39.7, -104.895, 39.805),
        ),
        # a NaN x or y is left out of its own axis alone
        (
            'MultiPoint',
            shapely.MultiPoint([(np.nan, 39.75), (-104.95, np.nan)]),
            (-104.95, 39.75, -104.95, 39.75),
        ),
    ],
)
def test_written_coverings_bound_every_coordinate(tmp_path, kind, odd, bounds):
    """The writer's covering is the least and greatest x and y of all of a
    geometry's coordinates, NaN left out one axis at a time, rounded
    outward, and validation takes a geometry's bounds the same way."""
    src = _among(tmp_path, kind, odd)
    out = write(src, tmp_path / 'out.parquet', zooms=[8, 10, 12], workers=1, log=lambda *_: None)
    validate(out, full=True)
    table = pq.read_table(out, columns=['id', 'bbox'])
    row = table['id'].to_pylist().index(25)
    covering = table['bbox'][row].as_py()
    assert [covering[k] for k in BOUNDS] == _outward(np.array([bounds])).tolist()[0]


@pytest.mark.parametrize(
    'kind, odd',
    [
        ('LineString', shapely.LineString([(np.nan, 10), (np.nan, 11)])),
        ('MultiPoint', shapely.MultiPoint([(np.nan, 10), (np.nan, 11)])),
        ('LineString', shapely.LineString([(np.inf, 10), (-104.95, 11)])),
    ],
)
def test_a_geometry_without_finite_bounds_is_shown_nowhere(tmp_path, kind, odd):
    """A geometry with no x or no y but NaN, or an infinite coordinate, has no
    finite bounds, so as a null or empty one does it gets a null covering and
    key, sorts last and is shown at no zoom, its WKB kept. The extent, grids
    and every other row's overviews are those of the file without it."""
    options = dict(zooms=[8, 10, 12], workers=1, log=lambda *_: None)
    out = write(_among(tmp_path, kind, odd), tmp_path / 'out.parquet', **options)
    validate(out, full=True)
    (tmp_path / 'alone').mkdir()
    alone = write(_among(tmp_path / 'alone', kind, None), tmp_path / 'alone.parquet', **options)
    table, reference = pq.read_table(out), pq.read_table(alone)
    last = table.num_rows - 1
    assert table['id'][last].as_py() == 25, 'it sorts last'
    assert not table['bbox'][last].is_valid and not table['hilbert'][last].is_valid
    assert table['geometry'][last].as_py() == shapely.to_wkb(odd)
    lod = table['lod'].combine_chunks()
    assert not any(lod.field(i)[last].is_valid for i in range(lod.type.num_fields))
    assert lod.slice(0, last).equals(reference['lod'].combine_chunks())
    lods = [
        json.loads(t.schema.metadata[b'geo'])['lod_columns']['lods'] for t in (table, reference)
    ]
    assert lods[0] == lods[1], 'the same grids, on the same origin'


@pytest.mark.parametrize(
    'column, message',
    [
        ('bbox', 'null bbox covering on a row with a Hilbert key'),
        ('hilbert', 'bbox covering on a row with a null Hilbert key'),
    ],
)
def test_coverings_follow_the_key_in_every_row_group(indexed, tmp_path, column, message):
    """Full mode checks in every row group that the covering and the Hilbert
    key are null on the same rows; quick mode samples the first row group only."""
    group, row = 2, 5

    def change(table, geo):
        return _replace(table, column, _null_at(table[column], group * 64 + row))

    bad = _rewritten(indexed, tmp_path / f'{column}.parquet', change)
    validate(bad)
    with pytest.raises(ValidationError, match=f'row group {group}, row {row}: {message}'):
        validate(bad, full=True)
