"""Resumable national nested overview experiment; no production files are changed.

Stages bounded source-row-group pieces using a native packed-own scan, then
copies original analytical compressed chunks verbatim into a standard Parquet
file with independent nested pages and sharded NESTIDX1 discovery.
"""

import argparse
import base64
import concurrent.futures
import copy
import gzip
import hashlib
import io
import json
import math
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from geoparquet_lod._compact import dumps, get, loads, set_value
from geoparquet_lod.pack import Unpacker
from overview_native import NativeOwn, compile_native

LEVELS = ['f4', 'f8', 'f11', 'f14', 'f16']
BOOT = 16384
MAX_SELECTED = 2048
MAX_GEOMETRY_ROWS = 262144
ATTRIBUTE_ROWS = 4096
SHARD_PAGES = 256
SHARD_RAW_BYTES = 65536
COORD = pa.struct([pa.field('x', pa.int32()), pa.field('y', pa.int32())])
NESTED = pa.list_(
    pa.field('element', pa.list_(pa.field('element', pa.list_(pa.field('element', COORD)))))
)


def compact_footer(path):
    with Path(path).open('rb') as file:
        file.seek(-8, 2)
        size = int.from_bytes(file.read(4), 'little')
        file.seek(-8 - size, 2)
        raw = file.read(size)
    return loads(raw), raw


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.partial')
    temporary.write_text(json.dumps(value, separators=(',', ':')) + '\n')
    temporary.replace(path)


def log(event, **values):
    print(json.dumps({'event': event, **values}), flush=True)


def geometry_spans(present):
    ids = np.flatnonzero(present)
    first, n = 0, len(present)
    spans = []
    while first < n:
        end = min(n, first + MAX_GEOMETRY_ROWS)
        index = int(np.searchsorted(ids, first))
        if index + MAX_SELECTED < len(ids):
            end = min(end, int(ids[index + MAX_SELECTED]))
        assert end > first
        spans.append([first, end])
        first = end
    return spans


def verify_samples(blobs, geometry, boxes, present, fields, dictionaries):
    own = np.flatnonzero(present)
    absent = np.flatnonzero(present == 0)
    selected = (
        own[np.linspace(0, len(own) - 1, min(64, len(own)), dtype=int)].tolist() if len(own) else []
    )
    selected += absent[:8].tolist()
    for row in selected:
        blob = blobs[row].as_py()
        expected = Unpacker(blob, 'MultiPolygon', fields, dictionaries).geometry() if blob else []
        value = geometry[row].as_py()
        actual = (
            [[[(p['x'], p['y']) for p in ring] for ring in polygon] for polygon in value]
            if value is not None
            else []
        )
        assert actual == expected, f'Independent own-coordinate mismatch at local row {row}'
        if expected:
            points = np.array([point for polygon in expected for ring in polygon for point in ring])
            assert boxes[row].tolist() == [*points.min(axis=0), *points.max(axis=0)]
    return len(selected)


