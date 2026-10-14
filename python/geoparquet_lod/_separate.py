"""Private benchmark experiment: geometry-only packed overviews with separately selectable attributes.

Each attribute column is a standard nullable LIST of its original Arrow scalar
type. Slot zero is the owning row's LOD value; subsequent slots correspond to
boundary copies in the geometry blob. This preserves per-LOD overrides and
copy values exactly, without joining them to potentially different source values.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .pack import Unpacker


def split_row(blob, family, fields, dictionaries):
    """Copy geometry varints verbatim; decode only attribute values."""
    u = Unpacker(blob, family, fields, dictionaries)
    output = bytearray()
    records = []

    def skip_geometry():
        def ring():
            for _ in range(u.varint() * 2):
                u.varint()

        if family == 'Point':
            u.varint()
            u.varint()
        elif family == 'MultiPolygon':
            for _ in range(u.varint()):
                for _ in range(u.varint()):
                    ring()
        else:
            for _ in range(u.varint()):
                ring()

    def record():
        start = u.pos
        skip_geometry()
        output.extend(blob[start : u.pos])
        records.append(u.attrs())
        output.append(0)  # empty attribute presence bitmap in geometry-only packing

    record()
    start = u.pos
    count = u.varint()
    output.extend(blob[start : u.pos])
    for _ in range(count):
        start = u.pos
        u.varint()  # target tile
        u.varint()  # original source row
        output.extend(blob[start : u.pos])
        record()
    for values in records:
        for name, value in values.items():
            if isinstance(value, tuple) and value[0] == 'string':
                length = value[1]
                values[name] = blob[u.pos : u.pos + length].decode('utf-8', 'strict')
                u.pos += length
    if u.pos != len(blob):
        raise ValueError('packed row has trailing or truncated data')
    return bytes(output), {f.name: [r[f.name] for r in records] for f in fields}


def separate(src, out, *, log=print):
    """Convert a packed file without changing row order, geometry, or source columns.

    This is an opt-in ablation format. The output includes a spatial index for
    geometry pages and their separately selectable attribute companions.
    """
    src, out = Path(src), Path(out)
    if src.resolve() == out.resolve():
        raise ValueError('separate requires a separate output path')
    out.parent.mkdir(parents=True, exist_ok=True)
    file = pq.ParquetFile(src)
    geo = json.loads(file.schema_arrow.metadata[b'geo'])
    lod = geo['lod_columns']
    if lod.get('encoding') != 'packed' or lod.get('pack_version') != 1:
        raise ValueError('separate requires packed LOD version 1')
    fields = [pa.field(f['name'], pa.type_for_alias(f['type'])) for f in lod['attributes']]
    dictionaries = {f['name']: f['values'] for f in lod['attributes'] if 'values' in f}
    mapping = {
        name: {f.name: f'__lod_attr_{i}_{j}' for j, f in enumerate(fields)}
        for i, name in enumerate(lod['lods'])
    }
    extra = [pa.field(mapping[name][f.name], pa.list_(f.type)) for name in mapping for f in fields]
    if set(file.schema_arrow.names) & {f.name for f in extra}:
        raise ValueError('source uses reserved companion column names')
    geo = copy.deepcopy(geo)
    geo['lod_columns'].update(encoding='packed_geometry', attribute_columns=mapping)
    metadata = dict(file.schema_arrow.metadata)
    metadata.pop(b'lod_spatial_index', None)
    metadata[b'geo'] = json.dumps(geo).encode()
    schema = pa.schema(list(file.schema_arrow) + extra, metadata=metadata)
    with tempfile.TemporaryDirectory(prefix='.lod-separate-', dir=out.parent) as tmp:
        path = Path(tmp) / 'output.parquet'
        with pq.ParquetWriter(
            path,
            schema,
            compression='zstd',
            compression_level=9,
            use_dictionary=False,
            write_page_index=True,
            data_page_size=65536,
            max_rows_per_page=2048,
        ) as writer:
            for g in range(file.metadata.num_row_groups):
                table = file.read_row_group(g)
                source = table[lod['column']].combine_chunks()
                geometries, companions = [], []
                for name, info in lod['lods'].items():
                    blobs, values = [], {f.name: [] for f in fields}
                    for blob in source.field(name).to_pylist():
                        if blob is None:
                            blobs.append(None)
                            for f in fields:
                                values[f.name].append(None)
                        else:
                            geometry, attrs = split_row(
                                blob, info['geometry_type'], fields, dictionaries
                            )
                            blobs.append(geometry)
                            for f in fields:
                                values[f.name].append(attrs[f.name])
                    geometries.append(pa.array(blobs, pa.binary()))
                    companions.extend(pa.array(values[f.name], pa.list_(f.type)) for f in fields)
                geometry = pa.StructArray.from_arrays(geometries, fields=list(source.type))
                columns = [
                    geometry if f.name == lod['column'] else table[f.name]
                    for f in file.schema_arrow
                ]
                writer.write_table(
                    pa.Table.from_arrays(columns + companions, schema=schema),
                    row_group_size=len(table),
                )
                log(f'  separated row group {g + 1}/{file.metadata.num_row_groups}')
        from ._layout import finalize_layout

        finalize_layout(path, out, log=log)
    return out
