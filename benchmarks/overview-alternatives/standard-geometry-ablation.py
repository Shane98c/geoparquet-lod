"""Matched physical row-group samples: quantized versus GeoParquet 1.1 geometries."""

import json
import time
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

SOURCE = Path('/private/tmp/overview-japan-compact-1024/japan.compact.parquet')
OUT = Path('/private/tmp/overview-standard-geometry')
OUT.mkdir(exist_ok=True)
f = pq.ParquetFile(SOURCE)
meta = json.loads(f.schema_arrow.metadata[b'overview_experiment'])
# Derive actual boundaries from level column statistics rather than assumptions.
by = {k: [] for k in [4, 8, 11, 14, 16]}
for i in range(f.num_row_groups):
    s = f.metadata.row_group(i).column(0).statistics
    by[int(s.min)].append(i)
route = json.loads(
    Path('/private/tmp/overview-japan-compact-reader/browser-results.json').read_text()
)
ranges = [
    (a, a + n)
    for sample in route['samples']
    if sample['variant'] == 'nested' and sample['run'] == 0
    for a, n in sample.get('ranges', [])
]
touched = set()
for i in range(f.num_row_groups):
    c = f.metadata.row_group(i).column(2)
    start = c.data_page_offset
    end = start + c.total_compressed_size
    if any(start < b and end > a for a, b in ranges):
        touched.add(i)
selected = []
for level, groups in by.items():
    chosen = set(
        groups[int(j)]
        for j in np.unique(np.linspace(0, len(groups) - 1, min(16, len(groups)), dtype=int))
    )
    chosen.update(touched.intersection(groups))
    selected.extend((level, i) for i in sorted(chosen))
variants = ['quantized', 'native_plain', 'native_bss', 'wkb_plain', 'wkb_delta']
tables = {v: [] for v in variants}
records = []
for level, rg in selected:
    t = f.read_row_group(rg).combine_chunks()
    a = t['geometry'].chunk(0)
    rings = a.values.values
    points = rings.values
    xy = np.column_stack([points.field('x').to_numpy(), points.field('y').to_numpy()])
    scale = np.array(meta['lods'][f'f{level}']['scale'])
    offset = np.array(meta['lods'][f'f{level}']['offset'])
    coords = xy * scale + offset
    floats = pa.StructArray.from_arrays(
        [pa.array(coords[:, 0]), pa.array(coords[:, 1])], names=['x', 'y']
    )
    inner = pa.ListArray.from_arrays(
        rings.offsets, floats, type=pa.list_(pa.field('element', floats.type))
    )
    middle = pa.ListArray.from_arrays(
        a.values.offsets, inner, type=pa.list_(pa.field('element', inner.type))
    )
    nested = pa.ListArray.from_arrays(
        a.offsets, middle, type=pa.list_(pa.field('element', middle.type))
    )
    shapes = shapely.from_ragged_array(
        shapely.GeometryType.MULTIPOLYGON,
        coords,
        (rings.offsets.to_numpy(), a.values.offsets.to_numpy(), a.offsets.to_numpy()),
    )
    blob = pa.array(shapely.to_wkb(shapes), type=pa.binary())
    assert np.array_equal(shapely.get_coordinates(shapely.from_wkb(blob.to_pylist())), coords)
    for v in variants:
        arr = a if v == 'quantized' else nested if v.startswith('native') else blob
        table = t.set_column(2, 'geometry', arr).replace_schema_metadata(None)
        tables[v].append(table)
    records.append(
        {
            'source_rg': rg,
            'route_touched': rg in touched,
            'level': level,
            'rows': len(t),
            'points': len(coords),
            'bbox': [
                float(coords[:, 0].min()),
                float(coords[:, 1].min()),
                float(coords[:, 0].max()),
                float(coords[:, 1].max()),
            ],
        }
    )
