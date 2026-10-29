"""Straddler copies: every tile a feature reaches holds it exactly once."""

import json

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import validate, write
from geoparquet_lod.key import KEY_DEPTH, hilbert_key, tile_x, tile_y
from geoparquet_lod.quantize import render_polygons
from geoparquet_lod.spill import MARGIN, straddlers, take_ragged
from unpacker import Unpacker


def key_in(x: int, y: int, zoom: int) -> np.ndarray:
    """A key whose cell is in tile (x, y) at `zoom`."""
    shift = KEY_DEPTH - zoom
    return hilbert_key(np.array([x << shift]), np.array([y << shift]), KEY_DEPTH)


def reached(xmin, ymin, xmax, ymax, zoom):
    """The tiles a bbox reaches at `zoom`, each reaching MARGIN past its edge, by trying each."""
    fx0, fx1, fy0, fy1 = (
        tile_x(xmin, zoom),
        tile_x(xmax, zoom),
        tile_y(ymax, zoom),
        tile_y(ymin, zoom),
    )
    return {
        (x, y)
        for x in range(int(fx0) - 2, int(fx1) + 3)
        for y in range(int(fy0) - 2, int(fy1) + 3)
        if fx0 - MARGIN < x + 1 and x <= fx1 + MARGIN and fy0 - MARGIN < y + 1 and y <= fy1 + MARGIN
    }


def tile_rows(keys, decoded, zoom, x, y):
    """File rows a reader finds in tile (x, y): the rows of its key interval
    with geometry, and the copies tagged with the tile there or on its host."""
    span = 4 ** (KEY_DEPTH - zoom)
    start = int(hilbert_key(np.array([x]), np.array([y]), zoom)[0]) * span
    inside = np.flatnonzero((keys >= start) & (keys < start + span))
    host = min(int(np.searchsorted(keys, start)), len(keys) - 1)
    rows = [int(r) for r in inside if decoded[r] and decoded[r]['geometry']]
    tag = (zoom << 48) | (x * 2**zoom + y)
    for r in np.unique(np.r_[inside, host]):
        if decoded[r]:
            rows += [c['id'] for c in decoded[r]['spill'] if c['tile'] == tag]
    return rows


def test_a_feature_centred_on_a_tile_edge_is_copied_where_its_key_is_not():
    """At an exact edge the key's arithmetic may put the feature on either side;
    the copy goes to the other side either way."""
    zoom, k = 14, 3413
    edge = -180 + k * 360 / 2**zoom
    bounds = [np.array([v]) for v in (edge - 2**-12, 39.7, edge + 2**-12, 39.7001)]
    assert (bounds[0] + bounds[2]) / 2 == edge
    (row,) = {y for _, y in reached(*(b[0] for b in bounds), zoom)}
    for own, other in ((k - 1, k), (k, k - 1)):
        rows, tiles = straddlers(*bounds, key_in(own, row, zoom), zoom)
        assert rows.tolist() == [0]
        assert tiles.tolist() == [other * 2**zoom + row]


def test_a_feature_short_of_a_tile_is_copied_there_within_the_margin():
    """A neighbour two pixels away still draws an outline across the edge; one
    in the encoder's 64/4096 buffer but past the margin draws nothing there."""
    zoom, k = 14, 3413
    edge = -180 + k * 360 / 2**zoom
    pixel = 360 / 2**zoom / 512
    row = int(tile_y(39.7, zoom))
    for gap, tiles in ((1, [k * 2**zoom + row]), (6, [])):
        bounds = [np.array([v]) for v in (edge - 40 * pixel, 39.7, edge - gap * pixel, 39.7001)]
        assert straddlers(*bounds, key_in(k - 1, row, zoom), zoom)[1].tolist() == tiles, gap


