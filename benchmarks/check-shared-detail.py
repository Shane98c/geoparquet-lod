"""Check unchanged analytical values, row IDs and coarse LODs after sharing detail.

python benchmarks/check-shared-detail.py baseline.parquet shared.parquet
"""

import json
import sys
import time

import numpy as np
import pyarrow.parquet as pq

if __name__ == '__main__':
    before, after = [pq.ParquetFile(path) for path in sys.argv[1:3]]
    start = time.monotonic()
    assert before.metadata.num_row_groups == after.metadata.num_row_groups
    metadata = [json.loads(f.metadata.metadata[b'geo'])['lod_columns'] for f in (before, after)]
    assert metadata[0]['prefix'] is None and metadata[1]['prefix'] is None
    assert metadata[0]['attributes'] == metadata[1]['attributes']
    assert metadata[0]['lods']['f16']['scale'] == metadata[1]['lods']['f14']['scale']
    assert metadata[0]['lods']['f16']['offset'] == metadata[1]['lods']['f14']['offset']
    assert metadata[1]['lods']['f14']['zooms'] == [14, 15, 16]
    assert 'f16' not in metadata[1]['lods']
    coarse = [name for name, lod in metadata[0]['lods'].items() if max(lod['zooms']) < 14]
    for name in coarse:
        assert metadata[0]['lods'][name] == metadata[1]['lods'][name]
    rows = 0
    for group in range(before.metadata.num_row_groups):
        a, b = [f.read_row_group(group) for f in (before, after)]
        # Includes original geometry, source attributes, and the Hilbert key.
        # Equality in the same row positions also verifies map row-ID stability.
        assert a.drop(['lod', 'bbox']).equals(b.drop(['lod', 'bbox'])), group
        for coordinate in ['xmin', 'ymin', 'xmax', 'ymax']:
            np.testing.assert_array_equal(
                a['bbox'].combine_chunks().field(coordinate).to_numpy(),
                b['bbox'].combine_chunks().field(coordinate).to_numpy(),
            )
        for name in coarse:
            assert (
                a['lod'].combine_chunks().field(name).equals(b['lod'].combine_chunks().field(name))
            ), (group, name)
        rows += a.num_rows
        print(f'Validated {rows:,} rows', file=sys.stderr, flush=True)
    print(
        json.dumps(
            {
                'rows': rows,
                'analytical_values_equal': True,
                'row_positions_equal': True,
                'unchanged_lods': coarse,
                'shared_zooms': [14, 15, 16],
                'seconds': time.monotonic() - start,
            },
            indent=2,
        )
    )
