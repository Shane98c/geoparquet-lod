"""Inspect extra bbox-selected overview features without changing any datasets."""

import json
import math
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from geoparquet_lod.pack import Unpacker
from geoparquet_lod.quantize import resolution
from geoparquet_lod.writer import hilbert_key

out = Path('/private/tmp/overview-japan')
validation = json.loads((out / 'validation.json').read_text())
source = pq.ParquetFile('/private/tmp/japan.tippecanoe-density.spatial.parquet')
metadata = json.loads(source.schema_arrow.metadata[b'geo'])['lod_columns']
fields = [pa.field(a['name'], pa.type_for_alias(a['type'])) for a in metadata['attributes']]
dictionaries = {a['name']: a['values'] for a in metadata['attributes'] if 'values' in a}
ends = np.cumsum(
    [source.metadata.row_group(g).num_rows for g in range(source.metadata.num_row_groups)]
)
checks = []
for check in validation['checks']:
    if check['properties'] or not check['onlyNested']:
        continue
    z, x, y = (check[k] for k in ['z', 'x', 'y'])
    level, n = f'f{z}', 2**z
    lod = metadata['lods'][level]
    ids = np.array(check['onlyNested'], np.int64)
    group_numbers = np.searchsorted(ends, ids, side='right')
    tile_key = int(hilbert_key(np.array([x]), np.array([y]), z)[0])
    details = []
    for group in np.unique(group_numbers):
        selected_ids = ids[group_numbers == group]
        first = 0 if group == 0 else ends[group - 1]
        local = selected_ids - first
        data = source.read_row_group(
            int(group), columns=['bbox', 'hilbert', f'lod.{level}'], use_threads=False
        ).take(pa.array(local))
        bboxes, keys = data['bbox'].to_pylist(), data['hilbert'].to_pylist()
        blobs = data['lod'].combine_chunks().field(level).to_pylist()
        for rid, bbox, key, blob in zip(selected_ids, bboxes, keys, blobs, strict=True):
            own = Unpacker(blob, 'MultiPolygon', fields, dictionaries).geometry()
            assert own
            # Match toMvt's vertex projection and Math.round, then ask for
            # strictly positive fill area inside the unbuffered tile.
            polygons, all_projected = [], []
            for polygon in own:
                rings = []
                for ring in polygon:
                    projected = []
                    for qx, qy in ring:
                        lon = qx * lod['scale'][0] + lod['offset'][0]
                        lat = qy * lod['scale'][1] + lod['offset'][1]
                        px = (((lon + 180) / 360 * n) - x) * 4096
                        sine = math.sin(math.radians(lat))
                        py = (
                            (0.5 - math.log((1 + sine) / (1 - sine)) / (4 * math.pi)) * n - y
                        ) * 4096
                        projected.append((math.floor(px + 0.5), math.floor(py + 0.5)))
                    rings.append(projected)
                    all_projected.extend(projected)
                polygons.append(shapely.Polygon(rings[0], rings[1:]))
            geometry = shapely.MultiPolygon(polygons)
            if not geometry.is_valid:
                geometry = shapely.make_valid(geometry)
            area = float(
                shapely.area(shapely.intersection(geometry, shapely.box(0, 0, 4096, 4096)))
            )
            points = np.array(all_projected)
            lat = math.radians((bbox['ymin'] + bbox['ymax']) / 2)
            wide = max(
                (bbox['xmax'] - bbox['xmin']) / resolution(z),
                (bbox['ymax'] - bbox['ymin']) / (resolution(z) * math.cos(lat)),
            )
            details.append(
                {
                    'id': int(rid),
                    'owner_is_query_tile': (int(key) >> (2 * (24 - z))) == tile_key,
                    'source_float32_bbox_pixel_width_upper': wide,
                    'quantized_mvt_bbox': [
                        int(v) for v in [*points.min(axis=0), *points.max(axis=0)]
                    ],
                    'positive_fill_inside_unbuffered_tile': area > 0,
                    'unbuffered_mvt_fill_area': area,
                    'source_bbox': bbox,
                }
            )
    result = {
        'place': check['place'],
        'z': z,
        'x': x,
        'y': y,
        'extra_features': len(details),
        'buffer_only': sum(not d['positive_fill_inside_unbuffered_tile'] for d in details),
        'positive_unbuffered_fill': sum(d['positive_fill_inside_unbuffered_tile'] for d in details),
        'owner_in_query_tile': sum(d['owner_is_query_tile'] for d in details),
        'source_bbox_upper_below_one_pixel': sum(
            d['source_float32_bbox_pixel_width_upper'] < 1 for d in details
        ),
        'samples': details[:5]
        + [d for d in details if d['positive_fill_inside_unbuffered_tile']][:5],
    }
    checks.append(result)
    print(json.dumps(result), flush=True)
(out / 'extra-semantics.json').write_text(
    json.dumps(
        {
            'checks': checks,
            'method': 'Packed own geometry projected/rounded like MVT; exact positive fill intersection with unbuffered tile; source Hilbert ownership and outward float32 bbox width bound',
        },
        indent=2,
    )
    + '\n'
)
