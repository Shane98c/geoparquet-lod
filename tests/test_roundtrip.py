"""Write synthetic datasets, validate them, and read payloads back."""

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import cluster, drop_rate, importance, only, size_threshold, validate, write
from geoparquet_lod.encoding import Attribute, decode_attributes, decode_polygon_tree

GEO = {
    'version': '1.1.0',
    'primary_column': 'geometry',
    'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': []}},
}


def geoparquet(path: Path, table: pa.Table, types: list[str]) -> Path:
    geo = json.loads(json.dumps(GEO))
    geo['columns']['geometry']['geometry_types'] = types
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), path)
    return path


@pytest.fixture
def buildings(tmp_path):
    rng = np.random.default_rng(1)
    n = 3000
    cx = -105 + rng.random(n) * 0.5
    cy = 39.6 + rng.random(n) * 0.3
    size = np.exp(rng.normal(-9.5, 0.6, n))
    polys = [shapely.box(x, y, x + s, y + s * 0.7) for x, y, s in zip(cx, cy, size, strict=True)]
    table = pa.table(
        {
            'risk': pa.array(rng.random(n).astype(np.float32)),
            'floors': pa.array(
                [None if i % 7 == 0 else int(i % 5 + 1) for i in range(n)], pa.int32()
            ),
            'name': pa.array([f'b{i}' for i in range(n)]),
            'geometry': pa.array([shapely.to_wkb(p) for p in polys], pa.binary()),
        }
    )
    return geoparquet(tmp_path / 'buildings.parquet', table, ['Polygon'])


@pytest.fixture
def points(tmp_path):
    rng = np.random.default_rng(2)
    n = 2000
    pts = [shapely.Point(-100 + rng.random() * 40, 30 + rng.random() * 15) for _ in range(n)]
    table = pa.table(
        {
            'population': pa.array(rng.integers(10, 1_000_000, n), pa.uint32()),
            'count': pa.array(np.ones(n, dtype=np.uint32)),
            'geometry': pa.array([shapely.to_wkb(p) for p in pts], pa.binary()),
        }
    )
    return geoparquet(tmp_path / 'points.parquet', table, ['Point'])


def payloads(path, lod_name):
    f = pq.ParquetFile(path)
    geo = json.loads(f.schema_arrow.metadata[b'geo'])['lod_columns']
    table = f.read(columns=['lod'])
    values = table.column('lod').combine_chunks().field(lod_name)
    return geo, [v.as_py() for v in values if v.is_valid]


def test_polygons_roundtrip(buildings, tmp_path):
    out = write(
        buildings,
        tmp_path / 'out.parquet',
        thinning=only('risk > 0.2', drop_rate(2.5)),
        carry=[('risk', 'f32'), ('floors', 'i32'), ('name', 'utf8')],
        workers=1,
        log=lambda *_: None,
    )
    summary = validate(out)
    assert summary['rows'] == 3000
    geo, _ = payloads(out, 'p0') if 'p0' in summary['lods'] else (None, None)
    meta = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    attrs = [Attribute(a['name'], a['type']) for a in meta['attributes']]
    shape = next(n for n, lod in meta['lods'].items() if lod['geometry'] == 'polygon_varint')
    _, values = payloads(out, shape)
    assert len(values) == 3000, 'every polygon is present at the shape zooms'
    attr, pos = decode_attributes(values[0], attrs)
    assert set(attr) == {'risk', 'floors', 'name'}
    polygons, end = decode_polygon_tree(values[0], pos)
    assert end == len(values[0]) and polygons and polygons[0][0][0] == polygons[0][0][-1]
    # integer nulls are written as 0
    nulls = [decode_attributes(v, attrs)[0]['floors'] for v in values]
    assert 0 in nulls
    # point LODs are nested: coarser zooms hold subsets
    point_lods = [n for n, lod in meta['lods'].items() if lod['geometry'] == 'point_f32']
    counts = [len(payloads(out, n)[1]) for n in point_lods]
    assert counts == sorted(counts)
    assert counts[-1] <= 3000 * 0.8 + 50, 'rows with risk <= 0.2 never appear at point zooms'


def test_points_importance_and_cluster(points, tmp_path):
    out = write(
        points,
        tmp_path / 'imp.parquet',
        thinning=importance('population', per_tile=20),
        polygons_from=None,
        carry=[('population', 'u32')],
        prefix_zoom=3,
        workers=1,
        log=lambda *_: None,
    )
    s = validate(out)
    assert s['prefix'] is not None and s['prefix']['row_group_end'] == 0
    out2 = write(
        points,
        tmp_path / 'cl.parquet',
        thinning=cluster(pixels=8, count='count'),
        polygons_from=None,
        carry=[('count', 'u32')],
        workers=1,
        log=lambda *_: None,
    )
    meta = json.loads(pq.read_schema(out2).metadata[b'geo'])['lod_columns']
    attrs = [Attribute(a['name'], a['type']) for a in meta['attributes']]
    coarsest = min(meta['lods'], key=lambda n: meta['lods'][n]['zooms'][0])
    _, values = payloads(out2, coarsest)
    total = sum(decode_attributes(v, attrs)[0]['count'] for v in values)
    assert total == 2000, 'cluster counts add up to every point'
    assert len(values) < 2000


def test_prefix_zoom_outside_range(buildings, tmp_path):
    out = write(
        buildings,
        tmp_path / 'z.parquet',
        thinning=size_threshold(4),
        zooms=range(9, 15),
        polygons_from=12,
        prefix_zoom=6,
        workers=1,
        log=lambda *_: None,
    )
    assert validate(out)['prefix'] is None