def encode_piece(path, table, spans, nested):
    partial = path.with_suffix('.partial')
    name = table.column_names[0]
    encoding = (
        {
            f'{name}.list.element.list.element.list.element.{axis}': 'DELTA_BINARY_PACKED'
            for axis in ['x', 'y']
        }
        if nested
        else None
    )
    with pq.ParquetWriter(
        partial,
        table.schema,
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
            writer.write_table(table.slice(first, last - first), row_group_size=last - first)
    decoded = pq.ParquetFile(partial).read()
    assert table.equals(decoded, check_metadata=True), f'Full stage Arrow mismatch: {path}'
    partial.replace(path)


def stage_group(task):
    source, directory, library, group = task
    pa.set_cpu_count(2)
    directory = Path(directory)
    work = directory / 'stages' / f'rg{group:03d}'
    work.mkdir(parents=True, exist_ok=True)
    completed = work / 'complete.json'
    if completed.exists():
        return json.loads(completed.read_text()) | {'reused': True}
    started = time.perf_counter()
    parquet = pq.ParquetFile(source)
    lod = json.loads(parquet.schema_arrow.metadata[b'geo'])['lod_columns']
    assert [a['name'] for a in lod['attributes']] == ['height', 'class']
    assert lod['attributes'][0]['type'] in ['double', 'float64']
    fields = [pa.field(a['name'], pa.type_for_alias(a['type'])) for a in lod['attributes']]
    dictionaries = {a['name']: a['values'] for a in lod['attributes'] if 'values' in a}
    properties = parquet.read_row_group(group, columns=['height', 'class'])
    n = len(properties)
    height = properties['height'].fill_null(0).to_numpy()
    height_valid = properties['height'].is_valid().to_numpy()
    codes = pc.index_in(properties['class'], value_set=pa.array(dictionaries['class']))
    assert not pc.and_(properties['class'].is_valid(), codes.is_null()).to_numpy().any()
    codes = codes.fill_null(-1).to_numpy().astype(np.int32, copy=False)
    decoder = NativeOwn(library)
    summary = {'group': group, 'rows': n, 'fields': {}}
    for name in ['height', 'class', *LEVELS]:
        piece, description = work / f'{name}.parquet', work / f'{name}.json'
        if piece.exists() and description.exists():
            saved = json.loads(description.read_text())
            assert piece.stat().st_size == saved['bytes']
            summary['fields'][name] = saved
            continue
        field_started = time.perf_counter()
        if name in ['height', 'class']:
            spans = [
                [first, min(n, first + ATTRIBUTE_ROWS)] for first in range(0, n, ATTRIBUTE_ROWS)
            ]
            pages = [[first, last, None] for first, last in spans]
            table, stats = properties.select([name]), {}
        else:
            blobs = (
                parquet.read_row_group(group, columns=[f'lod.{name}'])['lod']
                .combine_chunks()
                .field(name)
            )
            geometry, boxes, present, stats = decoder.decode(blobs, height, height_valid, codes)
            stats['independent_coordinate_samples'] = verify_samples(
                blobs, geometry, boxes, present, fields, dictionaries
            )
            spans = geometry_spans(present)
            scale, offset = lod['lods'][name]['scale'], lod['lods'][name]['offset']
            pages = []
            for first, last in spans:
                b = boxes[first:last][present[first:last] != 0]
                envelope = (
                    [
                        float(b[:, 0].min() * scale[0] + offset[0]),
                        float(b[:, 1].min() * scale[1] + offset[1]),
                        float(b[:, 2].max() * scale[0] + offset[0]),
                        float(b[:, 3].max() * scale[1] + offset[1]),
                    ]
                    if len(b)
                    else None
                )
                pages.append([first, last, envelope])
            table = pa.table({name: geometry})
        encode_piece(piece, table, spans, name in LEVELS)
        saved = {
            'bytes': piece.stat().st_size,
            'pages': pages,
            'seconds': time.perf_counter() - field_started,
            **stats,
        }
        atomic_json(description, saved)
        summary['fields'][name] = saved
        log(
            'field-staged',
            group=group,
            field=name,
            pages=len(pages),
            bytes=saved['bytes'],
            seconds=round(saved['seconds'], 3),
            selected=stats.get('selected'),
        )
    summary['seconds'] = time.perf_counter() - started
    atomic_json(completed, summary)
    log(
        'group-staged',
        group=group,
        seconds=round(summary['seconds'], 3),
        bytes=sum(field['bytes'] for field in summary['fields'].values()),
    )
    return summary


def source_schema(parquet):
    geo = json.loads(parquet.schema_arrow.metadata[b'geo'])
    lod = geo.pop('lod_columns')
    metadata = dict(parquet.schema_arrow.metadata or {})
    metadata[b'geo'] = json.dumps(geo, separators=(',', ':')).encode()
    metadata[b'overview_experiment'] = json.dumps(
        {
            'version': 1,
            'encoding': 'quantized_nested_int32_delta',
            'source': 'exact production own geometry; hosted copies omitted',
            'lods': {name: lod['lods'][name] for name in LEVELS},
            'source_attributes': ['height', 'class'],
        },
        separators=(',', ':'),
    ).encode()
    schema = pa.schema(
        [field for field in parquet.schema_arrow if field.name != 'lod']
        + [pa.field(name, NESTED) for name in LEVELS],
        metadata=metadata,
    )
    return schema, lod


def schema_bytes(schema):
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_batches([], schema=schema), buffer)
    return buffer.getvalue()


def copy_bytes(source, target, start, size, digest=None):
    source.seek(start)
    while size:
        data = source.read(min(size, 8 * 1024 * 1024))
        if not data:
            raise ValueError('Truncated source column chunk')
        target.write(data)
        if digest is not None:
            digest.update(data)
        size -= len(data)


