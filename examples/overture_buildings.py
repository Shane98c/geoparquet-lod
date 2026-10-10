"""Overture Maps buildings: a generic footprint dataset with no ranking column.

Coarse zooms show buildings that are large on screen (size_threshold) plus a
fixed-rate sample of the rest, so relative density survives: cities read as
filled and the countryside as sparse. Height and class ride along for styling.

    uv run python examples/overture_buildings.py overture_denver.parquet out.parquet
"""

import sys

from geoparquet_lod import combine, drop_rate, size_threshold, write

# worker processes re-import this file, so the write must not run on import
if __name__ == '__main__':
    src, out = sys.argv[1:3]
    write(
        src,
        out,
        thinning=combine(size_threshold(pixels=4), drop_rate(2.5)),
        carry=[('height', 'f32'), ('class', 'utf8')],
        workers=4,
    )
