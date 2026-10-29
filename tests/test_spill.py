"""Straddler copies: every tile a feature reaches holds it exactly once."""

import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import write
from geoparquet_lod.key import KEY_DEPTH, hilbert_key, tile_x, tile_y
from geoparquet_lod.spill import BUFFER, straddlers
from unpacker import Unpacker


def key_in(x: int, y: int, zoom: int) -> np.ndarray:
    """A key whose cell is in tile (x, y) at `zoom`."""
    shift = KEY_DEPTH - zoom
    return hilbert_key(np.array([x << shift]), np.array([y << shift]), KEY_DEPTH)


def reached(xmin, ymin, xmax, ymax, zoom):
    """The tiles a bbox reaches at `zoom`, the encoder's buffer included, by trying each."""
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
        if fx0 - BUFFER < x + 1 and x <= fx1 + BUFFER and fy0 - BUFFER < y + 1 and y <= fy1 + BUFFER
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


@pytest.fixture
def edge_features(tmp_path):
    """Shapes centred exactly on tile edges, and shapes three, four and two by two
    tiles across, at zoom 14."""
    zoom = 14
    tile = 360 / 2**zoom
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