def append_stage(path, target, pages):
    metadata, _ = compact_footer(path)
    groups = get(metadata, 4)[1]
    assert len(groups) == len(pages)
    columns, locations = [], [{} for _ in pages]
    with path.open('rb') as source:
        for ci in range(len(get(groups[0], 1)[1])):
            combined, start = None, target.tell()
            for p, group in enumerate(groups):
                original = get(group, 1)[1][ci]
                cm = copy.deepcopy(get(original, 3))
                assert 11 not in cm
                source.seek(get(original, 4))
                index = loads(source.read(get(original, 5)))
                indexed = get(index, 1)[1]
                assert len(indexed) == 1
                old_start, size = get(cm, 9), get(cm, 7)
                assert get(indexed[0], 1) == old_start and get(indexed[0], 2) == size
                assert get(group, 3) == pages[p][1] - pages[p][0]
                offset = target.tell()
                copy_bytes(source, target, old_start, size)
                leaf = '.'.join(part.decode() for part in get(cm, 3)[1])
                locations[p][leaf] = [offset, size]
                if combined is None:
                    combined = cm
                    set_value(combined, 9, offset)
                    for optional in [12, 13, 14, 15, 16, 17]:
                        combined.pop(optional, None)
                else:
                    assert get(combined, 3) == get(cm, 3)
                    for field in [5, 6, 7]:
                        set_value(combined, field, get(combined, field) + get(cm, field))
            assert target.tell() - start == get(combined, 7)
            columns.append({2: (6, 0), 3: (12, combined)})
    return columns, locations


def outer_box(boxes):
    valid = [b for b in boxes if b is not None]
    if not valid:
        return None
    result = [
        min(b[0] for b in valid),
        min(b[1] for b in valid),
        max(b[2] for b in valid),
        max(b[3] for b in valid),
    ]
    # Discovery only: outward decimal bounds shorten the national root and
    # cannot omit a feature. Fine page envelopes retain exact transformed bounds.
    rounded = [
        math.floor(result[0] * 1e6) / 1e6,
        math.floor(result[1] * 1e6) / 1e6,
        math.ceil(result[2] * 1e6) / 1e6,
        math.ceil(result[3] * 1e6) / 1e6,
    ]
    assert rounded[0] <= result[0] and rounded[1] <= result[1]
    assert rounded[2] >= result[2] and rounded[3] >= result[3]
    return rounded


def write_shards(target, kind, entries):
    shards, current, size = [], [], 2

    def flush():
        nonlocal current, size
        if not current:
            return
        raw = json.dumps(current, separators=(',', ':')).encode()
        assert len(raw) <= SHARD_RAW_BYTES and len(current) <= SHARD_PAGES
        encoded = gzip.compress(raw, mtime=0)
        offset = target.tell()
        target.write(encoded)
        shards.append(
            {
                'kind': kind,
                'first': current[0][0],
                'last': current[-1][1],
                'bbox': None
                if kind == 'attributes'
                else outer_box([entry[2][kind] for entry in current]),
                'range': [offset, len(encoded)],
            }
        )
        current, size = [], 2

    for entry in entries:
        length = len(json.dumps(entry, separators=(',', ':')).encode())
        if current and (len(current) >= SHARD_PAGES or size + length + 1 > SHARD_RAW_BYTES):
            flush()
        current.append(entry)
        size += length + 1
    flush()
    return shards


