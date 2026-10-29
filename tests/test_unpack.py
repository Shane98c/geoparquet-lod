"""The column decoder against the row decoder, malformed values, and overview tables."""

import json
import math

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import read_overview, write
from geoparquet_lod.unpack import UnpackError, carried, lod_values, unpack, unpack_batches
from unpacker import Unpacker


def quiet(*_):
    pass


def source(path, geometry, columns, types):
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': types}},
    }
    table = pa.table({**columns, 'geometry': pa.array(shapely.to_wkb(geometry), pa.binary())})
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), path)
    return path


def pairs(offsets):
    return zip(offsets[:-1], offsets[1:], strict=True)


def as_rows(u, family):
    """unpack's records grouped per value, in Unpacker's shape."""
    if family == 'Point':
        geometry = list(zip(u.geometry[0].tolist(), u.geometry[1].tolist(), strict=True))
    else:
        geom, poly, ring, x, y = (a.tolist() for a in u.geometry)
        rings = [list(zip(x[a:b], y[a:b], strict=True)) for a, b in pairs(ring)]
        if family == 'MultiPolygon':
            rings = [rings[a:b] for a, b in pairs(poly)]
        geometry = [rings[a:b] for a, b in pairs(geom)]
    attrs = {name: values.to_pylist() for name, values in u.attrs.items()}
    rows = {}
    for r, row in enumerate(u.row.tolist()):
        record = {'geometry': geometry[r], 'attrs': {k: v[r] for k, v in attrs.items()}}
        if u.copy[r]:
            copy = {'tile': int(u.tile[r]), 'id': int(u.id[r]), **record}
            rows[row]['spill'].append(copy)
        else:
            rows[row] = {**record, 'spill': []}
    return rows


