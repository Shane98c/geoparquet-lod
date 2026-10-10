"""Check the contract a renderer relies on, without reading the whole file."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from .encoding import (
    LINE_GEOMETRY,
    POINT_GEOMETRY,
    POLYGON_GEOMETRY,
    Attribute,
    decode_attributes,
    decode_polygon_tree,
)
from .writer import LOD_KEY, LOD_VERSION


class ValidationError(Exception):
    pass


def validate(path: str | Path, sample_rows: int = 2000) -> dict:
    """Raise ValidationError on the first problem; return a summary otherwise."""
    f = pq.ParquetFile(path)
    md = f.metadata
    geo = json.loads(dict(f.schema_arrow.metadata or {}).get(b'geo', b'{}') or b'{}')
    lod = geo.get(LOD_KEY)
    if not lod:
        raise ValidationError(f'no geo.{LOD_KEY} metadata')
    major = str(lod.get('version', '')).split('.')[0]
    if major != LOD_VERSION.split('.')[0]:
        raise ValidationError(f'version {lod.get("version")} is not compatible with {LOD_VERSION}')
    column = lod.get('column', 'lod')
    names = f.schema_arrow.names
    if column not in names:
        raise ValidationError(f'payload column {column} missing')
    if 'bbox' not in names:
        raise ValidationError('bbox covering column missing')
    covering = geo.get('columns', {}).get(geo.get('primary_column', 'geometry'), {}).get('covering')
    if not covering:
        raise ValidationError('geo.columns.<geometry>.covering missing')
    attributes = [Attribute(a['name'], a['type']) for a in lod.get('attributes', [])]
    lods = lod.get('lods') or {}
    if not lods:
        raise ValidationError('no lods declared')
    struct = f.schema_arrow.field(column).type
    children = {struct.field(i).name for i in range(struct.num_fields)}
    for name, child in lods.items():
        if name not in children:
            raise ValidationError(f'lod {name} declared but column {column}.{name} missing')
        if child['geometry'] not in (POINT_GEOMETRY, POLYGON_GEOMETRY, LINE_GEOMETRY):
            raise ValidationError(f'lod {name} has unknown geometry encoding {child["geometry"]}')
        if child['geometry'] != POINT_GEOMETRY and ('scale' not in child or 'offset' not in child):
            raise ValidationError(f'shape lod {name} lacks scale/offset')
    prefix = lod.get('prefix')
    if prefix and not (0 <= prefix['row_group_end'] < md.num_row_groups):
        raise ValidationError('prefix row_group_end out of range')

    # statistics on the covering and page indexes on every column, in every row group
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        for i in range(rg.num_columns):
            c = rg.column(i)
            if c.path_in_schema.startswith('bbox.'):
                if not c.is_stats_set:
                    raise ValidationError(f'row group {g}: {c.path_in_schema} has no statistics')
                if not c.has_column_index:
                    raise ValidationError(f'row group {g}: {c.path_in_schema} has no column index')
            if not c.has_offset_index:
                raise ValidationError(f'row group {g}: {c.path_in_schema} has no offset index')
    if prefix:
        # prefix row groups hold only rows shown at the prefix zoom: every row has that LOD
        coarsest = next(n for n, b in lods.items() if prefix['zoom'] in b['zooms'])
        for g in range(prefix['row_group_end'] + 1):
            col = (
                f.read_row_group(g, columns=[column])
                .column(column)
                .combine_chunks()
                .field(coarsest)
            )
            if col.null_count:
                raise ValidationError(
                    f'prefix row group {g} holds rows not shown at z{prefix["zoom"]}'
                )

    # decode a sample of every LOD and require exact byte consumption
    head = f.read_row_group(0, columns=[column]).slice(0, sample_rows)
    checked = {}
    for name, child in lods.items():
        values = head.column(column).combine_chunks().field(name)
        non_null = [v.as_py() for v in values if v.is_valid]
        for buf in non_null[:200]:
            _, pos = decode_attributes(buf, attributes)
            if child['geometry'] == POINT_GEOMETRY:
                pos += 8
            else:
                _, pos = decode_polygon_tree(buf, pos)
            if pos != len(buf):
                raise ValidationError(f'lod {name}: payload has {len(buf) - pos} trailing bytes')
        checked[name] = len(non_null)
    return {
        'rows': md.num_rows,
        'row_groups': md.num_row_groups,
        'footer_bytes': md.serialized_size,
        'lods': list(lods),
        'prefix': prefix,
        'sampled_non_null': checked,
    }


def inspect(path: str | Path) -> dict:
    """Metadata plus per-LOD column sizes in the first body row group."""
    f = pq.ParquetFile(path)
    md = f.metadata
    geo = json.loads(dict(f.schema_arrow.metadata or {}).get(b'geo', b'{}') or b'{}')
    lod = geo.get(LOD_KEY, {})
    g = min(md.num_row_groups - 1, (lod.get('prefix') or {}).get('row_group_end', -1) + 1)
    rg = md.row_group(g)
    sizes = {}
    for i in range(rg.num_columns):
        c = rg.column(i)
        sizes[c.path_in_schema] = int(c.total_compressed_size)
    return {
        'rows': md.num_rows,
        'row_groups': md.num_row_groups,
        'footer_bytes': md.serialized_size,
        'lod_columns': lod,
        'row_group_sampled': g,
        'compressed_bytes_by_column': sizes,
        'rows_in_sampled_group': rg.num_rows,
    }


def summarize_sizes(sizes: dict, rows: int) -> list[tuple[str, float]]:
    return [(k, v / max(rows, 1)) for k, v in sizes.items()]


__all__ = ['ValidationError', 'inspect', 'validate', 'np']
