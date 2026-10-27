"""Thinning functions composed across zooms, as the writer applies them."""

import numpy as np
import pyarrow as pa
import pytest
from geoparquet_lod.quantize import mercator
from geoparquet_lod.thinning import Context, density_budget

METRES_PER_DEGREE = 111_320


def rows_at(lon, lat, **columns):
    """Thinning rows with centroids given in degrees, as the writer hands them over in metres."""
    cx, cy = mercator(lon, lat)
    return pa.table({'id': np.arange(1, len(cx) + 1), 'cx': cx, 'cy': cy, **columns})


def test_density_budget_holds_across_zooms():
    rng = np.random.default_rng(0)
    n = 200_000
    # a dense city and sparse countryside, all in one hemisphere
    city = rng.random(n // 2) * 0.2
    country = rng.random(n // 2) * 40
    rows = rows_at(
        np.concatenate([city, country]),
        np.concatenate([city, country]),
        diag=np.full(n, 10.0),
        area=np.full(n, 100.0),
        u=rng.random(n),
    )
    ctx = Context((0, 1, 2, 3, 4, 5, 6, 7, 8), 8, 512)
    thin = density_budget(5_000)
    kept = rows
    for zoom in range(7, -1, -1):
        kept = thin(kept, zoom, ctx)
        _, counts = np.unique(ctx.tile_of(kept, zoom), return_counts=True)
        assert counts.max() <= 5_000 * 1.05, f'zoom {zoom} busiest tile {counts.max()}'
    assert kept.num_rows > 1_000, 'the coarsest zoom still shows the countryside'


def test_density_budget_takes_a_budget_per_zoom():
    rng = np.random.default_rng(2)
    n = 50_000
    rows = rows_at(
        rng.random(n) * 0.2,
        rng.random(n) * 0.2,
        diag=np.full(n, 10.0),
        area=np.full(n, 100.0),
        u=rng.random(n),
    )
    ctx = Context((0, 1, 2, 3, 4), 4, 512)
    thin = density_budget(lambda zoom: 1_000 * (zoom + 1))
    for zoom in range(5):
        kept = thin(rows, zoom, ctx)
        # everything sits in one tile at these zooms, so the budget is the row count
        assert abs(kept.num_rows - 1_000 * (zoom + 1)) <= 50, (zoom, kept.num_rows)


def test_drop_smallest_sets_one_bar_per_zoom():
    from geoparquet_lod.thinning import chain, drop_smallest, tile_cost, tiny_polygons

    rng = np.random.default_rng(1)
    city, town = 20_000, 200
    n = city + town
    rows = rows_at(
        np.concatenate([rng.random(city) * 0.05, 1 + rng.random(town) * 0.05]),
        rng.random(n) * 0.05,
        diag=np.full(n, 1e-4 * METRES_PER_DEGREE),
        area=rng.random(n) * 1e-8 * METRES_PER_DEGREE**2,
        nv=np.full(n, 5),
        u=rng.random(n),
    )
    ctx = Context(tuple(range(13)), 12, 512)
    budget = 20_000
    kept = drop_smallest(budget)(rows, 12, ctx)
    cost = tile_cost(kept, 12, ctx)
    _, inverse = np.unique(ctx.tile_of(kept, 12), return_inverse=True)
    assert np.bincount(inverse, weights=cost).max() <= budget, 'the busiest tile fits'
    # the bar the city needed applies to the town too: every row above it survives anywhere
    bar = ctx.pixel_area(kept, 12).min()
    assert kept.num_rows == np.count_nonzero(ctx.pixel_area(rows, 12) >= bar)
    assert kept.num_rows < n
    # after the fold the town keeps all its large buildings as themselves
    thin = chain(drop_smallest(budget), tiny_polygons(0.25))
    assert thin.placeholder_pixels == 0.25
    folded = thin(rows, 12, ctx)
    assert folded.num_rows <= kept.num_rows


def test_a_degree_draws_twice_as_tall_at_60n():
    _, y = mercator(np.zeros(4), np.array([0.0, 1e-4, 60.0, 60.0 + 1e-4]))
    assert y[3] - y[2] == pytest.approx(2 * (y[1] - y[0]), rel=1e-4)


def test_grid_coordinates_outside_int32_raise():
    from geoparquet_lod.encoding import round_away

    assert round_away(np.array([2**31 - 1.4, -(2**31) + 0.4])).tolist() == [2**31 - 1, -(2**31)]
    with pytest.raises(OverflowError):
        round_away(np.array([2.0**31]))


def test_hilbert_key_is_the_standard_curve():
    import duckdb
    from geoparquet_lod.writer import hilbert_key

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
