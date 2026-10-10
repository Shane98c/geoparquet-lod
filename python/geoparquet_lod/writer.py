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
5. One nullable binary column per LOD under `lod`, non-null exactly
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
    LINE_GEOMETRY,
    POINT_GEOMETRY,
    POLYGON_GEOMETRY,
    Attribute,
    interleave,
    pack_attributes,
    point_bytes,
    polygon_tree_bytes,
)
from .quantize import render_lines, render_polygons, resolution
from .thinning import Context, Thin, describe

LOD_KEY = 'lod_columns'
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


def lods(
    zooms: list[int],
    polygons_from: int | None,
    geometry_family: str,
    single_shape_lod: bool = False,
) -> list[dict]:
    """LODs: one stored representation per contiguous zoom range. Zooms may share
    an LOD only when row selection and carried attributes agree, and an LOD uses
    its finest zoom's grid. Below `polygons_from` the thinning changes the row
    set every zoom, so each zoom is its own point LOD; from `polygons_from` up
    every row is present, so zooms are paired on one quantized copy, or all
    share one copy when the full-detail payload is small enough that copies
    would cost more than they save. Point datasets get point LODs at every zoom."""
    point = lambda z: {'name': f'p{z}', 'zooms': [z], 'geometry': POINT_GEOMETRY, 'grid_zoom': None}  # noqa: E731
    if geometry_family == 'point' or polygons_from is None:
        return [point(z) for z in zooms]
    shape, prefix = (LINE_GEOMETRY, 'l') if geometry_family == 'line' else (POLYGON_GEOMETRY, 'f')
    out = [point(z) for z in zooms if z < polygons_from]
    shape_zooms = [z for z in zooms if z >= polygons_from]
    if single_shape_lod:
        out.append(
            {
                'name': f'{prefix}{shape_zooms[0]}',
                'zooms': shape_zooms,
                'geometry': shape,
                'grid_zoom': shape_zooms[-1],
            }
        )
        return out
    z = polygons_from
    while z <= zooms[-1]:
        pair = [z] + ([z + 1] if z + 1 <= zooms[-1] else [])
        out.append(
            {'name': f'{prefix}{z}', 'zooms': pair, 'geometry': shape, 'grid_zoom': pair[-1]}
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


def guess_zooms(con, sorted_path, geometry, family, zooms, polygons_from, extent, degrees, log):
    """Tippecanoe's `-zg` in spirit: the coarsest zoom is where the extent fits
    a tile, footprints start where the median feature spans a pixel, and the
    finest zoom is where the median vertex spacing reaches a pixel. Explicit
    values are kept; `'auto'` is replaced and logged."""
    import math

    import shapely

    from .quantize import resolution as res

    def zoom_where(size: float) -> int:
        # first zoom whose pixel is no larger than `size`
        return max(0, min(22, math.ceil(math.log2(res(0, degrees=degrees) / max(size, 1e-12)))))

    auto_zooms = zooms == 'auto'
    auto_from = polygons_from == 'auto'
    if auto_zooms or auto_from:
        sample = con.execute(
            f"SELECT diag, {geometry} FROM '{sorted_path}' USING SAMPLE 5000 ROWS"
        ).fetch_arrow_table()
        diag = float(np.median(sample.column('diag').to_numpy())) if sample.num_rows else 0.0
        geoms = shapely.from_wkb(sample.column(geometry).to_numpy(zero_copy_only=False))
        coords, index = shapely.get_coordinates(geoms, return_index=True)
        steps = np.hypot(*np.diff(coords, axis=0).T)
        same = index[1:] == index[:-1]
        spacing = float(np.median(steps[same])) if same.any() else 0.0
        extent_size = max(extent[2] - extent[0], extent[3] - extent[1])
        coarsest = max(0, zoom_where(extent_size) - 1)
        finest = min(18, max(coarsest + 1, zoom_where(spacing) if spacing else 16))
        first_polygon = zoom_where(diag) if family != 'point' and diag else None
        if auto_from:
            polygons_from = None if family == 'point' else min(first_polygon, finest)
        if auto_zooms:
            zooms = list(range(coarsest, finest + 1))
        log(
            f'  zoom guess: extent {extent_size:.3g}, median feature {diag:.3g}, median vertex spacing {spacing:.3g} '
            f'-> zooms {zooms[0]}-{zooms[-1]}, footprints from {polygons_from}'
        )
    zooms = list(zooms)
    if polygons_from is not None and polygons_from not in zooms:
        polygons_from = None if polygons_from > zooms[-1] else zooms[0]
    return zooms, polygons_from


def _full_detail_bytes(
    con, sorted_path, geometry, family, finest_zoom, origin, degrees, n_rows
) -> float:
    """Estimated payload bytes of the whole dataset at the finest zoom, from a sample."""
    sample = con.execute(
        f"SELECT {geometry} FROM '{sorted_path}' USING SAMPLE 2000 ROWS"
    ).fetch_arrow_table()
    if not sample.num_rows:
        return 0.0
    wkb = sample.column(geometry).to_numpy(zero_copy_only=False)
    zeros = np.zeros(len(wkb))
    render = render_lines if family == 'line' else render_polygons
    _, offsets = polygon_tree_bytes(
        render(wkb, zeros, zeros, resolution(finest_zoom, degrees=degrees), origin)
    )
    return float(np.diff(offsets).mean()) * n_rows


def _read_schema(src: str) -> pa.Schema:
    """Arrow schema with geo metadata; a glob reads its first match."""
    if any(ch in src for ch in '*?['):
        import glob

        matches = sorted(glob.glob(src))
        if not matches:
            raise FileNotFoundError(src)
        return pq.read_schema(matches[0])
    return pq.read_schema(src)


def _with_overrides(table: pa.Table, ids: np.ndarray, override: pa.Table) -> pa.Table:
    """Replace carried attribute values for rows that a thinning overrode at this zoom."""
    import pyarrow.compute as pc

    idx = pc.index_in(pa.array(ids), value_set=override.column('id').combine_chunks())
    for name in override.column_names:
        if name == 'id' or name not in table.column_names:
            continue
        values = pc.take(override.column(name).combine_chunks(), idx)
        merged = pc.if_else(
            pc.is_valid(values), values.cast(table.column(name).type), table.column(name)
        )
        table = table.set_column(table.schema.get_field_index(name), name, merged)
    return table


def _render(args):
    wkb, cx, cy, grids, family = args
    render = render_lines if family == 'line' else render_polygons
    return [polygon_tree_bytes(render(wkb, cx, cy, scale, offset)) for scale, offset in grids]


def write(
    src: str,
    out: str | Path,
    *,
    thinning: Thin,
    columns=(),
    zooms='auto',
    polygons_from: int | None | str = 'auto',
    carry=(),
    geometry: str = 'geometry',
    prefix_zoom: int | None = 6,
    single_lod_bytes: int = 16 * 1024 * 1024,
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
    t0 = time.time()
    con = connect(memory_limit, temp_dir)
    source_schema = _read_schema(src)
    attributes = infer_attributes(source_schema, carry)
    geo = json.loads(dict(source_schema.metadata or {}).get(b'geo', b'{}') or b'{}')
    types = [t.lower() for t in geo.get('columns', {}).get(geometry, {}).get('geometry_types', [])]
    if not types:
        sample = con.execute(
            f"SELECT ST_GeometryType({geometry}) FROM read_parquet('{src}') LIMIT 1000"
        ).fetchall()
        types = [str(t[0]).lower() for t in sample]
    if types and all('point' in t for t in types):
        family = 'point'
    elif types and all('linestring' in t for t in types):
        family = 'line'
    elif types and all('polygon' in t for t in types):
        family = 'polygon'
    else:
        raise ValueError(
            f'mixed or unknown geometry types {sorted(set(types))}; one family per file'
        )

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
    crs = geo.get('columns', {}).get(geometry, {}).get('crs')
    degrees = crs is None or 'degree' in json.dumps(crs).lower()
    zooms, polygons_from = guess_zooms(
        con,
        sorted_path,
        geometry,
        family,
        zooms,
        polygons_from,
        (xmin, ymin, xmax, ymax),
        degrees,
        log,
    )
    single = False
    if family != 'point' and polygons_from is not None:
        single = (
            _full_detail_bytes(
                con, sorted_path, geometry, family, zooms[-1], (xmin, ymin), degrees, n_rows
            )
            <= single_lod_bytes
        )
        if single:
            log('  full-detail shapes are small; one shape LOD serves every zoom')
    lod_plan = lods(zooms, polygons_from, family, single_shape_lod=single)

    # 2. which rows appear at which zoom: run `thinning` from the finest sampled zoom down
    sample_zooms = zooms[: zooms.index(polygons_from) + 1] if polygons_from in zooms else zooms
    slim = ', '.join(
        dict.fromkeys(['id', 'cx', 'cy', 'diag', *[a.name for a in attributes], *columns])
    )
    rows = con.execute(
        f"SELECT {slim}, (hash(id) % 1000000007) / 1000000007.0 AS u FROM '{sorted_path}' ORDER BY id"
    ).fetch_arrow_table()
    ctx = Context(tuple(zooms), sample_zooms[-1], 512, degrees)
    level = np.full(n_rows, len(sample_zooms) - 1, dtype=np.int32)
    overrides: dict[int, pa.Table] = {}
    kept = rows
    for i in range(len(sample_zooms) - 2, -1, -1):
        kept = thinning(kept, sample_zooms[i], ctx)
        ids = kept.column('id').to_numpy()
        level[ids - 1] = i
        carried = [a.name for a in attributes if a.name in kept.column_names]
        if carried:
            overrides[i] = kept.select(['id', *carried])
    con.register(
        'levels', pa.table({'id': np.arange(1, n_rows + 1, dtype=np.int64), 'level': level})
    )
    con.execute('CREATE TEMP TABLE rows AS SELECT * FROM levels')
    counts = dict(con.execute('SELECT level, count(*) FROM rows GROUP BY 1 ORDER BY 1').fetchall())
    log(
        '  rows entering at each zoom: '
        + ', '.join(f'z{zooms[k]}={v:,}' for k, v in counts.items())
    )

    # 3. schema and metadata, known before writing
    origin = (float(xmin), float(ymin))
    grids = {
        b['name']: (resolution(b['grid_zoom'], degrees=degrees), origin)
        for b in lod_plan
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
        pa.struct([pa.field(b['name'], pa.binary(), nullable=True) for b in lod_plan]),
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
        'column': 'lod',
        'order': 'hilbert',
        'zooms': zooms,
        'tile_size': 512,
        'prefix': None if prefix_end < 0 else {'zoom': prefix_zoom, 'row_group_end': prefix_end},
        'attributes': [{'name': a.name, 'type': a.type} for a in attributes],
        'thinning': describe(thinning),
        'lods': {
            b['name']: {
                'zooms': b['zooms'],
                'geometry': b['geometry'],
                **(
                    {'scale': [grids[b['name']][0]] * 2, 'offset': list(origin)}
                    if b['grid_zoom']
                    else {}
                ),
            }
            for b in lod_plan
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
        if family != 'point' and polygons_from is not None and sample.num_rows:
            wkb = sample.column(geometry).to_numpy(zero_copy_only=False)
            finest = lod_plan[-1]
            cx0 = np.zeros(len(wkb))
            parts = (render_lines if family == 'line' else render_polygons)(
                wkb, cx0, cx0, *grids[finest['name']]
            )
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
    log(f'  {len(lod_plan)} LODs, batch {batch_rows} rows, prefix row groups {prefix_end + 1}')

    # 5. write: prefix rows first, then everything else, each in Hilbert order
    writer = pq.ParquetWriter(out, schema, **options)
    pool = ProcessPoolExecutor(max_workers=workers)
    row_groups = 0
    lod_level = {b['name']: zooms.index(max(b['zooms'])) for b in lod_plan}

    def flush(table: pa.Table, fut):
        nonlocal row_groups
        level = table.column('level').to_numpy(zero_copy_only=False)
        level = np.where(np.isnan(level.astype(float)), 10_000, level).astype(np.int64)
        attrs = pack_attributes(table, attributes)
        points = point_bytes(table.column('cx').to_numpy(), table.column('cy').to_numpy())
        rendered = fut.result() if fut is not None else []
        ids = table.column('id').to_numpy()
        arrays = []
        k = 0
        for b in lod_plan:
            mask = level <= lod_level[b['name']]
            if b['geometry'] == POINT_GEOMETRY:
                z_index = zooms.index(b['zooms'][0])
                lod_attrs = attrs
                if z_index in overrides:
                    lod_attrs = pack_attributes(
                        _with_overrides(table, ids, overrides[z_index]), attributes
                    )
                arrays.append(interleave(table.num_rows, [lod_attrs, points], mask))
            else:
                arrays.append(interleave(table.num_rows, [attrs, rendered[k]], mask))
                k += 1
        lod = pa.StructArray.from_arrays(arrays, [b['name'] for b in lod_plan])
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
                    [grids[b['name']] for b in lod_plan if b['grid_zoom']],
                    family,
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
