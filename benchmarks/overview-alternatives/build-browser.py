"""Build matched complete browser files with embedded compact experimental index."""

import base64
import gzip
import io
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from geoparquet_lod._compact import dumps, get, loads, set_value
from geoparquet_lod.pack import Unpacker

OUT = Path('/private/tmp/overview-alternatives')
BOOT = 16384


def main():
    source = pq.read_table(OUT / 'reference.parquet')
    lod = json.loads(source.schema.metadata[b'geo'])['lod_columns']
    fields = [pa.field(x['name'], pa.type_for_alias(x['type'])) for x in lod['attributes']]
    dictionaries = {x['name']: x['values'] for x in lod['attributes'] if 'values' in x}
    spans = json.loads(gzip.decompress((OUT / 'f16.directory.json.gz').read_bytes()))
    levels = ['f8', 'f11', 'f14', 'f16']
    (OUT / 'source-attributes.json').write_text(
        json.dumps(
            {name: source[name].to_pylist() for name in ['height', 'class']}, separators=(',', ':')
        )
    )
    bounds = {}
    for level in levels:
        bb = np.full((len(source), 4), np.nan)
        transform = lod['lods'][level]
        for rid, blob in enumerate(source['lod'].combine_chunks().field(level).to_pylist()):
            if not blob:
                continue
            geom = Unpacker(blob, 'MultiPolygon', fields, dictionaries).geometry()
            if not geom:
                continue
            points = np.array([p for poly in geom for ring in poly for p in ring])
            lo = points.min(axis=0) * transform['scale'] + transform['offset']
            hi = points.max(axis=0) * transform['scale'] + transform['offset']
            bb[rid] = [*lo, *hi]
        bounds[level] = bb
    for variant in ['packed', 'nested']:
        columns = {n: source[n] for n in ['id', 'geometry', 'height', 'class']}
        for level in levels:
            filename = f'{level}.{"packed" if variant == "packed" else "nested_delta"}.parquet'
            columns[level] = pq.read_table(OUT / filename, columns=['overview'])['overview']
        table = pa.table(columns)
        geo = {
            'version': '1.1.0',
            'primary_column': 'geometry',
            'columns': {
                'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon', 'MultiPolygon']}
            },
        }
        table = table.replace_schema_metadata(
            {
                b'geo': json.dumps(geo).encode(),
                b'overview_experiment': json.dumps(
                    {
                        'version': 1,
                        'encoding': 'packed'
                        if variant == 'packed'
                        else 'quantized_nested_int32_delta',
                        'lods': {level: lod['lods'][level] for level in levels},
                        'source_attributes': ['height', 'class'],
                    }
                ).encode(),
            }
        )
        raw = OUT / f'browser.{variant}.raw.parquet'
        encoding = (
            {
                f'{level}.list.element.list.element.list.element.{axis}': 'DELTA_BINARY_PACKED'
                for level in levels
                for axis in ['x', 'y']
            }
            if variant == 'nested'
            else None
        )
        with pq.ParquetWriter(
            raw,
            table.schema,
            compression='zstd',
            compression_level=9,
            use_dictionary=False,
            write_statistics=False,
            write_page_index=False,
            data_page_size=2**30,
            max_rows_per_page=2**30,
            column_encoding=encoding,
        ) as writer:
            for first, last, _ in spans:
                writer.write_table(table.slice(first, last - first), row_group_size=last - first)
        data = raw.read_bytes()
        flen = int.from_bytes(data[-8:-4], 'little')
        metadata = loads(data[-8 - flen : -8])
        groups = get(metadata, 4)[1]
        pages = []
        for rg, group in enumerate(groups):
            first, last, _ = spans[rg]
            leaves = {}
            for chunk in get(group, 1)[1]:
                cm = get(chunk, 3)
                path = '.'.join(p.decode() for p in get(cm, 3)[1])
                start, size = get(cm, 9), get(cm, 7)
                leaves[path] = [start + BOOT, size]
                set_value(cm, 9, start + BOOT)
                if 11 in cm:
                    set_value(cm, 11, get(cm, 11) + BOOT)
            envelopes = {}
            for level in levels:
                bb = bounds[level][first:last]
                bb = bb[np.isfinite(bb[:, 0])]
                envelopes[level] = (
                    [
                        float(bb[:, 0].min()),
                        float(bb[:, 1].min()),
                        float(bb[:, 2].max()),
                        float(bb[:, 3].max()),
                    ]
                    if len(bb)
                    else None
                )
            pages.append([first, last, envelopes, leaves])
        directory = gzip.compress(json.dumps(pages, separators=(',', ':')).encode(), mtime=0)
        # Empty standard Parquet supplies a small schema-only footer to the JS decoder.
        schema_file = io.BytesIO()
        pq.write_table(table.slice(0, 0), schema_file)
        schema_bytes = schema_file.getvalue()
        root = {
            'version': 1,
            'variant': variant,
            'rows': len(source),
            'directory': [len(data) - 8 - flen + BOOT, len(directory)],
            'schema': base64.b64encode(schema_bytes).decode(),
            'lods': {level: lod['lods'][level] for level in levels},
            'attributes': lod['attributes'],
            'page_rows': 2048,
        }
        root_bytes = gzip.compress(json.dumps(root, separators=(',', ':')).encode(), mtime=0)
        assert len(root_bytes) + 12 < BOOT
        boot = b'NESTIDX1' + len(root_bytes).to_bytes(4, 'little') + root_bytes
        final_footer = dumps(metadata)
        target = OUT / f'browser.{variant}.parquet'
        target.write_bytes(
            data[:4]
            + boot.ljust(BOOT, b'\0')
            + data[4 : -8 - flen]
            + directory
            + final_footer
            + len(final_footer).to_bytes(4, 'little')
            + b'PAR1'
        )
        readback = pq.read_table(target)
        assert table.equals(readback, check_metadata=True)
        print(
            json.dumps(
                {
                    'variant': variant,
                    'file_bytes': target.stat().st_size,
                    'bootstrap_bytes': BOOT,
                    'directory_bytes': len(directory),
                    'footer_bytes': len(final_footer) + 8,
                    'groups': len(pages),
                    'all_source_and_overview_columns_equal': True,
                }
            ),
            flush=True,
        )


if __name__ == '__main__':
    main()
