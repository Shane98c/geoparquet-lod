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
