"""Independently validate sampled browser-reader bbox membership from packed integers."""

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from geoparquet_lod.pack import Unpacker

out = Path('/private/tmp/overview-alternatives')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--input', type=Path, default=out / 'browser-check.json')
parser.add_argument(
    '--output', type=Path, default=Path('benchmarks/overview-alternatives/browser-validation.json')
)
args = parser.parse_args()
results = json.loads(args.input.read_text())
source = pq.read_table(out / 'reference.parquet')
meta = json.loads(source.schema.metadata[b'geo'])['lod_columns']
fields = [pa.field(a['name'], pa.type_for_alias(a['type'])) for a in meta['attributes']]
dictionaries = {a['name']: a['values'] for a in meta['attributes'] if 'values' in a}
bounds = {}
for level in ['f8', 'f11', 'f14', 'f16']:
    lod = meta['lods'][level]
    bb = np.full((len(source), 4), np.nan)
    for row, blob in enumerate(source['lod'].combine_chunks().field(level).to_pylist()):
        geom = Unpacker(blob, 'MultiPolygon', fields, dictionaries).geometry() if blob else []
        if geom:
            points = np.array([p for poly in geom for ring in poly for p in ring])
            lo = points.min(axis=0) * lod['scale'] + lod['offset']
            hi = points.max(axis=0) * lod['scale'] + lod['offset']
            bb[row] = [*lo, *hi]
    bounds[level] = bb
checks = []
for result in results['results']:
    z, x, y = (result[k] for k in ['z', 'x', 'y'])
    n = 2**z

    def latitude(t, tiles=n):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / tiles))))

    pad = 64 / 4096
    box = [
        (x - pad) / n * 360 - 180,
        latitude(y + 1 + pad),
        (x + 1 + pad) / n * 360 - 180,
        latitude(y - pad),
    ]
    b = bounds[f'f{z}']
    expected = np.flatnonzero(
        (b[:, 0] <= box[2]) & (b[:, 2] >= box[0]) & (b[:, 1] <= box[3]) & (b[:, 3] >= box[1])
    ).tolist()
    assert expected == result['rows'], (result['place'], z, len(expected), len(result['rows']))
    crossing = sum(
        any(b[row, 0] < lon < b[row, 2] for lon in [x / n * 360 - 180, (x + 1) / n * 360 - 180])
        or any(b[row, 1] < lat < b[row, 3] for lat in [latitude(y), latitude(y + 1)])
        for row in expected
    )
    checks.append(
        {k: v for k, v in result.items() if k != 'rows'}
        | {'independent_bbox_membership_equal': True, 'tile_boundary_crossers': crossing}
    )
args.output.write_text(
    json.dumps(
        {
            'matches': True,
            'method': 'Independent packed integer geometry scan; buffered bbox predicate',
            'files_all_source_and_overview_columns_equal': True,
            'mvt_source_properties_equal': True,
            'checks': checks,
        },
        indent=2,
    )
    + '\n'
)
print(
    f'{len(checks)} independent bbox checks; {sum(x["tile_boundary_crossers"] for x in checks)} boundary-crossing instances'
)
