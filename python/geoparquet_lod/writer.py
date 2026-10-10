"""Write a GeoParquet file with per-zoom display payload columns.

Layout, in order of what matters:

1. Rows sorted once along a Hilbert curve, in large row groups, so a map
   tile touches one or two.
2. The rows shown at the coarsest zooms (up to `prefix_zoom`) come first in
   their own curve order, so a whole-dataset view reads one row group.
3. A float32 bbox covering with page statistics, and offset indexes on every
   column, so pages can be chosen without reading data.
4. Small byte-sized pages with v1 page headers, so sparse columns get long
   cheap pages and dense ones short pages.
5. One nullable binary column per zoom band under `lod`, non-null exactly
   for the rows shown there, holding the carried attributes and the geometry
   at that band's resolution.

Everything the renderer reads is in 3 and 5. A plain reader sees ordinary
GeoParquet with two extra columns.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .encoding import (
    POINT_GEOMETRY,
    POLYGON_GEOMETRY,
    Attribute,
    interleave,
    pack_attributes,
    point_bytes,
    polygon_tree_bytes,
)
from .quantize import render_polygons, resolution
from .thinning import Thinning

LOD_KEY = 'lod_columns'
LOD_VERSION = '0.2.0'
BBOX_FIELD = pa.field(
    'bbox',
    pa.struct(
        [pa.field(k, pa.float32(), nullable=False) for k in ('xmin', 'ymin', 'xmax', 'ymax')]
    ),
    nullable=False,
)


def connect(memory_limit: str | None = None, temp_dir: str | None = None):
    con = duckdb.connect()
    con.execute('INSTALL spatial; LOAD spatial;')
    if memory_limit:
        con.execute(f"SET memory_limit = '{memory_limit}'")
    if temp_dir:
        con.execute(f"SET temp_directory = '{temp_dir}'")
    con.execute('SET preserve_insertion_order = false')
    return con


def infer_attributes(schema: pa.Schema, carry) -> list[Attribute]:
    """`carry` is a list of names, (name, type) pairs, or Attribute; names get a wire type from Arrow."""
    out = []
    for item in carry:
        if isinstance(item, Attribute):
            out.append(item)
            continue
        if isinstance(item, tuple):
            out.append(Attribute(*item))
            continue
        t = schema.field(item).type
        if pa.types.is_floating(t):
            wire = 'f32'
        elif pa.types.is_integer(t):
            wire = 'i32' if pa.types.is_signed_integer(t) else 'u32'
        elif pa.types.is_string(t) or pa.types.is_large_string(t):
            wire = 'utf8'
        elif pa.types.is_list(t) and pa.types.is_floating(t.value_type):
            wire = 'f32[]'
        elif pa.types.is_list(t) and pa.types.is_integer(t.value_type):
            wire = 'i32[]'
        else:
            raise ValueError(f'cannot carry column {item} of type {t}; give an explicit wire type')
        out.append(Attribute(item, wire))
    return out


def bands(zooms: list[int], polygons_from: int | None, geometry_family: str) -> list[dict]:
    """Zoom bands: one point band per zoom below `polygons_from`, then pairs of
    zooms sharing a polygon band on the finer zoom's grid. Point datasets get
    point bands at every zoom."""
    out = []
    if geometry_family == 'point' or polygons_from is None:
        return [
            {'name': f'p{z}', 'zooms': [z], 'geometry': POINT_GEOMETRY, 'grid_zoom': None}
            for z in zooms
        ]
    for z in zooms:
        if z < polygons_from:
            out.append(
                {'name': f'p{z}', 'zooms': [z], 'geometry': POINT_GEOMETRY, 'grid_zoom': None}
            )
    z = polygons_from
    while z <= zooms[-1]:
        pair = [z] + ([z + 1] if z + 1 <= zooms[-1] else [])
        out.append(
            {'name': f'f{z}', 'zooms': pair, 'geometry': POLYGON_GEOMETRY, 'grid_zoom': pair[-1]}
        )
        z += 2
    return out


def _bbox_float32(column: pa.ChunkedArray) -> pa.Array:
    """Covering bbox in float32, rounded outward so it still contains the geometry."""
    import pyarrow.compute as pc

    parts = []
    for name, outward in (('xmin', -1), ('ymin', -1), ('xmax', 1), ('ymax', 1)):
        exact = pc.struct_field(column, name).to_numpy(zero_copy_only=False).astype(np.float64)
        v = exact.astype(np.float32)
        tight = (v > exact) if outward < 0 else (v < exact)
        v[tight] = np.nextafter(v[tight], np.float32(outward * np.inf))
        parts.append(pa.array(v, pa.float32()))
    return pa.StructArray.from_arrays(parts, fields=list(BBOX_FIELD.type))


def _read_schema(src: str) -> pa.Schema:
    """Arrow schema with geo metadata; a glob reads its first match."""
    if any(ch in src for ch in '*?['):
        import glob

        matches = sorted(glob.glob(src))
        if not matches:
            raise FileNotFoundError(src)
        return pq.read_schema(matches[0])
    return pq.read_schema(src)


def _render(args):
    wkb, cx, cy, grids = args
    return [
        polygon_tree_bytes(render_polygons(wkb, cx, cy, scale, offset)) for scale, offset in grids
    ]


def write(
    src: str,
    out: str | Path,
    *,
    thinning: Thinning,
    zooms=range(2, 17),
    polygons_from: int | None = 13,
    carry=(),
    geometry: str = 'geometry',
    prefix_zoom: int | None = 6,
    row_group_rows: int = 1_048_576,
    page_bytes: int = 4096,
    batch_rows: int | None = None,
    workers: int | None = None,
    memory_limit: str | None = None,
    temp_dir: str | None = None,
    log=print,
) -> Path:
    """Write `src` (any GeoParquet with a WKB geometry column) to `out` in the
    LOD columns layout. Returns the output path."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    zooms = list(zooms)
    t0 = time.time()
    con = connect(memory_limit, temp_dir)
    source_schema = _read_schema(src)
    attributes = infer_attributes(source_schema, carry)
    geo = json.loads(dict(source_schema.metadata or {}).get(b'geo', b'{}') or b'{}')
    family = (
        'point'
        if any(
            t.lower().startswith('point')
            for t in geo.get('columns', {}).get(geometry, {}).get('geometry_types', [])
        )
        else 'polygon'
    )
    band_plan = bands(zooms, polygons_from, family)

    # 1. a sorted, slim copy with centroid, diagonal and Hilbert key
    xmin, ymin, xmax, ymax = con.execute(
        f'SELECT min(ST_XMin({geometry})), min(ST_YMin({geometry})), max(ST_XMax({geometry})), max(ST_YMax({geometry})) '
        f"FROM read_parquet('{src}')"
    ).fetchone()
    box = f'{{min_x:{xmin}, min_y:{ymin}, max_x:{xmax}, max_y:{ymax}}}::BOX_2D'
    sorted_path = out.with_suffix('.sorted.parquet')
    log(f'sorting {src}')
    con.execute(
        f"""
        COPY (
            SELECT row_number() OVER () AS id,
                ST_X(ST_Centroid({geometry}))::FLOAT AS cx, ST_Y(ST_Centroid({geometry}))::FLOAT AS cy,
                sqrt((ST_XMax({geometry}) - ST_XMin({geometry})) ^ 2 + (ST_YMax({geometry}) - ST_YMin({geometry})) ^ 2) AS diag,
                {{xmin: ST_XMin({geometry}), ymin: ST_YMin({geometry}), xmax: ST_XMax({geometry}), ymax: ST_YMax({geometry})}} AS bbox_,
                ST_Hilbert({geometry}, {box}) AS h,
                * EXCLUDE (bbox)
            FROM (SELECT * FROM read_parquet('{src}') ORDER BY ST_Hilbert({geometry}, {box}))
        ) TO '{sorted_path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {row_group_rows})
    """
        if 'bbox' in source_schema.names
        else f"""
        COPY (
            SELECT row_number() OVER () AS id,
                ST_X(ST_Centroid({geometry}))::FLOAT AS cx, ST_Y(ST_Centroid({geometry}))::FLOAT AS cy,
                sqrt((ST_XMax({geometry}) - ST_XMin({geometry})) ^ 2 + (ST_YMax({geometry}) - ST_YMin({geometry})) ^ 2) AS diag,
                {{xmin: ST_XMin({geometry}), ymin: ST_YMin({geometry}), xmax: ST_XMax({geometry}), ymax: ST_YMax({geometry})}} AS bbox_,
                ST_Hilbert({geometry}, {box}) AS h,
                *
            FROM (SELECT * FROM read_parquet('{src}') ORDER BY ST_Hilbert({geometry}, {box}))
        ) TO '{sorted_path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {row_group_rows})
    """
    )
    n_rows = con.execute(f"SELECT count(*) FROM '{sorted_path}'").fetchone()[0]
    log(f'  {n_rows:,} rows sorted, {time.time() - t0:.0f}s')

    # 2. which rows appear at which zoom
    slim = ', '.join(
        dict.fromkeys(['id', 'cx', 'cy', 'diag', *[a.name for a in attributes], *thinning.columns])
    )
    con.execute(f"CREATE TEMP TABLE rows AS SELECT {slim} FROM '{sorted_path}'")
    # every row is drawn from the first footprint zoom, so thinning only decides the zooms below it
    sample_zooms = zooms[: zooms.index(polygons_from) + 1] if polygons_from in zooms else zooms
    thinning.assign(con, sample_zooms)
    counts = dict(con.execute('SELECT level, count(*) FROM rows GROUP BY 1 ORDER BY 1').fetchall())
    log(
        '  rows entering at each zoom: '
        + ', '.join(f'z{zooms[k]}={v:,}' for k, v in counts.items() if k is not None)
    )

    # 3. schema and metadata, known before writing
    origin = (float(xmin), float(ymin))
    degrees = (
        geo.get('columns', {}).get(geometry, {}).get('crs') is None
        or 'degree' in json.dumps(geo).lower()
    )
    grids = {
        b['name']: (resolution(b['grid_zoom'], degrees=degrees), origin)
        for b in band_plan
        if b['grid_zoom']
    }
    prefix_level = None if prefix_zoom is None else zooms.index(prefix_zoom)
    prefix_rows = (
        0
        if prefix_level is None
        else sum(v for k, v in counts.items() if k is not None and k <= prefix_level)
    )
    prefix_end = -(-prefix_rows // row_group_rows) - 1 if prefix_rows else -1
    lod_field = pa.field(
        'lod',
        pa.struct([pa.field(b['name'], pa.binary(), nullable=True) for b in band_plan]),
        nullable=False,
    )
    rest = [f for f in source_schema if f.name not in ('bbox', geometry)]
    schema = pa.schema([BBOX_FIELD, lod_field, *rest, source_schema.field(geometry)])
    geo.setdefault('version', '1.1.0')
    geo.setdefault('primary_column', geometry)
    geo.setdefault('columns', {}).setdefault(geometry, {'encoding': 'WKB', 'geometry_types': []})
    geo['columns'][geometry]['covering'] = {
        'bbox': {k: ['bbox', k] for k in ('xmin', 'ymin', 'xmax', 'ymax')}
    }
    geo[LOD_KEY] = {
        'version': LOD_VERSION,
        'column': 'lod',
        'order': 'hilbert',
        'zooms': zooms,
        'tile_size': 512,
        'prefix': None if prefix_end < 0 else {'zoom': prefix_zoom, 'row_group_end': prefix_end},
        'attributes': [{'name': a.name, 'type': a.type} for a in attributes],
        'thinning': thinning.describe(),
        'bands': {
            b['name']: {
                'zooms': b['zooms'],
                'geometry': b['geometry'],
                **(
                    {'scale': [grids[b['name']][0]] * 2, 'offset': list(origin)}
                    if b['grid_zoom']
                    else {}
                ),
            }
            for b in band_plan
        },
    }
    schema = schema.with_metadata(
        {**(source_schema.metadata or {}), b'geo': json.dumps(geo).encode()}
    )

    # 4. page sizing: write batches small enough that dense payload pages land near page_bytes
    if batch_rows is None:
        sample = con.execute(
            f"SELECT {geometry} FROM '{sorted_path}' LIMIT 2000"
        ).fetch_arrow_table()
        if family == 'polygon' and polygons_from is not None and sample.num_rows:
            wkb = sample.column(geometry).to_numpy(zero_copy_only=False)
            finest = band_plan[-1]
            cx0 = np.zeros(len(wkb))
            parts = render_polygons(wkb, cx0, cx0, *grids[finest['name']])
            _, offsets = polygon_tree_bytes(parts)
            avg = max(8, np.diff(offsets).mean() + sum(4 for _ in attributes))
        else:
            avg = 8 + 4 * len(attributes)
        batch_rows = int(min(1024, max(32, page_bytes // avg * 2)))
    options = dict(
        compression='zstd',
        use_dictionary=False,
        write_statistics=['bbox.xmin', 'bbox.ymin', 'bbox.xmax', 'bbox.ymax'],
        write_page_index=True,
        write_batch_size=batch_rows,
        data_page_size=page_bytes,
        data_page_version='1.0',
    )
    log(f'  {len(band_plan)} bands, batch {batch_rows} rows, prefix row groups {prefix_end + 1}')

    # 5. write: prefix rows first, then everything else, each in Hilbert order
    writer = pq.ParquetWriter(out, schema, **options)
    pool = ProcessPoolExecutor(max_workers=workers)
    row_groups = 0
    band_level = {b['name']: zooms.index(max(b['zooms'])) for b in band_plan}

    def flush(table: pa.Table, fut):
        nonlocal row_groups
        level = table.column('level').to_numpy(zero_copy_only=False)
        level = np.where(np.isnan(level.astype(float)), 10_000, level).astype(np.int64)
        attrs = pack_attributes(table, attributes)
        points = point_bytes(table.column('cx').to_numpy(), table.column('cy').to_numpy())
        rendered = fut.result() if fut is not None else []
        arrays = []
        k = 0
        for b in band_plan:
            mask = level <= band_level[b['name']]
            if b['geometry'] == POINT_GEOMETRY:
                arrays.append(interleave(table.num_rows, [attrs, points], mask))
            else:
                arrays.append(interleave(table.num_rows, [attrs, rendered[k]], mask))
                k += 1
        lod = pa.StructArray.from_arrays(arrays, [b['name'] for b in band_plan])
        cols = {name: table.column(name) for name in table.column_names}
        out_table = pa.table(
            {
                'bbox': _bbox_float32(cols['bbox_']),
                'lod': lod,
                **{f.name: cols[f.name] for f in rest},
                geometry: cols[geometry],
            }
        )
        writer.write_table(out_table.cast(schema), row_group_size=out_table.num_rows)
        row_groups += 1
        if row_groups % 10 == 0:
            log(f'  {row_groups} row groups, {time.time() - t0:.0f}s')

    def write_rows(where: str):
        reader = con.execute(f"""
            SELECT s.*, r.level FROM '{sorted_path}' s JOIN rows r USING (id)
            WHERE {where} ORDER BY s.h
        """).to_arrow_reader(row_group_rows)
        pending = []
        for batch in reader:
            table = pa.Table.from_batches([batch])
            fut = None
            if grids:
                job = (
                    table.column(geometry).to_numpy(zero_copy_only=False),
                    table.column('cx').to_numpy(),
                    table.column('cy').to_numpy(),
                    [grids[b['name']] for b in band_plan if b['grid_zoom']],
                )
                fut = pool.submit(_render, job)
            pending.append((table, fut))
            if len(pending) >= (workers or 8) * 2:
                flush(*pending.pop(0))
        for t, f in pending:
            flush(t, f)

    if prefix_end >= 0:
        write_rows(f'r.level <= {prefix_level}')
        if row_groups - 1 != prefix_end:
            raise RuntimeError(
                f'prefix wrote {row_groups} row groups, metadata expected {prefix_end + 1}'
            )
        write_rows(f'r.level > {prefix_level} OR r.level IS NULL')
    else:
        write_rows('TRUE')
    writer.close()
    pool.shutdown()
    sorted_path.unlink()
    log(
        f'{n_rows:,} rows, {row_groups} row groups, {out.stat().st_size / 1e9:.2f} GB, {time.time() - t0:.0f}s -> {out}'
    )
    return out
