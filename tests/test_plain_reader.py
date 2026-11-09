"""The JS reader draws a GeoParquet file without LODs from its primary geometry
at every zoom, whether its pages are indexed or not, whether or not it records
column statistics, and whatever its covering column is named or, as in GeoParquet
2.0, a `bbox` column it does not declare."""

import json
import math
import subprocess
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely

ROOT = Path(__file__).resolve().parents[1]
ZOOMS = [0, 4, 9, 14]
# GDAL names the covering after the geometry column; DuckDB's GeoParquet 2.0 declares none.
UNINDEXED = {'unindexed', 'gdal_covering', 'geoparquet_2'}


def table(covering='bbox', declared=True):
    rng = np.random.default_rng(7)
    near = np.column_stack([-105 + rng.random(1500) * 0.5, 39.6 + rng.random(1500) * 0.3])
    far = np.column_stack([-180 + rng.random(500) * 360, -60 + rng.random(500) * 120])
    points = shapely.points(np.concatenate([near, far]))
    xmin, ymin, xmax, ymax = shapely.bounds(points).T
    column = {'encoding': 'WKB', 'geometry_types': ['Point']}
    if declared:
        column['covering'] = {'bbox': {k: [covering, k] for k in ('xmin', 'ymin', 'xmax', 'ymax')}}
    geo = {
        'version': '1.1.0' if declared else '2.0.0',
        'primary_column': 'geometry',
        'columns': {'geometry': column},
    }
    return pa.table(
        {
            'height': pa.array(np.arange(len(points)) % 50, pa.int32()),
            'name': pa.array([f'b{i}' for i in range(len(points))]),
            covering: pa.StructArray.from_arrays(
                [xmin, ymin, xmax, ymax], ['xmin', 'ymin', 'xmax', 'ymax']
            ),
            'geometry': shapely.to_wkb(points),
        }
    ).replace_schema_metadata({b'geo': json.dumps(geo).encode()})


def write(t, path, layout):
    if layout in UNINDEXED:
        # DuckDB writes one row group and no page index.
        con = duckdb.connect()
        con.register('t', t)
        geo = t.schema.metadata[b'geo'].decode()
        con.execute(f"COPY t TO '{path}' (FORMAT parquet, KV_METADATA {{geo: $geo}})", {'geo': geo})
    else:
        # Without statistics there is no column index to prune by, only page locations.
        pq.write_table(
            t,
            path,
            row_group_size=300,
            data_page_size=1024,
            write_page_index=True,
            write_statistics=layout == 'indexed',
        )
    column = pq.ParquetFile(path).metadata.row_group(0).column(0)
    assert column.has_offset_index == (layout not in UNINDEXED)
    assert column.has_column_index == (layout == 'indexed')


def tile(x, y, z):
    n = 2**z
    phi = math.radians(y)
    return n * (x + 180) / 360, n * (1 - math.log(math.tan(phi) + 1 / math.cos(phi)) / math.pi) / 2


@pytest.mark.parametrize(
    'layout', ['unindexed', 'indexed', 'without_statistics', 'gdal_covering', 'geoparquet_2']
)
def test_every_zoom_draws_primary_geometry_of_a_file_without_lods(tmp_path, layout):
    covering = 'geometry_bbox' if layout == 'gdal_covering' else 'bbox'
    t = table(covering, declared=layout != 'geoparquet_2')
    path = tmp_path / 'plain.parquet'
    write(t, path, layout)
    bbox = t[covering].to_pylist()
    cases = []
    for row in range(0, t.num_rows, 7):
        b = bbox[row]
        scaled = [tile(b['xmin'], b['ymin'], z) for z in ZOOMS]
        # a point well inside its tile at every zoom
        if any(abs(v % 1 - 0.5) > 0.45 for xy in scaled for v in xy):
            continue
        cases.append(
            {
                'row': row,
                'bbox': [b['xmin'], b['ymin'], b['xmax'], b['ymax']],
                'tiles': [[z, int(x), int(y)] for z, (x, y) in zip(ZOOMS, scaled, strict=True)],
                'properties': {'height': row % 50, 'name': f'b{row}'},
            }
        )
        if len(cases) == 25:
            break
    assert len(cases) == 25
    (tmp_path / 'cases.json').write_text(json.dumps({'rows': t.num_rows, 'cases': cases}))
    result = subprocess.run(
        [
            'node',
            str(ROOT / 'js' / 'test' / 'plain.test.mjs'),
            str(path),
            str(tmp_path / 'cases.json'),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert 'plain GeoParquet: 25 features at every zoom' in result.stdout
