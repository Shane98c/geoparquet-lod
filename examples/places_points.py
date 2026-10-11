"""Point features with a natural importance: the biggest first.

Points at every zoom (no footprint zoom). Zoomed out, each tile keeps its
top 300 by population; `drop_rate` on its own would sample uniformly and
could drop a capital in favour of a village.

    uv run python examples/places_points.py places.parquet out.parquet
"""

import sys

from geoparquet_lod import importance, write

# worker processes re-import this file, so the write must not run on import
if __name__ == '__main__':
    src, out = sys.argv[1:3]
    write(
        src,
        out,
        thinning=importance('population', per_tile=300),
        zooms=range(0, 15),
        carry=['name', 'population'],
        prefix_zoom=5,
    )
