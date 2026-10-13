"""Compare all table values of a writer output and its optimized copy."""

import sys
import time

import numpy as np
import pyarrow.parquet as pq

if __name__ == '__main__':
    a, b = [pq.ParquetFile(path) for path in sys.argv[1:3]]
    started = time.monotonic()
    assert a.metadata.num_row_groups == b.metadata.num_row_groups
    assert a.schema_arrow.equals(b.schema_arrow, check_metadata=True)
    rows = 0
    for group in range(a.metadata.num_row_groups):
        x, y = a.read_row_group(group), b.read_row_group(group)
        assert x.drop(['bbox']).equals(y.drop(['bbox'])), group
        for coordinate in ['xmin', 'ymin', 'xmax', 'ymax']:
            np.testing.assert_array_equal(
                x['bbox'].combine_chunks().field(coordinate).to_numpy(),
                y['bbox'].combine_chunks().field(coordinate).to_numpy(),
            )
        rows += x.num_rows
    print(f'All {rows:,} rows, every column identical; {time.monotonic() - started:.1f}s')
