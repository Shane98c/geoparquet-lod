"""Write a GeoParquet file with per-zoom display columns.

Layout, in order of what matters:

1. Rows sorted once along a Hilbert curve, in large row groups, so a map
   tile touches one or two.
2. The rows shown at the coarsest zooms (up to `prefix_zoom`) come first in
   their own curve order, so a whole-dataset view reads one row group.
3. A float32 bbox covering with page statistics, and offset indexes on every
   column, so pages can be chosen without reading data.
4. Pages of about 16 KB with v1 page headers: small enough that a tile reads
   little it does not need, large enough that a row group's offset indexes,
   which a reader fetches whole, stay small. Measured on a city, 4 KB pages
   cost more in indexes than they save in data and 64 KB the reverse.
5. One nullable struct per LOD under `lod`, non-null exactly for the rows
   shown there, holding the geometry on that LOD's grid and the carried
   attributes as typed children.

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
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .encoding import (
    ENCODING,
    GEOMETRY_TYPES,
    PACK_VERSION,
    infer_attributes,
    lod_type,
    point_parts,
)
from .pack import pack
from .quantize import (
    DOT_CELLS,
    MAX_LATITUDE,
    SUBPIXELS,
    grid,
    render_lines,
    render_polygons,
    resolution,
)
from .spill import hosts, straddlers, take_ragged
from .thinning import Context, Thin, default_thinning, describe

LOD_KEY = 'lod_columns'
KEY_COLUMN = 'hilbert'
KEY_DEPTH = 24
DICTIONARY_MAX = 256
KEY_FIELD = pa.field(KEY_COLUMN, pa.int64(), nullable=True)


def hilbert_key(x: np.ndarray, y: np.ndarray, depth: int) -> np.ndarray:
    """Standard Hilbert index (xy2d) of cells (x, y) on a 2^depth grid."""
    x = np.asarray(x, dtype=np.int64).copy()
    y = np.asarray(y, dtype=np.int64).copy()
    n = 1 << depth
    d = np.zeros(len(x), dtype=np.int64)
    s = n >> 1
    while s > 0:
        rx = (x & s) > 0
        ry = (y & s) > 0
        d += s * s * ((3 * rx.astype(np.int64)) ^ ry.astype(np.int64))
        flip = ~ry & rx
        x = np.where(flip, n - 1 - x, x)
        y = np.where(flip, n - 1 - y, y)
        x, y = np.where(~ry, y, x), np.where(~ry, x, y)
        s >>= 1
    return d


# columns the writer computes carry a prefix no source column uses; thinnings see them unprefixed
THINNING_COLUMNS = ('id', 'cx', 'cy', 'diag', 'area', 'nv', 'u')
LOD_VERSION = '0.1.0'
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


def lods(
    zooms: list[int],
    complete_from: int | None,
    geometry_family: str,
    single_shape_lod: bool = False,
) -> list[dict]:
    """LODs: one stored representation per contiguous zoom range. Zooms may share
    an LOD only when row selection and carried attributes agree, and an LOD uses
    its finest zoom's grid. Below `complete_from` the thinning may change the
    row set every zoom, so each zoom is its own LOD; from `complete_from` up every
    row is present, so zooms are paired on one quantized copy, or all share one
    copy when the full-detail geometry is small enough that copies would cost
    more than they save. Shapes stay shapes at every zoom, collapsing to
    placeholder squares on coarse grids; point datasets get point LODs."""
    if geometry_family == 'point':
        return [
            {'name': f'p{z}', 'zooms': [z], 'geometry_type': 'Point', 'grid_zoom': z} for z in zooms
        ]
    shape = GEOMETRY_TYPES[geometry_family]
    prefix = 'l' if geometry_family == 'line' else 'f'
    one = lambda z: {'name': f'{prefix}{z}', 'zooms': [z], 'geometry_type': shape, 'grid_zoom': z}  # noqa: E731
    if complete_from is None:
        return [one(z) for z in zooms]
    out = [one(z) for z in zooms if z < complete_from]
    complete = [z for z in zooms if z >= complete_from]
    if single_shape_lod:
        out.append(
            {
                'name': f'{prefix}{complete[0]}',
                'zooms': complete,
                'geometry_type': shape,
                'grid_zoom': complete[-1],
            }
        )
        return out
    z = complete_from
    while z <= zooms[-1]:
        pair = [z] + ([z + 1] if z + 1 <= zooms[-1] else [])
        out.append(
            {'name': f'{prefix}{z}', 'zooms': pair, 'geometry_type': shape, 'grid_zoom': pair[-1]}
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


def guess_zooms(con, sorted_path, geometry, family, zooms, complete_from, extent, degrees, log):
    """Tippecanoe's `-zg` in spirit: the coarsest zoom is where the extent fits
    a tile, every feature is present from the zoom where the median feature
    spans a pixel, and the finest zoom is where the median vertex spacing spans
    four pixels, so the
    finest grid does not visibly bend shapes when the map overzooms it.
    Explicit values are kept; `'auto'` is replaced and logged."""
    import math

    import shapely

    from .quantize import resolution as res

    def zoom_where(size: float) -> int:
        # first zoom whose pixel is no larger than `size`
        return max(0, min(22, math.ceil(math.log2(res(0, degrees=degrees) / max(size, 1e-12)))))

    auto_zooms = zooms == 'auto'
    auto_from = complete_from == 'auto'
    if auto_zooms or auto_from:
        sample = con.execute(
            f"SELECT __diag AS diag, {geometry} FROM '{sorted_path}' WHERE __cx IS NOT NULL USING SAMPLE 5000 ROWS"
        ).to_arrow_table()
        diag = float(np.median(sample.column('diag').to_numpy())) if sample.num_rows else 0.0
        geoms = shapely.from_wkb(sample.column(geometry).to_numpy(zero_copy_only=False))
        coords, index = shapely.get_coordinates(geoms, return_index=True)
        steps = np.hypot(*np.diff(coords, axis=0).T)
        same = index[1:] == index[:-1]
        spacing = float(np.median(steps[same])) if same.any() else 0.0
        extent_size = max(extent[2] - extent[0], extent[3] - extent[1])
        coarsest = max(0, zoom_where(extent_size) - 1)
        finest = min(18, max(coarsest + 1, zoom_where(spacing / 4) if spacing else 16))
        first_complete = zoom_where(diag) if family != 'point' and diag else None
        if auto_from:
            complete_from = None if family == 'point' else min(first_complete, finest)
        if auto_zooms:
            zooms = list(range(coarsest, finest + 1))
        log(
            f'  zoom guess: extent {extent_size:.3g}, median feature {diag:.3g}, median vertex spacing {spacing:.3g} '
            f'-> zooms {zooms[0]}-{zooms[-1]}, complete from {complete_from}'
        )
    zooms = list(zooms)
    if complete_from is not None and complete_from not in zooms:
        complete_from = None if complete_from > zooms[-1] else zooms[0]
    return zooms, complete_from


def _full_detail_bytes(
    con, sorted_path, geometry, family, finest_zoom, origin, degrees, n_rows
) -> float:
    """Estimated geometry bytes of the whole dataset at the finest zoom, from a sample:
    two int32 per vertex before compression."""
    sample = con.execute(
        f"SELECT {geometry} FROM '{sorted_path}' WHERE __cx IS NOT NULL USING SAMPLE 2000 ROWS"
    ).to_arrow_table()
    if not sample.num_rows:
        return 0.0
    wkb = sample.column(geometry).to_numpy(zero_copy_only=False)
    zeros = np.zeros(len(wkb))
    render = render_lines if family == 'line' else render_polygons
    parts = render(wkb, zeros, zeros, grid(finest_zoom, degrees=degrees), origin)
    return 8.0 * len(parts[3]) / len(wkb) * n_rows


def _read_schema(src: str) -> pa.Schema:
    """Arrow schema with geo metadata; a glob reads its first match."""
    if any(ch in src for ch in '*?['):
        import glob

        matches = sorted(glob.glob(src))
        if not matches:
            raise FileNotFoundError(src)
        return pq.read_schema(matches[0])
    return pq.read_schema(src)


def _leaf_paths(schema: pa.Schema) -> list[list[str]]:
    """Parquet leaf paths of an Arrow schema as pyarrow names them: every list
    level is `list.element` whatever the Arrow child field is called."""
    out = []

    def walk(field: pa.Field, path: list[str]):
        t = field.type
        if pa.types.is_struct(t):
            for i in range(t.num_fields):
                walk(t.field(i), [*path, t.field(i).name])
        elif pa.types.is_list(t) or pa.types.is_fixed_size_list(t):
            walk(t.value_field, [*path, 'list', 'element'])
        else:
            out.append(path)

    for f in schema:
        walk(f, [f.name])
    return out


def _with_overrides(table: pa.Table, ids: np.ndarray, override: pa.Table) -> pa.Table:
    """Replace carried attribute values for rows that a thinning overrode at this zoom."""
    import pyarrow.compute as pc

    idx = pc.index_in(pa.array(ids), value_set=override.column('id').combine_chunks())
    matched = pc.is_valid(idx)
    for name in override.column_names:
        if name == 'id' or name not in table.column_names:
            continue
        values = pc.take(override.column(name).combine_chunks(), idx)
        merged = pc.if_else(matched, values.cast(table.column(name).type), table.column(name))
        table = table.set_column(table.schema.get_field_index(name), name, merged)
    return table


def _concat_parts(parts_list):
    """Ragged polygon arrays of consecutive batches as one store."""
    geom, poly, ring, xs, ys = (
        [np.zeros(1, np.int64)],
        [np.zeros(1, np.int64)],
        [np.zeros(1, np.int64)],
        [],
        [],
    )
    for g, p, r, x, y in parts_list:
        geom.append(g[1:] + geom[-1][-1])
        poly.append(p[1:] + poly[-1][-1])
        ring.append(r[1:] + ring[-1][-1])
        xs.append(x)
        ys.append(y)
    return (
        np.concatenate(geom),
        np.concatenate(poly),
        np.concatenate(ring),
        np.concatenate(xs) if xs else np.zeros(0, np.int32),
        np.concatenate(ys) if ys else np.zeros(0, np.int32),
    )


def _spill_for_batch(
    b, ids, attributes, spill_entries, spill_store, attr_values, file_pos, grids, src_ids
):
    """The copies one batch hosts for LOD `b`, as `pack` takes them, or None."""
    entries = spill_entries.get(b['name'])
    if entries is None:
        return None
    host, tile_of, src = entries
    lo = np.searchsorted(host, ids - 1, side='left')
    hi = np.searchsorted(host, ids - 1, side='right')
    counts = hi - lo
    if not counts.any():
        return None
    pick = np.concatenate(
        [np.arange(a, c) for a, c in zip(lo[counts > 0], hi[counts > 0], strict=True)]
    )
    (ids_sorted, perm), store = spill_store[b['name']]
    pos = perm[np.searchsorted(ids_sorted, src[pick])]
    if b['geometry_type'] == 'Point':
        parts = point_parts(store[0][pos], store[1][pos], *grids[b['name']])
    else:
        parts = take_ragged(store, pos)
    attr_sorted, attr_perm = src_ids
    at = attr_perm[np.searchsorted(attr_sorted, src[pick])]
    attrs = {a.name: attr_values[a.name].take(pa.array(at)).cast(a.type) for a in attributes}
    return (counts, tile_of[pick], file_pos[src[pick]], parts, attrs)


def is_crs84(column) -> bool:
    """GeoParquet: an omitted `crs` is OGC:CRS84, an explicit null is unknown."""
    if 'crs' not in column:
        return True
    crs = column['crs']
    if not isinstance(crs, dict):
        return False
    ident = crs.get('id') or {}
    authority, code = str(ident.get('authority', '')).upper(), str(ident.get('code', ''))
    return (authority, code) in (('OGC', 'CRS84'), ('EPSG', '4326'))


def _render(args):
    """Quantized parts per shape LOD for one batch, rendering only the rows shown
    there; the other rows get empty geometry, which the null struct hides."""
    wkb, cx, cy, grids, family, dot_cells = args
    render = render_lines if family == 'line' else render_polygons
    n = len(wkb)
    out = []
    for scale, offset, idx in grids:
        geom_offsets, poly_offsets, ring_offsets, x, y = render(
            wkb[idx], cx[idx], cy[idx], scale, offset, dot_cells
        )
        counts = np.zeros(n + 1, dtype=np.int64)
        counts[idx + 1] = np.diff(geom_offsets)
        out.append((np.cumsum(counts), poly_offsets, ring_offsets, x, y))
    return out


def write(
    src: str,
    out: str | Path,
    *,
    thinning: Thin | None = None,
    columns=(),
    zooms='auto',
    complete_from: int | None | str = 'auto',
    carry=(),
    geometry: str = 'geometry',
    prefix_zoom: int | None = 6,
    single_lod_bytes: int = 16 * 1024 * 1024,
    row_group_rows: int = 1_048_576,
    page_bytes: int = 16384,
    page_rows: int = 2048,
    compression_level: int = 9,
    batch_rows: int | None = None,
    workers: int | None = None,
    memory_limit: str | None = None,
    temp_dir: str | None = None,
    log=print,
) -> Path:
    """Write `src` (any GeoParquet with a WKB geometry column) to `out` in the
    LOD columns layout. `thinning` chooses the rows shown at every zoom;
    `complete_from` is the zoom from which every feature is shown whatever the
    thinning says, and the base zoom a rate-based thinning counts from; `'auto'`
    guesses the base and records which zooms the thinning leaves complete.
    `compression_level` is zstd's;
    decoding speed does not depend on it. Returns the output path."""
    src = str(src)
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
    for name in ('lod', KEY_COLUMN):
        if name in source_schema.names:
            raise ValueError(f'source column {name!r} clashes with the output column of that name')
    bad = [a.name for a in attributes if a.name in THINNING_COLUMNS]
    bad += [c for c in columns if c in THINNING_COLUMNS]
    if bad:
        raise ValueError(
            f'columns {bad} clash with the names thinnings see; rename them in the source'
        )
    valid = f'({geometry} IS NOT NULL AND NOT ST_IsEmpty({geometry}))'
    xmin, ymin, xmax, ymax = con.execute(
        f'SELECT min(ST_XMin({geometry})), min(ST_YMin({geometry})), max(ST_XMax({geometry})), max(ST_YMax({geometry})) '
        f"FROM read_parquet('{src}') WHERE {valid}"
    ).fetchone()
    if xmin is None:
        raise ValueError(f'{src} has no non-empty geometries')
    # the Web Mercator cell at depth KEY_DEPTH holding the bbox centre, cells numbered as XYZ
    # tiles are; the Hilbert index of that cell is computed in numpy and the rows sorted by it
    cells = 1 << KEY_DEPTH
    lon = f'(ST_XMin({geometry}) + ST_XMax({geometry})) / 2'
    lat = f'radians(greatest(-{MAX_LATITUDE}, least({MAX_LATITUDE}, (ST_YMin({geometry}) + ST_YMax({geometry})) / 2)))'
    cell_x = f'least({cells - 1}, greatest(0, floor(({lon} + 180) / 360 * {cells})))::BIGINT'
    cell_y = f'least({cells - 1}, greatest(0, floor((1 - ln(tan({lat}) + 1 / cos({lat})) / pi()) / 2 * {cells})))::BIGINT'
    sorted_path = out.with_suffix('.sorted.parquet')
    unsorted_path = out.with_suffix('.unsorted.parquet')
    log(f'sorting {src}')
    passthrough = '* EXCLUDE (bbox)' if 'bbox' in source_schema.names else '*'
    con.execute(
        f"""
        COPY (
            SELECT row_number() OVER () AS __id,
                CASE WHEN {valid} THEN ST_X(ST_Centroid({geometry}))::DOUBLE END AS __cx,
                CASE WHEN {valid} THEN ST_Y(ST_Centroid({geometry}))::DOUBLE END AS __cy,
                CASE WHEN {valid} THEN sqrt((ST_XMax({geometry}) - ST_XMin({geometry})) ^ 2 + (ST_YMax({geometry}) - ST_YMin({geometry})) ^ 2) END AS __diag,
                CASE WHEN {valid} THEN ST_Area({geometry}) END AS __area,
                CASE WHEN {valid} THEN ST_NPoints({geometry}) END AS __nv,
                CASE WHEN {valid} THEN {{xmin: ST_XMin({geometry}), ymin: ST_YMin({geometry}), xmax: ST_XMax({geometry}), ymax: ST_YMax({geometry})}} END AS __bbox,
                CASE WHEN {valid} THEN {cell_x} END AS __kx,
                CASE WHEN {valid} THEN {cell_y} END AS __ky,
                {passthrough}
            FROM read_parquet('{src}')
        ) TO '{unsorted_path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {row_group_rows})
    """
    )
    cells_table = con.execute(
        f"SELECT __id, __kx, __ky FROM '{unsorted_path}' WHERE __kx IS NOT NULL"
    ).fetchnumpy()
    keys = pa.table(
        {
            '__id': cells_table['__id'],
            '__h': hilbert_key(cells_table['__kx'], cells_table['__ky'], KEY_DEPTH),
        }
    )
    con.register('keys', keys)
    con.execute(
        f"""
        COPY (
            SELECT u.* EXCLUDE (__kx, __ky), k.__h
            FROM '{unsorted_path}' u LEFT JOIN keys k USING (__id)
            ORDER BY k.__h NULLS LAST
        ) TO '{sorted_path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {row_group_rows})
    """
    )
    unsorted_path.unlink()
    del keys, cells_table
    n_rows = con.execute(f"SELECT count(*) FROM '{sorted_path}'").fetchone()[0]
    log(f'  {n_rows:,} rows sorted, {time.time() - t0:.0f}s')
    degrees = is_crs84(geo.get('columns', {}).get(geometry, {}))
    if not degrees:
        raise NotImplementedError(
            'the primary geometry must be OGC:CRS84 or EPSG:4326 (crs omitted or declared); reproject first'
        )
    forced_complete = complete_from if isinstance(complete_from, int) else None
    zooms, complete_from = guess_zooms(
        con,
        sorted_path,
        geometry,
        family,
        zooms,
        complete_from,
        (xmin, ymin, xmax, ymax),
        degrees,
        log,
    )
    # 2. which rows appear at which zoom: run `thinning` at every zoom over every row
    if thinning is None:
        thinning = default_thinning(family)
        log(f'  thinning: {describe(thinning)["name"]}')
    slim = ', '.join(
        dict.fromkeys(
            [
                '__id AS id',
                '__cx AS cx',
                '__cy AS cy',
                '__diag AS diag',
                '__area AS area',
                '__nv AS nv',
            ]
            + [a.name for a in attributes]
            + list(columns)
        )
    )
    rows = con.execute(
        f'SELECT {slim}, (hash({geometry}) % 1000000007) / 1000000007.0 AS u '
        f"FROM '{sorted_path}' WHERE __cx IS NOT NULL ORDER BY __id"
    ).to_arrow_table()
    # a string attribute with few distinct values is packed as an index into a value table
    dictionaries: dict[str, list] = {}
    for a in attributes:
        if pa.types.is_string(a.type) or pa.types.is_large_string(a.type):
            distinct = con.execute(
                f"SELECT DISTINCT {a.name} FROM '{sorted_path}' WHERE {a.name} IS NOT NULL "
                f'ORDER BY 1 LIMIT {DICTIONARY_MAX + 1}'
            ).fetchall()
            if len(distinct) <= DICTIONARY_MAX:
                dictionaries[a.name] = [v[0] for v in distinct]
    ctx = Context(tuple(zooms), zooms[-1] if complete_from is None else complete_from, 512, degrees)
    # bit i of a row's levels is set when it is shown at zooms[i]; null or empty geometry is shown nowhere
    levels = np.zeros(n_rows, dtype=np.int64)
    if rows.num_rows < n_rows:
        log(
            f'  {n_rows - rows.num_rows:,} rows have null or empty geometry and are shown at no zoom'
        )
    overrides: dict[int, pa.Table] = {}
    counts = {}
    for i in range(len(zooms) - 1, -1, -1):
        # the finest zoom shows every row, whatever the thinning says, as does every zoom
        # from an explicit complete_from
        complete = i == len(zooms) - 1 or (
            forced_complete is not None and zooms[i] >= forced_complete
        )
        kept = rows if complete else thinning(rows, zooms[i], ctx)
        ids = kept.column('id').to_numpy()
        levels[ids - 1] |= 1 << i
        counts[i] = len(ids)
        carried = [a.name for a in attributes if a.name in kept.column_names]
        if carried:
            overrides[i] = kept.select(['id', *carried])
    log(
        '  rows shown at each zoom: '
        + ', '.join(f'z{zooms[i]}={counts[i]:,}' for i in range(len(zooms)))
    )
    n_valid = rows.num_rows
    del rows, kept
    # the zooms are complete from the first of the run of finest zooms that keep every row
    complete_from = None
    for i in range(len(zooms) - 1, -1, -1):
        if counts[i] != n_valid:
            break
        complete_from = zooms[i]
    single = False
    if family != 'point' and complete_from is not None:
        single = (
            _full_detail_bytes(
                con, sorted_path, geometry, family, zooms[-1], (xmin, ymin), degrees, n_rows
            )
            <= single_lod_bytes
        )
        if single:
            log('  full-detail shapes are small; one shape LOD serves every zoom')
    lod_plan = lods(zooms, complete_from, family, single_shape_lod=single)
    con.register(
        'levels', pa.table({'id': np.arange(1, n_rows + 1, dtype=np.int64), 'levels': levels})
    )
    con.execute('CREATE TEMP TABLE rows AS SELECT * FROM levels')

    # 3. schema and metadata, known before writing
    origin = (float(xmin), float(ymin))
    # placeholder squares are given in pixels; the grid is finer than a pixel
    dot_cells = float(getattr(thinning, 'placeholder_pixels', DOT_CELLS / SUBPIXELS)) * SUBPIXELS
    grids = {b['name']: (grid(b['grid_zoom'], degrees=degrees), origin) for b in lod_plan}
    shape_lods = [b for b in lod_plan if b['geometry_type'] != 'Point']
    # the prefix covers the zooms up to prefix_zoom that the file actually thins
    prefix_candidates = [
        z
        for z in zooms
        if prefix_zoom is not None
        and z <= prefix_zoom
        and (complete_from is None or z < complete_from)
    ]
    prefix_level = zooms.index(prefix_candidates[-1]) if prefix_candidates else None
    prefix_mask = 0 if prefix_level is None else (1 << (prefix_level + 1)) - 1
    prefix_rows = int(np.count_nonzero(levels & prefix_mask))
    prefix_end = -(-prefix_rows // row_group_rows) - 1 if prefix_rows else -1

    # 3b. straddlers: a feature reaching into a tile beyond its own gets a copy of its LOD
    # geometry on the first row of that tile's key interval, so a tile is one interval
    lod_bit = {b['name']: zooms.index(b['zooms'][0]) for b in lod_plan}
    extent = con.execute(
        f'SELECT __id, __bbox.xmin AS xmin, __bbox.ymin AS ymin, __bbox.xmax AS xmax, '
        f"__bbox.ymax AS ymax, __h FROM '{sorted_path}' WHERE __cx IS NOT NULL ORDER BY __id"
    ).fetchnumpy()
    valid_ids = extent['__id'].astype(np.int64) - 1
    bounds = {k: np.full(n_rows, np.nan) for k in ('xmin', 'ymin', 'xmax', 'ymax')}
    for k in bounds:
        bounds[k][valid_ids] = extent[k]
    keys = np.full(n_rows, -1, np.int64)
    keys[valid_ids] = extent['__h']
    del extent
    in_prefix = (levels & prefix_mask) != 0
    by_key = valid_ids[np.argsort(keys[valid_ids], kind='stable')]
    section_rows = {
        True: by_key[in_prefix[by_key]],
        False: by_key[~in_prefix[by_key]],
    }
    file_pos = np.full(n_rows, -1, np.int64)
    file_pos[np.concatenate([section_rows[True], section_rows[False]])] = np.arange(len(valid_ids))
    spill_entries: dict[str, tuple] = {}  # LOD name -> (host, zoom, tile, src), sorted by host
    for b in lod_plan:
        shown = np.flatnonzero((levels >> lod_bit[b['name']]) & 1)
        parts = []
        for z in b['zooms']:
            # a shape under a pixel draws as a placeholder square its own tile shows whole
            res = resolution(z, degrees=degrees)
            lat = np.radians(
                np.clip(
                    (bounds['ymin'][shown] + bounds['ymax'][shown]) / 2, -MAX_LATITUDE, MAX_LATITUDE
                )
            )
            wide = (
                np.maximum(
                    (bounds['xmax'][shown] - bounds['xmin'][shown]) / res,
                    (bounds['ymax'][shown] - bounds['ymin'][shown]) / (res * np.cos(lat)),
                )
                >= 1
            )
            large = shown[wide]
            src, tiles = straddlers(
                bounds['xmin'][large],
                bounds['ymin'][large],
                bounds['xmax'][large],
                bounds['ymax'][large],
                z,
            )
            src = large[src]
            # hosts live in the main section, which a reader always searches
            rows_in = section_rows[False] if len(section_rows[False]) else section_rows[True]
            host = hosts(tiles, z, KEY_DEPTH, keys[rows_in], rows_in)
            parts.append((host, (z << 48) | tiles, src))
        if parts:
            host, tile_of, src = (np.concatenate(c) for c in zip(*parts, strict=True))
            if len(host):
                order = np.argsort(host, kind='stable')
                spill_entries[b['name']] = (host[order], tile_of[order], src[order])
    spill_total = sum(len(v[0]) for v in spill_entries.values())
    log(f'  {spill_total:,} straddler copies across {len(spill_entries)} LODs')
    del bounds, keys, by_key
    lod_field = pa.field(
        'lod',
        pa.struct([pa.field(b['name'], lod_type(), nullable=True) for b in lod_plan]),
        nullable=False,
    )
    rest = [f for f in source_schema if f.name not in ('bbox', geometry)]
    schema = pa.schema([BBOX_FIELD, KEY_FIELD, lod_field, *rest, source_schema.field(geometry)])
    geo.setdefault('version', '1.1.0')
    geo.setdefault('primary_column', geometry)
    geo.setdefault('columns', {}).setdefault(geometry, {'encoding': 'WKB', 'geometry_types': []})
    geo['columns'][geometry]['covering'] = {
        'bbox': {k: ['bbox', k] for k in ('xmin', 'ymin', 'xmax', 'ymax')}
    }
    geo[LOD_KEY] = {
        'version': LOD_VERSION,
        'column': 'lod',
        'order': 'hilbert_webmercator',
        'key': {'column': KEY_COLUMN, 'depth': KEY_DEPTH},
        'zooms': zooms,
        'tile_size': 512,
        'prefix': None
        if prefix_end < 0
        else {'zoom': zooms[prefix_level], 'row_group_end': prefix_end},
        'encoding': ENCODING,
        'pack_version': PACK_VERSION,
        'attributes': [
            {
                'name': a.name,
                'type': str(a.type),
                **({'values': dictionaries[a.name]} if a.name in dictionaries else {}),
            }
            for a in attributes
        ],
        'thinning': describe(thinning),
        'lods': {
            b['name']: {
                'zooms': b['zooms'],
                'geometry_type': b['geometry_type'],
                'scale': [grids[b['name']][0]] * 2,
                'offset': list(origin),
                'placeholder': dot_cells,
            }
            for b in lod_plan
        },
    }
    schema = schema.with_metadata(
        {**(source_schema.metadata or {}), b'geo': json.dumps(geo).encode()}
    )

    # 4. page sizing: write batches small enough that dense coordinate pages land near page_bytes
    if batch_rows is None:
        sample = con.execute(
            f"SELECT {geometry} FROM '{sorted_path}' WHERE __cx IS NOT NULL LIMIT 2000"
        ).to_arrow_table()
        if family != 'point' and sample.num_rows:
            wkb = sample.column(geometry).to_numpy(zero_copy_only=False)
            finest = lod_plan[-1]
            cx0 = np.zeros(len(wkb))
            parts = (render_lines if family == 'line' else render_polygons)(
                wkb, cx0, cx0, *grids[finest['name']]
            )
            avg = max(8.0, 8.0 * len(parts[3]) / len(wkb))
        else:
            avg = 8.0
        batch_rows = int(min(1024, max(32, page_bytes // avg * 2)))
    options = dict(
        compression='zstd',
        compression_level=compression_level,
        use_dictionary=False,
        column_encoding={KEY_COLUMN: 'DELTA_BINARY_PACKED'},
        write_statistics=['bbox.xmin', 'bbox.ymin', 'bbox.xmax', 'bbox.ymax', KEY_COLUMN],
        write_page_index=True,
        write_batch_size=batch_rows,
        data_page_size=page_bytes,
        max_rows_per_page=page_rows,
        data_page_version='1.0',
    )
    log(f'  {len(lod_plan)} LODs, batch {batch_rows} rows, prefix row groups {prefix_end + 1}')

    # 5. write: prefix rows first, then everything else, each in Hilbert order
    writer = pq.ParquetWriter(out, schema, **options)
    # workers keep the largest batch they rendered; recycling them bounds the pool's memory
    pool = ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=16)

    # the straddlers' copies, rendered once per LOD, and their attributes
    spill_store: dict[str, tuple] = {}
    spill_src = (
        np.unique(np.concatenate([v[2] for v in spill_entries.values()]))
        if spill_entries
        else np.zeros(0, np.int64)
    )
    spill_attr_values: dict[str, np.ndarray] = {}
    if len(spill_src):
        # one pass over the sorted copy picks the straddlers out as it goes, so a national file's
        # tens of millions of copies cost one scan and never sit in memory as WKB at once; each
        # LOD's store is in file order with a permutation for lookup by id
        attr_select = ''.join(f', {a.name}' for a in attributes)
        lod_srcs = {
            b['name']: np.unique(spill_entries[b['name']][2])
            for b in lod_plan
            if b['name'] in spill_entries
        }
        rendered_parts: dict[str, list] = {name: [] for name in lod_srcs}
        point_parts_of: dict[str, list] = {name: [] for name in lod_srcs}
        store_ids: dict[str, list] = {name: [] for name in lod_srcs}
        attr_chunks: dict[str, list] = {a.name: [] for a in attributes}
        attr_ids: list = []
        scanned = 0
        reader = con.execute(
            f'SELECT __id, {geometry} AS g, __cx AS cx, __cy AS cy{attr_select} '
            f"FROM '{sorted_path}' WHERE __cx IS NOT NULL"
        ).to_arrow_reader(1_000_000)
        for batch in reader:
            ids_b = batch.column('__id').to_numpy() - 1
            hit = np.flatnonzero(np.isin(ids_b, spill_src, assume_unique=True))
            scanned += len(ids_b)
            if scanned % 20_000_000 < 1_000_000:
                log(f'  scanned {scanned:,} rows for straddler copies, {time.time() - t0:.0f}s')
            if not len(hit):
                continue
            table = pa.Table.from_batches([batch]).take(pa.array(hit))
            chunk_ids = ids_b[hit]
            cx_c = table.column('cx').to_numpy(zero_copy_only=False)
            cy_c = table.column('cy').to_numpy(zero_copy_only=False)
            wkb_c = table.column('g').to_numpy(zero_copy_only=False)
            attr_ids.append(chunk_ids)
            for a in attributes:
                attr_chunks[a.name].append(table.column(a.name).combine_chunks())
            for b in lod_plan:
                name = b['name']
                if name not in lod_srcs:
                    continue
                idx = np.flatnonzero(np.isin(chunk_ids, lod_srcs[name], assume_unique=True))
                if not len(idx):
                    continue
                store_ids[name].append(chunk_ids[idx])
                if b['geometry_type'] == 'Point':
                    point_parts_of[name].append((cx_c[idx], cy_c[idx]))
                    continue
                scale, offset = grids[name]
                jobs = [
                    (
                        wkb_c[c],
                        cx_c[c],
                        cy_c[c],
                        [(scale, offset, np.arange(len(c)))],
                        family,
                        dot_cells,
                    )
                    for c in (idx[i : i + 50_000] for i in range(0, len(idx), 50_000))
                ]
                rendered_parts[name].extend(r[0] for r in pool.map(_render, jobs))
        ids_all = np.concatenate(attr_ids) if attr_ids else np.zeros(0, np.int64)
        perm = np.argsort(ids_all)
        src_ids = (ids_all[perm], perm)
        for a in attributes:
            spill_attr_values[a.name] = pa.concat_arrays(attr_chunks[a.name])
        for b in lod_plan:
            name = b['name']
            if name not in lod_srcs:
                continue
            ids_l = np.concatenate(store_ids[name])
            perm_l = np.argsort(ids_l)
            if b['geometry_type'] == 'Point':
                xs = np.concatenate([p[0] for p in point_parts_of[name]])
                ys = np.concatenate([p[1] for p in point_parts_of[name]])
                spill_store[name] = ((ids_l[perm_l], perm_l), (xs, ys))
            else:
                spill_store[name] = ((ids_l[perm_l], perm_l), _concat_parts(rendered_parts[name]))
        del rendered_parts, point_parts_of, attr_chunks, store_ids, attr_ids
        log(f'  straddler copies rendered, {time.time() - t0:.0f}s')
    row_groups = 0

    def flush(table: pa.Table, fut):
        nonlocal row_groups
        levels = table.column('__levels').to_numpy(zero_copy_only=False).astype(np.int64)
        cx = table.column('__cx').to_numpy(zero_copy_only=False)
        cy = table.column('__cy').to_numpy(zero_copy_only=False)
        rendered = fut.result() if fut is not None else []
        ids = table.column('__id').to_numpy()
        arrays = []
        k = 0
        for b in lod_plan:
            mask = (levels >> lod_bit[b['name']]) & 1 == 1
            z_index = lod_bit[b['name']]
            source = (
                _with_overrides(table, ids, overrides[z_index]) if z_index in overrides else table
            )
            attrs = {
                a.name: source.column(a.name).combine_chunks().cast(a.type) for a in attributes
            }
            if b['geometry_type'] == 'Point':
                parts = point_parts(np.nan_to_num(cx), np.nan_to_num(cy), *grids[b['name']])
            else:
                parts = rendered[k]
                k += 1
            spill = _spill_for_batch(
                b,
                ids,
                attributes,
                spill_entries,
                spill_store,
                spill_attr_values,
                file_pos,
                grids,
                src_ids if len(spill_src) else None,
            )
            if spill is not None:
                mask = mask | (spill[0] > 0)
            blobs = pack(b['geometry_type'], parts, attributes, attrs, spill, dictionaries)
            arrays.append(pc.if_else(pa.array(mask), blobs, pa.scalar(None, pa.binary())))
        lod = pa.StructArray.from_arrays(arrays, [b['name'] for b in lod_plan])
        cols = {name: table.column(name) for name in table.column_names}
        out_table = pa.table(
            {
                'bbox': _bbox_float32(cols['__bbox']),
                KEY_COLUMN: cols['__h'].cast(pa.int64()),
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
        # Match file_pos: equal Hilbert keys retain source-id order. Without
        # this tie-break, a boundary copy can point at a different source row.
        reader = con.execute(f"""
            SELECT s.*, r.levels AS __levels FROM '{sorted_path}' s JOIN rows r ON r.id = s.__id
            WHERE {where} ORDER BY s.__h NULLS LAST, s.__id
        """).to_arrow_reader(row_group_rows)
        pending = []
        for batch in reader:
            table = pa.Table.from_batches([batch])
            fut = None
            if shape_lods:
                levels = table.column('__levels').to_numpy(zero_copy_only=False).astype(np.int64)
                job = (
                    table.column(geometry).to_numpy(zero_copy_only=False),
                    table.column('__cx').to_numpy(zero_copy_only=False),
                    table.column('__cy').to_numpy(zero_copy_only=False),
                    [
                        (*grids[b['name']], np.flatnonzero((levels >> lod_bit[b['name']]) & 1))
                        for b in shape_lods
                    ],
                    family,
                    dot_cells,
                )
                fut = pool.submit(_render, job)
            pending.append((table, fut))
            if len(pending) >= (workers or 8) * 2:
                flush(*pending.pop(0))
        for t, f in pending:
            flush(t, f)

    if prefix_end >= 0:
        log(f'  prefix: rows shown by z{zooms[prefix_level]} come first')
        write_rows(f'(r.levels & {prefix_mask}) != 0')
        if row_groups - 1 != prefix_end:
            raise RuntimeError(
                f'prefix wrote {row_groups} row groups, metadata expected {prefix_end + 1}'
            )
        write_rows(f'(r.levels & {prefix_mask}) = 0')
    else:
        write_rows('TRUE')
    writer.close()
    pool.shutdown()
    sorted_path.unlink()
    log(
        f'{n_rows:,} rows, {row_groups} row groups, {out.stat().st_size / 1e9:.2f} GB, {time.time() - t0:.0f}s -> {out}'
    )
    return out