def test_every_other_tile_a_feature_reaches_gets_one_copy():
    rng = np.random.default_rng(4)
    n, zoom = 600, 12
    tile = 360 / 2**zoom
    cx, cy = -105 + rng.random(n) * 0.5, 39.6 + rng.random(n) * 0.3
    half_w = rng.choice([0.2, 0.6, 1.1, 1.6, 2.2], n) * tile
    half_h = rng.choice([0.2, 0.6, 1.1], n) * tile * 0.77
    xmin, xmax, ymin, ymax = cx - half_w, cx + half_w, cy - half_h, cy + half_h
    kx = np.floor(tile_x((xmin + xmax) / 2, KEY_DEPTH)).astype(np.int64)
    ky = np.floor(tile_y((ymin + ymax) / 2, KEY_DEPTH)).astype(np.int64)
    rows, tiles = straddlers(xmin, ymin, xmax, ymax, hilbert_key(kx, ky, KEY_DEPTH), zoom)
    expected, shapes = [], set()
    for i in range(n):
        tiles_i = reached(xmin[i], ymin[i], xmax[i], ymax[i], zoom)
        xs, ys = {x for x, _ in tiles_i}, {y for _, y in tiles_i}
        shapes.add((len(xs), len(ys)))
        own = (kx[i] >> (KEY_DEPTH - zoom), ky[i] >> (KEY_DEPTH - zoom))
        assert own in tiles_i
        expected += [(i, x * 2**zoom + y) for x, y in tiles_i if (x, y) != own]
    assert {(3, 1), (4, 1), (2, 2), (5, 3)} <= shapes
    assert sorted(zip(rows.tolist(), tiles.tolist(), strict=True)) == sorted(expected)


def test_take_ragged_picks_whole_rows_in_order():
    polys = [
        shapely.MultiPolygon([shapely.box(i + j, 0, i + j + 0.5, 1) for j in range(i % 3 + 1)])
        for i in range(40)
    ]
    polys[7] = shapely.box(0, 0, 4, 4).difference(shapely.box(1, 1, 2, 2))
    zeros = np.zeros(len(polys))
    store = render_polygons(shapely.to_wkb(polys), zeros, zeros, 1000.0, (0.0, 0.0))
    picks = np.array([7, 3, 3, 39, 0])
    geom, poly, ring, x, y = take_ragged(store, picks)
    for i, p in enumerate(picks):
        alone = render_polygons(
            shapely.to_wkb([polys[p]]), zeros[:1], zeros[:1], 1000.0, (0.0, 0.0)
        )
        polygons = slice(geom[i], geom[i + 1] + 1)
        assert np.array_equal(np.diff(poly[polygons]), np.diff(alone[1]))
        rings = slice(poly[geom[i]], poly[geom[i + 1]] + 1)
        assert np.array_equal(np.diff(ring[rings]), np.diff(alone[2]))
        coords = slice(ring[poly[geom[i]]], ring[poly[geom[i + 1]]])
        assert np.array_equal(x[coords], alone[3]) and np.array_equal(y[coords], alone[4])
    assert [len(a) for a in take_ragged(store, np.zeros(0, np.int64))] == [1, 1, 1, 0, 0]


@pytest.fixture
def edge_features(tmp_path):
    """Shapes centred exactly on tile edges, shapes three, four and two by two
    tiles across, and shapes ending a pixel and six pixels short of a tile, at
    zoom 14."""
    zoom = 14
    tile = 360 / 2**zoom
    pixel = tile / 512
    k = int(tile_x(-105.0, zoom))
    edge = -180 + k * tile
    row = int(tile_y(39.7, zoom))
    lat_edge = float(np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * row / 2**zoom)))))
    middle = edge + tile / 2
    boxes = {
        'x edge': (edge - 2**-12, 39.7005, edge + 2**-12, 39.7008),
        'y edge': (edge + 0.3 * tile, lat_edge - 2**-13, edge + 0.4 * tile, lat_edge + 2**-13),
        'equator': (edge + 0.3 * tile, -(2**-13), edge + 0.4 * tile, 2**-13),
        'three across': (middle - 1.3 * tile, 39.7005, middle + 1.3 * tile, 39.7008),
        'four across': (edge - 1.6 * tile, 39.7005, edge + 1.6 * tile, 39.7008),
        'corner': (
            edge - 0.2 * tile,
            lat_edge - 0.2 * tile,
            edge + 0.2 * tile,
            lat_edge + 0.2 * tile,
        ),
        'within the margin': (edge - 0.3 * tile, 39.7005, edge - pixel, 39.7008),
        'past the margin': (edge - 0.3 * tile, 39.7005, edge - 6 * pixel, 39.7008),
    }
    table = pa.table(
        {
            'name': list(boxes),
            'geometry': shapely.to_wkb([shapely.box(*b) for b in boxes.values()]),
        }
    )
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    src = tmp_path / 'edges.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    return src, boxes


