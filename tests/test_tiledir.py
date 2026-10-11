"""Experimental directory membership and cross-language page decoding."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from geoparquet_lod.encoding import lod_type
from geoparquet_lod.tiledir import BUFFER, Compact, build, morton, tile_span


def test_tile_span_boundaries():
    assert tile_span([0, 0, 0, 0], 2, [0, 0]) == (1, 1, 2, 2)
    edge = -BUFFER * 360 / 4
    assert tile_span([edge, 1, edge, 1], 2, [0, 0])[2] == 2
    assert tile_span([edge - 0.01, 1, edge - 0.01, 1], 2, [0.02, 0.02])[2] == 2
    assert tile_span([-10, -10, 10, 10], 4, [0, 0]) == (7, 7, 8, 8)
    bbox32 = np.array([-104.996, 39.74, -104.995, 39.741], dtype=np.float32)
    assert tile_span(bbox32, 16, [2e-6, 2e-6]) == tile_span(bbox32.tolist(), 16, [2e-6, 2e-6])
    assert morton(3, 2, 2) == 13
    with pytest.raises(ValueError, match='truncated'):
        Compact(b'\x15').value(12)


@pytest.mark.parametrize('family', ['Point', 'MultiPolygon'])
def test_directory_reader(tmp_path, family):
    attributes = [pa.field('label', pa.string()), pa.field('value', pa.int32())]
    scale = 0.0001
    rows, bounds = [], []
    edge = -BUFFER * 360 / 64
    for i in range(320):
        x = [0, -0.0001, 0.0001, edge, edge - scale, edge + scale, 5.625, 11.25][i % 8]
        y = (i % 11 - 5) * 0.03
        qx, qy = round(x / scale), round(y / scale)
        if family == 'Point':
            geometry = [qx, qy]
            bound = [x, y, x, y]
        else:
            size = 100000 if i % 13 == 0 else 100
            geometry = [
                [[[qx, qy], [qx + size, qy], [qx + size, qy + 100], [qx, qy + 100], [qx, qy]]]
            ]
            bound = [x, y, x + size * scale, y + 100 * scale]
        rows.append(
            None
            if i % 9 == 0
            else dict(geometry=geometry, label=f'kind{i % 3}', value=None if i % 5 == 0 else i)
        )
        bounds.append(dict(zip(('xmin', 'ymin', 'xmax', 'ymax'), bound, strict=True)))
    lod = dict(zooms=[4, 5, 6], geometry_type=family, scale=[scale, scale], offset=[0, 0])
    geo = dict(
        version='1.1.0',
        primary_column='geometry',
        columns={'geometry': dict(encoding='WKB', geometry_types=[])},
        lod_columns=dict(
            version='0.1.0',
            column='lod',
            order='hilbert',
            zooms=[4, 5, 6],
            tile_size=512,
            prefix=None,
            encoding='quantized_geoarrow',
            coordinates='interleaved',
            attributes=[dict(name=f.name, type=str(f.type)) for f in attributes],
            thinning={},
            lods={'f4': lod},
        ),
    )
    table = pa.table(
        dict(
            bbox=pa.array(bounds, pa.struct([pa.field(k, pa.float32()) for k in bounds[0]])),
            lod=pa.StructArray.from_arrays(
                [pa.array(rows, lod_type(family, attributes))], names=['f4']
            ),
        )
    ).replace_schema_metadata({b'geo': json.dumps(geo).encode()})
    table = table.cast(
        pa.schema(
            [f.with_nullable(False) if f.name == 'lod' else f for f in table.schema],
            metadata=table.schema.metadata,
        )
    )
    path = tmp_path / 'fixture.parquet'
    pq.write_table(
        table,
        path,
        row_group_size=64,
        data_page_size=16384,
        write_batch_size=64,
        write_page_index=True,
        compression='zstd',
        use_dictionary=True,
    )
    info = build(path)
    assert info['lods'][0]['entries'] > 1
    assert info['root_bytes'] > 0
    assert info['size'] > info['open_bytes']
    with pytest.raises(ValueError, match='differ'):
        build(path, path)
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ['node', str(root / 'js/test/tiledir.test.mjs'), str(path)], check=True, cwd=root
    )
