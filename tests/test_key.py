"""The Hilbert key and the tile math the writer's SQL and numpy share."""

import duckdb
import numpy as np
import pyarrow as pa
from geoparquet_lod.key import (
    KEY_DEPTH,
    hilbert_key,
    mercator,
    sql_key_cell,
    sql_mercator_x,
    sql_mercator_y,
    tile_x,
    tile_y,
)


def test_hilbert_key_is_the_standard_curve():
    rng = np.random.default_rng(0)
    x = rng.integers(0, 65536, 5000)
    y = rng.integers(0, 65536, 5000)
    con = duckdb.connect()
    con.execute('LOAD spatial')
    con.register('p', pa.table({'x': x.astype(float), 'y': y.astype(float)}))
    theirs = con.execute(
        'SELECT ST_Hilbert(x, y, {min_x: 0, min_y: 0, max_x: 65535, max_y: 65535}::BOX_2D) FROM p'
    ).fetchnumpy()
    assert np.array_equal(next(iter(theirs.values())).astype(np.int64), hilbert_key(x, y, 16))
    # the quadtree prefix: a cell's index at a coarser depth is the finer index shifted down
    deep = hilbert_key(x, y, 16)
    assert np.array_equal(hilbert_key(x >> 5, y >> 5, 11), deep >> 10)


def test_sql_and_numpy_agree():
    rng = np.random.default_rng(2)
    n = 5000
    lon, lat = rng.uniform(-180, 180, n), rng.uniform(-89, 89, n)
    con = duckdb.connect()
    con.register('p', pa.table({'i': np.arange(n), 'lon': lon, 'lat': lat}))
    cell_x, cell_y = sql_key_cell('lon', 'lat')
    theirs = con.execute(
        f'SELECT {cell_x} AS kx, {cell_y} AS ky, {sql_mercator_x("lon")} AS mx, '
        f'{sql_mercator_y("lat")} AS my FROM p ORDER BY i'
    ).fetchnumpy()
    cells = 1 << KEY_DEPTH
    assert np.array_equal(theirs['kx'], np.clip(np.floor(tile_x(lon, KEY_DEPTH)), 0, cells - 1))
    assert np.array_equal(theirs['ky'], np.clip(np.floor(tile_y(lat, KEY_DEPTH)), 0, cells - 1))
    mx, my = mercator(lon, lat)
    np.testing.assert_allclose(theirs['mx'], mx, rtol=1e-12)
    np.testing.assert_allclose(theirs['my'], my, rtol=1e-12, atol=1e-6)