def test_every_tile_a_feature_reaches_holds_it_once(edge_features, tmp_path):
    src, boxes = edge_features
    out = write(
        src,
        tmp_path / 'edges.lod.parquet',
        zooms=[13, 14],
        complete_from=13,
        workers=1,
        log=lambda *_: None,
    )
    assert len(reached(*boxes['within the margin'], 14)) == 2
    assert len(reached(*boxes['past the margin'], 14)) == 1
    table = pq.read_table(out)
    meta = json.loads(table.schema.metadata[b'geo'])['lod_columns']
    keys = table.column('hilbert').to_numpy()
    names = table.column('name').to_pylist()
    for zoom in (13, 14):
        name, info = next((n, i) for n, i in meta['lods'].items() if zoom in i['zooms'])
        decoded = [
            Unpacker(blob, info['geometry_type'], []).row() if blob is not None else None
            for blob in table.column('lod').combine_chunks().field(name).to_pylist()
        ]
        for row, label in enumerate(names):
            tiles = reached(*boxes[label], zoom)
            xs, ys = {x for x, _ in tiles}, {y for _, y in tiles}
            near = {
                (x, y)
                for x in range(min(xs) - 1, max(xs) + 2)
                for y in range(min(ys) - 1, max(ys) + 2)
            }
            for x, y in near:
                found = tile_rows(keys, decoded, zoom, x, y)
                assert found.count(row) == ((x, y) in tiles), (label, zoom, x, y)


@pytest.mark.parametrize('override', [99, None])
def test_a_copy_carries_the_value_its_row_has_at_the_lod(tmp_path, override):
    """A thinning's value for an attribute, null included, reaches the copy in
    the next tile as well as the row, so the feature reads the same on both
    sides of the edge, here on a host the LOD does not show."""
    zoom = 14
    edge = -180 + 3413 * 360 / 2**zoom
    row = int(tile_y(39.7, zoom)) + 0.5
    lat = float(np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * row / 2**zoom)))))
    boxes = [
        shapely.box(edge - 0.003, lat - 0.0005, edge + 0.001, lat + 0.0005),
        shapely.box(edge + 0.005, lat - 0.0005, edge + 0.006, lat + 0.0005),
    ]
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table({'value': pa.array([1, 2], pa.int64()), 'geometry': shapely.to_wkb(boxes)})
    src = tmp_path / 'override.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)

    def first(rows, zoom, ctx):
        kept = rows.filter(pc.equal(rows.column('value'), 1))
        values = pa.array([override] * kept.num_rows, pa.int64())
        return kept.set_column(kept.schema.get_field_index('value'), 'value', values)

    out = write(
        src,
        tmp_path / 'override.lod.parquet',
        thinning=first,
        zooms=[zoom, zoom + 1],
        complete_from=zoom + 1,
        carry=['value'],
        single_lod_bytes=0,
        workers=1,
        log=lambda *_: None,
    )
    validate(out, full=True)
    table = pq.read_table(out)
    meta = json.loads(table.schema.metadata[b'geo'])['lod_columns']
    name, info = next((n, i) for n, i in meta['lods'].items() if zoom in i['zooms'])
    decoded = [
        Unpacker(blob, info['geometry_type'], [pa.field('value', pa.int64())]).row()
        for blob in table.column('lod').combine_chunks().field(name).to_pylist()
    ]
    straddler, host = (table.column('value').to_pylist().index(v) for v in (1, 2))
    assert decoded[straddler]['attrs'] == {'value': override}
    assert decoded[host]['geometry'] == [], 'the host is not shown at the LOD'
    (copy,) = decoded[host]['spill']
    assert copy['id'] == straddler
    assert copy['attrs'] == {'value': override}
