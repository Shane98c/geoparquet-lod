"""Check the contract a renderer relies on, without reading the whole file."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

from .encoding import ENCODING, INT32, PACK_VERSION, is_carried_type
from .pack import FAMILY_DEPTH
from .unpack import Unpacked, UnpackError, carried, lod_values, unpack_batches
from .writer import LOD_KEY, LOD_VERSION

BOUNDS = ('xmin', 'ymin', 'xmax', 'ymax')
# rows of covering and primary geometry parsed at a time
COVERING_BATCH_ROWS = 65_536


class ValidationError(Exception):
    pass


def validate(path: str | Path, sample_rows: int = 2000, *, full: bool = False) -> dict:
    """Check metadata and a quick sample, or every packed row and index with full=True.

    The sample is the first sample_rows rows of the first row group: quick
    mode decodes their packed values, and both modes check that each one's
    covering is its primary geometry's bounds rounded outward to float32,
    null exactly where the geometry has no finite bounds: where it is null
    or empty, has no x or no y but NaN, or an infinite coordinate. Full mode
    checks every row's covering the same way, streams one LOD column of one
    row group at a time, decoding it in batches, and checks a row group at a
    time that the covering and the Hilbert key are null on the same rows.
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
    version = tuple(int(part) for part in re.findall(r'\d+', str(geo.get('version', '')))[:2])
    if not (1, 1) <= version < (2,):
        raise ValidationError(
            f'geo version {geo.get("version")!r} is not GeoParquet 1.1 or a later 1.x, '
            'which define the bbox covering'
        )
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
    _check_attributes(lod.get('attributes', []))

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
    _check_coverings(f, geometry, 0, sample_rows)
    # every LOD's packed rows: non-empty parts, rings of three or more vertices, valid copies
    fields, dictionaries = carried(lod)
    checked = dict.fromkeys(lods, 0)
    groups = range(md.num_row_groups) if full else range(1)
    for group in groups:
        if full:
            _check_key_covering(f, group, key_column)
            _check_coverings(f, geometry, group)
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
        raise ValidationError(
            'bbox must be nullable: a geometry without finite bounds has a null covering'
        )
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


def _check_attributes(attributes) -> None:
    """Raise unless every carried attribute has a name and the Arrow name of a
    type the packer writes."""
    if not isinstance(attributes, list) or not all(isinstance(a, dict) for a in attributes):
        raise ValidationError('attributes must be a list of {name, type} objects')
    for a in attributes:
        name, kind = a.get('name'), a.get('type')
        if not isinstance(name, str):
            raise ValidationError(f'attribute {json.dumps(a)} has no name')
        try:
            t = pa.type_for_alias(kind) if isinstance(kind, str) else None
        except ValueError:
            t = None
        if t is None or not is_carried_type(t):
            raise ValidationError(
                f'attribute {name}: type {json.dumps(kind)} is not an integer, float, bool '
                'or string type'
            )


def _check_coverings(
    parquet: pq.ParquetFile, geometry: str, group: int, rows: int | None = None
) -> None:
    """Raise unless, in the first `rows` rows of row group `group`, or all of
    them, each row's covering is its primary geometry's bounds rounded
    outward to float32, and null exactly where those bounds are not all
    finite. Reads COVERING_BATCH_ROWS rows at a time."""
    if rows == 0:
        return
    size = COVERING_BATCH_ROWS if rows is None else min(rows, COVERING_BATCH_ROWS)
    start = 0
    for batch in parquet.iter_batches(size, row_groups=[group], columns=['bbox', geometry]):
        if rows is not None:
            batch = batch.slice(0, rows - start)
        wkb = batch.column(geometry)
        null = wkb.is_null().to_numpy(zero_copy_only=False)
        shapes = shapely.from_wkb(wkb.to_numpy(zero_copy_only=False), on_invalid='ignore')
        invalid = np.flatnonzero(shapely.is_missing(shapes) & ~null)
        if len(invalid):
            row = start + invalid[0]
            raise ValidationError(
                f'row group {group}, row {row}: primary geometry is not valid WKB'
            )
        bounds = _bounds(shapes)
        present = np.isfinite(bounds).all(axis=1)
        bbox = batch.column('bbox')
        covered = bbox.is_valid().to_numpy(zero_copy_only=False)
        wrong = np.flatnonzero(covered != present)
        if len(wrong):
            row = wrong[0]
            if present[row]:
                message = 'null bbox covering on a geometry with finite bounds'
            elif null[row]:
                message = 'bbox covering on a null geometry'
            elif shapely.is_empty(shapes[row]):
                message = 'bbox covering on an empty geometry'
            else:
                message = 'bbox covering on a geometry without finite bounds'
            raise ValidationError(f'row group {group}, row {start + row}: {message}')
        stored = np.column_stack([bbox.field(k).to_numpy(zero_copy_only=False) for k in BOUNDS])
        exact = bounds[present]
        expected = _outward(exact)
        stored = stored[present]
        differs = stored != expected
        bad = np.flatnonzero(differs.any(axis=1))
        if len(bad):
            i = bad[0]
            k = int(np.argmax(differs[i]))
            row = start + np.flatnonzero(present)[i]
            raise ValidationError(
                f'row group {group}, row {row}: bbox.{BOUNDS[k]} is {float(stored[i, k])!r}, '
                f"not its geometry's {float(exact[i, k])!r} rounded outward to float32, "
                f'{float(expected[i, k])!r}'
            )
        start += len(batch)
        if rows is not None and start >= rows:
            break


def _bounds(shapes: np.ndarray) -> np.ndarray:
    """Rows of each geometry's `xmin, ymin, xmax, ymax`: the least and
    greatest x and y of its coordinates, NaN values left out one axis at a
    time, and NaN for an axis with none."""
    coords, index = shapely.get_coordinates(shapes, return_index=True)
    out = np.full((len(shapes), 4), np.nan)
    if len(coords):
        starts = np.flatnonzero(np.r_[True, index[1:] != index[:-1]])
        for k, (reduce, axis) in enumerate(
            ((np.fmin, 0), (np.fmin, 1), (np.fmax, 0), (np.fmax, 1))
        ):
            out[index[starts], k] = reduce.reduceat(coords[:, axis], starts)
    return out


def _outward(bounds: np.ndarray) -> np.ndarray:
    """Rows of float64 `xmin, ymin, xmax, ymax` rounded outward to float32:
    each minimum to the greatest float32 at most it, each maximum to the
    least float32 at least it."""
    rounded = bounds.astype(np.float32)
    outward = np.array([-np.inf, -np.inf, np.inf, np.inf], np.float32)
    past = np.where(outward < 0, rounded > bounds, rounded < bounds)
    return np.where(past, np.nextafter(rounded, outward), rounded)


def _check_key_covering(parquet: pq.ParquetFile, group: int, key_column: str) -> None:
    """Raise unless, in row group `group`, the covering and the Hilbert key
    are null on the same rows: those whose geometry has no finite bounds."""
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
