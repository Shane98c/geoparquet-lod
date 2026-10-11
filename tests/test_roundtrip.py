"""Write synthetic datasets, validate them, and read payloads back."""

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import cluster, drop_rate, importance, only, size_threshold, validate, write
from geoparquet_lod.encoding import geometry_rows

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


def shown(path, lod_name):
    """The LOD struct values of rows shown at `lod_name`."""
    f = pq.ParquetFile(path)
    values = f.read(columns=['lod']).column('lod').combine_chunks().field(lod_name)
    return values.filter(values.is_valid())


def test_polygons_roundtrip(buildings, tmp_path):
    out = write(
        buildings,
        tmp_path / 'out.parquet',
        thinning=only('risk > 0.2', drop_rate(2.5)),
        carry=['risk', ('floors', 'int32'), 'name'],
        workers=1,
        log=lambda *_: None,
    )
    summary = validate(out)
    assert summary['rows'] == 3000
    meta = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    assert meta['encoding'] == 'quantized_geoarrow' and meta['coordinates'] == 'interleaved'
    finest = max(meta['zooms'])
    shape = next(n for n, lod in meta['lods'].items() if finest in lod['zooms'])
    assert meta['lods'][shape]['geometry_type'] == 'MultiPolygon'
    values = shown(out, shape)
    assert len(values) == 3000, 'every polygon is present from the complete zoom'
    assert set(values.type.names) == {'geometry', 'risk', 'floors', 'name'}
    assert values.field('floors').type == pa.int32()
    rg = pq.ParquetFile(out).metadata.row_group(0)
    coordinate_chunks = [
        rg.column(i)
        for i in range(rg.num_columns)
        if rg.column(i).path_in_schema.startswith(f'lod.{shape}.geometry')
    ]
    assert coordinate_chunks and all('BYTE_STREAM_SPLIT' in c.encodings for c in coordinate_chunks)
    polygons = geometry_rows(values.field('geometry').slice(0, 5))
    assert polygons[0][0][0][0] == polygons[0][0][0][-1], 'rings are closed'
    # attribute nulls survive as nulls
    assert values.field('floors').null_count > 0
    # a shape LOD decodes back near the source
    lod = meta['lods'][shape]
    x, y = polygons[0][0][0][0]
    assert abs(lod['offset'][0] + lod['scale'][0] * x + 105) < 0.6
    # coarser LODs hold subsets, as polygons, with placeholder squares for collapsed ones
    thinned = [n for n, lod in meta['lods'].items() if lod['zooms'][0] < 12]
    assert all(meta['lods'][n]['geometry_type'] == 'MultiPolygon' for n in thinned)
    counts = [len(shown(out, n)) for n in thinned]
    coarse = geometry_rows(shown(out, thinned[0]).field('geometry').slice(0, 3))
    assert all(len(g[0][0]) == 5 for g in coarse), 'collapsed buildings are squares'
    assert counts == sorted(counts)
    assert counts[-1] <= 3000 * 0.8 + 50, 'rows with risk <= 0.2 never appear at point zooms'


def test_points_importance_and_cluster(points, tmp_path):
    out = write(
        points,
        tmp_path / 'imp.parquet',
        thinning=importance('population', per_tile=20),
        carry=['population'],
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
        carry=['count'],
        workers=1,
        log=lambda *_: None,
    )
    meta = json.loads(pq.read_schema(out2).metadata[b'geo'])['lod_columns']
    coarsest = min(meta['lods'], key=lambda n: meta['lods'][n]['zooms'][0])
    values = shown(out2, coarsest)
    assert sum(values.field('count').to_pylist()) == 2000, 'cluster counts add up to every point'
    assert len(values) < 2000


def test_prefix_zoom_outside_range(buildings, tmp_path):
    out = write(
        buildings,
        tmp_path / 'z.parquet',
        thinning=size_threshold(4),
        zooms=range(9, 15),
        complete_from=12,
        prefix_zoom=6,
        workers=1,
        log=lambda *_: None,
    )
    assert validate(out)['prefix'] is None


def test_lines_roundtrip(tmp_path):
    rng = np.random.default_rng(3)
    lines = []
    for i in range(400):
        x, y = -105 + rng.random() * 0.3, 39.7 + rng.random() * 0.2
        n = 2 + i % 7
        pts = [(x + k * 1e-4, y + (k % 2) * 1e-4) for k in range(n)]
        geom = shapely.LineString(pts)
        if i % 5 == 0:
            geom = shapely.MultiLineString(
                [pts, [(x, y - 2e-4), (x + 3e-4, y - 2e-4), (x + 3e-4, y - 4e-4)]]
            )
        lines.append(geom)
    table = pa.table(
        {
            'road': pa.array([f'r{i}' for i in range(400)]),
            'geometry': pa.array(shapely.to_wkb(lines), pa.binary()),
        }
    )
    src = geoparquet(tmp_path / 'lines.parquet', table, ['LineString', 'MultiLineString'])
    out = write(src, tmp_path / 'lines.lod.parquet', carry=['road'], workers=1, log=lambda *_: None)
    meta = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    finest = next(n for n, lod in meta['lods'].items() if max(meta['zooms']) in lod['zooms'])
    assert meta['lods'][finest]['geometry_type'] == 'MultiLineString'
    values = shown(out, finest)
    assert len(values) == 400
    rows = geometry_rows(values.field('geometry'))
    # every part keeps its vertex count: the finest grid is below the vertex spacing
    f = pq.ParquetFile(out)
    src_geoms = shapely.from_wkb(
        f.read(columns=['geometry']).column('geometry').to_numpy(zero_copy_only=False)
    )
    for decoded, g in zip(rows, src_geoms, strict=True):
        parts = list(g.geoms) if hasattr(g, 'geoms') else [g]
        assert [len(p) for p in decoded] == [len(p.coords) for p in parts]
    validate(out)


