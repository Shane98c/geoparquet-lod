"""Dense points that should merge rather than thin: one dot per 8 px cell
carrying how many it stands for.

`count` must exist in the source as an integer column, 1 for every row; the
cluster overrides the carried copy at each coarser zoom with the number of
points a dot stands for, so a renderer can size or label dots by it.

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
