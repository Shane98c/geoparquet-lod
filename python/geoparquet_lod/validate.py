"""Check the contract a renderer relies on, without reading the whole file."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .encoding import ENCODING, PACK_VERSION
from .pack import FAMILY_DEPTH, Unpacker
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
    if lod.get('encoding') not in (ENCODING, 'packed_geometry'):
        raise ValidationError(f'geometry encoding {lod.get("encoding")!r} is not {ENCODING}')
    if lod.get('pack_version') != PACK_VERSION:
        raise ValidationError(f'pack_version {lod.get("pack_version")!r} is not {PACK_VERSION}')
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
        # prefix row groups hold only rows shown at some zoom at or below the prefix zoom
        coarse = [n for n, b in lods.items() if min(b['zooms']) <= prefix['zoom']]
        for g in range(prefix['row_group_end'] + 1):
            col = f.read_row_group(g, columns=[column]).column(column).combine_chunks()
            shown = None
            for n in coarse:
                valid = col.field(n).is_valid()
                shown = valid if shown is None else pc.or_(shown, valid)
            if shown is not None and not pc.all(shown).as_py():
                raise ValidationError(
                    f'prefix row group {g} holds rows not shown by z{prefix["zoom"]}'
                )

    separate = lod.get('encoding') == 'packed_geometry'
    companions = lod.get('attribute_columns', {})
    if separate:
        for name in lods:
            for attr in lod.get('attributes', []):
                column_name = companions.get(name, {}).get(attr['name'])
                if not column_name or column_name not in f.schema_arrow.names:
                    raise ValidationError(f'lod {name}: missing companion for {attr["name"]}')
                typ = f.schema_arrow.field(column_name).type
                if not pa.types.is_list(typ) or typ.value_type != pa.type_for_alias(attr['type']):
                    raise ValidationError(f'lod {name}: invalid companion type for {attr["name"]}')

    # a sample of every LOD: non-empty parts, closed rings
    projected = [column] + (
        [c for cols in companions.values() for c in cols.values()] if separate else []
    )
    head = f.read_row_group(0, columns=projected).slice(0, sample_rows)
    fields = [pa.field(a['name'], pa.type_for_alias(a['type'])) for a in lod.get('attributes', [])]
    dictionaries = {a['name']: a['values'] for a in lod.get('attributes', []) if 'values' in a}
    if separate:
        fields = []
    checked = {}
    for name, child in lods.items():
        values = head.column(column).combine_chunks().field(name)
        shown = values.filter(values.is_valid())
        for index, blob in enumerate(values.slice(0, 200).to_pylist()):
            if blob is None:
                if separate and any(
                    head[c][index].as_py() is not None for c in companions[name].values()
                ):
                    raise ValidationError(f'lod {name}: attributes on an absent geometry row')
                continue
            u = Unpacker(blob, child['geometry_type'], fields, dictionaries)
            row = u.row()
            if u.pos != len(blob):
                raise ValidationError(f'lod {name}: a packed row has trailing bytes')
            if separate:
                for c in companions[name].values():
                    value = head[c][index].as_py()
                    if value is None or len(value) != 1 + len(row['spill']):
                        raise ValidationError(f'lod {name}: companion slots do not match geometry')
            _check_geometry(name, child['geometry_type'], row['geometry'], bool(row['spill']))
            for copy in row['spill']:
                _check_geometry(name, child['geometry_type'], copy['geometry'])
        checked[name] = len(shown)
    return {
        'rows': md.num_rows,
        'row_groups': md.num_row_groups,
        'footer_bytes': md.serialized_size,
        'lods': list(lods),
        'prefix': prefix,
        'sampled_non_null': checked,
    }


def _check_geometry(name: str, gtype: str, g, hosts: bool = False) -> None:
    if gtype == 'Point':
        if len(g) != 2:
            raise ValidationError(f'lod {name}: a point is not an [x, y] pair')
        return
    if not g:
        # a row shown only to host straddler copies has no geometry of its own
        if hosts:
            return
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
