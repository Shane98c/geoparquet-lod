"""Reproducible Tokyo ablation from the Japan primary GeoParquet table.

Usage: .venv/bin/python benchmarks/build-separate.py JAPAN.parquet OUTDIR
The filter uses its bbox covering; all primary columns remain in the subset.
"""

import json
import sys
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
from geoparquet_lod import chain, drop_smallest, tiny_polygons
from geoparquet_lod._layout import finalize_layout
from geoparquet_lod._separate import separate
from geoparquet_lod.writer import _write_unindexed as write


def main():
    source, directory = sys.argv[1:]
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute('SET threads=4')
    schema = pq.read_schema(source)
    geo = json.loads(schema.metadata[b'geo'])
    geo.pop('lod_columns', None)
    geo['columns'][geo['primary_column']].pop('covering', None)
    columns = [name for name in schema.names if name not in ('lod', 'hilbert', 'bbox')]
    projection = ', '.join('"' + name.replace('"', '""') + '"' for name in columns)
    table = con.execute(
        f"""SELECT {projection} FROM read_parquet(?)
        WHERE bbox.xmax >= 139.39 AND bbox.xmin <= 140.10
        AND bbox.ymax >= 35.45 AND bbox.ymin <= 35.90""",
        [source],
    ).to_arrow_table()
    table = table.replace_schema_metadata({b'geo': json.dumps(geo).encode()})
    pq.write_table(table, out / 'source.parquet', compression='zstd')
    print(f'{len(table):,} source rows', flush=True)
    del table
    con.close()
    write(
        out / 'source.parquet',
        out / 'packed.raw.parquet',
        zooms=range(8, 15),
        prefix_zoom=None,
        complete_from=14,
        carry=['height', 'class'],
        thinning=chain(drop_smallest(lambda z: 1_300_000 if z <= 8 else 850_000), tiny_polygons()),
        row_group_rows=262144,
        workers=4,
    )
    finalize_layout(out / 'packed.raw.parquet', out / 'packed.parquet')
    separate(out / 'packed.raw.parquet', out / 'separate.parquet')
    print(
        json.dumps(
            {
                'rows': pq.read_metadata(out / 'source.parquet').num_rows,
                'bytes': {
                    name: (out / f'{name}.parquet').stat().st_size
                    for name in ['source', 'packed', 'separate']
                },
            },
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
