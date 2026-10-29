"""Check the contract a renderer relies on, without reading the whole file."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .encoding import ENCODING, INT32, PACK_VERSION
from .pack import FAMILY_DEPTH
from .unpack import Unpacked, UnpackError, carried, lod_values, unpack_batches
from .writer import LOD_KEY, LOD_VERSION


class ValidationError(Exception):
    pass


def validate(path: str | Path, sample_rows: int = 2000, *, full: bool = False) -> dict:
    """Check metadata and a quick sample, or every packed row and index with full=True.

    Full mode streams one LOD column of one row group at a time and decodes it
    in batches. sample_rows bounds quick mode only.
    """
    if not isinstance(sample_rows, int) or sample_rows < 0:
        raise ValueError('sample_rows must be a nonnegative integer')
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
    if lod.get('pack_version') != PACK_VERSION:
        raise ValidationError(f'pack_version {lod.get("pack_version")!r} is not {PACK_VERSION}')
    if md.num_row_groups == 0 or md.num_rows == 0:
        raise ValidationError('empty file: an empty dataset carries no overviews')
    column = lod.get('column', 'lod')
    names = f.schema_arrow.names
    if column not in names:
        raise ValidationError(f'LOD column {column} missing')
    if 'bbox' not in names:
        raise ValidationError('bbox covering column missing')
    covering = geo.get('columns', {}).get(geo.get('primary_column', 'geometry'), {}).get('covering')
    if not covering:
        raise ValidationError('geo.columns.<geometry>.covering missing')
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
        if gtype not in FAMILY_DEPTH:
            raise ValidationError(f'lod {name} has unknown geometry_type {gtype!r}')
        field = children[name]
        if not field.nullable or not pa.types.is_binary(field.type):
            raise ValidationError(f'{column}.{name} must be a nullable binary column')
        for key in ('scale', 'offset'):
            v = child.get(key)
            if not (
                isinstance(v, list)
                and len(v) == 2
                and all(isinstance(x, (int, float)) and math.isfinite(x) for x in v)
            ):
                raise ValidationError(f'lod {name}: {key} must be an [x, y] pair')
        if min(child['scale']) <= 0:
            raise ValidationError(f'lod {name}: scale must be positive')
        placeholder = child.get('placeholder')
        if not (
            isinstance(placeholder, (int, float)) and math.isfinite(placeholder) and placeholder > 0
        ):
            raise ValidationError(f'lod {name}: placeholder must be a positive width in grid cells')

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
    # every LOD's packed rows: non-empty parts, rings of three or more vertices, valid copies
    fields, dictionaries = carried(lod)
    checked = dict.fromkeys(lods, 0)
    groups = range(md.num_row_groups) if full else range(1)
    for group in groups:
        for name, child in lods.items():
            values = lod_values(f, group, column, name)
            if not full:
                values = values.slice(0, sample_rows)
            try:
                for decoded in unpack_batches(values, child['geometry_type'], fields, dictionaries):
                    problem = _check_records(decoded, child, md.num_rows)
                    if problem:
                        record, message = problem
                        raise ValidationError(
                            f'row group {group}, row {decoded.row[record]}, lod {name}: {message}'
                        )
            except UnpackError as exc:
                raise ValidationError(
                    f'row group {group}, row {exc.row}, lod {name}: invalid packed row: {exc}'
                ) from exc
            checked[name] += len(values) - values.null_count
    if full:
        _check_spatial_index(path, f, lod)
    return {
        'rows': md.num_rows,
        'row_groups': md.num_row_groups,
        'footer_bytes': md.serialized_size,
        'lods': list(lods),
        'mode': 'full' if full else 'quick',
        'checked_non_null': checked,
    }


def _check_spatial_index(path, parquet, lod):
    from ._validate_index import check_index

    try:
        check_index(path, parquet, lod)
    except Exception as exc:
        raise ValidationError(f'spatial index: {exc}') from exc


def _check_records(u: Unpacked, child: dict, num_rows: int):
    """The first record breaking the geometry or copy rules, and how, or None."""
    found = []
    if child['geometry_type'] == 'Point':
        x, y = u.geometry
    else:
        found += _check_geometry(u, child['geometry_type'])
        x, y = u.geometry[3:]
    outside = np.flatnonzero((np.minimum(x, y) < INT32.min) | (np.maximum(x, y) > INT32.max))
    if len(outside):
        found.append((_record_of_vertex(u, outside[0]), 'a grid coordinate outside int32'))
    zoom = (u.tile >> np.uint64(48)).astype(np.int64)
    cell = u.tile & np.uint64((1 << 48) - 1)
    cells = np.uint64(1) << (2 * np.minimum(zoom, 24)).astype(np.uint64)
    tag = u.copy & (~np.isin(zoom, child['zooms']) | (cell >= cells))
    found += [(i, 'invalid spill tile tag') for i in np.flatnonzero(tag)[:1]]
    rows = u.copy & (u.id >= num_rows)
    found += [(i, 'spill row id out of range') for i in np.flatnonzero(rows)[:1]]
    return min(found, default=None)


def _check_geometry(u: Unpacked, gtype: str) -> list:
    """Empty geometry on a record that needs some, empty polygons, and rings
    and parts too short. A polygon ring's closing vertex is implicit, so it
    stores three vertices or more and decodes with one more."""
    geom, poly, ring = u.geometry[:3]
    found = []
    # a row shown only to host straddler copies has no geometry of its own
    hosts = np.append(u.copy[1:], False) & ~u.copy
    empty = np.flatnonzero((np.diff(geom) == 0) & ~hosts)
    if len(empty):
        what = 'a copy with empty geometry' if u.copy[empty[0]] else 'empty geometry on a shown row'
        found.append((empty[0], what))
    vertices = np.diff(ring)
    if gtype == 'MultiPolygon':
        polygons = np.flatnonzero(np.diff(poly) == 0)
        if len(polygons):
            found.append((_parent(geom, polygons[0]), 'empty polygon'))
        short = np.flatnonzero(vertices < 4)
        what = 'polygon ring with fewer than three vertices'
    else:
        short = np.flatnonzero(vertices < 2)
        what = 'line with fewer than two vertices'
    if len(short):
        found.append((_parent(geom, _parent(poly, short[0])), what))
    return found


def _parent(offsets: np.ndarray, child: int) -> int:
    return int(np.searchsorted(offsets, child, side='right') - 1)


def _record_of_vertex(u: Unpacked, vertex: int) -> int:
    if len(u.geometry) == 2:
        return int(vertex)
    geom, poly, ring = u.geometry[:3]
    return _parent(geom, _parent(poly, _parent(ring, vertex)))


def inspect(path: str | Path) -> dict:
    """Metadata plus per-leaf column sizes in the first row group."""
    f = pq.ParquetFile(path)
    md = f.metadata
    geo = json.loads(dict(f.schema_arrow.metadata or {}).get(b'geo', b'{}') or b'{}')
    lod = geo.get(LOD_KEY, {})
    g = 0
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


__all__ = ['ValidationError', 'inspect', 'validate']
