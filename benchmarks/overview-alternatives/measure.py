"""Matched source-aligned packed/WKB overview ablation, actual Parquet range IO.

Each source-ordered group is one independently compressed page per column. Both
variants use identical groups and a gzip bbox directory. Footer IO is charged.
This is a Python analytical harness, not the production JS rendering decoder.
"""

import argparse
import gzip
import io
import json
import math
import time
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from geoparquet_lod.pack import Unpacker


def overlap(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def geometry(g, lod):
    scale, offset = lod['scale'], lod['offset']
    return shapely.MultiPolygon(
        [
            shapely.Polygon(
                [(x * scale[0] + offset[0], y * scale[1] + offset[1]) for x, y in p[0]],
                [
                    [(x * scale[0] + offset[0], y * scale[1] + offset[1]) for x, y in r]
                    for r in p[1:]
                ],
            )
            for p in g
        ]
    )


class Ranges(io.RawIOBase):
    def __init__(self, path):
        self.f = open(path, 'rb')
        self.ranges = []
        self.cache = {}

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.f.tell()

    def seek(self, offset, whence=0):
        return self.f.seek(offset, whence)

    def read(self, size=-1):
        start = self.tell()
        if size >= 0:
            for (base, length), value in self.cache.items():
                if base <= start and start + size <= base + length:
                    self.f.seek(start + size)
                    return value[start - base : start - base + size]
        b = self.f.read(size)
        self.ranges.append([start, len(b)])
        self.cache[start, len(b)] = b
        return b

    def close(self):
        self.f.close()
        super().close()


def stats(r):
    return {
        'requests': len(r.ranges),
        'bytes': sum(x[1] for x in r.ranges),
        'ranges': r.ranges.copy(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', default='/private/tmp/overview-alternatives')
    parser.add_argument('--output', default='benchmarks/overview-alternatives/results.json')
    a = parser.parse_args()
    out = Path(a.directory)
    source = pq.read_table(out / 'reference.parquet')
    meta = json.loads(source.schema.metadata[b'geo'])['lod_columns']
    fields = [pa.field(x['name'], pa.type_for_alias(x['type'])) for x in meta['attributes']]
    dictionaries = {x['name']: x['values'] for x in meta['attributes'] if 'values' in x}
    attrs = {f.name: source[f.name].to_pylist() for f in fields}
    n = len(source)
    lod_array = source['lod'].combine_chunks()
    keys = source['hilbert'].to_numpy()
    levels = {}
    validations = {}
    sizes = {}
    samples = []
    for name, lod in meta['lods'].items():
        z = max(lod['zooms'])
        if z not in [8, 11, 14, 16]:
            continue
        blobs = lod_array.field(name).to_pylist()
        geoms = []
        packed = []
        spill_records = []
        for rid, blob in enumerate(blobs):
            if blob is None:
                packed.append(None)
                geoms.append(None)
                continue
            u = Unpacker(blob, lod['geometry_type'], fields, dictionaries)
            own = u.geometry()
            ownattrs = u.attrs()
            cut = u.pos
            record = Unpacker(blob, lod['geometry_type'], fields, dictionaries).row()
            spill_records.extend(record['spill'])
            if own:
                for f in fields:
                    assert ownattrs[f.name] == attrs[f.name][rid], (name, rid, f.name)
                packed.append(blob[:cut] + b'\0')
                geoms.append(geometry(own, lod))
            else:
                packed.append(None)
                geoms.append(None)
        for record in spill_records:
            rid = record['id']
            assert geoms[rid] is not None
            assert shapely.equals_exact(geometry(record['geometry'], lod), geoms[rid], 0)
            assert record['attrs'] == {f.name: attrs[f.name][rid] for f in fields}
        wbks = shapely.to_wkb(geoms)
        bounds = shapely.bounds(geoms)
        present = np.array([g is not None for g in geoms])
        # Same source spans for both representations: tile boundaries, then 2048-row cap.
        cells = keys >> (2 * (24 - z))
        boundaries = np.r_[0, np.flatnonzero(cells[1:] != cells[:-1]) + 1, n]
        spans = []
        for first, last in zip(boundaries[:-1], boundaries[1:], strict=True):
            for start in range(int(first), int(last), 2048):
                end = min(start + 2048, int(last))
                valid = present[start:end]
                if valid.any():
                    b = bounds[start:end][valid]
                    envelope = [
                        float(b[:, 0].min()),
                        float(b[:, 1].min()),
                        float(b[:, 2].max()),
                        float(b[:, 3].max()),
                    ]
                else:
                    envelope = None
                spans.append([start, end, envelope])
        directory = gzip.compress(json.dumps(spans, separators=(',', ':')).encode(), mtime=0)
        (out / f'{name}.directory.json.gz').write_bytes(directory)
        base = {
            'source_row_id': pa.array(np.arange(n), pa.int64()),
            'height': source['height'],
            'class': source['class'],
        }
        variants = {
            'packed': pa.table({**base, 'overview': pa.array(packed, pa.binary())}),
            'wkb': pa.table({**base, 'overview': pa.array(wbks, pa.binary())}),
            'packed_copies': pa.table({**base, 'overview': pa.array(blobs, pa.binary())}),
        }
        geo = {
            'version': '1.1.0',
            'primary_column': 'overview',
            'columns': {'overview': {'encoding': 'WKB', 'geometry_types': ['MultiPolygon']}},
        }
        variants['wkb'] = variants['wkb'].replace_schema_metadata(
            {b'geo': json.dumps(geo).encode()}
        )
        for variant, table in variants.items():
            path = out / f'{name}.{variant}.parquet'
            with pq.ParquetWriter(
                path,
                table.schema,
                compression='zstd',
                compression_level=9,
                use_dictionary=False,
                write_statistics=False,
                data_page_size=2**30,
                max_rows_per_page=2**30,
                write_batch_size=128,
            ) as w:
                for start, end, _ in spans:
                    w.write_table(table.slice(start, end - start), row_group_size=end - start)
            m = pq.read_metadata(path)
            sizes[f'{name}.{variant}'] = {
                'file_bytes': path.stat().st_size,
                'footer_bytes': m.serialized_size + 8,
                'directory_bytes': len(directory),
                'compressed_columns': {
                    column: sum(
                        m.row_group(rg).column(i).total_compressed_size
                        for rg in range(m.num_row_groups)
                    )
                    for i, column in enumerate(table.column_names)
                },
            }
        levels[name] = {
            'zoom': z,
            'selected': int(present.sum()),
            'spills': len(spill_records),
            'groups': len(spans),
        }
        validations[name] = {
            'own_source_attributes_equal': True,
            'all_spill_geometry_attributes_equal': True,
        }
        # Same WKB columns can be queried through ordinary DuckDB SQL + spatial.
        c = duckdb.connect()
        c.execute('LOAD spatial')
        t = time.perf_counter()
        analytical = c.execute(
            """SELECT count(overview),sum(ST_NPoints(overview)),
          sum(ST_Area(overview)),avg(height) FILTER(WHERE overview IS NOT NULL)
          FROM read_parquet(?)""",
            [str(out / f'{name}.wkb.parquet')],
        ).fetchone()
        levels[name]['duckdb'] = {
            'rows': analytical[0],
            'vertices': analytical[1],
            'area_degrees2': analytical[2],
            'mean_height': analytical[3],
            'ms': (time.perf_counter() - t) * 1000,
        }
        assert analytical[0] == int(present.sum())
        assert analytical[1] == int(shapely.get_num_coordinates(geoms).sum())
        validations[name]['duckdb_count_and_vertices_equal'] = True
        c.close()
        for place, lon, lat in [('city', 139.745, 35.665), ('periphery', 139.495, 35.705)]:
            width = 3 if z >= 14 else 1
            nt = 2**z
            tx = int((lon + 180) / 360 * nt)
            ty = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * nt)
            west, east = tx - width // 2, tx + width // 2 + 1
            north, south = ty - width // 2, ty + width // 2 + 1
            view = [
                west / nt * 360 - 180,
                math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * south / nt)))),
                east / nt * 360 - 180,
                math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * north / nt)))),
            ]
            expected = set(
                np.flatnonzero(
                    present
                    & (bounds[:, 0] <= view[2])
                    & (bounds[:, 2] >= view[0])
                    & (bounds[:, 1] <= view[3])
                    & (bounds[:, 3] >= view[1])
                ).tolist()
            )
            groups = [i for i, (_, _, b) in enumerate(spans) if b is not None and overlap(b, view)]
            crossing = sum(
                1
                for rid in expected
                if (bounds[rid, 0] < tx / nt * 360 - 180 < bounds[rid, 2])
                or (
                    bounds[rid, 1]
                    < math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / nt))))
                    < bounds[rid, 3]
                )
            )
            for styled in [False, True]:
                for variant in ['packed', 'wkb']:
                    for transport in ['unbatched', 'adjacent']:
                        for run in range(3):
                            path = out / f'{name}.{variant}.parquet'
                            r = Ranges(path)
                            # Exact footer ranges; metadata injection avoids Arrow's generic 64KiB tail overfetch.
                            r.seek(-8, 2)
                            tail = r.read(8)
                            flen = int.from_bytes(tail[:4], 'little')
                            r.seek(-8 - flen, 2)
                            footer = r.read(flen)
                            metadata = pq.read_metadata(io.BytesIO(b'PAR1' + footer + tail))
                            pf = pq.ParquetFile(
                                r, metadata=metadata, pre_buffer=False, buffer_size=0
                            )
                            cols = ['overview'] + (
                                ['height', 'class'] if styled and variant == 'wkb' else []
                            )

                            def query(
                                validate=False,
                                groups=groups,
                                pf=pf,
                                cols=cols,
                                spans=spans,
                                variant=variant,
                                lod=lod,
                                view=view,
                                geoms=geoms,
                                styled=styled,
                                expected=expected,
                                name=name,
                                place=place,
                            ):
                                found = {}
                                decoded_rows = 0
                                for rg in groups:
                                    tab = pf.read_row_group(rg, columns=cols, use_threads=False)
                                    vals = tab['overview'].to_pylist()
                                    start = spans[rg][0]
                                    gs = (
                                        shapely.from_wkb(vals)
                                        if variant == 'wkb'
                                        else [
                                            geometry(
                                                Unpacker(
                                                    b, lod['geometry_type'], fields, dictionaries
                                                ).geometry(),
                                                lod,
                                            )
                                            if b is not None
                                            else None
                                            for b in vals
                                        ]
                                    )
                                    bb = shapely.bounds(gs)
                                    for k, g in enumerate(gs):
                                        if g is None or not overlap(bb[k], view):
                                            continue
                                        rid = start + k
                                        decoded_rows += 1
                                        if validate:
                                            assert shapely.equals_exact(g, geoms[rid], 0)
                                        if styled:
                                            aa = (
                                                {f.name: tab[f.name][k].as_py() for f in fields}
                                                if variant == 'wkb'
                                                else Unpacker(
                                                    vals[k],
                                                    lod['geometry_type'],
                                                    fields,
                                                    dictionaries,
                                                ).row()['attrs']
                                            )
                                            if validate:
                                                assert aa == {
                                                    f.name: attrs[f.name][rid] for f in fields
                                                }
                                        found[rid] = g
                                assert set(found) == expected, (
                                    name,
                                    place,
                                    variant,
                                    len(found),
                                    len(expected),
                                )
                                return decoded_rows

                            t = time.perf_counter()
                            if transport == 'adjacent':
                                intervals = []
                                for rg in groups:
                                    for ci, column in enumerate(pf.schema.names):
                                        if column not in cols:
                                            continue
                                        chunk = metadata.row_group(rg).column(ci)
                                        intervals.append(
                                            [chunk.data_page_offset, chunk.total_compressed_size]
                                        )
                                merged = []
                                for offset, size in sorted(intervals):
                                    if merged and sum(merged[-1]) == offset:
                                        merged[-1][1] += size
                                    else:
                                        merged.append([offset, size])
                                for offset, size in merged:
                                    r.seek(offset)
                                    r.read(size)
                            count = query()
                            elapsed = (time.perf_counter() - t) * 1000
                            cold = stats(r)
                            cold['requests'] += 1
                            cold['bytes'] += len(directory)
                            r.ranges = []
                            t = time.perf_counter()
                            query()
                            warmms = (time.perf_counter() - t) * 1000
                            samples.append(
                                {
                                    'lod': name,
                                    'place': place,
                                    'styled': styled,
                                    'variant': variant,
                                    'run': run,
                                    'ms': elapsed,
                                    'warm_ms': warmms,
                                    'transport': transport,
                                    'cold': cold,
                                    'warm': stats(r),
                                    'features': count,
                                    'selected_groups': len(groups),
                                    'viewport': view,
                                    'boundary_crossers': crossing,
                                }
                            )
                            query(validate=True)
                            r.close()
        print(name, levels[name], flush=True)
    result = {
        'source_rows': n,
        'reference_bytes': (out / 'reference.parquet').stat().st_size,
        'config': {
            'source': str(out),
            'group_row_cap': 2048,
            'codec': 'zstd9',
            'runs': 3,
            'network_delay_ms': 0,
            'footer_included': True,
            'directory_included': True,
        },
        'levels': levels,
        'sizes': sizes,
        'validations': validations,
        'samples': samples,
    }
    Path(a.output).write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
