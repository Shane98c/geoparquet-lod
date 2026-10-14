"""Write synthetic datasets, validate them, and read payloads back."""

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import cluster, drop_rate, importance, only, size_threshold, validate, write
from geoparquet_lod.pack import Unpacker

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
            'zone': pa.array([f'zone{i % 4}' for i in range(n)]),
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
    """The packed LOD values of rows shown at `lod_name`."""
    f = pq.ParquetFile(path)
    values = f.read(columns=['lod']).column('lod').combine_chunks().field(lod_name)
    return values.filter(values.is_valid())


def unpacked(values, family, fields=()):
    """Decoded rows of packed LOD values."""
    fields = [pa.field(n, t) for n, t in fields]
    return [Unpacker(b, family, fields).row() for b in values.to_pylist()]


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
    assert meta['encoding'] == 'packed' and meta['pack_version'] == 1
    finest = max(meta['zooms'])
    shape = next(n for n, lod in meta['lods'].items() if finest in lod['zooms'])
    assert meta['lods'][shape]['geometry_type'] == 'MultiPolygon'
    values = shown(out, shape)
    assert len(values) == 3000, 'every polygon is present from the complete zoom'
    assert values.type == pa.binary()
    assert [a['name'] for a in meta['attributes']] == ['risk', 'floors', 'name']
    assert next(a for a in meta['attributes'] if a['name'] == 'floors')['type'] == 'int32'
    fields = [(a['name'], pa.type_for_alias(a['type'])) for a in meta['attributes']]
    decoded = unpacked(values.slice(0, 5), 'MultiPolygon', fields)
    polygons = [d['geometry'] for d in decoded]
    assert polygons[0][0][0][0] == polygons[0][0][0][-1], 'rings are closed'
    # attribute nulls survive as nulls
    everything = unpacked(values, 'MultiPolygon', fields)
    assert any(d['attrs']['floors'] is None for d in everything)
    assert all(isinstance(d['attrs']['name'], str) for d in everything)
    # a shape LOD decodes back near the source
    lod = meta['lods'][shape]
    x, y = polygons[0][0][0][0]
    assert abs(lod['offset'][0] + lod['scale'][0] * x + 105) < 0.6
    # coarser LODs hold subsets, as polygons, with placeholder squares for collapsed ones
    thinned = [n for n, lod in meta['lods'].items() if lod['zooms'][0] < 12]
    assert all(meta['lods'][n]['geometry_type'] == 'MultiPolygon' for n in thinned)
    counts = [len(shown(out, n)) for n in thinned]
    coarse = [
        d['geometry'] for d in unpacked(shown(out, thinned[0]).slice(0, 3), 'MultiPolygon', fields)
    ]
    assert all(len(g[0][0]) == 5 for g in coarse), 'collapsed buildings are squares'
    assert counts == sorted(counts)
    assert counts[-1] <= 3000 * 0.8 + 50, 'rows with risk <= 0.2 never appear at point zooms'


def test_points_importance_and_cluster(points, tmp_path):
    out = write(
        points,
        tmp_path / 'imp.parquet',
        thinning=importance('population', per_tile=20),
        carry=['population'],
        workers=1,
        log=lambda *_: None,
    )
    s = validate(out)
    assert s['prefix'] is None
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
    decoded = unpacked(values, 'Point', [('count', pa.uint32())])
    assert sum(d['attrs']['count'] for d in decoded) == 2000, 'cluster counts add up to every point'
    assert len(values) < 2000