print('Samples prepared', len(records), flush=True)
results = {}
for v in variants:
    bbox = np.array([r['bbox'] for r in records])
    bbox = [
        float(bbox[:, 0].min()),
        float(bbox[:, 1].min()),
        float(bbox[:, 2].max()),
        float(bbox[:, 3].max()),
    ]
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {
            'geometry': {
                'encoding': 'multipolygon' if v.startswith('native') else 'WKB',
                'geometry_types': ['MultiPolygon'],
                'bbox': bbox,
            }
        },
    }
    md = (
        {b'geo': json.dumps(geo).encode()}
        if v != 'quantized'
        else {b'overview_experiment': json.dumps(meta).encode()}
    )
    schema = tables[v][0].schema.with_metadata(md)
    enc = {'source_row': 'DELTA_BINARY_PACKED'}
    if v == 'quantized':
        enc.update(
            {
                f'geometry.list.element.list.element.list.element.{axis}': 'DELTA_BINARY_PACKED'
                for axis in ['x', 'y']
            }
        )
    if v == 'native_bss':
        enc.update(
            {
                f'geometry.list.element.list.element.list.element.{axis}': 'BYTE_STREAM_SPLIT'
                for axis in ['x', 'y']
            }
        )
    if v == 'wkb_delta':
        enc['geometry'] = 'DELTA_LENGTH_BYTE_ARRAY'
    path = OUT / f'{v}.parquet'
    start = time.perf_counter()
    with pq.ParquetWriter(
        path,
        schema,
        compression='zstd',
        compression_level=9,
        use_dictionary=False,
        column_encoding=enc,
        write_statistics=True,
        data_page_size=2**30,
        max_rows_per_page=2**30,
        write_batch_size=1024,
    ) as w:
        for table in tables[v]:
            w.write_table(table.replace_schema_metadata(md), row_group_size=len(table))
    p = pq.ParquetFile(path)
    groups = []
    for i, r in enumerate(records):
        group = p.metadata.row_group(i)
        columns = {}
        for j in range(group.num_columns):
            c = group.column(j)
            columns[c.path_in_schema] = c.total_compressed_size
        assert p.read_row_group(i).equals(
            tables[v][i].replace_schema_metadata(md), check_metadata=True
        )
        groups.append(
            r
            | {
                'columns': columns,
                'geometry_bytes': sum(n for k, n in columns.items() if k.startswith('geometry')),
            }
        )
    results[v] = {
        'file_bytes': path.stat().st_size,
        'write_seconds': time.perf_counter() - start,
        'groups': groups,
        'metadata': geo if v != 'quantized' else None,
    }
    print(v, path.stat().st_size, flush=True)
c = duckdb.connect()
c.execute('LOAD spatial')
for v in variants:
    path = str(OUT / f'{v}.parquet')
    queries = {}
    for name, sql in {
        'describe': f"DESCRIBE SELECT * FROM read_parquet('{path}')",
        'auto_spatial': f"SELECT count(*),sum(ST_NPoints(geometry)),sum(ST_Area(geometry)),min(ST_XMin(geometry)),max(ST_XMax(geometry)) FROM read_parquet('{path}')",
        'explicit_wkb': f"SELECT sum(ST_NPoints(ST_GeomFromWKB(geometry))) FROM read_parquet('{path}')",
    }.items():
        try:
            queries[name] = {'result': c.execute(sql).fetchall()}
        except Exception as e:
            queries[name] = {'error': str(e)}
    results[v]['duckdb'] = queries
# Independent spatial reference: WKB round trips were already checked against
# transformed input coordinates above; now compare spatial SQL to Shapely.
shapes = shapely.from_wkb(pq.read_table(OUT / 'wkb_plain.parquet')['geometry'].to_pylist())
coords = shapely.get_coordinates(shapes)
spatial_reference = [
    len(shapes),
    int(shapely.get_num_coordinates(shapes).sum()),
    float(shapely.area(shapes).sum()),
    float(coords[:, 0].min()),
    float(coords[:, 0].max()),
]
for variant in ['wkb_plain', 'wkb_delta']:
    actual = results[variant]['duckdb']['auto_spatial']['result'][0]
    assert np.allclose(actual, spatial_reference, rtol=1e-12, atol=1e-12)
for variant in variants:
    for actual, expected in zip(
        results[variant]['groups'], results['quantized']['groups'], strict=True
    ):
        assert {k: v for k, v in actual['columns'].items() if not k.startswith('geometry')} == {
            k: v for k, v in expected['columns'].items() if not k.startswith('geometry')
        }
(OUT / 'results.json').write_text(
    json.dumps(
        {
            'duckdb_version': duckdb.__version__,
            'shapely_reference_spatial_result': spatial_reference,
            'wkb_duckdb_matches_shapely': True,
            'non_geometry_column_compressed_sizes_equal': True,
            'metadata_validation': 'Manual check against official GeoParquet1.1 JSON schema; automated jsonschema package unavailable',
            'source': str(SOURCE),
            'rows': sum(r['rows'] for r in records),
            'groups': len(records),
            'points': sum(r['points'] for r in records),
            'variants': results,
        },
        indent=2,
    )
)
print('Done', flush=True)
