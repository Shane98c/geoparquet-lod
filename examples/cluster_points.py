"""Dense points that should merge rather than thin: one dot per 8 px cell
carrying how many it stands for.

`count` is a carried attribute the cluster overrides at each coarser zoom,
so a renderer can size or label dots by it. At the finest zoom every point
is its own dot with count 1.

    uv run python examples/cluster_points.py points.parquet out.parquet
"""

import sys

from geoparquet_lod import cluster, write

# worker processes re-import this file, so the write must not run on import
if __name__ == '__main__':
    src, out = sys.argv[1:3]
    write(
        src,
        out,
        thinning=cluster(pixels=8, count='count'),
        zooms=range(0, 15),
        carry=['count'],
        prefix_zoom=4,
    )
