"""The spatial directory changes access, never the analytical table or row ids."""

import gzip
import importlib
import json
import subprocess
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import drop_rate, validate
from geoparquet_lod import write as build
from geoparquet_lod._compact import dumps, get, loads
from geoparquet_lod._layout import finalize_layout
from geoparquet_lod.writer import _write_unindexed as write
from geoparquet_lod.writer import hilbert_key
from unpacker import Unpacker

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    'family,tile_pages,leaf_pages,page_bytes',
    [
        ('polygon', True, 16, 16384),
        ('polygon', True, 1, 32768),
        ('point', False, 16, 16384),
        ('point', True, 16, 65536),
        ('line', True, 16, 131072),
    ],
)
def test_spatial_roundtrip(tmp_path, monkeypatch, family, tile_pages, leaf_pages, page_bytes):
    rng = np.random.default_rng(913)
    sparse = family == 'point' and tile_pages
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
    options = dict(
        zooms=range(10, 15),
        complete_from=13,
        thinning=drop_rate(10 if sparse else 2.5),
        carry=['height', 'name'],
        row_group_rows=n if sparse else 256,
        page_bytes=1024,
        page_rows=32,
        batch_rows=32,
        workers=1,
        log=lambda *_: None,
    )
    write(src, old, **options)
    if family == 'point' and not sparse:
        # Keep-pages must retain support for standard v2 pages through the
        # general decoder when the sparse v1/PLAIN fast path doesn't apply.
        table = pq.read_table(old)
        pq.write_table(
            table,
            old,
            row_group_size=256,
            compression='zstd',
            use_dictionary=False,
            write_page_index=True,
            data_page_version='2.0',
            max_rows_per_page=32,
        )
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
        finalize_layout(
            old,
            new,
            leaf_pages=leaf_pages,
            tile_pages=tile_pages,
            page_bytes=page_bytes,
            log=lambda *_: None,
        )
    if sparse:
        raw = new.read_bytes()
        root = json.loads(gzip.decompress(raw[16 : 16 + int.from_bytes(raw[12:16], 'little')]))
        largest = 0
        for sections in root['streams'].values():
            for offset, depth, triples in sections:
                assert depth == 0
                for length in triples[2::3]:
                    pages = json.loads(gzip.decompress(raw[offset : offset + length]))
                    largest = max(largest, max(page[5] for page in pages))
                    offset += length
        assert largest > 2048, 'exercise sparse pages spanning many analytical rows'
    if leaf_pages == 1:
        raw = new.read_bytes()
        root = json.loads(gzip.decompress(raw[16 : 16 + int.from_bytes(raw[12:16], 'little')]))
        assert any(
            section[1] > 0 for sections in root['streams'].values() for section in sections
        ), 'fixture exercises directory branches'
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
