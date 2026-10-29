"""Check the contract a renderer relies on, without reading the whole file."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

from .encoding import ENCODING, INT32, PACK_VERSION
from .pack import FAMILY_DEPTH
from .unpack import Unpacked, UnpackError, carried, lod_values, unpack_batches
from .writer import LOD_KEY, LOD_VERSION

BOUNDS = ('xmin', 'ymin', 'xmax', 'ymax')


class ValidationError(Exception):
    pass


def validate(path: str | Path, sample_rows: int = 2000, *, full: bool = False) -> dict:
    """Check metadata and a quick sample, or every packed row and index with full=True.

    The sample is the first sample_rows rows of the first row group: quick
    mode decodes their packed values, and both modes check that their
    covering is null exactly where their primary geometry is null or empty.
    Full mode streams one LOD column of one row group at a time, decoding it
    in batches, and checks a row group at a time that the covering and the
    Hilbert key are null on the same rows.
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
    geometry = geo.get('primary_column', 'geometry')
    if geometry not in names:
        raise ValidationError(f'primary geometry column {geometry} missing')
    wkb = f.schema_arrow.field(geometry).type
    if not (pa.types.is_binary(wkb) or pa.types.is_large_binary(wkb)):
        raise ValidationError(f'primary geometry column {geometry} must be WKB binary, not {wkb}')
    if 'bbox' not in names:
        raise ValidationError('bbox covering column missing')
    covering = geo.get('columns', {}).get(geometry, {}).get('covering')
    if not covering:
        raise ValidationError(f'geo.columns.{geometry}.covering missing')
    _check_covering(f.schema_arrow.field('bbox'), covering, geometry)
    key = lod.get('key')
    key_column = key.get('column') if isinstance(key, dict) else None
    if key_column not in names:
        raise ValidationError(f'key column {key_column} missing')
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
    _check_sample_covering(f, geometry, sample_rows)
    # every LOD's packed rows: non-empty parts, rings of three or more vertices, valid copies
    fields, dictionaries = carried(lod)
    checked = dict.fromkeys(lods, 0)
    groups = range(md.num_row_groups) if full else range(1)
    for group in groups:
        if full:
            _check_key_covering(f, group, key_column)
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


def _check_covering(field: pa.Field, covering, geometry: str) -> None:
    """Raise unless `field` is a nullable struct of four required float32
    bounds and the geometry's `covering` declares each at its own path."""
    if not pa.types.is_struct(field.type):
        raise ValidationError(f'bbox must be a struct column, not {field.type}')
    if not field.nullable:
        raise ValidationError('bbox must be nullable: null or empty geometry has a null covering')
    names = [child.name for child in field.type]
    if sorted(names) != sorted(BOUNDS):
        raise ValidationError(
            f'bbox fields must be xmin, ymin, xmax and ymax, not {", ".join(names)}'
        )
    for child in field.type:
        if child.type != pa.float32():
            raise ValidationError(f'bbox.{child.name} must be float32, not {child.type}')
        if child.nullable:
            raise ValidationError(f'bbox.{child.name} must be required, not nullable')
    where = f'geo.columns.{geometry}.covering.bbox'
    declared = covering.get('bbox') if isinstance(covering, dict) else None
    if not isinstance(declared, dict) or sorted(declared) != sorted(BOUNDS):
        raise ValidationError(
            f'{where} must declare xmin, ymin, xmax and ymax, not {json.dumps(declared)}'
        )
    for bound in BOUNDS:
        if declared[bound] != ['bbox', bound]:
            raise ValidationError(
                f'{where}.{bound} must be ["bbox", "{bound}"], not {json.dumps(declared[bound])}'
            )


def _check_sample_covering(parquet: pq.ParquetFile, geometry: str, rows: int) -> None:
    """Raise unless, in the first `rows` rows of the first row group, the
    covering is null exactly where the primary geometry is null or empty."""
    if not rows:
        return
    batch = next(parquet.iter_batches(rows, row_groups=[0], columns=['bbox', geometry]), None)
    if batch is None:
        return
    wkb = batch.column(geometry)
    null = wkb.is_null().to_numpy(zero_copy_only=False)
    shapes = shapely.from_wkb(wkb.to_numpy(zero_copy_only=False), on_invalid='ignore')
    invalid = np.flatnonzero(shapely.is_missing(shapes) & ~null)
    if len(invalid):
        raise ValidationError(f'row group 0, row {invalid[0]}: primary geometry is not valid WKB')
    present = ~null & ~shapely.is_empty(shapes)
    covered = batch.column('bbox').is_valid().to_numpy(zero_copy_only=False)
    wrong = np.flatnonzero(covered != present)
    if len(wrong):
        row = wrong[0]
        if present[row]:
            message = 'null bbox covering on a non-empty geometry'
        else:
            message = f'bbox covering on {"a null" if null[row] else "an empty"} geometry'
        raise ValidationError(f'row group 0, row {row}: {message}')


def _check_key_covering(parquet: pq.ParquetFile, group: int, key_column: str) -> None:
    """Raise unless, in row group `group`, the covering and the Hilbert key
    are null on the same rows: those of null or empty geometry."""
    table = parquet.read_row_group(group, columns=['bbox', key_column])
    covered = table.column('bbox').is_valid().to_numpy()
    keyed = table.column(key_column).is_valid().to_numpy()
    wrong = np.flatnonzero(covered != keyed)
    if len(wrong):
        row = wrong[0]
        if keyed[row]:
            message = 'null bbox covering on a row with a Hilbert key'
        else:
            message = 'bbox covering on a row with a null Hilbert key'
        raise ValidationError(f'row group {group}, row {row}: {message}')


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
