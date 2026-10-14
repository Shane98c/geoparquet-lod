"""Small standard Parquet fixtures for the specialized compact reader."""

import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
geometry = pa.list_(pa.list_(pa.list_(pa.struct([('x', pa.int32()), ('y', pa.int32())]))))
schema = pa.schema(
    [
        ('source_row', pa.int64()),
        ('geometry', geometry),
        ('height', pa.float64()),
        ('class', pa.string()),
    ]
)
ring = [{'x': -100, 'y': 200}, {'x': 50, 'y': 220}, {'x': 80, 'y': 300}, {'x': -100, 'y': 200}]
# Long rings force page boundaries within one feature; holes and multiple
# polygons exercise all three repetition levels.
shapes = [[[ring, ring], [ring]], [[ring * 600]], [[ring]]]
for name, version, dictionary, nullable in [
    ('delta-v1', '1.0', False, False),
    ('delta-v2', '2.0', False, False),
    ('dictionary', '1.0', True, False),
    ('empty-null', '1.0', False, True),
]:
    rows = shapes + ([None, [], [[[]]], [[ring]]] if nullable else [])
    table = pa.Table.from_arrays(
        [
            pa.array(list(range(len(rows))), pa.int64()),
            pa.array(rows, geometry),
            pa.array([None if i % 2 else 12.5 for i in range(len(rows))]),
            pa.array(['building' if i % 2 else None for i in range(len(rows))]),
        ],
        schema=schema,
    )
    pq.write_table(
        table,
        out / f'{name}.parquet',
        compression='zstd',
        use_dictionary=dictionary,
        data_page_version=version,
        column_encoding=None
        if dictionary
        else {
            'geometry.list.element.list.element.list.element.x': 'DELTA_BINARY_PACKED',
            'geometry.list.element.list.element.list.element.y': 'DELTA_BINARY_PACKED',
        },
        data_page_size=128,
        write_batch_size=32,
    )
print('Wrote four coordinate fixtures')
