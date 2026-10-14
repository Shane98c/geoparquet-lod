"""Compact ordinary overview rows; leaves the Japan source and other experiments intact."""

import argparse
import base64
import gzip
import hashlib
import importlib.util
import json
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from geoparquet_lod._compact import dumps, get, set_value
from overview_native import NativeOwn, compile_native

spec = importlib.util.spec_from_file_location(
    'grouped', Path(__file__).with_name('build-japan-grouped.py')
)
grouped = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grouped)


def build(source, directory, group_rows):
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / 'japan.compact.parquet'
    assert not output.exists(), 'Choose another directory; completed experiments are immutable'
    started = time.perf_counter()
    parquet = pq.ParquetFile(source)
    lod = json.loads(parquet.schema_arrow.metadata[b'geo'])['lod_columns']
    source_footer = grouped.compact_footer(source)[1]
    provenance = {
        'source': str(source),
        'source_bytes': source.stat().st_size,
        'source_footer_sha256': hashlib.sha256(source_footer).hexdigest(),
        'source_rows': parquet.metadata.num_rows,
        'levels': grouped.LEVELS,
        'group_rows': group_rows,
        'geometry': 'whole quantized nested int32, same selection/coordinates as production own geometry',
        'attributes': ['height', 'class'],
        'source_link': 'zero-based source_row in immutable source',
        'packaging': 'ordinary companion Parquet, original source untouched',
    }
    schema = pa.schema(
        [
            pa.field('level', pa.int32()),
            pa.field('source_row', pa.int64()),
            pa.field('geometry', grouped.NESTED),
            parquet.schema_arrow.field('height'),
            parquet.schema_arrow.field('class'),
        ],
        metadata={b'overview_experiment': json.dumps({**provenance, 'lods': lod['lods']}).encode()},
    )
    plain = directory / 'compact-stage.parquet'
    descriptions, validation, first = [], [], 0
    decoder = NativeOwn(compile_native(directory))
    fields = [pa.field(a['name'], pa.type_for_alias(a['type'])) for a in lod['attributes']]
    dictionaries = {a['name']: a['values'] for a in lod['attributes'] if 'values' in a}
    encoding = {
        f'geometry.list.element.list.element.list.element.{axis}': 'DELTA_BINARY_PACKED'
        for axis in ['x', 'y']
    }
    encoding['source_row'] = 'DELTA_BINARY_PACKED'
    # Each small row group has adjacent source ID, coordinate and attribute leaves.
    # The level column and all values remain directly readable by ordinary Parquet tools.
    with pq.ParquetWriter(
        plain,
        schema,
        compression='zstd',
        compression_level=9,
        use_dictionary=False,
        column_encoding=encoding,
        write_statistics=True,
        data_page_size=2**30,
        max_rows_per_page=2**30,
        write_batch_size=1024,
    ) as writer:
        for level in grouped.LEVELS:
            tables, boxes, buffered, source_first, selected_total = [], [], 0, 0, 0
            target_rows = 8192 if level == 'f4' else group_rows
            scale, offset = (
                np.array(lod['lods'][level]['scale']),
                np.array(lod['lods'][level]['offset']),
            )

            def emit(count, level=level, scale=scale, offset=offset):
                nonlocal tables, boxes, buffered, first
                table = pa.concat_tables(tables)
                bounds = np.concatenate(boxes)
                take, b = table.slice(0, count), bounds[:count]
                box = [
                    float(b[:, 0].min() * scale[0] + offset[0]),
                    float(b[:, 1].min() * scale[1] + offset[1]),
                    float(b[:, 2].max() * scale[0] + offset[0]),
                    float(b[:, 3].max() * scale[1] + offset[1]),
                ]
                writer.write_table(take, row_group_size=count)
                descriptions.append([first, first + count, {level: box}, {}])
                first += count
                tables, boxes, buffered = [table.slice(count)], [bounds[count:]], buffered - count

            for group in range(parquet.metadata.num_row_groups):
                props = parquet.read_row_group(group, columns=['height', 'class'])
                codes = pc.index_in(props['class'], value_set=pa.array(dictionaries['class']))
                assert not pc.and_(props['class'].is_valid(), codes.is_null()).to_numpy().any()
                blobs = (
                    parquet.read_row_group(group, columns=[f'lod.{level}'])['lod']
                    .combine_chunks()
                    .field(level)
                )
                geometry, bounds, present, checked = decoder.decode(
                    blobs,
                    props['height'].fill_null(0).to_numpy(),
                    props['height'].is_valid().to_numpy(),
                    codes.fill_null(-1).to_numpy().astype(np.int32, copy=False),
                )
                checked['coordinate_samples'] = grouped.verify_samples(
                    blobs, geometry, bounds, present, fields, dictionaries
                )
                ids = np.flatnonzero(present)
                rows = len(ids)
                indices = pa.array(ids)
                compact_geometry = pc.take(geometry, indices)
                assert compact_geometry.null_count == 0
                table = pa.Table.from_arrays(
                    [
                        pa.array(np.full(rows, int(level[1:]), dtype=np.int32)),
                        pa.array(ids + source_first),
                        compact_geometry,
                        pc.take(props['height'], indices),
                        pc.take(props['class'], indices),
                    ],
                    schema=schema,
                )
                # Full per-source-group Arrow round trip verifies every compact value.
                check_path = directory / 'roundtrip.parquet'
                pq.write_table(table, check_path, compression='zstd')
                assert pq.read_table(check_path).equals(table, check_metadata=True)
                validation.append(
                    {
                        'level': level,
                        'source_group': group,
                        **checked,
                        'all_compact_values_arrow_equal': True,
                    }
                )
                tables.append(table)
                boxes.append(bounds[ids])
                buffered += rows
                selected_total += rows
                while buffered >= target_rows:
                    emit(target_rows)
                source_first += len(props)
            if buffered:
                emit(buffered)
            grouped.log(
                'compact-level-written',
                level=level,
                selected=selected_total,
                row_groups=len(descriptions),
                seconds=round(time.perf_counter() - started, 2),
            )
    # Insert an ignorable bootstrap region and relocate standard footer offsets.
    # All row-group payloads are copied exactly; no geometry is hidden in a blob.
    metadata, old_footer = grouped.compact_footer(plain)
    row_groups = get(metadata, 4)[1]
    assert len(row_groups) == len(descriptions)
    partial = output.with_suffix('.parquet.partial')
    payload_hash = hashlib.sha256()
    with plain.open('rb') as src, partial.open('w+b') as dst:
        dst.write(b'PAR1' + bytes(grouped.BOOT))
        grouped.copy_bytes(src, dst, 4, plain.stat().st_size - len(old_footer) - 12, payload_hash)
        for rg, page in zip(row_groups, descriptions, strict=True):
            assert get(rg, 3) == page[1] - page[0]
            for chunk in get(rg, 1)[1]:
                cm = get(chunk, 3)
                assert 11 not in cm and 14 not in cm
                set_value(cm, 9, get(cm, 9) + grouped.BOOT)
                set_value(chunk, 2, 0)
                for optional in [4, 5, 6, 7]:
                    assert optional not in chunk
                path = '.'.join(part.decode() for part in get(cm, 3)[1])
                page[3][path] = [get(cm, 9), get(cm, 7)]
            if 5 in rg:
                set_value(rg, 5, get(rg, 5) + grouped.BOOT)
        shards = []
        for level in grouped.LEVELS:
            shards.extend(
                grouped.write_shards(dst, level, [p for p in descriptions if level in p[2]])
            )
        root = {
            'version': 1,
            'variant': 'nested',
            'layout': 'compact-rows',
            'rows': first,
            'source_rows': parquet.metadata.num_rows,
            'schema': base64.b64encode(grouped.schema_bytes(schema.remove_metadata())).decode(),
            'lods': {level: lod['lods'][level] for level in grouped.LEVELS},
            'attributes': lod['attributes'],
            'shards': shards,
        }
        encoded = gzip.compress(json.dumps(root, separators=(',', ':')).encode(), mtime=0)
        if len(encoded) + 12 >= grouped.BOOT:
            manifest = gzip.compress(json.dumps(shards, separators=(',', ':')).encode(), mtime=0)
            root['shard_manifest'] = [dst.tell(), len(manifest)]
            del root['shards']
            dst.write(manifest)
            encoded = gzip.compress(json.dumps(root, separators=(',', ':')).encode(), mtime=0)
        assert len(encoded) + 12 < grouped.BOOT
        footer = dumps(metadata)
        dst.write(footer + len(footer).to_bytes(4, 'little') + b'PAR1')
        dst.seek(4)
        dst.write(b'NESTIDX1' + len(encoded).to_bytes(4, 'little') + encoded)
    # Verify relocation against every original compressed column stream.
    expected = pq.ParquetFile(plain)
    actual = pq.ParquetFile(partial)
    assert actual.schema_arrow.equals(expected.schema_arrow, check_metadata=True)
    assert actual.metadata.num_rows == first
    actual_hash = hashlib.sha256()
    with partial.open('rb') as file:
        file.seek(4 + grouped.BOOT)
        remaining = plain.stat().st_size - len(old_footer) - 12
        while remaining:
            data = file.read(min(remaining, 8 * 1024 * 1024))
            assert data
            actual_hash.update(data)
            remaining -= len(data)
    assert actual_hash.digest() == payload_hash.digest()
    for group in sorted({0, len(descriptions) - 1, *range(0, len(descriptions), 997)}):
        assert actual.read_row_group(group).equals(
            expected.read_row_group(group), check_metadata=True
        )
    # Each level has contiguous source IDs in Hilbert/source order, never duplicate rows.
    partial.replace(output)
    result = {
        **provenance,
        'overview_rows': first,
        'row_groups': len(row_groups),
        'file_bytes': output.stat().st_size,
        'footer_bytes': len(footer) + 8,
        'root_bytes': len(encoded),
        'shards': len(shards),
        'directory_bytes': sum(s['range'][1] for s in shards),
        'column_payload_sha256': payload_hash.hexdigest(),
        'all_compact_groups_arrow_roundtrip': True,
        'all_relocated_payload_bytes_identical': True,
        'seconds': time.perf_counter() - started,
        'validation': validation,
    }
    grouped.atomic_json(directory / 'build-results.json', result)
    grouped.log('compact-complete', **{k: v for k, v in result.items() if k != 'validation'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--source', type=Path, default=Path('/private/tmp/japan.tippecanoe-density.spatial.parquet')
    )
    parser.add_argument(
        '--directory', type=Path, default=Path('/private/tmp/overview-japan-compact')
    )
    parser.add_argument('--group-rows', type=int, default=4096)
    args = parser.parse_args()
    assert args.group_rows >= 128
    build(args.source, args.directory, args.group_rows)
