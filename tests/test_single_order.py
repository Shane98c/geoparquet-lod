"""Changing row layout must remap boundary copies to the right analytical row."""

import json

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import shapely
from geoparquet_lod import drop_rate, write
from geoparquet_lod.pack import Unpacker
from geoparquet_lod.writer import _write_unindexed


def test_prefix_and_single_order_keep_feature_identity(tmp_path):
    # Repeated centroids force Hilbert-key ties; names distinguish source rows.
    n = 1800
    geometry = [
        shapely.box(-105 + (i % 30) * 0.002, 39.7, -104.996 + (i % 30) * 0.002, 39.704)
        for i in range(n)
    ]
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    source = pa.table(
        {'name': [f'feature-{i}' for i in range(n)], 'geometry': shapely.to_wkb(geometry)}
    )
    src = tmp_path / 'source.parquet'
    pq.write_table(source.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    layouts = []
    for prefix in (11, None):
        out = tmp_path / f'{prefix}.parquet'
        build = write if prefix is None else _write_unindexed
        options = {} if prefix is None else {'prefix_zoom': prefix}
        build(
            src,
            out,
            zooms=range(10, 15),
            complete_from=13,
            **options,
            thinning=drop_rate(2.5),
            carry=['name'],
            row_group_rows=256,
            workers=1,
            log=lambda *_: None,
        )
        table = pq.read_table(out)
        analytical = table.select(source.column_names)
        assert analytical.take(
            pc.sort_indices(analytical, sort_keys=[('name', 'ascending')])
        ).equals(source.take(pc.sort_indices(source, sort_keys=[('name', 'ascending')])))
        meta = json.loads(table.schema.metadata[b'geo'])['lod_columns']
        names = table['name'].to_pylist()
        decoded = []
        for lod, info in meta['lods'].items():
            for name, blob in zip(
                names, table['lod'].combine_chunks().field(lod).to_pylist(), strict=True
            ):
                if blob is None:
                    continue
                row = Unpacker(blob, info['geometry_type'], [pa.field('name', pa.string())]).row()
                if row['geometry']:
                    decoded.append((lod, name, None, row['geometry']))
                for copy in row['spill']:
                    assert names[copy['id']] == copy['attrs']['name'], (
                        'copy id points at its analytical source'
                    )
                    decoded.append((lod, names[copy['id']], copy['tile'], copy['geometry']))
        layouts.append(sorted(json.dumps(value) for value in decoded))
    assert layouts[0] == layouts[1], 'layout changes neither selection nor boundary copies'
