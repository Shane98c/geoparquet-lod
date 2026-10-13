"""Order-independent whole-row checksums of every source analytical column.

Includes source IDs inside each row hash, so values cannot be reassigned between
features unnoticed. This is a multiset checksum, not an exact equality proof.
"""

import json
import sys
import time

import duckdb
import pyarrow.parquet as pq

if __name__ == '__main__':
    columns = [
        name
        for name in pq.ParquetFile(sys.argv[1]).schema_arrow.names
        if name not in ('bbox', 'hilbert', 'lod')
    ]
    fields = ', '.join('"' + name.replace('"', '""') + '"' for name in columns)
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute('SET threads=4')
    results = []
    for path in sys.argv[1:]:
        started = time.monotonic()
        count, xor, total = con.execute(
            f'SELECT count(*), bit_xor(h), sum(h::HUGEINT) FROM (SELECT hash({fields}) h FROM read_parquet(?))',
            [path],
        ).fetchone()
        results.append(
            {
                'path': path,
                'rows': count,
                'xor': xor,
                'sum': total,
                'seconds': time.monotonic() - started,
            }
        )
    print(json.dumps({'columns': columns, 'results': results}, indent=2), flush=True)
    assert all(
        (r['rows'], r['xor'], r['sum'])
        == (results[0]['rows'], results[0]['xor'], results[0]['sum'])
        for r in results
    )