def test_null_geometry_and_source_columns(buildings, tmp_path):
    t = pq.read_table(buildings)
    geoms = t.column('geometry').to_pylist()
    geoms[0] = None
    geoms[1] = shapely.to_wkb(shapely.Polygon())
    t = t.set_column(t.schema.get_field_index('geometry'), 'geometry', pa.array(geoms, pa.binary()))
    # source columns whose names the writer uses internally survive untouched
    t = t.append_column('id', pa.array([f'src{i}' for i in range(t.num_rows)]))
    t = t.append_column('area', pa.array(np.full(t.num_rows, 7.0)))
    src = geoparquet(tmp_path / 'nulls.parquet', t, ['Polygon'])
    out = write(src, tmp_path / 'nulls.lod.parquet', carry=['risk'], workers=1, log=lambda *_: None)
    validate(out)
    f = pq.ParquetFile(out)
    names = f.schema_arrow.names
    assert 'id' in names and 'area' in names and 'risk' in names
    back = f.read(columns=['id', 'area', 'lod'])
    assert set(back.column('id').to_pylist()) == {f'src{i}' for i in range(t.num_rows)}
    assert back.column('area').to_pylist() == [7.0] * t.num_rows
    meta = json.loads(f.schema_arrow.metadata[b'geo'])['lod_columns']
    finest = next(n for n, lod in meta['lods'].items() if max(meta['zooms']) in lod['zooms'])
    lod = back.column('lod').combine_chunks().field(finest)
    assert lod.null_count == 2, 'null and empty geometries are shown at no zoom'


def test_null_override_and_combined_tiny(points, tmp_path):
    from geoparquet_lod import combine, tiny_polygons
    from geoparquet_lod.thinning import drop_rate

    def nullify(rows, zoom, ctx):
        kept = drop_rate(2.0)(rows, zoom, ctx)
        return kept.set_column(
            kept.schema.get_field_index('population'),
            'population',
            pa.nulls(kept.num_rows, pa.uint32()),
        )

    out = write(
        points,
        tmp_path / 'null_override.parquet',
        thinning=nullify,
        carry=['population'],
        workers=1,
        log=lambda *_: None,
    )
    meta = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    coarsest = min(meta['lods'], key=lambda n: meta['lods'][n]['zooms'][0])
    assert shown(out, coarsest).field('population').null_count == len(shown(out, coarsest)), (
        'an explicit null override is written'
    )

    # tiny_polygons keeps accumulating area when composed
    from geoparquet_lod.thinning import Context

    n = 16
    rows = pa.table(
        {
            'id': pa.array(np.arange(1, n + 1)),
            'cx': pa.array(np.full(n, -100.0, np.float32)),
            'cy': pa.array(np.full(n, 40.0, np.float32)),
            'diag': pa.array(np.zeros(n)),
            'area': pa.array(np.full(n, 0.25)),
            'u': pa.array(np.linspace(0, 1, n, endpoint=False)),
        }
    )
    ctx = Context((0, 1, 2), 2, 512, True)
    solo = tiny_polygons(2.0)
    both = combine(tiny_polygons(2.0), size_threshold(1e12))
    square = (2.0 * ctx.resolution(1)) ** 2
    # four rows make a hair over one square at zoom 1, so sixteen rows keep four representatives
    rows = rows.set_column(
        rows.schema.get_field_index('area'), 'area', pa.array(np.full(n, square / 4 * 1.001))
    )
    k1 = solo(rows, 1, ctx)
    k2 = both(rows, 1, ctx)
    assert k1.num_rows == k2.num_rows == 4
    assert sum(k2.column('area').to_pylist()) == pytest.approx(sum(k1.column('area').to_pylist()))
    # the four carry a square each; one zoom coarser they make one square of four
    assert both(k2, 0, ctx).num_rows == solo(k1, 0, ctx).num_rows == 1
    assert both.placeholder_pixels == 2.0


def test_cli_auto_thinning():
    from geoparquet_lod.cli import parse_thinning

    assert parse_thinning('auto', None).__name__ == 'drop_rate(2.5)'
    assert parse_thinning('auto', 'risk > 0').__name__.startswith('only(')


def test_tile_directory_sidecar(buildings, tmp_path):
    out = write(
        buildings,
        tmp_path / 'td.parquet',
        carry=['risk'],
        workers=1,
        tile_directory=True,
        log=lambda *_: None,
    )
    sidecar = tmp_path / 'td.parquet.tiledir.parquet'
    assert sidecar.exists()
    meta = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    assert 'tile_directory' not in meta
    assert validate(out)['tile_directory'] == 'bound'

    from geoparquet_lod.validate import ValidationError

    table = pq.read_table(sidecar)
    header = json.loads(pq.ParquetFile(sidecar).metadata.metadata[b'lod_tile_directory'])
    header['source']['footer_sha256'] = '0' * 64
    pq.write_table(
        table.replace_schema_metadata({b'lod_tile_directory': json.dumps(header).encode()}),
        sidecar,
    )
    with pytest.raises(ValidationError, match='different write'):
        validate(out)
    sidecar.unlink()
    assert validate(out)['tile_directory'] is None
