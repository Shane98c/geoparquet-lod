"""Simulate cold tile reads from the standard overview table.

A tile's row groups are those of its level whose key statistics intersect the
tile interval, plus the copy host group. Each group is read as one contiguous
byte range from its first needed column chunk to its last, then ranges are
coalesced with the reader's policy. Directory lookups are charged as in the
packed layout: a gzip JSON shard of up to 1,024 group records per level.

    uv run python benchmarks/standard-overviews/measure-standard.py standard.parquet tiles.json packed.json
"""

import gzip
import json
import sys

import numpy as np
import pyarrow.parquet as pq
from geoparquet_lod.writer import hilbert_key

MAX_GAP, MIN_GAP, GAP_RATIO, MAX_RUN = 32 * 1024, 4 * 1024, 0.5, 4 * 1024 * 1024
SHARD = 1024


def coalesce(ranges):
    runs, run, useful, run_start, run_end = [], [], 0, 0, 0
    for a, b in sorted(ranges):
        gap = min(MAX_GAP, max(MIN_GAP, useful * GAP_RATIO))
        if run and (a - run_end > gap or max(run_end, b) - run_start > MAX_RUN):
            runs.append((run_start, run_end))
            run, useful = [], 0
        if not run:
            run_start = a
        run_end = max(run_end, b) if run else b
        run.append((a, b))
        useful += b - a
    if run:
        runs.append((run_start, run_end))
    return runs


def main():
    path, tiles_path, packed_path = sys.argv[1:4]
    f = pq.ParquetFile(path)
    meta = json.loads(f.schema_arrow.metadata[b'overview_table'])
    summary = json.load(open(path + '.json'))
    depth = meta['key']['depth']
    md = f.metadata
    columns = [md.row_group(0).column(i).path_in_schema for i in range(md.row_group(0).num_columns)]
    first_needed = columns.index('row')
    geometry_end = max(i for i, c in enumerate(columns) if c.startswith('geometry.'))
    styled_end = columns.index('class')

    def chunk_span(g, last):
        rg = md.row_group(g)
        start = min(
            c.dictionary_page_offset or c.data_page_offset
            for c in (rg.column(i) for i in range(first_needed, last + 1))
        )
        end = max(
            c.data_page_offset + c.total_compressed_size
            for c in (rg.column(i) for i in range(first_needed, last + 1))
        )
        return start, end

    stats = [
        (md.row_group(g).column(columns.index('key')).statistics) for g in range(md.num_row_groups)
    ]
    levels = {}
    for name, info in summary['levels'].items():
        a, b = info['groups']
        records = [[stats[g].min, stats[g].max, *chunk_span(g, styled_end)] for g in range(a, b)]
        shards = [
            gzip.compress(json.dumps(records[i : i + SHARD], separators=(',', ':')).encode())
            for i in range(0, len(records), SHARD)
        ]
        levels[name] = {'groups': list(range(a, b)), 'shards': shards}
    root = {
        name: [[records[0][0], records[-1][1], len(s)] for s in lv['shards']]
        for name, lv in levels.items()
        for records in [[[stats[g].min, stats[g].max] for g in lv['groups']]]
    }
    root_bytes = len(gzip.compress(json.dumps(root, separators=(',', ':')).encode()))
    directory_bytes = sum(len(s) for lv in levels.values() for s in lv['shards'])

    def lod_for(z):
        return next(name for name, info in meta['lods'].items() if z in info['zooms'])

    def select(z, x, y):
        name = lod_for(z)
        span = 4 ** (depth - z)
        start = int(hilbert_key(np.array([x]), np.array([y]), z)[0]) * span
        end = start + span
        groups = levels[name]['groups']
        hits = [g for g in groups if stats[g].max >= start and stats[g].min < end]
        host = next((g for g in groups if stats[g].max >= start), groups[-1])
        if host not in hits:
            hits.append(host)
        shard_index = sorted({groups.index(g) // SHARD for g in hits})
        return name, start, end, sorted(hits), host, shard_index

    def read(z, x, y, styled, fetched_ranges, fetched_shards):
        name, start, end, hits, host, shard_index = select(z, x, y)
        requests, nbytes = 0, 0
        for i in shard_index:
            if (name, i) not in fetched_shards:
                fetched_shards.add((name, i))
                requests += 1
                nbytes += len(levels[name]['shards'][i])
        ranges = [chunk_span(g, styled_end if styled else geometry_end) for g in hits]
        for a, b in coalesce(ranges):
            if any(a >= fa and b <= fb for fa, fb in fetched_ranges):
                continue
            fetched_ranges.append((a, b))
            requests += 1
            nbytes += b - a
        return {'requests': requests, 'bytes': nbytes}, (name, start, end, hits, host)

    def members(name, start, end, hits, host, z, x, y):
        tag = (z << 48) | (x * 2**z + y)
        rows = set()
        for g in hits:
            t = f.read_row_group(g, columns=['row', 'tile', 'key'])
            keys = t['key'].to_numpy()
            is_copy = t['tile'].is_valid().to_numpy(zero_copy_only=False)
            tiles = t['tile'].fill_null(-1).to_numpy()
            ids = t['row'].to_numpy()
            rows.update(ids[~is_copy & (keys >= start) & (keys < end)].tolist())
            rows.update(ids[is_copy & (tiles == tag)].tolist())
        return sorted(rows)

    packed = {(t['z'], t['x'], t['y']): t for t in json.load(open(packed_path))}
    out = {
        'file_bytes': summary['bytes'],
        'footer_bytes': summary['footer_bytes'],
        'root_bytes': root_bytes,
        'directory_bytes': directory_bytes,
        'levels': summary['levels'],
        'tiles': [],
    }
    for tile in json.load(open(tiles_path)):
        z, x, y = tile['z'], tile['x'], tile['y']
        row = {**tile}
        for styled in (False, True):
            fetched_ranges, fetched_shards = [], set()
            cold, selection = read(z, x, y, styled, fetched_ranges, fetched_shards)
            nxt, _ = read(z, x + 1, y, styled, fetched_ranges, fetched_shards)
            row['styled' if styled else 'geometry'] = {
                'open': {'requests': 1, 'bytes': 16384},
                'cold': cold,
                'next': nxt,
            }
        ids = members(*selection, z, x, y)
        row['features'] = len(ids)
        row['matches_packed'] = ids == packed[(z, x, y)]['rows']
        out['tiles'].append(row)
    print(json.dumps(out))


if __name__ == '__main__':
    main()
