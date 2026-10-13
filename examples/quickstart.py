"""Create a small synthetic dataset and an indexed map, with no dataset download.

    uv run python examples/quickstart.py [output-directory]

Run once in a fresh output directory; refuses to overwrite existing files.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from geoparquet_lod import chain, drop_smallest, tiny_polygons, write
from geoparquet_lod.optimize import optimize


def main(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / 'buildings.source.parquet'
    packed = directory / 'buildings.packed.parquet'
    indexed = directory / 'buildings.lod.parquet'
    for path in (source, packed, indexed):
        if path.exists():
            raise FileExistsError(f'{path} exists; choose a fresh output directory')

    rng = np.random.default_rng(42)
    count = 5000
    x = -105.02 + rng.random(count) * 0.08
    y = 39.7 + rng.random(count) * 0.06
    size = rng.uniform(0.00006, 0.00025, count)
    table = pa.table(
        {
            'id': np.arange(count, dtype=np.int64),
            'height': rng.uniform(3, 40, count).astype(np.float32),
            'class': ['building'] * count,
            'name': [f'Synthetic building {i}' for i in range(count)],
            'geometry': pa.array(shapely.to_wkb(shapely.box(x, y, x + size, y + size))),
        }
    )
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), source)
    write(
        str(source),
        packed,
        thinning=chain(drop_smallest(850_000), tiny_polygons()),
        carry=['height', 'class'],
        prefix_zoom=None,
        zooms=list(range(8, 17)),
        complete_from=14,
        workers=2,
    )
    optimize(packed, indexed)
    print(f'Indexed demo: {indexed.resolve()}')


if __name__ == '__main__':
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path('.'))
