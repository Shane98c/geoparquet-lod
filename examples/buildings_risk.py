"""Buildings with a risk score: show only the ones that matter when zoomed out.

Coarse zooms are a fixed-rate sample of the buildings with nonzero risk;
every building is drawn as a footprint from z13. Two risk values ride along
so the map can colour by either period without another request.

    uv run python examples/buildings_risk.py buildings.parquet out.parquet
"""

import sys

from geoparquet_lod import drop_rate, only, write

# worker processes re-import this file, so the write must not run on import
if __name__ == '__main__':
    src, out = sys.argv[1:3]
    write(
        src,
        out,
        thinning=only('rps_2011 > 0 AND rps_2047 > 0', drop_rate(2.5)),
        zooms=range(2, 17),
        polygons_from=13,
        carry=[('rps_2011', 'f32'), ('rps_2047', 'f32')],
        prefix_zoom=6,
    )
