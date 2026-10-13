"""Report compressed Parquet column sizes, excluding page indexes and footer.

python benchmarks/column-sizes.py file.parquet [other.parquet ...]
"""

import json
import sys
from pathlib import Path

import pyarrow.parquet as pq


def sizes(path):
    metadata = pq.ParquetFile(path).metadata
    columns = {}
    for group in range(metadata.num_row_groups):
        row_group = metadata.row_group(group)
        for index in range(row_group.num_columns):
            column = row_group.column(index)
            name = column.path_in_schema
            columns[name] = columns.get(name, 0) + column.total_compressed_size
    total = Path(path).stat().st_size
    lod = json.loads(metadata.metadata[b'geo']).get('lod_columns', {})
    return {
        'file': Path(path).name,
        'bytes': total,
        'rows': metadata.num_rows,
        'row_groups': metadata.num_row_groups,
        'compressed_columns': columns,
        'indexes_footer_other': total - sum(columns.values()),
        'lods': lod.get('lods', {}),
    }


if __name__ == '__main__':
    print(json.dumps([sizes(path) for path in sys.argv[1:]], indent=2))
