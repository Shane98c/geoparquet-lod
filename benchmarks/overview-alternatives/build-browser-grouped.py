"""Build valid single-row-group files with independent per-column page spans.

Each physical leaf is one contiguous Parquet column chunk. X/Y pages cannot be
interleaved in a standard single row group: the compact directory locates each
page independently. Source scalar fields are stored once, in adjacent streams.

Usage: .venv/bin/python benchmarks/overview-alternatives/build-browser-grouped.py
Preserves browser.{packed,nested}.parquet and all previous evidence.
"""

import argparse
import base64
import copy
import gzip
import io
import json
import tempfile
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
from geoparquet_lod._compact import dumps, get, loads, set_value

BOOTSTRAP_BYTES = 16384
LEVELS = ['f8', 'f11', 'f14', 'f16']
ATTRIBUTE_ROWS = 4096


def footer(path):
    with path.open('rb') as source:
        source.seek(-8, 2)
        size = int.from_bytes(source.read(4), 'little')
        source.seek(-8 - size, 2)
        return loads(source.read(size))


def spans_for_rows(rows, step):
    return [[first, min(first + step, rows)] for first in range(0, rows, step)]


def stage_column(column, path, spans, nested):
    encoding = (
        {
            f'{column.column_names[0]}.list.element.list.element.list.element.{axis}': 'DELTA_BINARY_PACKED'
            for axis in ['x', 'y']
        }
        if nested
        else None
    )
    with pq.ParquetWriter(
        path,
        column.schema,
        compression='zstd',
        compression_level=9,
        use_dictionary=False,
        write_statistics=False,
        write_page_index=True,
        data_page_size=2**30,
        max_rows_per_page=2**30,
        write_batch_size=128,
        column_encoding=encoding,
    ) as writer:
        for first, last in spans:
            writer.write_table(column.slice(first, last - first), row_group_size=last - first)


def append_leaf_streams(path, target, spans):
    """Concatenate one physical leaf at a time, merging additive metadata.

    Returns standard ColumnChunks plus page locations keyed by row-group/page
    span. The temporary file has one independently encoded page per span/leaf.
    No dictionary, statistics, or offset-index pointers survive concatenation.
    """
    metadata = footer(path)
    groups = get(metadata, 4)[1]
    assert len(groups) == len(spans)
    columns, locations = [], [{} for _ in spans]
    with path.open('rb') as source:
        for ci in range(len(get(groups[0], 1)[1])):
            combined = None
            stream_start = target.tell()
            for page, group in enumerate(groups):
                original = get(group, 1)[1][ci]
                cm = copy.deepcopy(get(original, 3))
                assert 11 not in cm, 'Dictionary encoding cannot be concatenated this way'
                source.seek(get(original, 4))
                index = loads(source.read(get(original, 5)))
                indexed_pages = get(index, 1)[1]
                assert len(indexed_pages) == 1, 'Expected one independently encoded page'
                first_page = indexed_pages[0]
                old_start, size = get(cm, 9), get(cm, 7)
                assert get(first_page, 1) == old_start
                assert get(first_page, 2) == size
                assert get(first_page, 3) == 0
                assert get(group, 3) == spans[page][1] - spans[page][0]
                new_start = target.tell()
                source.seek(old_start)
                data = source.read(size)
                assert len(data) == size
                target.write(data)
                leaf = '.'.join(part.decode() for part in get(cm, 3)[1])
                locations[page][leaf] = [new_start, size]
                if combined is None:
                    combined = cm
                    set_value(combined, 9, new_start)
                    # These are chunk-specific optional statistics/auxiliary data.
                    for field in [12, 13, 14, 15, 16, 17]:
                        combined.pop(field, None)
                else:
                    assert get(combined, 3) == get(cm, 3)
                    assert get(combined, 2) == get(cm, 2)
                    assert get(combined, 4) == get(cm, 4)
                    for field in [5, 6, 7]:
                        set_value(combined, field, get(combined, field) + get(cm, field))
            assert combined is not None
            assert target.tell() - stream_start == get(combined, 7)
            columns.append({2: (6, 0), 3: (12, combined)})
    return columns, locations