def test_explicit_zooms_use_single_order(buildings, tmp_path):
    out = write(
        buildings,
        tmp_path / 'z.parquet',
        thinning=size_threshold(4),
        zooms=range(9, 15),
        complete_from=12,
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
    rows = [d['geometry'] for d in unpacked(values, 'MultiLineString', [('road', pa.string())])]
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
    decoded = unpacked(shown(out, coarsest), 'Point', [('population', pa.uint32())])
    assert all(d['attrs']['population'] is None for d in decoded), (
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
    square = (
        4.0 * ctx.pixel_unit(rows, 1)[0]
    )  # a two-pixel square in degrees squared at this latitude
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


def test_straddlers_are_copied_into_the_tiles_they_reach(buildings, tmp_path):
    from geoparquet_lod.spill import straddlers
    from geoparquet_lod.writer import hilbert_key

    out = write(buildings, tmp_path / 'spill.parquet', workers=1, log=lambda *_: None)
    validate(out)
    meta = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    depth = meta['key']['depth']
    t = pq.read_table(out, columns=['geometry', 'hilbert', 'lod'])
    # exact bounds, as the writer saw them; the stored covering is float32 and rounded outward
    bounds = shapely.bounds(shapely.from_wkb(t.column('geometry').to_numpy(zero_copy_only=False)))
    b = dict(zip(('xmin', 'ymin', 'xmax', 'ymax'), bounds.T, strict=True))
    keys = t.column('hilbert').to_numpy()
    lod = t.column('lod').combine_chunks()
    finest = max(meta['zooms'])
    name = next(n for n, c in meta['lods'].items() if finest in c['zooms'])
    column = lod.field(name)
    src, tiles = straddlers(b['xmin'], b['ymin'], b['xmax'], b['ymax'], finest)
    assert len(src) > 0, 'the fixture has buildings crossing tile edges at the finest zoom'
    n = 2**finest
    span = 4 ** (depth - finest)
    spill = [
        Unpacker(b, 'MultiPolygon', []).row()['spill'] if b is not None else None
        for b in column.to_pylist()
    ]
    f = pq.ParquetFile(out)
    prefix = meta['prefix']
    main_start = (
        sum(f.metadata.row_group(g).num_rows for g in range(prefix['row_group_end'] + 1))
        if prefix
        else 0
    )
    main_keys = keys[main_start:]
    for row, tile in list(zip(src, tiles, strict=True))[:50]:
        start = int(hilbert_key(np.array([tile // n]), np.array([tile % n]), finest)[0]) * span
        # the tile's own rows, plus the first main-section row at or past its interval
        inside = np.flatnonzero((keys >= start) & (keys < start + span))
        host = main_start + min(int(np.searchsorted(main_keys, start)), len(main_keys) - 1)
        inside = np.unique(np.concatenate([inside, [host]]))
        copies = [
            e
            for host in inside
            for e in (spill[host] or [])
            if e['tile'] == (finest << 48) | int(tile)
        ]
        assert any(e['id'] == row for e in copies), f'row {row} not copied into tile {tile}'


def test_reader_finds_straddler_copies(buildings, tmp_path):
    """The JS reader, reading a tile as its key interval, returns the copies."""
    import subprocess

    from geoparquet_lod.spill import straddlers

    out = write(
        buildings,
        tmp_path / 'spill.parquet',
        carry=['floors', 'zone'],
        workers=1,
        log=lambda *_: None,
    )
    meta = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    finest = max(meta['zooms'])
    zone = next(a for a in meta['attributes'] if a['name'] == 'zone')
    assert zone['values'] == ['zone0', 'zone1', 'zone2', 'zone3'], (
        'few distinct strings get a value table'
    )
    t = pq.read_table(out, columns=['geometry'])
    bounds = shapely.bounds(shapely.from_wkb(t.column('geometry').to_numpy(zero_copy_only=False)))
    rows, tiles = straddlers(bounds[:, 0], bounds[:, 1], bounds[:, 2], bounds[:, 3], finest)
    n = 2**finest
    cases = [
        {'z': finest, 'x': int(tl // n), 'y': int(tl % n), 'id': int(r), 'attribute': 'zone'}
        for r, tl in zip(rows[:20], tiles[:20], strict=True)
    ]
    (tmp_path / 'cases.json').write_text(json.dumps(cases))
    script = Path(__file__).resolve().parents[1] / 'js' / 'test' / 'spill.test.mjs'
    result = subprocess.run(
        ['node', str(script), str(out), str(tmp_path / 'cases.json')],
        capture_output=True,
        text=True,
        check=True,
    )
    assert 'spill tests passed' in result.stdout
