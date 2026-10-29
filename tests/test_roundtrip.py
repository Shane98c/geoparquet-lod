"""Write synthetic datasets, validate them, and read payloads back."""

import json
import math
import re
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import cluster, drop_rate, importance, only, size_threshold, validate, write
from geoparquet_lod.writer import DICTIONARY_MAX
from unpacker import Unpacker

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


def copied_straddlers(bounds, keys, zoom):
    """The straddlers that get copies: shapes a pixel or larger at `zoom`."""
    from geoparquet_lod.key import mercator, resolution
    from geoparquet_lod.spill import straddlers

    xmin, ymin, xmax, ymax = bounds.T
    x0, y0 = mercator(xmin, ymin)
    x1, y1 = mercator(xmax, ymax)
    large = np.flatnonzero(np.maximum(x1 - x0, y1 - y0) / resolution(zoom) >= 1)
    rows, tiles = straddlers(xmin[large], ymin[large], xmax[large], ymax[large], keys[large], zoom)
    return large[rows], tiles


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
    assert meta['encoding'] == 'packed' and meta['pack_version'] == 2
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
    lon = np.degrees((lod['offset'][0] + lod['scale'][0] * x) / 6378137)
    assert abs(lon + 105) < 0.6
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


def test_polygon_rings_are_stored_open():
    """A ring's closing vertex is not stored; decoding repeats the first vertex."""
    from geoparquet_lod.pack import pack

    square = [(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]
    x, y = (np.array(c, np.int32) for c in zip(*square, strict=True))
    parts = (np.array([0, 1]), np.array([0, 1]), np.array([0, 5]), x, y)
    blob = pack('MultiPolygon', parts, [], {}).to_pylist()[0]
    # one polygon of one ring of four vertices: the first from the origin, then three steps
    assert blob == bytes([1, 1, 4, 0, 0, 4, 0, 0, 4, 3, 0, 0, 0])
    assert Unpacker(blob, 'MultiPolygon', []).row()['geometry'] == [[square]]
    lines = pack('MultiLineString', parts, [], {}).to_pylist()[0]
    assert Unpacker(lines, 'MultiLineString', []).row()['geometry'] == [square]
    for ring in ([(0, 0), (2, 0), (2, 2), (0, 2)], [(0, 0), (2, 0), (0, 0)]):
        x, y = (np.array(c, np.int32) for c in zip(*ring, strict=True))
        with pytest.raises(ValueError, match='closed'):
            pack('MultiPolygon', (*parts[:2], np.array([0, len(ring)]), x, y), [], {})


INT64_MAX = 2**63 - 1


def int64_limits(path, value):
    """Small buildings and two boxes across a z10 tile edge, which are copied
    into the next tile at z10 and finer. uint64 `big` is `value` on the first
    box and null on the second and every third building; int64 `signed` is
    its minimum on the first box and its maximum on the second."""
    rng = np.random.default_rng(6)
    x, y = -105.3 + rng.random(300) * 0.3, 39.6 + rng.random(300) * 0.2
    boxes = shapely.box(
        np.r_[x, -105.13, -105.13],
        np.r_[y, 39.7, 39.75],
        np.r_[x + 1e-4, -105.1, -105.1],
        np.r_[y + 1e-4, 39.72, 39.77],
    )
    table = pa.table(
        {
            'big': pa.array(
                [None if i % 3 == 0 else i for i in range(300)] + [value, None], pa.uint64()
            ),
            'signed': pa.array([*range(300), -INT64_MAX - 1, INT64_MAX], pa.int64()),
            'geometry': pa.array(shapely.to_wkb(boxes), pa.binary()),
        }
    )
    return geoparquet(path, table, ['Polygon'])


def test_carried_integers_reach_the_int64_limits(tmp_path):
    from geoparquet_lod import read_overview
    from geoparquet_lod.unpack import carried, lod_values, unpack_batches

    src = int64_limits(tmp_path / 'limits.parquet', INT64_MAX)
    out = write(
        src,
        tmp_path / 'limits.lod.parquet',
        carry=['big', 'signed'],
        zooms=range(9, 13),
        workers=1,
        log=lambda *_: None,
    )
    validate(out, full=True)
    f = pq.ParquetFile(out)
    lod = json.loads(f.schema_arrow.metadata[b'geo'])['lod_columns']
    fields, dictionaries = carried(lod)
    copies = set()
    for name, child in lod['lods'].items():
        values = lod_values(f, 0, 'lod', name)
        for u in unpack_batches(values, child['geometry_type'], fields, dictionaries):
            pairs = zip(u.attrs['signed'].to_pylist(), u.attrs['big'].to_pylist(), strict=True)
            copies.update(pair for pair, copy in zip(pairs, u.copy, strict=True) if copy)
    assert {(-INT64_MAX - 1, INT64_MAX), (INT64_MAX, None)} <= copies
    table = read_overview(out, 12)
    big = dict(zip(table['signed'].to_pylist(), table['big'].to_pylist(), strict=True))
    assert big[-INT64_MAX - 1] == INT64_MAX and big[INT64_MAX] is None
    assert table['big'].null_count == 101


def test_carried_uint64_above_int64_is_refused(tmp_path):
    src = int64_limits(tmp_path / 'over.parquet', INT64_MAX + 1)
    with pytest.raises(ValueError, match=f'cannot pack big value {INT64_MAX + 1}'):
        write(
            src,
            tmp_path / 'over.lod.parquet',
            carry=['big', 'signed'],
            zooms=range(9, 13),
            workers=1,
            log=lambda *_: None,
        )


def test_copies_refuse_uint64_above_int64():
    """The packer checks the values of the copies a row hosts as well as its own."""
    from geoparquet_lod.pack import pack
    from geoparquet_lod.unpack import unpack

    x, y = np.array([0, 2, 2, 0, 0], np.int32), np.array([0, 0, 2, 2, 0], np.int32)
    parts = (np.array([0, 1]), np.array([0, 1]), np.array([0, 5]), x, y)
    fields = [pa.field('big', pa.uint64())]
    own = {'big': pa.array([None], pa.uint64())}

    def host(copy):
        spill = (
            np.array([1]),
            np.array([10 << 48]),
            np.array([7]),
            parts,
            {'big': pa.array([copy], pa.uint64())},
        )
        return pack('MultiPolygon', parts, fields, own, spill)

    decoded = unpack(host(INT64_MAX), 'MultiPolygon', fields)
    assert decoded.attrs['big'].to_pylist() == [None, INT64_MAX]
    with pytest.raises(ValueError, match=f'cannot pack big value {INT64_MAX + 1}'):
        host(INT64_MAX + 1)


@pytest.mark.parametrize(
    'kind', ['int8', 'int16', 'int32', 'int64', 'uint8', 'uint16', 'uint32', 'uint64']
)
def test_every_integer_type_packs_its_range(kind):
    """Every integer type packs whole except uint64, which packs up to the int64 maximum."""
    from geoparquet_lod.pack import pack
    from geoparquet_lod.unpack import unpack

    t = pa.type_for_alias(kind)
    limits = np.iinfo(kind)
    values = pa.array([int(limits.min), min(int(limits.max), INT64_MAX), None], t)
    fields = [pa.field('v', t)]
    blobs = pack('Point', (np.zeros(3, np.int32), np.zeros(3, np.int32)), fields, {'v': values})
    assert unpack(blobs, 'Point', fields).attrs['v'].equals(values)


def varint(value: int) -> bytes:
    out = bytearray()
    while value >= 128:
        out.append(value & 127 | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def test_float16_packs_its_bit_pattern():
    """A carried float16 packs as a varint of its half-precision bits, two
    bytes below 2.0 and three for larger or negative values. Values round to
    the nearest float16, and NaN and the infinities pack as themselves."""
    from geoparquet_lod.pack import pack
    from geoparquet_lod.unpack import unpack

    values = pa.array(
        [0.1, 1.5, 1.999, 2.0, -0.5, 65504.0, 65519.0, math.inf, -math.inf, math.nan, None],
        pa.float32(),
    )
    half = values.cast(pa.float16())
    bits = half.to_numpy(zero_copy_only=False).view(np.uint16)[:-1].tolist()
    assert bits[:7] == [0x2E66, 0x3E00, 0x3FFF, 0x4000, 0xB800, 0x7BFF, 0x7BFF]
    fields = [pa.field('v', pa.float16())]
    points = (np.zeros(len(values), np.int32),) * 2
    blobs = pack('Point', points, fields, {'v': values})
    # a point at the origin, the presence bitmap, the value and no copies
    assert blobs.to_pylist() == [bytes([0, 0, 1, *varint(b), 0]) for b in bits] + [bytes(4)]
    assert [len(varint(b)) for b in bits[:5]] == [2, 2, 2, 3, 3]
    decoded = unpack(blobs, 'Point', fields).attrs['v']
    assert decoded.type == pa.float16() and decoded.null_count == 1
    assert decoded.to_numpy(zero_copy_only=False).view(np.uint16)[:-1].tolist() == bits


@pytest.mark.parametrize(
    'source, carried, value, largest',
    [
        (pa.float32(), pa.float16(), 65520.0, '65504'),
        (pa.float32(), pa.float16(), -70000.0, '65504'),
        (pa.float64(), pa.float16(), 1e6, '65504'),
        (pa.int32(), pa.float16(), 100_000, '65504'),
        (pa.float64(), pa.float32(), 1e39, '3.40282e+38'),
    ],
)
def test_a_finite_value_too_large_for_its_float_type_is_refused(source, carried, value, largest):
    """Casting would make it infinite. A value rounding to the largest packs."""
    from geoparquet_lod.pack import cast_carried

    field = pa.field('v', carried)
    held = cast_carried(pa.array([1, -65519, None], source), field)
    assert held.to_pylist() == [1.0, -65504.0 if carried == pa.float16() else -65519.0, None]
    message = f'cannot pack v value {value}: a carried {carried} is at most {largest} in magnitude'
    with pytest.raises(ValueError, match=re.escape(message)):
        cast_carried(pa.array([1, value], source), field)


@pytest.mark.parametrize('where', ['source', 'thinning'])
def test_the_writer_refuses_a_value_float16_cannot_hold(tmp_path, where):
    """In a source column or in a value a thinning gives the attribute."""
    x = -105 + np.arange(40) * 1e-3
    share = np.linspace(0, 1, 40, dtype=np.float32)
    if where == 'source':
        share[7] = 7e4
    table = pa.table(
        {
            'share': share,
            'geometry': pa.array(shapely.to_wkb(shapely.box(x, 39.7, x + 5e-4, 39.7005))),
        }
    )
    src = geoparquet(tmp_path / 'share.parquet', table, ['Polygon'])

    def large(rows, zoom, ctx):
        kept = rows.slice(0, 4)
        values = pa.array([7e4] * kept.num_rows, pa.float32())
        return kept.set_column(kept.schema.get_field_index('share'), 'share', values)

    with pytest.raises(ValueError, match=re.escape('cannot pack share value 70000.0')):
        write(
            src,
            tmp_path / 'share.lod.parquet',
            thinning=large if where == 'thinning' else None,
            zooms=[13, 14],
            complete_from=14,
            carry=[('share', 'halffloat')],
            workers=1,
            log=lambda *_: None,
        )


def test_float16_overviews_hold_the_source_rounded(buildings, tmp_path):
    """The source column keeps its float32 values; every LOD, its copies
    included, holds them rounded to float16."""
    from geoparquet_lod import read_overview
    from geoparquet_lod.unpack import carried, lod_values, unpack_batches

    out = write(
        buildings,
        tmp_path / 'half.parquet',
        carry=[('risk', 'halffloat')],
        workers=1,
        log=lambda *_: None,
    )
    validate(out, full=True)
    f = pq.ParquetFile(out)
    lod = json.loads(f.schema_arrow.metadata[b'geo'])['lod_columns']
    assert lod['attributes'] == [{'name': 'risk', 'type': 'halffloat'}]
    risk = pq.read_table(out, columns=['risk']).column('risk').combine_chunks()
    assert risk.type == pa.float32(), 'the source column keeps full precision'
    rounded = risk.cast(pa.float16()).to_numpy(zero_copy_only=False)
    fields, dictionaries = carried(lod)
    copies = 0
    for name, child in lod['lods'].items():
        for u in unpack_batches(lod_values(f, 0, 'lod', name), child['geometry_type'], fields):
            rows = np.where(u.copy, u.id, u.row)
            got = u.attrs['risk'].to_numpy(zero_copy_only=False)
            assert got.dtype == np.float16 and np.array_equal(got, rounded[rows], equal_nan=True)
            copies += int(u.copy.sum())
    assert copies, 'the fixture has boundary copies'
    table = read_overview(out, max(lod['zooms']))
    assert table.schema.field('risk').type == pa.float16()
    assert np.array_equal(table['risk'].to_numpy(), rounded[table['row'].to_numpy()])


def test_points_importance_and_cluster(points, tmp_path):
    out = write(
        points,
        tmp_path / 'imp.parquet',
        thinning=importance('population', per_tile=20),
        carry=['population'],
        workers=1,
        log=lambda *_: None,
    )
    validate(out)
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


def test_explicit_zooms(buildings, tmp_path):
    out = write(
        buildings,
        tmp_path / 'z.parquet',
        thinning=size_threshold(4),
        zooms=range(9, 15),
        complete_from=12,
        workers=1,
        log=lambda *_: None,
    )
    validate(out)
    meta = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    assert meta['zooms'] == list(range(9, 15))


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
    # a sample of every row reaches the null and empty geometry, which sorts last
    validate(out, sample_rows=t.num_rows, full=True)
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
    ctx = Context((0, 1, 2), 2, 512)
    solo = tiny_polygons(2.0)
    both = combine(tiny_polygons(2.0), size_threshold(1e12))
    square = 4.0 * ctx.pixel_unit(rows, 1)[0]  # a two-pixel square in metres squared
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
    from geoparquet_lod.key import hilbert_key

    out = write(buildings, tmp_path / 'spill.parquet', workers=1, log=lambda *_: None)
    validate(out)
    meta = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    depth = meta['key']['depth']
    t = pq.read_table(out, columns=['geometry', 'hilbert', 'lod'])
    # exact bounds, as the writer saw them; the stored covering is float32 and rounded outward
    bounds = shapely.bounds(shapely.from_wkb(t.column('geometry').to_numpy(zero_copy_only=False)))
    keys = t.column('hilbert').to_numpy()
    lod = t.column('lod').combine_chunks()
    finest = max(meta['zooms'])
    name = next(n for n, c in meta['lods'].items() if finest in c['zooms'])
    column = lod.field(name)
    src, tiles = copied_straddlers(bounds, keys, finest)
    assert len(src) > 0, 'the fixture has buildings crossing tile edges at the finest zoom'
    n = 2**finest
    span = 4 ** (depth - finest)
    spill = [
        Unpacker(b, 'MultiPolygon', []).row()['spill'] if b is not None else None
        for b in column.to_pylist()
    ]
    for row, tile in list(zip(src, tiles, strict=True))[:50]:
        start = int(hilbert_key(np.array([tile // n]), np.array([tile % n]), finest)[0]) * span
        # the tile's own rows, plus the first row at or past its interval
        inside = np.flatnonzero((keys >= start) & (keys < start + span))
        host = min(int(np.searchsorted(keys, start)), len(keys) - 1)
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
    t = pq.read_table(out, columns=['geometry', 'hilbert'])
    bounds = shapely.bounds(shapely.from_wkb(t.column('geometry').to_numpy(zero_copy_only=False)))
    rows, tiles = copied_straddlers(bounds, t.column('hilbert').to_numpy(), finest)
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


def test_other_crs_and_spherical_edges_are_refused(buildings, tmp_path):
    """The writer refuses them before sorting, and the JS reader refuses a file declaring them."""
    import subprocess

    out = write(buildings, tmp_path / 'ok.parquet', workers=1, log=lambda *_: None)
    built = pq.read_table(out)
    variants = {
        'utm': {'crs': {'id': {'authority': 'EPSG', 'code': 32613}}},
        'unknown': {'crs': None},
        'spherical': {'edges': 'spherical'},
    }
    refused = []
    for name, change in variants.items():
        for table, path in (
            (pq.read_table(buildings), tmp_path / f'{name}-source.parquet'),
            (built, tmp_path / f'{name}.lod.parquet'),
        ):
            geo = json.loads(table.schema.metadata[b'geo'])
            geo['columns']['geometry'].update(change)
            metadata = {**table.schema.metadata, b'geo': json.dumps(geo).encode()}
            pq.write_table(table.replace_schema_metadata(metadata), path)
        with pytest.raises(NotImplementedError):
            write(tmp_path / f'{name}-source.parquet', tmp_path / f'{name}.parquet', log=print)
        refused.append(str(tmp_path / f'{name}.lod.parquet'))
    script = Path(__file__).resolve().parents[1] / 'js' / 'test' / 'crs.test.mjs'
    result = subprocess.run(
        ['node', str(script), str(out), *refused], capture_output=True, text=True, check=True
    )
    assert f'crs tests passed, {len(refused)} files refused' in result.stdout


def test_thinnings_see_projected_area(tmp_path):
    """Area is the projected geometry's, exact for tall and multipart polygons too."""
    from geoparquet_lod.quantize import mercator

    polys = [
        shapely.box(10, 0, 11, 80),
        shapely.MultiPolygon([shapely.box(0, 0, 1, 1), shapely.box(0, 70, 1, 71)]),
        shapely.box(-105, 39.6, -104.999, 39.601),
    ]
    source = geoparquet(
        tmp_path / 'tall.parquet',
        pa.table({'geometry': pa.array([shapely.to_wkb(p) for p in polys], pa.binary())}),
        ['Polygon', 'MultiPolygon'],
    )
    seen = {}

    def capture(rows, zoom, ctx):
        seen.update(
            zip(rows.column('id').to_pylist(), rows.column('area').to_pylist(), strict=True)
        )
        return rows

    write(source, tmp_path / 'tall.lod.parquet', thinning=capture, zooms=[0, 1, 2], log=print)
    projected = shapely.transform(polys, lambda c: np.stack(mercator(c[:, 0], c[:, 1]), 1))
    assert [seen[i + 1] for i in range(3)] == pytest.approx(shapely.area(projected), rel=1e-6)


def test_an_extent_too_wide_for_the_grid_is_refused_before_sorting(tmp_path):
    pts = [shapely.Point(-170, 0), shapely.Point(170, 0)]
    source = geoparquet(
        tmp_path / 'wide.parquet',
        pa.table({'geometry': pa.array([shapely.to_wkb(p) for p in pts], pa.binary())}),
        ['Point'],
    )
    with pytest.raises(ValueError, match='int32'):
        write(source, tmp_path / 'wide.lod.parquet', zooms=[21, 22], log=print)
    assert not list(tmp_path.glob('**/*.sorted.parquet'))


def test_zooms_past_the_key_depth_are_refused_before_sorting(tmp_path):
    """A tile finer than the key's cells is no interval of keys, so the
    writer refuses it; the key's own depth serves, a copy across a tile edge
    included."""
    from geoparquet_lod.key import KEY_DEPTH

    n = 2**KEY_DEPTH
    west, east = ((5_000_000 + d) / n * 360 - 180 for d in (-0.3, 0.3))
    north, south = (
        float(np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (6_000_000 + d) / n)))))
        for d in (0.2, 0.8)
    )
    box = shapely.box(west, south, east, north)
    source = geoparquet(
        tmp_path / 'deep.parquet',
        pa.table({'geometry': pa.array([shapely.to_wkb(box)], pa.binary())}),
        ['Polygon'],
    )
    for zooms in ([KEY_DEPTH, KEY_DEPTH + 1], [-1, 0]):
        with pytest.raises(ValueError, match='Hilbert key depth'):
            write(source, tmp_path / 'deep.lod.parquet', zooms=zooms, log=print)
    assert list(tmp_path.iterdir()) == [source]
    out = write(
        source,
        tmp_path / 'deep.lod.parquet',
        zooms=[KEY_DEPTH],
        complete_from=KEY_DEPTH,
        workers=1,
        log=lambda *_: None,
    )
    validate(out, full=True)
    (blob,) = pq.read_table(out).column('lod').combine_chunks().field(f'f{KEY_DEPTH}').to_pylist()
    (copy,) = Unpacker(blob, 'MultiPolygon', []).row()['spill']
    assert copy['tile'] >> 48 == KEY_DEPTH


@pytest.mark.parametrize('count', [DICTIONARY_MAX - 2, DICTIONARY_MAX - 1, DICTIONARY_MAX])
def test_a_string_a_thinning_introduces_joins_the_value_table(tmp_path, monkeypatch, count):
    """A value only a thinning gives a string attribute is in its value table
    while the table holds DICTIONARY_MAX values or fewer; past that the
    attribute packs plain strings. The thinning's values are counted a few
    rows at a time, its new one in the last batch."""
    from geoparquet_lod import read_overview, writer

    monkeypatch.setattr(writer, 'DICTIONARY_SCAN_ROWS', 7)
    labels = [f'v{i:03d}' for i in range(count)]
    pts = shapely.points(-105 + np.arange(count) * 1e-4, np.full(count, 39.7))
    source = geoparquet(
        tmp_path / 'labels.parquet',
        pa.table({'label': labels, 'geometry': pa.array(shapely.to_wkb(pts), pa.binary())}),
        ['Point'],
    )

    def rename(rows, zoom, ctx):
        kept = rows.filter(pc.greater(rows['id'], 1))
        label = pc.if_else(pc.equal(kept['id'], count), 'NEW', kept['label'])
        return kept.set_column(kept.schema.get_field_index('label'), 'label', label)

    out = write(
        source,
        tmp_path / 'labels.lod.parquet',
        thinning=rename,
        zooms=[13, 14],
        complete_from=14,
        carry=['label'],
        workers=1,
        log=lambda *_: None,
    )
    validate(out, full=True)
    (label,) = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']['attributes']
    table = sorted([*labels, 'NEW'])
    assert label.get('values') == (table if len(table) <= DICTIONARY_MAX else None)
    assert sorted(read_overview(out, 13)['label'].to_pylist()) == sorted([*labels[1:-1], 'NEW'])
    assert sorted(read_overview(out, 14)['label'].to_pylist()) == labels


def test_a_string_past_the_value_table_limit_is_not_counted_in_python(tmp_path, monkeypatch):
    """Strings past DICTIONARY_MAX values pack plain without becoming a set of
    Python strings: a source column of that many values is counted without
    its thinning's values, a thinning giving one batch that many stops there,
    and one giving them over several batches stops at the batch passing the
    limit. Each count stays under 256 KiB of Python memory; the 17,500
    strings the thinning gives `kind` take over a megabyte as Python objects."""
    import tracemalloc

    from geoparquet_lod import read_overview, writer

    n = 20_000
    rng = np.random.default_rng(5)
    pts = shapely.points(-105 + rng.random(n) * 0.1, 39.7 + rng.random(n) * 0.1)
    source = geoparquet(
        tmp_path / 'strings.parquet',
        pa.table(
            {
                'label': [f'label-{i}' for i in range(n)],
                'kind': ['a', 'b'] * (n // 2),
                'zone': ['a', 'b'] * (n // 2),
                'geometry': pa.array(shapely.to_wkb(pts), pa.binary()),
            }
        ),
        ['Point'],
    )

    def relabel(rows, zoom, ctx):
        kept = rows.filter(pc.not_equal(pc.bit_wise_and(rows['id'], 7), 0))
        ids = kept['id'].cast(pa.string())
        columns = {
            'label': pc.if_else(pc.equal(kept['id'], 1), 'NEW', kept['label']),
            'kind': pc.binary_join_element_wise('kind-', ids, ''),
            'zone': pc.binary_join_element_wise(
                'zone-', pc.divide(kept['id'], 10).cast(pa.string()), ''
            ),
        }
        for name, column in columns.items():
            kept = kept.set_column(kept.schema.get_field_index(name), name, column)
        return kept

    peaks = {}
    value_table = writer._value_table

    def traced(con, path, name, overrides):
        tracemalloc.start()
        try:
            return value_table(con, path, name, overrides)
        finally:
            peaks[name] = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()

    monkeypatch.setattr(writer, '_value_table', traced)
    monkeypatch.setattr(writer, 'DICTIONARY_SCAN_ROWS', 1000)
    out = write(
        source,
        tmp_path / 'strings.lod.parquet',
        thinning=relabel,
        zooms=[13, 14],
        complete_from=14,
        carry=['label', 'kind', 'zone'],
        workers=1,
        log=lambda *_: None,
    )
    attributes = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']['attributes']
    assert [a['name'] for a in attributes if 'values' not in a] == ['label', 'kind', 'zone']
    assert set(peaks) == {'label', 'kind', 'zone'}
    assert max(peaks.values()) < 256 * 1024, peaks
    shown = read_overview(out, 13)
    assert 'NEW' in shown['label'].to_pylist()
    assert pc.count_distinct(shown['kind']).as_py() == shown.num_rows


def test_pack_refuses_a_string_outside_its_value_table():
    from geoparquet_lod.pack import pack

    fields = [pa.field('label', pa.string())]
    points = (np.zeros(2, np.int32), np.zeros(2, np.int32))
    tables = {'label': ['A', 'B']}
    pack('Point', points, fields, {'label': pa.array(['B', None])}, dictionaries=tables)
    with pytest.raises(ValueError, match="cannot pack label value 'NEW'"):
        pack('Point', points, fields, {'label': pa.array(['A', 'NEW'])}, dictionaries=tables)


def test_placeholder_pixels_sets_the_square_size(buildings, tmp_path):
    from geoparquet_lod.quantize import SUBPIXELS

    out = write(
        buildings,
        tmp_path / 'squares.parquet',
        placeholder_pixels=1.0,
        workers=1,
        log=lambda *_: None,
    )
    lods = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']['lods']
    assert {c['placeholder'] for c in lods.values()} == {1.0 * SUBPIXELS}


def test_write_reads_a_glob_and_adds_metadata(buildings, tmp_path):
    parts = tmp_path / 'parts'
    parts.mkdir()
    table = pq.read_table(buildings)
    half = table.num_rows // 2
    for i, part in enumerate((table.slice(0, half), table.slice(half))):
        pq.write_table(part, parts / f'part-{i}.parquet')
    out = write(
        str(parts / '*.parquet'),
        tmp_path / 'glob.parquet',
        metadata={'license_name': 'ODBL'},
        workers=1,
        log=lambda *_: None,
    )
    validate(out)
    assert pq.read_metadata(out).num_rows == table.num_rows
    assert pq.read_schema(out).metadata[b'license_name'] == b'ODBL'
