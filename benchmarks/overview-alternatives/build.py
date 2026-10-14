"""Build a bounded, deterministic Tokyo source and packed semantic reference."""

import argparse
import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
from geoparquet_lod import drop_rate, write


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', default='/private/tmp/lod-separate/source.parquet')
    p.add_argument('--out', default='/private/tmp/overview-alternatives')
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    c = duckdb.connect()
    c.execute('LOAD spatial')
    c.execute('SET threads=4')
    t = c.execute(
        """SELECT id, height, class, ST_AsWKB(geometry) AS geometry
    FROM read_parquet(?) WHERE
    (ST_XMin(geometry)<=139.77 AND ST_XMax(geometry)>=139.72
     AND ST_YMin(geometry)<=35.69 AND ST_YMax(geometry)>=35.64) OR
    (ST_XMin(geometry)<=139.52 AND ST_XMax(geometry)>=139.47
     AND ST_YMin(geometry)<=35.73 AND ST_YMax(geometry)>=35.68)""",
        [a.source],
    ).to_arrow_table()
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon', 'MultiPolygon']}},
    }
    t = t.replace_schema_metadata({b'geo': json.dumps(geo).encode()})
    pq.write_table(t, out / 'source.parquet', compression='zstd')
    write(
        out / 'source.parquet',
        out / 'reference.parquet',
        zooms=range(8, 17),
        complete_from=16,
        thinning=drop_rate(2),
        carry=['height', 'class'],
        workers=2,
        row_group_rows=262144,
    )
    print(
        json.dumps({'rows': len(t), 'reference_bytes': (out / 'reference.parquet').stat().st_size})
    )


if __name__ == '__main__':
    main()
