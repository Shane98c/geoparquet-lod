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
from geoparquet_lod import drop_rate, validate, write
from geoparquet_lod._compact import dumps, loads
from geoparquet_lod.optimize import optimize
from geoparquet_lod.pack import Unpacker
from geoparquet_lod.writer import hilbert_key

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    'family,prefix,tile_pages,leaf_pages',
    [
        ('polygon', 11, True, 16),
        ('polygon', None, True, 1),
        ('point', None, False, 16),
        ('line', None, True, 16),
    ],
)
def test_spatial_roundtrip(tmp_path, monkeypatch, family, prefix, tile_pages, leaf_pages):
    rng = np.random.default_rng(913)
    n = 1200
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
    write(
        src,
        old,
        zooms=range(10, 15),
        complete_from=13,
        prefix_zoom=prefix,
        thinning=drop_rate(2.5),
        carry=['height', 'name'],
        row_group_rows=256,
        page_bytes=1024,
        page_rows=32,
        batch_rows=32,
        workers=1,
        log=lambda *_: None,
    )
    before = old.read_bytes()
    size = int.from_bytes(before[-8:-4], 'little')
    footer = before[-8 - size : -8]
    assert dumps(loads(footer)) == footer
    if leaf_pages == 1:
        monkeypatch.setattr(
            importlib.import_module('geoparquet_lod.optimize'), '_ROOT_TARGET_BYTES', 800
        )
    optimize(old, new, leaf_pages=leaf_pages, tile_pages=tile_pages, log=lambda *_: None)
    if leaf_pages == 1:
        raw = new.read_bytes()
        root = json.loads(gzip.decompress(raw[16 : 16 + int.from_bytes(raw[12:16], 'little')]))
        assert any(
            section[1] > 0 for sections in root['streams'].values() for section in sections
        ), 'fixture exercises directory branches'
    assert old.read_bytes() == before, 'source is immutable'
    assert validate(new)['rows'] == n
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
    # boundary copies. Do not inherit the old reader's page overfetch behaviour.
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


def test_optimize_requires_separate_output(tmp_path):
    with pytest.raises(ValueError, match='separate output'):
        optimize(tmp_path / 'same', tmp_path / 'same')