def same(a, b):
    if isinstance(a, float) and isinstance(b, float):
        return a == b or (math.isnan(a) and math.isnan(b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same(p, q) for p, q in zip(a, b, strict=True))
    return a == b


def assert_agrees(path):
    """Every value of every LOD decodes the same both ways; returns the copies seen."""
    f = pq.ParquetFile(path)
    lod = json.loads(f.schema_arrow.metadata[b'geo'])['lod_columns']
    fields, dictionaries = carried(lod)
    copies = 0
    for group in range(f.metadata.num_row_groups):
        table = f.read_row_group(group, columns=['lod'])
        for name, child in lod['lods'].items():
            family = child['geometry_type']
            values = table.column('lod').combine_chunks().field(name)
            got = as_rows(unpack(values, family, fields, dictionaries), family)
            expected = {
                i: Unpacker(blob, family, fields, dictionaries).row()
                for i, blob in enumerate(values.to_pylist())
                if blob is not None
            }
            assert got.keys() == expected.keys()
            for i, row in expected.items():
                if family == 'Point':
                    row['geometry'] = tuple(row['geometry'])
                    for c in row['spill']:
                        c['geometry'] = tuple(c['geometry'])
                assert same(got[i], row), (group, name, i)
                copies += len(row['spill'])
    return copies


@pytest.fixture(scope='module')
def polygons(tmp_path_factory):
    work = tmp_path_factory.mktemp('unpack')
    rng = np.random.default_rng(4)
    n = 1500
    x, y = -105 + rng.random(n) * 0.4, 39.6 + rng.random(n) * 0.3
    size = np.exp(rng.normal(-9, 0.8, n))
    boxes = shapely.box(x, y, x + size, y + size * 0.6)
    holes = shapely.Polygon(
        [(-104.9, 39.7), (-104.89, 39.7), (-104.89, 39.71), (-104.9, 39.71)],
        [[(-104.898, 39.702), (-104.892, 39.702), (-104.892, 39.708), (-104.898, 39.708)]],
    )
    pair = shapely.MultiPolygon([shapely.box(-104.8, 39.8, -104.79, 39.81), boxes[0]])
    geometry = [holes, pair, *boxes[2:]]
    columns = {
        'risk': pa.array(rng.random(n).astype(np.float32)),
        'height': pa.array([math.nan if i % 11 == 0 else i * 0.1 for i in range(n)]),
        'floors': pa.array([None if i % 7 == 0 else i % 9 for i in range(n)], pa.uint8()),
        'flag': pa.array([None if i % 5 == 0 else i % 2 == 0 for i in range(n)]),
        'name': pa.array([None if i % 13 == 0 else f'建物 {i} é' for i in range(n)]),
        'zone': pa.array([f'zone{i % 4}' for i in range(n)]),
        'kind': pa.nulls(n, pa.string()),
    }
    src = source(work / 'polygons.parquet', geometry, columns, ['Polygon', 'MultiPolygon'])
    out = work / 'out.parquet'
    return write(src, out, carry=list(columns), row_group_rows=512, workers=1, log=quiet)


def test_polygons_agree(polygons):
    assert assert_agrees(polygons) > 0, 'the fixture has boundary copies'


@pytest.mark.parametrize('batch_bytes', [1, 300])
def test_batches_decode_as_one(polygons, batch_bytes):
    f = pq.ParquetFile(polygons)
    lod = json.loads(f.schema_arrow.metadata[b'geo'])['lod_columns']
    fields, dictionaries = carried(lod)
    batches = 0
    for group in range(f.metadata.num_row_groups):
        for name, child in lod['lods'].items():
            family = child['geometry_type']
            values = lod_values(f, group, 'lod', name)
            whole = as_rows(unpack(values.combine_chunks(), family, fields, dictionaries), family)
            rows = {}
            for u in unpack_batches(values, family, fields, dictionaries, batch_bytes):
                rows.update(as_rows(u, family))
                batches += 1
            assert same(rows, whole), (group, name)
    assert batches > 3 * f.metadata.num_row_groups * len(lod['lods'])


def test_read_overview(polygons):
    f = pq.ParquetFile(polygons)
    assert f.metadata.num_row_groups > 1, 'row indexes continue across row groups'
    lod = json.loads(f.schema_arrow.metadata[b'geo'])['lod_columns']
    fields, dictionaries = carried(lod)
    for zoom in (lod['zooms'][0] + 2, lod['zooms'][-1]):
        name, child = next((n, c) for n, c in lod['lods'].items() if zoom in c['zooms'])
        (sx, sy), (ox, oy) = child['scale'], child['offset']
        table = read_overview(polygons, zoom)
        values = f.read(columns=['lod'])['lod'].combine_chunks().field(name).to_pylist()
        decoded = [
            (i, Unpacker(blob, 'MultiPolygon', fields, dictionaries).row())
            for i, blob in enumerate(values)
            if blob is not None
        ]
        rows = [(i, row) for i, row in decoded if row['geometry']]
        assert table['row'].to_pylist() == [i for i, _ in rows], 'shown rows, copies left out'
        shapes = shapely.from_wkb(table['geometry'].to_numpy(zero_copy_only=False))
        for shape, (_, row) in zip(shapes, rows, strict=True):
            rings = [[list(r.coords) for r in (p.exterior, *p.interiors)] for p in shape.geoms]
            grid = [
                [[(ox + sx * x, oy + sy * y) for x, y in r] for r in p] for p in row['geometry']
            ]
            assert rings == grid
        for a in fields:
            assert same(table[a.name].to_pylist(), [row['attrs'][a.name] for _, row in rows])
    metadata = table.schema.field('geometry').metadata
    assert json.loads(metadata[b'ARROW:extension:metadata'])['crs'] == 'EPSG:3857'
    with pytest.raises(ValueError, match='not one of'):
        read_overview(polygons, lod['zooms'][-1] + 1)


def test_read_overview_in_batches(polygons, monkeypatch):
    zoom = json.loads(pq.read_schema(polygons).metadata[b'geo'])['lod_columns']['zooms'][-1]
    whole = read_overview(polygons, zoom)
    monkeypatch.setattr('geoparquet_lod.unpack.BATCH_BYTES', 500)
    batched = read_overview(polygons, zoom)
    assert batched['row'].num_chunks > whole['row'].num_chunks
    assert batched.schema.equals(whole.schema, check_metadata=True)
    assert same(batched.to_pylist(), whole.to_pylist())


def test_lines_and_points_agree(tmp_path):
    rng = np.random.default_rng(5)
    lines = []
    for i in range(300):
        x, y = -105 + rng.random() * 0.3, 39.7 + rng.random() * 0.2
        line = [(x + k * 1e-3, y + (k % 2) * 1e-3) for k in range(2 + i % 6)]
        lines.append(shapely.MultiLineString([line, [(x, y - 2e-3), (x + 4e-3, y)]]))
    names = pa.array([f'road {i}' for i in range(300)])
    src = source(tmp_path / 'lines.parquet', lines, {'road': names}, ['MultiLineString'])
    assert_agrees(write(src, tmp_path / 'lines.lod.parquet', carry=['road'], workers=1, log=quiet))
    points = shapely.points(-100 + rng.random(800) * 30, 30 + rng.random(800) * 15)
    population = pa.array(rng.integers(10, 1_000_000, 800), pa.uint32())
    src = source(tmp_path / 'points.parquet', points, {'population': population}, ['Point'])
    out = write(src, tmp_path / 'points.lod.parquet', carry=['population'], workers=1, log=quiet)
    assert_agrees(out)


SQUARE = [1, 1, 4, 0, 0, 4, 0, 0, 4, 3, 0]  # one open ring, before attributes and copies


@pytest.mark.parametrize(
    'value, message',
    [
        (bytes([*SQUARE, 0]), 'runs past the end'),
        (bytes([*SQUARE, 0, 0, 7]), 'trailing bytes'),
        (bytes([*SQUARE, 0, 0x80]), 'cut short'),
        (bytes([1, 1, 9, 0, 0, 4, 0, 0, 4, 3, 0, 0, 0]), 'count runs past'),
        (bytes([1, 1, 4, *[0xFF] * 9, 2, 0, 4, 0, 0, 4, 3, 0, 0, 0]), 'wider than 64 bits'),
        (bytes([*SQUARE, 1, 3, 0]), 'string bytes run past'),
        (bytes([*SQUARE, 2, 9, 0]), 'past its value table'),
    ],
)
def test_malformed_values_name_their_row(value, message):
    fields = [pa.field('name', pa.string()), pa.field('zone', pa.string())]
    good = bytes([*SQUARE, 0b01, 2, 0, *b'ab'])
    values = pa.array([good, None, good, value, good], pa.binary())
    zones = {'zone': ['a', 'b']}
    assert unpack(values.slice(0, 3), 'MultiPolygon', fields, zones).row.tolist() == [0, 2]
    with pytest.raises(UnpackError, match=message) as error:
        unpack(values, 'MultiPolygon', fields, zones)
    assert error.value.row == 3
    chunks = pa.chunked_array([values.slice(0, 2), values.slice(2)])
    with pytest.raises(UnpackError, match=message) as error:
        list(unpack_batches(chunks, 'MultiPolygon', fields, zones, batch_bytes=1))
    assert error.value.row == 3, 'rows count from the first chunk'
