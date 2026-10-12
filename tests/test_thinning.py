"""Thinning functions composed across zooms, as the writer applies them."""

import numpy as np
import pyarrow as pa
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


def test_chain_drop_smallest_keeps_largest_per_tile():
    from geoparquet_lod.thinning import chain, drop_smallest, tiny_polygons

    rng = np.random.default_rng(1)
    n = 20_000
    rows = pa.table(
        {
            'id': np.arange(1, n + 1),
            'cx': rng.random(n) * 0.05,
            'cy': rng.random(n) * 0.05,
            'diag': np.full(n, 1e-4),
            'area': rng.random(n) * 1e-8,
            'u': rng.random(n),
        }
    )
    ctx = Context((0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12), 12, 512, True)
    thin = chain(tiny_polygons(1.5), drop_smallest(500))
    assert thin.placeholder_pixels == 1.5
    kept = thin(rows, 12, ctx)
    _, counts = np.unique(ctx.tile_of(kept, 12), return_counts=True)
    assert counts.max() <= 500
    # what survives is the largest, measured by the area tiny_polygons carried
    reduced = tiny_polygons(1.5)(rows, 12, ctx)
    threshold = np.sort(reduced.column('area').to_numpy())[-500]
    assert kept.column('area').to_numpy().min() >= threshold
