"""Whole geometry and LOD-specific attributes survive independent projection."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import validate
from geoparquet_lod._layout import finalize_layout
from geoparquet_lod._separate import separate
from geoparquet_lod.pack import Unpacker
from geoparquet_lod.writer import _write_unindexed as write

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('family', ['polygon', 'line', 'point'])
def test_separate(tmp_path, family):
    rng = np.random.default_rng(32)
    x, y = -105 + rng.random(400) * 0.4, 39.6 + rng.random(400) * 0.4
    if family == 'polygon':
        geometry = shapely.box(x, y, x + 0.035, y + 0.02)
        geometry[0] = shapely.box(-105.2, 39.5, -104.5, 40.1)
    elif family == 'line':
        geometry = shapely.linestrings(
            np.stack([np.column_stack([x, y]), np.column_stack([x + 0.04, y + 0.03])], axis=1)
        )
        geometry[0] = shapely.LineString([(-105.2, 39.5), (-104.5, 40.1)])
    else:
        geometry = shapely.points(x, y)
    geometry[1] = None
    table = pa.table(
        {
            'geometry': shapely.to_wkb(geometry),
            'name': [None if i % 5 == 0 else f'café {i}' for i in range(400)],
            'height': pa.array([None if i % 7 == 0 else i % 90 for i in range(400)], pa.int32()),
            'count': pa.array([4_000_000_000 + i for i in range(400)], pa.uint32()),
            'ratio': pa.array([i / 8 for i in range(400)], pa.float32()),
            'value': pa.array([i / 10 for i in range(400)], pa.float64()),
            'half': pa.array(np.arange(400, dtype=np.float16) / 8, pa.float16()),
            'active': [i % 3 == 0 for i in range(400)],
            'source_only': [f'original-{i}' for i in range(400)],
        }
    ).replace_schema_metadata(
        {
            b'geo': json.dumps(
                {
                    'version': '1.1.0',
                    'primary_column': 'geometry',
                    'columns': {
                        'geometry': {
                            'encoding': 'WKB',
                            'geometry_types': [
                                {'polygon': 'Polygon', 'line': 'LineString', 'point': 'Point'}[
                                    family
                                ]
                            ],
                        }
                    },
                }
            ).encode()
        }
    )
    src, packed, split, a, b = [
        tmp_path / f'{name}.parquet' for name in ['source', 'packed', 'split', 'a', 'b']
    ]
    pq.write_table(table, src)

    def override(rows, zoom, ctx):
        return rows.set_column(
            rows.schema.get_field_index('height'),
            'height',
            pa.array([None if i % 2 else 999 for i in range(len(rows))], pa.int32()),
        )

    carry = ['height', 'name', 'count', 'ratio', 'value', 'active', 'half']
    write(
        src,
        packed,
        zooms=range(9, 13),
        complete_from=12,
        prefix_zoom=9 if family == 'point' else None,
        carry=carry,
        thinning=override,
        row_group_rows=128,
        workers=1,
        log=lambda *_: None,
    )
    separate(packed, split, log=lambda *_: None)
    finalize_layout(packed, a, page_bytes=1024, log=lambda *_: None)
    finalize_layout(split, b, page_bytes=1024, log=lambda *_: None)
    validate(b)
    old, new = pq.read_table(packed), pq.read_table(b)
    meta = json.loads(old.schema.metadata[b'geo'])['lod_columns']
    newmeta = json.loads(new.schema.metadata[b'geo'])['lod_columns']
    fields = [pa.field(f['name'], pa.type_for_alias(f['type'])) for f in meta['attributes']]
    dictionaries = {f['name']: f['values'] for f in meta['attributes'] if 'values' in f}
    for col in old.column_names:
        if col not in ('lod', 'bbox'):
            assert old[col].equals(new[col]), col
    copies = 0
    for name, info in meta['lods'].items():
        oldblobs = old['lod'].combine_chunks().field(name).to_pylist()
        newblobs = new['lod'].combine_chunks().field(name).to_pylist()
        values = {
            attr: new[column].to_pylist()
            for attr, column in newmeta['attribute_columns'][name].items()
        }
        for row, (blob, other) in enumerate(zip(oldblobs, newblobs, strict=True)):
            if blob is None:
                assert other is None
                assert all(value[row] is None for value in values.values())
                continue
            before = Unpacker(blob, info['geometry_type'], fields, dictionaries).row()
            after = Unpacker(other, info['geometry_type'], []).row()
            assert before['geometry'] == after['geometry']
            assert len(before['spill']) == len(after['spill'])
            copies += len(before['spill'])
            for first, last in zip(before['spill'], after['spill'], strict=True):
                assert (first['tile'], first['id'], first['geometry']) == (
                    last['tile'],
                    last['id'],
                    last['geometry'],
                )
            for attr, value in values.items():
                assert value[row] == [
                    before['attrs'][attr],
                    *[copy['attrs'][attr] for copy in before['spill']],
                ]
    if family != 'point':
        assert copies > 0
    # Exercise source projection across dictionary and v2 pages, with page
    # boundaries unrelated to selected IDs. Keep LOD metadata and row order.
    projected = tmp_path / 'projection.parquet'
    pq.write_table(
        old,
        projected,
        compression='zstd',
        use_dictionary=['name', 'source_only'],
        data_page_version='2.0',
        max_rows_per_page=11,
        row_group_size=128,
        write_page_index=True,
    )
    selected = [399, 0, 37, 37, 128, 17]
    columns = ['name', 'height', 'half', 'count', 'source_only', 'value', 'active']
    expected = {name: [old[name][i].as_py() for i in selected] for name in columns}
    expected_path = tmp_path / 'projection.json'
    expected_path.write_text(json.dumps({'rows': selected, 'expected': expected}))
    subprocess.run(
        [
            'node',
            'js/test/separate.test.mjs',
            str(a),
            str(b),
            str(split),
            str(projected),
            str(expected_path),
        ],
        cwd=ROOT,
        check=True,
    )
