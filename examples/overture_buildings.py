"""Overture Maps buildings: a generic footprint dataset with no ranking column.

Coarse zooms show buildings that are large on screen (size_threshold) plus a
density-limited sample of the rest, so cities read as filled and the
countryside as sparse. Height and class ride along for styling.

    uv run python examples/overture_buildings.py overture_denver.parquet out.parquet
"""

import sys

from geoparquet_lod import combine, density_budget, size_threshold, write

# worker processes re-import this file, so the write must not run on import
if __name__ == '__main__':
    src, out = sys.argv[1:3]
    write(
        src,
        out,
        thinning=combine(size_threshold(pixels=4), density_budget(20_000)),
        zooms=range(2, 17),
        polygons_from=13,
        carry=[('height', 'f32'), ('class', 'utf8')],
        workers=4,
    )
