"""Rewrite a packed GeoParquet LOD file's overviews as an ordinary Parquet table.

One row per shown feature per level, plus one row per boundary copy tagged
with the tile it serves, in Hilbert order with tile-aligned row groups:

    level, row, tile, geometry (nested int32 x/y on the level's grid), carried
    attributes, key

    uv run python benchmarks/standard-overviews/build.py packed.parquet standard.parquet
"""

import argparse
import json
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from geoparquet_lod.pack import Unpacker

GEOMETRY = pa.list_(pa.list_(pa.list_(pa.struct([('x', pa.int32()), ('y', pa.int32())]))))


def decode_chunk(args):
    path, group, name, family, fields, dictionaries, first_row = args
    f = pq.ParquetFile(path)
    table = f.read_row_group(group, columns=['hilbert', f'lod.{name}'])
    keys = table.column('hilbert').to_numpy(zero_copy_only=False)
    blobs = table.column('lod').combine_chunks().field(name)
    arrow_fields = [pa.field(a['name'], pa.type_for_alias(a['type'])) for a in fields]
    out = {k: [] for k in ('key', 'row', 'tile', *[a['name'] for a in fields])}
    polys, rings, verts, xs, ys = [0], [0], [0], [], []

    def add(key, row, tile, geometry, attrs):
        out['key'].append(int(key))
        out['row'].append(int(row))
        out['tile'].append(tile)
        for a in fields:
            out[a['name']].append(attrs.get(a['name']))
        for polygon in geometry:
            for ring in polygon:
                for x, y in ring:
                    xs.append(x)
                    ys.append(y)
                verts.append(len(xs))
            rings.append(len(verts) - 1)
        polys.append(len(rings) - 1)

    for i, blob in enumerate(blobs.to_pylist()):
        if blob is None:
            continue
        rec = Unpacker(blob, family, arrow_fields, dictionaries).row()
        if rec['geometry']:
            add(keys[i], first_row + i, None, rec['geometry'], rec['attrs'])
        for copy in rec['spill']:
            add(keys[i], copy['id'], copy['tile'], copy['geometry'], copy['attrs'])
    n = len(out['key'])
    if not n:
        return name, None
    struct = pa.StructArray.from_arrays(
        [pa.array(xs, pa.int32()), pa.array(ys, pa.int32())], ['x', 'y']
    )
    inner = pa.ListArray.from_arrays(pa.array(verts, pa.int32()), struct)
    middle = pa.ListArray.from_arrays(pa.array(rings, pa.int32()), inner)
    geometry = pa.ListArray.from_arrays(pa.array(polys, pa.int32()), middle)
    columns = {
        'level': pa.array([name] * n, pa.string()),
        'row': pa.array(out['row'], pa.int64()),
        'tile': pa.array(out['tile'], pa.int64()),
        'geometry': geometry,
    }
    for a in fields:
        columns[a['name']] = pa.array(out[a['name']], pa.type_for_alias(a['type']))
    columns['key'] = pa.array(out['key'], pa.int64())
    return name, pa.table(columns)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packed')
    parser.add_argument('out')
    parser.add_argument('--group-rows', type=int, default=4096)
    parser.add_argument('--workers', type=int, default=8)
    args = parser.parse_args()
    t0 = time.time()
    f = pq.ParquetFile(args.packed)
    lod = json.loads(f.schema_arrow.metadata[b'geo'])['lod_columns']
    fields = lod['attributes']
    dictionaries = {a['name']: a['values'] for a in fields if 'values' in a}
    starts = np.cumsum(
        [0] + [f.metadata.row_group(g).num_rows for g in range(f.metadata.num_row_groups)]
    )
    jobs = [
        (args.packed, g, name, info['geometry_type'], fields, dictionaries, int(starts[g]))
        for name, info in lod['lods'].items()
        for g in range(f.metadata.num_row_groups)
    ]
    chunks = {name: [] for name in lod['lods']}
    with Pool(args.workers) as pool:
        for i, (name, table) in enumerate(pool.imap_unordered(decode_chunk, jobs)):
            if table is not None:
                chunks[name].append(table)
            if (i + 1) % 20 == 0:
                print(f'  decoded {i + 1}/{len(jobs)} chunks, {time.time() - t0:.0f}s', flush=True)
    schema = pa.schema(
        [
            pa.field('level', pa.string()),
            pa.field('row', pa.int64()),
            pa.field('tile', pa.int64()),
            pa.field('geometry', GEOMETRY),
            *[pa.field(a['name'], pa.type_for_alias(a['type'])) for a in fields],
            pa.field('key', pa.int64()),
        ]
    ).with_metadata(
        {
            b'overview_table': json.dumps(
                {
                    'lods': lod['lods'],
                    'attributes': fields,
                    'key': {'column': 'key', 'depth': lod['key']['depth']},
                    'tile_size': lod['tile_size'],
                    'zooms': lod['zooms'],
                }
            ).encode()
        }
    )
    depth = lod['key']['depth']
    summary = {'levels': {}, 'group_rows': args.group_rows}
    with pq.ParquetWriter(
        args.out,
        schema,
        compression='zstd',
        compression_level=9,
        use_dictionary=['level', *[a['name'] for a in fields if 'values' in a]],
        write_statistics=['key', 'level'],
        write_page_index=True,
        data_page_size=65536,
        max_rows_per_page=2048,
    ) as writer:
        group_index = 0
        for name, info in lod['lods'].items():
            if not chunks[name]:
                continue
            table = pa.concat_tables(chunks[name]).select(schema.names).cast(schema)
            chunks[name] = []
            order = pc.sort_indices(
                pa.table({'key': table['key'], 'copy': pc.is_valid(table['tile'])}),
                sort_keys=[('key', 'ascending'), ('copy', 'ascending')],
            )
            table = table.take(order)
            keys = table['key'].to_numpy()
            shift = 2 * max(0, depth - max(info['zooms']))
            cells = keys >> shift
            bounds = np.r_[0, np.flatnonzero(cells[1:] != cells[:-1]) + 1, len(keys)]
            first_group = group_index
            for a, b in zip(bounds[:-1], bounds[1:], strict=True):
                for start in range(int(a), int(b), args.group_rows):
                    end = min(int(b), start + args.group_rows)
                    writer.write_table(table.slice(start, end - start), row_group_size=end - start)
                    group_index += 1
            copies = int(pc.sum(pc.is_valid(table['tile'])).as_py() or 0)
            summary['levels'][name] = {
                'rows': table.num_rows,
                'own': table.num_rows - copies,
                'copies': copies,
                'groups': [first_group, group_index],
            }
            print(
                f'  {name}: {table.num_rows:,} rows in {group_index - first_group} groups, {time.time() - t0:.0f}s',
                flush=True,
            )
    summary['bytes'] = Path(args.out).stat().st_size
    summary['seconds'] = round(time.time() - t0)
    summary['footer_bytes'] = pq.ParquetFile(args.out).metadata.serialized_size
    Path(args.out + '.json').write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != 'levels'}))


if __name__ == '__main__':
    main()
