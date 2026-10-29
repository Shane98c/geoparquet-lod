"""The spatial directory changes access, never the analytical table or row ids."""

import gzip
import importlib
import json
import subprocess
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import chain, drop_rate, validate
from geoparquet_lod import write as build
from geoparquet_lod._compact import dumps, get, loads
from geoparquet_lod._layout import (
    PIECE_SAVING,
    PIECE_TILE,
    covering_cells,
    finalize_layout,
    page_cells,
    page_pieces,
    read_box_node,
    read_node,
)
from geoparquet_lod._validate_index import check_index
from geoparquet_lod.writer import _write_unindexed as write
from geoparquet_lod.writer import hilbert_key
from unpacker import Unpacker

ROOT = Path(__file__).resolve().parents[1]


def _root(raw):
    return json.loads(gzip.decompress(raw[16 : 16 + int.from_bytes(raw[12:16], 'little')]))


def _leaves(raw, node):
    """The decoded leaves under a directory node, in order."""
    level, entries = read_node(node)
    if not level:
        return [entries]
    return [
        leaf
        for offset, length in zip(entries['offset'], entries['length'], strict=True)
        for leaf in _leaves(raw, json.loads(gzip.decompress(raw[offset : offset + length])))
    ]


@pytest.mark.parametrize(
    'family,sparse,leaf_pages,page_bytes,empty',
    [
        ('polygon', False, 16, 16384, False),
        ('polygon', False, 1, 32768, False),
        ('point', False, 16, 16384, False),
        ('point', True, 16, 65536, False),
        ('line', False, 16, 131072, False),
        # the coarsest zoom shows no rows, so its LOD has no pages
        ('polygon', False, 16, 16384, True),
    ],
)
def test_spatial_roundtrip(tmp_path, monkeypatch, family, sparse, leaf_pages, page_bytes, empty):
    rng = np.random.default_rng(913)
    n = 12000 if sparse else 1200
    x, y = -105 + rng.random(n) * 0.25, 39.6 + rng.random(n) * 0.25
    geometry = (
        shapely.points(x, y) if family == 'point' else shapely.box(x, y, x + 0.002, y + 0.001)
    )
    if family == 'line':
        geometry = shapely.linestrings(
            np.stack([np.column_stack([x, y]), np.column_stack([x + 0.003, y + 0.002])], axis=1)
        )
    geometry[0] = None
    geometry[1] = {
        'point': shapely.Point(),
        'line': shapely.LineString(),
        'polygon': shapely.Polygon(),
    }[family]
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {
            'geometry': {
                'encoding': 'WKB',
                'geometry_types': [
                    {'point': 'Point', 'line': 'LineString', 'polygon': 'Polygon'}[family]
                ],
            }
        },
    }
    table = pa.table(
        {
            'geometry': shapely.to_wkb(geometry),
            'name': [f'building-{i}' for i in range(n)],
            'height': pa.array([None if i % 7 == 0 else i % 80 for i in range(n)], pa.int32()),
        }
    ).replace_schema_metadata({b'geo': json.dumps(geo).encode()})
    src, old, new = [tmp_path / name for name in ('source.parquet', 'old.parquet', 'new.parquet')]
    pq.write_table(table, src)
    thinning = drop_rate(10 if sparse else 2.5)
    if empty:
        thinning = chain(thinning, lambda rows, zoom, ctx: rows.slice(0, 0) if zoom == 10 else rows)
    options = dict(
        zooms=range(10, 15),
        complete_from=13,
        thinning=thinning,
        carry=['height', 'name'],
        row_group_rows=n if sparse else 256,
        page_bytes=1024,
        page_rows=32,
        batch_rows=32,
        workers=1,
        log=lambda *_: None,
    )
    write(src, old, **options)
    before = old.read_bytes()
    size = int.from_bytes(before[-8:-4], 'little')
    footer = before[-8 - size : -8]
    assert dumps(loads(footer)) == footer
    if leaf_pages == 1:
        monkeypatch.setattr(
            importlib.import_module('geoparquet_lod._layout'), '_ROOT_TARGET_BYTES', 800
        )
    if family == 'line':
        # Exercise the public build against the unindexed table and the
        # independent JS membership/MVT assertions below.
        build(src, new, **options, lod_page_bytes=page_bytes)
        assert not list(tmp_path.glob('.lod-build-*'))
    else:
        finalize_layout(old, new, leaf_pages=leaf_pages, page_bytes=page_bytes, log=lambda *_: None)
    raw = new.read_bytes()
    root = _root(raw)
    if empty:
        assert root['directories']['f10'] == [1, [], [], [], []]
    leaves = [leaf for top in root['directories'].values() for leaf in _leaves(raw, top)]
    if sparse:
        largest = max(leaf['count'].max() for leaf in leaves if len(leaf['count']))
        assert largest > 2048, 'exercise sparse pages spanning many analytical rows'
    if leaf_pages == 1:
        assert any(top[0] > 1 for top in root['directories'].values()), (
            'fixture exercises directory branches'
        )
        assert root['primary_index']['directory'][0] > 1, 'and primary directory branches'
    assert root['primary_index']['attributes'] == ['height', 'name']
    assert old.read_bytes() == before, 'source is immutable'
    assert validate(new, full=True)['rows'] == n
    a, b = pq.read_table(old), pq.read_table(new)
    # Arrow equality treats NaN != NaN in the covering of empty geometries.
    assert a.drop(['bbox']).equals(b.drop(['bbox'])), (
        'all exact and overview values remain identical, in the same order'
    )
    for coordinate in ['xmin', 'ymin', 'xmax', 'ymax']:
        np.testing.assert_array_equal(
            a['bbox'].combine_chunks().field(coordinate).to_numpy(),
            b['bbox'].combine_chunks().field(coordinate).to_numpy(),
        )
    assert a.schema.metadata[b'geo'] == b.schema.metadata[b'geo']
    con = duckdb.connect()
    assert con.execute('SELECT count(*) FROM read_parquet(?)', [str(new)]).fetchone()[0] == n
    assert (
        con.execute('SELECT geometry, height, name FROM read_parquet(?)', [str(new)]).fetchall()
        == con.execute('SELECT geometry, height, name FROM read_parquet(?)', [str(old)]).fetchall()
    )
    cases = []
    for z in range(10, 16):
        xx = int((-104.88 + 180) / 360 * 2**z)
        yy = int((1 - np.arcsinh(np.tan(np.radians(39.72))) / np.pi) / 2 * 2**z)
        cases.extend([[z, xx + dx, yy + dy] for dx, dy in [(0, 0), (1, 0), (0, 1), (-1, 0)]])
    # World edges include empty intervals before/after the last key, whose host
    # selection must still work. Distant empty views must remain empty.
    cases.extend([[14, 0, 0], [14, 16383, 16383]])
    # Independent full-table reference: exact key membership plus all matching
    # boundary copies, computed without either reader.
    meta = json.loads(a.schema.metadata[b'geo'])['lod_columns']
    fields = [pa.field(f['name'], pa.type_for_alias(f['type'])) for f in meta['attributes']]
    dictionaries = {f['name']: f['values'] for f in meta['attributes'] if 'values' in f}
    decoded = {
        name: [
            Unpacker(blob, info['geometry_type'], fields, dictionaries).row() if blob else None
            for blob in a.column('lod').combine_chunks().field(name).to_pylist()
        ]
        for name, info in meta['lods'].items()
    }
    keys = a.column(meta['key']['column']).to_numpy()
    expected = []
    for z, xx, yy in cases:
        if z > max(meta['zooms']):
            expected.append(None)
            continue
        name = next(name for name, info in meta['lods'].items() if z in info['zooms'])
        span = 4 ** (meta['key']['depth'] - z)
        start = int(hilbert_key(np.array([xx]), np.array([yy]), z)[0]) * span
        tile = z * 2**48 + xx * 2**z + yy
        features = []
        for row, value in enumerate(decoded[name]):
            if value is None:
                continue
            if start <= keys[row] < start + span and value['geometry']:
                features.append([row, value['geometry'], value['attrs']])
            features.extend(
                [[v['id'], v['geometry'], v['attrs']] for v in value['spill'] if v['tile'] == tile]
            )
        expected.append(features)
    cases_path = tmp_path / 'cases.json'
    cases_path.write_text(json.dumps({'cases': cases, 'expected': expected}))
    result = subprocess.run(
        ['node', str(ROOT / 'js/test/spatial.test.mjs'), str(old), str(new), str(cases_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('zooms', [[3], [6, 7], [10, 11]])
def test_cell_bounds_select_like_exact_keys(zooms):
    """Directory bounds are whole cells of an LOD's finest zoom. Every tile
    served from that LOD, at its zooms or coarser, is a whole number of cells,
    so own-page and host-page selection match selection by exact keys."""
    rng = np.random.default_rng(31)
    depth = 10
    zoom = min(depth, max(zooms))
    sentinel = 4**depth
    # clustered keys: repeated cells, cells across pages, then trailing null keys
    centres = rng.integers(0, sentinel, 40)
    keys = np.sort(np.clip(rng.choice(centres, 3000) + rng.integers(-300, 300, 3000), 0, None))
    keys = np.r_[np.minimum(keys, sentinel - 1), [sentinel] * 25].astype(np.uint64)
    bounds = np.unique(np.r_[0, rng.integers(1, len(keys) - 40, 300), len(keys)])
    present = (rng.random(len(keys)) < 0.3) & (keys < sentinel)
    present[bounds[40] : bounds[45]] = False
    pages = page_cells(keys, bounds, present, depth, zoom)
    first, valid = pages['first'], pages['valid']
    assert (valid > 0).all() and (valid < pages['count']).any()
    shift = 2 * (depth - zoom)
    for i, row in enumerate(first):
        cells = [int(k) >> shift for k in keys[row : row + pages['count'][i]]]
        fences = [(c, r) for r, c in enumerate(cells) if r == 0 or c != cells[r - 1]]
        mine = pages['fence_page'] == i
        assert fences == list(zip(pages['fence_cell'][mine], pages['fence_row'][mine], strict=True))
    exact = keys[first].astype(np.int64), keys[first + valid - 1].astype(np.int64)
    span = 4 ** (depth - zoom)
    rounded = pages['low'] * span, (pages['high'] + 1) * span - 1
    assert (rounded[0] <= exact[0]).all() and (rounded[1] >= exact[1]).all()
    if zoom < depth:
        assert (rounded[0] < exact[0]).any() and (rounded[1] > exact[1]).any()

    def select(low, high, z):
        """Per tile at zoom z: pages with own keys, and the copy host page."""
        size = 4 ** (depth - z)
        start = np.arange(4**z, dtype=np.int64) * size
        own = (low[None] < start[:, None] + size) & (high[None] >= start[:, None])
        return own, np.minimum(np.searchsorted(high, start), len(high) - 1)

    for z in range(zoom + 1):
        for by_key, by_cell in zip(select(*exact, z), select(*rounded, z), strict=True):
            np.testing.assert_array_equal(by_key, by_cell)


def test_layout_requires_separate_output(tmp_path):
    with pytest.raises(ValueError, match='separate output'):
        finalize_layout(tmp_path / 'same', tmp_path / 'same')


def test_row_reads_skip_the_footer_and_fetch_offset_indexes_once(tmp_path):
    rng = np.random.default_rng(5)
    n = 6000
    x, y = -105 + rng.random(n) * 0.2, 39.6 + rng.random(n) * 0.2
    table = pa.table(
        {
            **{f'a{i}': pa.array(rng.random(n)) for i in range(6)},
            'GEOID': pa.array([f'08013{i // 7:010d}' for i in range(n)]),
            'geometry': pa.array(shapely.to_wkb(shapely.box(x, y, x + 1e-4, y + 1e-4))),
        }
    )
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    src = tmp_path / 'src.parquet'
    pq.write_table(table.replace_schema_metadata({'geo': json.dumps(geo)}), src)
    out = build(
        src,
        tmp_path / 'rows.lod.parquet',
        zooms=list(range(12)),
        row_group_rows=2000,
        page_rows=256,
        workers=1,
        log=lambda *_: None,
    )
    columns = ['a0', 'a3', 'a5', 'GEOID']
    row = 3100
    meta = pq.ParquetFile(out).metadata
    assert meta.num_row_groups == 3
    expected = pq.read_table(out, columns=columns).slice(row, 1).to_pylist()[0]
    xmin = pq.read_table(out, columns=['bbox']).slice(row, 1)['bbox'][0]['xmin'].as_py()
    starts = np.cumsum([0] + [meta.row_group(g).num_rows for g in range(meta.num_row_groups)])
    g = int(np.searchsorted(starts, row, side='right') - 1)
    data = out.read_bytes()
    length = int.from_bytes(data[-8:-4], 'little')
    footer = loads(data[-8 - length : -8])
    root = json.loads(gzip.decompress(data[16 : 16 + int.from_bytes(data[12:16], 'little')]))
    assert root['footer_bytes'] == length
    offset, size = root['row_metadata']
    rows = loads(gzip.decompress(data[offset : offset + size]))
    names = [get(e, 4).decode() for e in get(rows, 2)[1][1:]]
    schema = pq.ParquetFile(out).schema_arrow
    assert names == [f.name for f in schema if not pa.types.is_nested(f.type)]
    assert size < length
    chunks = [
        chunk
        for chunk in get(get(footer, 4)[1][g], 1)[1]
        if b'.'.join(get(get(chunk, 3), 3)[1]).decode() in columns
    ]
    indexes = [
        min(get(c, 4) for c in chunks),
        max(get(c, 4) + get(c, 5) for c in chunks),
    ]
    # the row group's offset indexes sit together, so the requested ones span
    # no more than the row group's own indexes
    whole = sum(get(c, 5) for c in get(get(footer, 4)[1][g], 1)[1])
    assert indexes[1] - indexes[0] <= whole
    cases = tmp_path / 'cases.json'
    cases.write_text(
        json.dumps(
            {
                'row': row,
                'columns': columns,
                'expected': expected,
                'indexes': indexes,
                'xmin': xmin,
            }
        )
    )
    result = subprocess.run(
        ['node', str(ROOT / 'js/test/row-read.test.mjs'), str(out), str(cases)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_zooming_past_the_finest_lod_reuses_exact_geometry(tmp_path):
    """Tiles past the finest LOD read each exact geometry once, however many
    tiles and zooms it spans."""
    import subprocess

    import shapely

    # one polygon of thousands of vertices, spanning many tiles past the finest zoom
    polygon = shapely.Point(-105.0, 40.0).buffer(0.5, quad_segs=1000)
    table = pa.table(
        {'name': ['one'], 'geometry': pa.array([shapely.to_wkb(polygon)], pa.binary())}
    )
    src = tmp_path / 'one.parquet'
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    pq.write_table(table.replace_schema_metadata({'geo': json.dumps(geo)}), src)
    out = build(
        src, tmp_path / 'one.lod.parquet', zooms=list(range(9)), workers=1, log=lambda *_: None
    )
    result = subprocess.run(
        ['node', str(ROOT / 'js/test/primary-cache.test.mjs'), str(out)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _cheapest_cuts(cells, bounds, rounds=2):
    """The cut rows of a direct search: each round cuts every piece where its
    halves' cost is least, when that is at most PIECE_SAVING of its own."""

    def cost(rows):
        x0, y0, x1, y1 = (c[rows] for c in cells)
        return (x1.max() - x0.min() + PIECE_TILE) * (y1.max() - y0.min() + PIECE_TILE)

    rows = np.flatnonzero(~np.isnan(cells[0]))
    page = np.searchsorted(bounds, rows, side='right') - 1
    pieces, cuts = [rows[page == p] for p in np.unique(page)], []
    for _ in range(rounds):
        cut = []
        for r in pieces:
            costs = [cost(r[:i]) + cost(r[i:]) for i in range(1, len(r))]
            if costs and min(costs) <= PIECE_SAVING * cost(r):
                i = int(np.argmin(costs)) + 1
                cuts.append(int(r[i]))
                cut += [r[:i], r[i:]]
            else:
                cut.append(r)
        pieces = cut
    return sorted(cuts)


def test_page_pieces_cut_where_a_box_spans_empty_space():
    """Pieces cut a page where its halves' boxes cover far fewer tiles, as a
    direct search over every cut finds; rows without an extent never start
    a piece."""
    rng = np.random.default_rng(2)
    found = 0
    for _ in range(300):
        n = int(rng.integers(2, 60))
        x = np.cumsum(rng.choice([1, 1, 1, 200], n)) + rng.integers(0, 5, n)
        y = rng.integers(0, 50, n) + np.where(rng.random(n) < 0.1, 500, 0)
        cells = [x, y, x + rng.integers(0, 4, n), y + rng.integers(0, 4, n)]
        cells = [c.astype(float) for c in cells]
        gone = rng.random(n) < 0.1
        for c in cells:
            c[gone] = np.nan
        bounds = np.unique(np.r_[0, rng.integers(1, n, 3), n])
        cuts = page_pieces(cells, bounds)
        assert cuts.tolist() == _cheapest_cuts(cells, bounds)
        assert not np.isnan(cells[0][cuts]).any() and not np.isin(cuts, bounds).any()
        found += len(cuts)
    assert found > 100, 'the fixture cuts pages'


def test_covering_cells():
    """Cells round a covering outward, and a row without four finite, ordered
    bounds, or with a null covering or bound, has no extent."""
    rows = [
        (-105.00001, 39.7, -104.99, 39.70001),
        (179.9999, -90.0, 180.0, -89.99),
        (float('nan'),) * 4,
        None,
        (None, 39.7, -104.99, 39.71),
        (float('-inf'), 39.7, -104.99, 39.71),
        (-104.99, 39.7, -105.0, 39.71),
    ]
    struct = pa.struct([(k, pa.float32()) for k in ('xmin', 'ymin', 'xmax', 'ymax')])
    bbox = pa.array([None if r is None else dict(zip(struct.names, r, strict=True)) for r in rows])
    x0, y0, x1, y1 = covering_cells(bbox.cast(struct))
    assert np.isnan(x0[2:]).all() and np.isnan(y1[2:]).all()
    xmin, ymin, xmax, ymax = (np.float32(np.array(c, float)) for c in zip(*rows[:2], strict=True))
    np.testing.assert_array_equal(x0[:2], np.floor((xmin.astype(float) + 180) * 4096))
    np.testing.assert_array_equal(y1[:2], np.floor((ymax.astype(float) + 90) * 4096))
    for low, high, cell0, cell1, origin in ((xmin, xmax, x0, x1, 180), (ymin, ymax, y0, y1, 90)):
        assert (cell0[:2] / 4096 - origin <= low).all() and (
            (cell1[:2] + 1) / 4096 - origin > high
        ).all()


@pytest.mark.parametrize('missing', ['nan', 'null'])
def test_rows_without_an_extent_stay_out_of_the_primary_directory(tmp_path, missing):
    """Null and empty geometries sort last and fill whole pages. The writer
    gives them a null covering, and another producer might write NaN; either
    way they have no extent, so no page of theirs is indexed, and exact reads
    find every other row."""
    rng = np.random.default_rng(8)
    shapes = 300
    x, y = -105 + rng.random(shapes) * 0.2, 39.6 + rng.random(shapes) * 0.2
    geometry = [*shapely.box(x, y, x + 0.001, y + 0.001), *[None] * 50, *[shapely.Polygon()] * 50]
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table(
        {'geometry': shapely.to_wkb(np.array(geometry, dtype=object)), 'height': range(400)}
    )
    src, packed = tmp_path / 'source.parquet', tmp_path / 'packed.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    write(
        src,
        packed,
        zooms=range(10, 13),
        complete_from=12,
        carry=['height'],
        row_group_rows=128,
        page_rows=16,
        batch_rows=16,
        workers=1,
        log=lambda *_: None,
    )
    written = pq.read_table(packed)
    keyed = written.column('hilbert').is_valid().to_numpy(zero_copy_only=False)
    assert keyed.sum() == shapes and keyed[:shapes].all(), 'rows without geometry sort last'
    bbox = written.column('bbox').combine_chunks()
    assert bbox.is_valid().to_numpy(zero_copy_only=False).tolist() == keyed.tolist()
    if missing == 'nan':
        bbox = pa.StructArray.from_arrays(
            [pc.fill_null(child, float('nan')) for child in bbox.flatten()], fields=list(bbox.type)
        )
        written = written.set_column(written.schema.get_field_index('bbox'), 'bbox', bbox)
        pq.write_table(
            written,
            packed,
            row_group_size=128,
            max_rows_per_page=16,
            use_dictionary=False,
            data_page_version='1.0',
            write_page_index=True,
        )
    out = finalize_layout(packed, tmp_path / 'laid.parquet', leaf_pages=4, log=lambda *_: None)
    parquet = pq.ParquetFile(out)
    lod = json.loads(parquet.schema_arrow.metadata[b'geo'])['lod_columns']
    check_index(out, parquet, lod)
    raw = out.read_bytes()
    root = _root(raw)
    index = root['primary_index']
    pages = [leaf for leaf in _box_leaves(raw, index['directory'], index['attributes'])]
    first = np.concatenate([leaf['first'] for leaf in pages])
    assert first.max() < shapes, 'no page of rows without an extent is indexed'
    covering = written.column('bbox').combine_chunks()
    values = {k: covering.field(k).to_numpy(zero_copy_only=False) for k in covering.type.names}
    cases = []
    for box in ([-180, -85, 180, 85], [-105, 39.6, -104.9, 39.7], [-104.95, 39.65, -104.94, 39.66]):
        hit = (
            keyed
            & (values['xmin'] <= box[2])
            & (values['xmax'] >= box[0])
            & (values['ymin'] <= box[3])
            & (values['ymax'] >= box[1])
        )
        cases.append({'bbox': box, 'rows': np.flatnonzero(hit).tolist()})
    assert len(cases[0]['rows']) == shapes and 0 < len(cases[2]['rows']) < len(cases[1]['rows'])
    path = tmp_path / 'cases.json'
    path.write_text(json.dumps(cases))
    result = subprocess.run(
        ['node', str(ROOT / 'js/test/primary.test.mjs'), str(out), str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _box_leaves(raw, node, attributes):
    """The decoded leaves under a primary directory node, in order."""
    level, entries = read_box_node(node, attributes)
    if not level:
        return [entries]
    return [
        leaf
        for offset, length in zip(entries['offset'], entries['length'], strict=True)
        for leaf in _box_leaves(
            raw, json.loads(gzip.decompress(raw[offset : offset + length])), attributes
        )
    ]


def test_root_shrinks_its_largest_top_node_first(tmp_path, monkeypatch):
    """While the root exceeds its target, the largest top node gains a level
    of branches, the primary directory's included; a root that does not fit
    once every top is a single entry is refused."""
    rng = np.random.default_rng(1)
    n = 3000
    x, y = -105 + rng.random(n) * 0.3, 39.6 + rng.random(n) * 0.3
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table(
        {'geometry': shapely.to_wkb(shapely.box(x, y, x + 1e-3, y + 1e-3)), 'h': range(n)}
    )
    src, packed = tmp_path / 'source.parquet', tmp_path / 'packed.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    write(
        src,
        packed,
        zooms=range(10, 13),
        complete_from=12,
        carry=['h'],
        row_group_rows=512,
        page_rows=16,
        batch_rows=16,
        workers=1,
        log=lambda *_: None,
    )
    layout = importlib.import_module('geoparquet_lod._layout')
    sizes = {}
    for target in (16256, 3000, 1400):
        monkeypatch.setattr(layout, '_ROOT_TARGET_BYTES', target)
        out = finalize_layout(
            packed, tmp_path / f'{target}.parquet', leaf_pages=2, log=lambda *_: None
        )
        root = _root(out.read_bytes())
        sizes[target] = int.from_bytes(out.read_bytes()[12:16], 'little')
        assert sizes[target] <= target
        assert validate(out, full=True)['rows'] == n
        levels = [top[0] for top in root['directories'].values()]
        exact = root['primary_index']['directory']
        if target == 16256:
            assert exact[0] == 1 and max(levels) == 1, 'a roomy root keeps every tree shallow'
        if target == 1400:
            assert exact[0] > 1, 'the primary directory gains branches too'
    monkeypatch.setattr(layout, '_ROOT_TARGET_BYTES', 200)
    with pytest.raises(ValueError, match='exceeds the 16 KiB bootstrap'):
        finalize_layout(packed, tmp_path / 'tiny.parquet', leaf_pages=2, log=lambda *_: None)