def build(directory, variant):
    original_path = directory / f'browser.{variant}.parquet'
    original = pq.read_table(original_path)
    rows = len(original)
    metadata_description = json.loads(original.schema.metadata[b'overview_experiment'])
    levels = metadata_description['lods']
    reference_meta = json.loads(pq.read_schema(directory / 'reference.parquet').metadata[b'geo'])[
        'lod_columns'
    ]
    level_spans = {
        name: json.loads(gzip.decompress((directory / f'{name}.directory.json.gz').read_bytes()))
        for name in LEVELS
    }
    for spans in level_spans.values():
        assert spans[0][0] == 0 and spans[-1][1] == rows
        assert all(a[1] == b[0] for a, b in zip(spans[:-1], spans[1:], strict=True))
    scalar_spans = spans_for_rows(rows, ATTRIBUTE_ROWS)
    # Schema/metadata remain identical to the previous complete browser file.
    schema_buffer = io.BytesIO()
    pq.write_table(original.slice(0, 0), schema_buffer)
    schema_bytes = schema_buffer.getvalue()
    schema_size = int.from_bytes(schema_bytes[-8:-4], 'little')
    metadata = loads(schema_bytes[-8 - schema_size : -8])
    set_value(metadata, 3, rows)
    entries, columns, blocked = [], [], []
    attributes = [[first, last, {}, {}] for first, last in scalar_spans]
    result = {}
    with tempfile.TemporaryDirectory(prefix='.grouped-build-', dir=directory) as temporary:
        work = Path(temporary)
        output = work / 'assembled.parquet'
        with output.open('w+b') as target:
            target.write(b'PAR1' + bytes(BOOTSTRAP_BYTES))
            for name in original.column_names:
                spans = (
                    [[0, rows]]
                    if name in ['id', 'geometry']
                    else scalar_spans
                    if name in ['height', 'class']
                    else [[first, last] for first, last, _ in level_spans[name]]
                )
                stage = work / f'{name}.parquet'
                stage_column(
                    original.select([name]), stage, spans, variant == 'nested' and name in LEVELS
                )
                chunks, locations = append_leaf_streams(stage, target, spans)
                columns.extend(chunks)
                if name in ['id', 'geometry']:
                    blocked.extend(locations[0].values())
                elif name in ['height', 'class']:
                    for entry, leaves in zip(attributes, locations, strict=True):
                        entry[3].update(leaves)
                else:
                    for (first, last, envelope), leaves in zip(
                        level_spans[name], locations, strict=True
                    ):
                        entries.append([first, last, {name: envelope}, leaves])
                stage.unlink()
            entries.extend(attributes)
            assert all(set(entry[3]) == {'height', 'class'} for entry in attributes)
            directory_bytes = gzip.compress(
                json.dumps(entries, separators=(',', ':')).encode(), mtime=0
            )
            directory_offset = target.tell()
            target.write(directory_bytes)
            group = {
                1: (9, (12, columns)),
                2: (6, sum(get(get(chunk, 3), 6) for chunk in columns)),
                3: (6, rows),
                6: (6, sum(get(get(chunk, 3), 7) for chunk in columns)),
            }
            metadata[4] = (9, (12, [group]))
            encoded_footer = dumps(metadata)
            target.write(encoded_footer + len(encoded_footer).to_bytes(4, 'little') + b'PAR1')
            root = {
                'version': 1,
                'variant': variant,
                'layout': 'independent-pages',
                'rows': rows,
                'directory': [directory_offset, len(directory_bytes)],
                'schema': base64.b64encode(schema_bytes).decode(),
                'lods': levels,
                'attributes': reference_meta['attributes'],
                'page_rows': ATTRIBUTE_ROWS,
                'blocked': blocked,
                'physical_layout': 'contiguous-leaf-streams',
                'attribute_rows': ATTRIBUTE_ROWS,
            }
            root_bytes = gzip.compress(json.dumps(root, separators=(',', ':')).encode(), mtime=0)
            assert len(root_bytes) + 12 < BOOTSTRAP_BYTES
            target.seek(4)
            target.write(
                (b'NESTIDX1' + len(root_bytes).to_bytes(4, 'little') + root_bytes).ljust(
                    BOOTSTRAP_BYTES, b'\0'
                )
            )
        readback = pq.read_table(output)
        assert original.equals(readback, check_metadata=True), 'Full Arrow comparison failed'
        db = duckdb.connect()
        counts = db.execute(
            'SELECT count(*),count(height),count(class) FROM read_parquet(?)',
            [str(output)],
        ).fetchone()
        assert counts == (
            rows,
            rows - original['height'].null_count,
            rows - original['class'].null_count,
        )
        db.close()
        final = directory / f'browser.grouped.{variant}.parquet'
        output.replace(final)
        sizes = {}
        for chunk in columns:
            cm = get(chunk, 3)
            path = '.'.join(part.decode() for part in get(cm, 3)[1])
            sizes[path] = get(cm, 7)
        result = {
            'variant': variant,
            'path': str(final),
            'rows': rows,
            'row_groups': 1,
            'file_bytes': final.stat().st_size,
            'bootstrap_bytes': BOOTSTRAP_BYTES,
            'directory_bytes': len(directory_bytes),
            'footer_bytes': len(encoded_footer) + 8,
            'level_pages': {name: len(spans) for name, spans in level_spans.items()},
            'attribute_pages': len(attributes),
            'attribute_rows': ATTRIBUTE_ROWS,
            'physical_layout': 'contiguous-leaf-streams',
            'blocked': blocked,
            'compressed_leaf_bytes': sizes,
            'all_source_and_overview_columns_equal': True,
            'duckdb_count_and_source_null_counts_equal': True,
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--directory', type=Path, default=Path('/private/tmp/overview-alternatives')
    )
    args = parser.parse_args()
    results = []
    for variant in ['packed', 'nested']:
        result = build(args.directory, variant)
        results.append(result)
        print(json.dumps(result), flush=True)
    (args.directory / 'browser-grouped-build.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