def assemble(source_path, directory, summaries, validate=True):
    source_path, directory = Path(source_path), Path(directory)
    parquet = pq.ParquetFile(source_path)
    schema, lod = source_schema(parquet)
    empty = schema_bytes(schema)
    footer_size = int.from_bytes(empty[-8:-4], 'little')
    metadata = loads(empty[-8 - footer_size : -8])
    set_value(metadata, 3, parquet.metadata.num_rows)
    paths = [
        pq.ParquetFile(io.BytesIO(empty)).schema.column(i).path
        for i in range(len(pq.ParquetFile(io.BytesIO(empty)).schema.names))
    ]
    old_metadata, _ = compact_footer(source_path)
    old_groups = get(old_metadata, 4)[1]
    output = directory / 'japan.nested.parquet.partial'
    groups, shards, first, copied_chunks = [], [], 0, 0
    copied_digest = hashlib.sha256()
    started = time.perf_counter()
    with source_path.open('rb') as source, output.open('w+b') as target:
        target.write(b'PAR1' + bytes(BOOT))
        for group_number, summary in enumerate(summaries):
            rows = summary['rows']
            chunks = {}
            for original in get(old_groups[group_number], 1)[1]:
                cm = copy.deepcopy(get(original, 3))
                leaf = '.'.join(part.decode() for part in get(cm, 3)[1])
                if leaf.startswith('lod.') or leaf in ['height', 'class']:
                    continue
                assert 1 not in original and 8 not in original and 9 not in original
                assert 14 not in cm, 'Bloom filter relocation is not implemented'
                old_start = min(get(cm, 9), get(cm, 11, get(cm, 9)))
                new_start = target.tell()
                copied_digest.update(f'{group_number}:{leaf}:'.encode())
                copy_bytes(source, target, old_start, get(cm, 7), copied_digest)
                delta = new_start - old_start
                set_value(cm, 9, get(cm, 9) + delta)
                if 11 in cm:
                    set_value(cm, 11, get(cm, 11) + delta)
                # The copied payload and its statistics are exact. Optional old
                # page-index pointers are omitted, never retained at stale offsets.
                chunk = copy.deepcopy(original)
                for optional in [4, 5, 6, 7]:
                    chunk.pop(optional, None)
                chunk[2], chunk[3] = (6, 0), (12, cm)
                chunks[leaf] = chunk
                copied_chunks += 1
            attribute_entries = None
            work = directory / 'stages' / f'rg{group_number:03d}'
            for name in ['height', 'class', *LEVELS]:
                description = summary['fields'][name]
                new_chunks, locations = append_stage(
                    work / f'{name}.parquet', target, description['pages']
                )
                for chunk in new_chunks:
                    cm = get(chunk, 3)
                    leaf = '.'.join(part.decode() for part in get(cm, 3)[1])
                    chunks[leaf] = chunk
                if name in ['height', 'class']:
                    if attribute_entries is None:
                        attribute_entries = [
                            [first + a, first + b, {}, {}] for a, b, _ in description['pages']
                        ]
                    for entry, leaves in zip(attribute_entries, locations, strict=True):
                        entry[3].update(leaves)
                else:
                    entries = [
                        [first + a, first + b, {name: box}, leaves]
                        for (a, b, box), leaves in zip(description['pages'], locations, strict=True)
                    ]
                    shards.extend(write_shards(target, name, entries))
            assert all(set(entry[3]) == {'height', 'class'} for entry in attribute_entries)
            shards.extend(write_shards(target, 'attributes', attribute_entries))
            assert set(chunks) == set(paths)
            ordered = [chunks[path] for path in paths]
            groups.append(
                {
                    1: (9, (12, ordered)),
                    2: (6, sum(get(get(c, 3), 6) for c in ordered)),
                    3: (6, rows),
                    6: (6, sum(get(get(c, 3), 7) for c in ordered)),
                }
            )
            first += rows
            log(
                'group-assembled',
                group=group_number,
                rows=first,
                bytes=target.tell(),
                shards=len(shards),
            )
        assert first == parquet.metadata.num_rows
        browser_schema = pa.schema([schema.field(name) for name in ['height', 'class', *LEVELS]])
        root = {
            'version': 1,
            'variant': 'nested',
            'layout': 'independent-pages',
            'rows': first,
            'schema': base64.b64encode(schema_bytes(browser_schema)).decode(),
            'lods': {name: lod['lods'][name] for name in LEVELS},
            'attributes': lod['attributes'],
            'page_rows': ATTRIBUTE_ROWS,
            'attribute_rows': ATTRIBUTE_ROWS,
            'shards': shards,
            'physical_layout': 'contiguous-leaf-streams',
            'shard_limits': {'pages': SHARD_PAGES, 'raw_bytes': SHARD_RAW_BYTES},
        }
        root_bytes = gzip.compress(json.dumps(root, separators=(',', ':')).encode(), mtime=0)
        if len(root_bytes) + 12 >= BOOT:
            raise ValueError(
                f'Root needs {len(root_bytes) + 12} bytes; add a descriptor manifest tier'
            )
        metadata[4] = (9, (12, groups))
        encoded_footer = dumps(metadata)
        target.write(encoded_footer + len(encoded_footer).to_bytes(4, 'little') + b'PAR1')
        file_bytes = target.tell()
        target.seek(4)
        target.write(
            (b'NESTIDX1' + len(root_bytes).to_bytes(4, 'little') + root_bytes).ljust(BOOT, b'\0')
        )
    result = {
        'source_rows': first,
        'row_groups': len(groups),
        'file_bytes': file_bytes,
        'root_compressed_bytes': len(root_bytes),
        'shards': len(shards),
        'directory_bytes': sum(shard['range'][1] for shard in shards),
        'footer_bytes': len(encoded_footer) + 8,
        'copied_source_chunks': copied_chunks,
        'copied_source_payload_sha256': copied_digest.hexdigest(),
        'assembly_seconds': time.perf_counter() - started,
    }
    atomic_json(directory / 'assembly.json', result)
    log('file-assembled', **result)
    if validate:
        rewritten = pq.ParquetFile(output)
        validated = 0
        validation_started = time.perf_counter()
        for group in range(parquet.metadata.num_row_groups):
            for name in parquet.schema_arrow.names:
                if name == 'lod':
                    continue
                before = parquet.read_row_group(group, columns=[name], use_threads=False)[name]
                after = rewritten.read_row_group(group, columns=[name], use_threads=False)[name]
                assert before.equals(after), f'Source value mismatch: group{group} {name}'
                validated += 1
            log('source-group-validated', group=group)
        result.update(
            {
                'all_source_columns_arrow_equal': True,
                'source_column_group_comparisons': validated,
                'source_validation_seconds': time.perf_counter() - validation_started,
            }
        )
    output.replace(directory / 'japan.nested.parquet')
    atomic_json(directory / 'assembly.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--source', type=Path, default=Path('/private/tmp/japan.tippecanoe-density.spatial.parquet')
    )
    parser.add_argument('--directory', type=Path, default=Path('/private/tmp/overview-japan'))
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--groups', type=int, help='Bounded staging-only pilot source groups')
    parser.add_argument('--stage-only', action='store_true')
    args = parser.parse_args()
    assert 1 <= args.workers <= 4
    assert args.groups is None or args.stage_only
    args.directory.mkdir(parents=True, exist_ok=True)
    pa.set_cpu_count(4)
    parquet = pq.ParquetFile(args.source)
    _, source_footer = compact_footer(args.source)
    library = compile_native(args.directory)
    manifest = {
        'version': 1,
        'source': str(args.source.resolve()),
        'source_bytes': args.source.stat().st_size,
        'source_footer_sha256': hashlib.sha256(source_footer).hexdigest(),
        'rows': parquet.metadata.num_rows,
        'groups': parquet.metadata.num_row_groups,
        'levels': LEVELS,
        'max_selected_per_page': MAX_SELECTED,
        'max_geometry_rows': MAX_GEOMETRY_ROWS,
        'attribute_rows': ATTRIBUTE_ROWS,
        'codec': 'ZSTD9',
        'native_decoder_sha256': hashlib.sha256(
            Path(__file__).with_name('overview-native.cpp').read_bytes()
        ).hexdigest(),
        'native_wrapper_sha256': hashlib.sha256(
            Path(__file__).with_name('overview_native.py').read_bytes()
        ).hexdigest(),
    }
    previous = args.directory / 'build-manifest.json'
    if previous.exists():
        assert json.loads(previous.read_text()) == manifest, (
            'Staging manifest differs; choose another directory'
        )
    else:
        atomic_json(previous, manifest)
    count = args.groups or parquet.metadata.num_row_groups
    tasks = [(str(args.source), str(args.directory), str(library), group) for group in range(count)]
    log('build-started', groups=count, workers=args.workers, levels=LEVELS)
    started = time.perf_counter()
    if args.workers == 1:
        summaries = [stage_group(task) for task in tasks]
    else:
        summaries = []
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(stage_group, task) for task in tasks]
            for future in concurrent.futures.as_completed(futures):
                summaries.append(future.result())
    summaries.sort(key=lambda result: result['group'])
    totals = {
        name: {
            key: sum(group['fields'][name].get(key, 0) for group in summaries)
            for key in ['selected', 'vertices', 'independent_coordinate_samples', 'bytes']
        }
        for name in LEVELS
    }
    atomic_json(
        args.directory / 'staging-summary.json',
        {'seconds': time.perf_counter() - started, 'groups': count, 'totals': totals},
    )
    log('staging-complete', seconds=round(time.perf_counter() - started, 3), totals=totals)
    if not args.stage_only:
        result = assemble(args.source, args.directory, summaries)
        log('build-complete', seconds=round(time.perf_counter() - started, 3), **result)


if __name__ == '__main__':
    main()
