"""Check the contract a renderer relies on, without reading the whole file."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .encoding import COORDINATES, DELTAS, ENCODING, LIST_DEPTH, geometry_rows, geometry_type
from .tiledir import KEY as TILE_DIRECTORY_KEY
from .tiledir import sidecar_path, source_binding
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
    major, minor = (str(lod.get('version', '')).split('.') + [''])[:2]
    ours = LOD_VERSION.split('.')
    if major != ours[0] or (major == '0' and minor != ours[1]):
        raise ValidationError(f'version {lod.get("version")} is not compatible with {LOD_VERSION}')
    if lod.get('encoding') != ENCODING:
        raise ValidationError(f'geometry encoding {lod.get("encoding")!r} is not {ENCODING}')
    if lod.get('coordinates') != COORDINATES:
        raise ValidationError(f'coordinate layout {lod.get("coordinates")!r} is not {COORDINATES}')
    if lod.get('deltas') != DELTAS:
        raise ValidationError(f'coordinate deltas {lod.get("deltas")!r} are not {DELTAS}')
    if md.num_row_groups == 0 or md.num_rows == 0:
        raise ValidationError('empty file: an empty dataset carries no LOD columns')
    column = lod.get('column', 'lod')
    names = f.schema_arrow.names
    if column not in names:
        raise ValidationError(f'LOD column {column} missing')
    if 'bbox' not in names:
        raise ValidationError('bbox covering column missing')
    covering = geo.get('columns', {}).get(geo.get('primary_column', 'geometry'), {}).get('covering')
    if not covering:
        raise ValidationError('geo.columns.<geometry>.covering missing')
    attributes = [a['name'] for a in lod.get('attributes', [])]
    lods = lod.get('lods') or {}
    if not lods:
        raise ValidationError('no lods declared')
    struct = f.schema_arrow.field(column)
    if struct.nullable:
        raise ValidationError(f'{column} must be a required struct')
    children = {
        struct.type.field(i).name: struct.type.field(i) for i in range(struct.type.num_fields)
    }
    for name, child in lods.items():
        if name not in children:
            raise ValidationError(f'lod {name} declared but column {column}.{name} missing')
        gtype = child.get('geometry_type')
        if gtype not in LIST_DEPTH:
            raise ValidationError(f'lod {name} has unknown geometry_type {gtype!r}')
        field = children[name]
        if not field.nullable or not pa.types.is_struct(field.type):
            raise ValidationError(f'{column}.{name} must be a nullable struct')
        try:
            geom = field.type.field('geometry')
        except KeyError:
            raise ValidationError(f'{column}.{name} has no geometry child') from None
        if geom.nullable or geom.type != geometry_type(gtype):
            raise ValidationError(f'{column}.{name}.geometry is not {ENCODING} {gtype}')
        for a in attributes:
            if a not in field.type.names:
                raise ValidationError(f'{column}.{name} lacks carried attribute {a}')
            if pa.types.is_nested(field.type.field(a).type):
                raise ValidationError(f'{column}.{name}.{a} is nested; attributes are scalars')
        for key in ('scale', 'offset'):
            v = child.get(key)
            if not (isinstance(v, list) and len(v) == 2):
                raise ValidationError(f'lod {name}: {key} must be an [x, y] pair')
        if min(child['scale']) <= 0:
            raise ValidationError(f'lod {name}: scale must be positive')
        placeholder = child.get('placeholder')
        if not (isinstance(placeholder, (int, float)) and placeholder > 0):
            raise ValidationError(f'lod {name}: placeholder must be a positive width in grid cells')
    prefix = lod.get('prefix')
    if prefix and not (0 <= prefix['row_group_end'] < md.num_row_groups):
        raise ValidationError('prefix row_group_end out of range')

    directory = check_tile_directory(path)

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

    # a sample of every LOD: non-empty parts, closed rings
    head = f.read_row_group(0, columns=[column]).slice(0, sample_rows)
    checked = {}
    for name, child in lods.items():
        values = head.column(column).combine_chunks().field(name)
        shown = values.filter(values.is_valid())
        geoms = geometry_rows(shown.field('geometry').slice(0, 200))
        for g in geoms:
            _check_geometry(name, child['geometry_type'], g)
        checked[name] = len(shown)
    return {
        'rows': md.num_rows,
        'row_groups': md.num_row_groups,
        'footer_bytes': md.serialized_size,
        'lods': list(lods),
        'prefix': prefix,
        'sampled_non_null': checked,
        'tile_directory': directory,
    }


def check_tile_directory(path: str | Path) -> str | None:
    """When a tile directory sits beside the file, check that it is bound to this file.
    Returns 'bound', or None when there is none."""
    sidecar = sidecar_path(path)
    if not sidecar.exists():
        return None
    meta = pq.ParquetFile(sidecar).metadata.metadata or {}
    header = json.loads(meta.get(TILE_DIRECTORY_KEY.encode(), b'{}'))
    if header.get('source') != source_binding(path):
        raise ValidationError(f'{sidecar.name} is bound to a different write of this file')
    return 'bound'


def _check_geometry(name: str, gtype: str, g) -> None:
    if gtype == 'Point':
        if len(g) != 2:
            raise ValidationError(f'lod {name}: a point is not an [x, y] pair')
        return
    if not g:
        raise ValidationError(f'lod {name}: empty geometry on a shown row')
    for part in g:
        rings = part if gtype == 'MultiPolygon' else [part]
        if not rings:
            raise ValidationError(f'lod {name}: empty polygon')
        for ring in rings:
            if len(ring) < 2:
                raise ValidationError(f'lod {name}: ring with fewer than two vertices')
            if any(len(v) != 2 for v in ring):
                raise ValidationError(f'lod {name}: a vertex is not an [x, y] pair')
            if gtype == 'MultiPolygon' and ring[0] != ring[-1]:
                raise ValidationError(f'lod {name}: unclosed ring')


def inspect(path: str | Path) -> dict:
    """Metadata plus per-leaf column sizes in the first body row group."""
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


__all__ = ['ValidationError', 'inspect', 'validate']
