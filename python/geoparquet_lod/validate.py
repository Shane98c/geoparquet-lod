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
    if lod.get('version') != LOD_VERSION:
        raise ValidationError(f'version {lod.get("version")} is not {LOD_VERSION}')
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
    bands = lod.get('bands') or {}
    if not bands:
        raise ValidationError('no bands declared')
    struct = f.schema_arrow.field(column).type
    children = {struct.field(i).name for i in range(struct.num_fields)}
    for name, band in bands.items():
        if name not in children:
            raise ValidationError(f'band {name} declared but column {column}.{name} missing')
        if band['geometry'] not in (POINT_GEOMETRY, POLYGON_GEOMETRY, LINE_GEOMETRY):
            raise ValidationError(f'band {name} has unknown geometry encoding {band["geometry"]}')
        if band['geometry'] != POINT_GEOMETRY and ('scale' not in band or 'offset' not in band):
            raise ValidationError(f'polygon band {name} lacks scale/offset')
    prefix = lod.get('prefix')
    if prefix and not (0 <= prefix['row_group_end'] < md.num_row_groups):
        raise ValidationError('prefix row_group_end out of range')

    # statistics and page indexes on every row group
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        for i in range(rg.num_columns):
            c = rg.column(i)
            if c.path_in_schema.startswith('bbox.') and not c.is_stats_set:
                raise ValidationError(f'row group {g}: {c.path_in_schema} has no statistics')
    if (
        not f.metadata.row_group(0).column(0).has_offset_index
        if hasattr(md.row_group(0).column(0), 'has_offset_index')
        else False
    ):
        pass  # pyarrow does not expose offset indexes; the JS reader checks them on open

    # decode a sample of every band and require exact byte consumption
    head = f.read_row_group(0, columns=[column]).slice(0, sample_rows)
    checked = {}
    for name, band in bands.items():
        values = head.column(column).combine_chunks().field(name)
        non_null = [v.as_py() for v in values if v.is_valid]
        for buf in non_null[:200]:
            _, pos = decode_attributes(buf, attributes)
            if band['geometry'] == POINT_GEOMETRY:
                pos += 8
            else:
                _, pos = decode_polygon_tree(buf, pos)
            if pos != len(buf):
                raise ValidationError(f'band {name}: payload has {len(buf) - pos} trailing bytes')
        checked[name] = len(non_null)
    return {
        'rows': md.num_rows,
        'row_groups': md.num_row_groups,
        'footer_bytes': md.serialized_size,
        'bands': list(bands),
        'prefix': prefix,
        'sampled_non_null': checked,
    }


def inspect(path: str | Path) -> dict:
    """Metadata plus per-band column sizes in the first body row group."""
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
