"""COGP-like quantized nested int32 control: storage + fetched ranges only.

The physical shape is multipolygon/list/list/list/struct<x:int32,y:int32>.
Scale/offset are explicit experiment metadata, not standard GeoArrow CRS coordinates.
"""

import gzip
import json
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from geoparquet_lod.pack import Unpacker
from measure import Ranges, overlap, stats


def main():
    out = Path('/private/tmp/overview-alternatives')
    result_path = Path('benchmarks/overview-alternatives/results.json')
    result = json.loads(result_path.read_text())
    result['samples'] = [
        x for x in result['samples'] if x['variant'] not in ['nested', 'nested_delta']
    ]
    source = pq.read_table(out / 'reference.parquet')
    meta = json.loads(source.schema.metadata[b'geo'])['lod_columns']
    fields = [pa.field(x['name'], pa.type_for_alias(x['type'])) for x in meta['attributes']]
    dictionaries = {x['name']: x['values'] for x in meta['attributes'] if 'values' in x}
    coord = pa.struct([pa.field('x', pa.int32()), pa.field('y', pa.int32())])
    typ = pa.list_(pa.list_(pa.list_(coord)))
    for name in result['levels']:
        raw = []
        for blob in source['lod'].combine_chunks().field(name).to_pylist():
            own = Unpacker(blob, 'MultiPolygon', fields, dictionaries).geometry() if blob else []
            raw.append(
                [[[{'x': x, 'y': y} for x, y in ring] for ring in poly] for poly in own]
                if own
                else None
            )
        arr = pa.array(raw, type=typ)
        table = pa.table(
            {
                'source_row_id': pa.array(np.arange(len(source)), pa.int64()),
                'height': source['height'],
                'class': source['class'],
                'overview': arr,
            }
        )
        table = table.replace_schema_metadata(
            {
                b'overview_experiment': json.dumps(
                    {
                        'encoding': 'quantized_nested_multipolygon_int32',
                        'scale': meta['lods'][name]['scale'],
                        'offset': meta['lods'][name]['offset'],
                    }
                ).encode()
            }
        )
        directory = (out / f'{name}.directory.json.gz').read_bytes()
        spans = json.loads(gzip.decompress(directory))
        for variant in ['nested', 'nested_delta']:
            path = out / f'{name}.{variant}.parquet'
            with pq.ParquetWriter(
                path,
                table.schema,
                compression='zstd',
                compression_level=9,
                use_dictionary=False,
                write_statistics=False,
                data_page_size=2**30,
                column_encoding=(
                    {
                        f'overview.list.element.list.element.list.element.{axis}': 'DELTA_BINARY_PACKED'
                        for axis in ['x', 'y']
                    }
                    if variant == 'nested_delta'
                    else None
                ),
                max_rows_per_page=2**30,
                write_batch_size=128,
            ) as w:
                for start, end, _ in spans:
                    w.write_table(table.slice(start, end - start), row_group_size=end - start)
            # Full independent Arrow read verifies every null, coordinate and source attribute.
            decoded = pq.read_table(path)
            assert decoded['overview'].to_pylist() == raw
            assert decoded['height'].equals(source['height']) and decoded['class'].equals(
                source['class']
            )
            metadata = pq.read_metadata(path)
            components = {}
            for ci in range(metadata.num_columns):
                column = metadata.row_group(0).column(ci).path_in_schema
                components[column] = sum(
                    metadata.row_group(rg).column(ci).total_compressed_size
                    for rg in range(metadata.num_row_groups)
                )
            result['sizes'][f'{name}.{variant}'] = {
                'file_bytes': path.stat().st_size,
                'footer_bytes': metadata.serialized_size + 8,
                'directory_bytes': len(directory),
                'compressed_columns': components,
            }
            result['validations'][name]['nested_all_coordinates_and_attributes_equal'] = True
            c = duckdb.connect()
            count = c.execute(
                'SELECT count(overview),sum(len(overview)) FROM read_parquet(?)', [str(path)]
            ).fetchone()
            result['levels'][name]['duckdb_nested'] = {'rows': count[0], 'polygons': count[1]}
            c.close()
            scenarios = [
                x
                for x in result['samples']
                if x['lod'] == name and x['variant'] == 'wkb' and x['run'] == 0
            ]
            for scenario in scenarios:
                r = Ranges(path)
                r.seek(-8, 2)
                tail = r.read(8)
                flen = int.from_bytes(tail[:4], 'little')
                r.seek(-8 - flen, 2)
                r.read(flen)
                intervals = []
                cols = {'overview'} | ({'height', 'class'} if scenario['styled'] else set())
                for rg, (_, _, bbox) in enumerate(spans):
                    if bbox is None or not overlap(bbox, scenario['viewport']):
                        continue
                    for ci in range(metadata.num_columns):
                        chunk = metadata.row_group(rg).column(ci)
                        if chunk.path_in_schema.split('.')[0] in cols:
                            intervals.append([chunk.data_page_offset, chunk.total_compressed_size])
                ranges = []
                for offset, size in sorted(intervals):
                    if scenario['transport'] == 'adjacent' and ranges and sum(ranges[-1]) == offset:
                        ranges[-1][1] += size
                    else:
                        ranges.append([offset, size])
                for offset, size in ranges:
                    r.seek(offset)
                    r.read(size)
                cold = stats(r)
                cold['requests'] += 1
                cold['bytes'] += len(directory)
                result['samples'].append(
                    {
                        **{
                            k: v
                            for k, v in scenario.items()
                            if k not in ('cold', 'warm', 'ms', 'warm_ms', 'variant')
                        },
                        'variant': variant,
                        'cold': cold,
                        'measurement': 'actual compressed-range fetch only; no decode timer',
                    }
                )
                r.close()
            print(name, components, flush=True)
    result_path.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
