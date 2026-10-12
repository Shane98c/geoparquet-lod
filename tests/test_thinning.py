"""Thinning functions composed across zooms, as the writer applies them."""

import numpy as np
import pyarrow as pa
import pytest
from geoparquet_lod.thinning import Context, density_budget


def test_density_budget_holds_across_zooms():
    rng = np.random.default_rng(0)
    n = 200_000
    # a dense city and sparse countryside, all in one hemisphere
    city = rng.random(n // 2) * 0.2
    country = rng.random(n // 2) * 40
    rows = pa.table(
        {
            'id': np.arange(1, n + 1),
            'cx': np.concatenate([city, country]),
            'cy': np.concatenate([city, country]),
            'diag': np.full(n, 1e-4),
            'area': np.full(n, 1e-8),
            'u': rng.random(n),
        }
    )
    ctx = Context((0, 1, 2, 3, 4, 5, 6, 7, 8), 8, 512, True)
    thin = density_budget(5_000)
    kept = rows
    for zoom in range(7, -1, -1):
        kept = thin(kept, zoom, ctx)
        _, counts = np.unique(ctx.tile_of(kept, zoom), return_counts=True)
        assert counts.max() <= 5_000 * 1.05, f'zoom {zoom} busiest tile {counts.max()}'
    assert kept.num_rows > 1_000, 'the coarsest zoom still shows the countryside'


def test_drop_smallest_sets_one_bar_per_zoom():
    from geoparquet_lod.thinning import chain, drop_smallest, tile_cost, tiny_polygons

    rng = np.random.default_rng(1)
    city, town = 20_000, 200
    n = city + town
    rows = pa.table(
        {
            'id': np.arange(1, n + 1),
            'cx': np.concatenate([rng.random(city) * 0.05, 1 + rng.random(town) * 0.05]),
            'cy': rng.random(n) * 0.05,
            'diag': np.full(n, 1e-4),
            'area': rng.random(n) * 1e-8,
            'nv': np.full(n, 5),
            'u': rng.random(n),
        }
    )
    ctx = Context(tuple(range(13)), 12, 512, True)
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


def test_pixel_area_grows_toward_the_poles():
    rows = pa.table(
        {
            'id': np.array([1, 2]),
            'cx': np.array([0.0, 0.0]),
            'cy': np.array([0.0, 60.0]),
            'diag': np.full(2, 1e-4),
            'area': np.full(2, 1e-8),
            'nv': np.full(2, 5),
            'u': np.zeros(2),
        }
    )
    ctx = Context((0, 1, 2, 3, 4, 5, 6, 7, 8), 8, 512, True)
    px = ctx.pixel_area(rows, 8)
    assert px[1] == pytest.approx(px[0] * 2, rel=1e-6), 'a degree is twice as tall on screen at 60N'
