"""Build GeoParquet with per-zoom display columns and a spatial page index.

Overview generation retains the source columns and writes whole quantized
geometry, carried attributes, and boundary copies into packed binary columns.
Layout finalization aligns overview pages to tiles and adds the spatial directory
without changing table values. Ordinary readers use the standard Parquet footer.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
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
    INT32,
    PACK_VERSION,
    infer_attributes,
    lod_type,
    point_parts,
)
from .key import (
    EARTH_RADIUS,
    KEY_DEPTH,
    hilbert_key,
    mercator,
    mercator_y,
    resolution,
    sql_key_cell,
    sql_mercator_x,
    sql_mercator_y,
)
from .pack import pack
from .quantize import DOT_CELLS, SUBPIXELS, grid, render_lines, render_polygons
from .spill import hosts, straddlers, take_ragged
from .thinning import Context, Thin, default_thinning, describe

LOD_KEY = 'lod_columns'
KEY_COLUMN = 'hilbert'
DICTIONARY_MAX = 256
KEY_FIELD = pa.field(KEY_COLUMN, pa.int64(), nullable=True)


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


# The finest zoom a guess picks; maps overzoom the finest LOD past it.
AUTO_FINEST_ZOOM = 15
# A zoom whose thinning keeps this share of rows is made complete: the few
# rows dropped cost a whole copy of every feature in an LOD of its own.
NEAR_COMPLETE = 0.9999
# Render workers when not given; each holds about the largest job it rendered.
DEFAULT_WORKERS = 8
# DuckDB's share of physical memory when no limit is given; its own default
# is 80%, which leaves too little for the writer's arrays on large builds.
DEFAULT_DUCKDB_MEMORY = 0.25

SORT_BUCKET_ROWS = 2_000_000
SORT_FLUSH_ROWS = 65_536
# rows per render job; a worker's memory follows the largest job it has rendered
RENDER_ROWS = 131_072
# The unsorted copy is only read in ways that ignore its order, so its row groups can be
# small, which bounds what DuckDB buffers per thread while writing it.
UNSORTED_ROW_GROUP_ROWS = 122_880


def _sort_by_key(
    unsorted_path: Path,
    sorted_path: Path,
    ids: np.ndarray,
    keys: np.ndarray,
    row_group_rows: int,
    bucket_rows: int | None = None,
) -> None:
    """Rewrite `unsorted_path` in (key, id) order, rows without a key last by id.

    `ids` (1-based `__id`) and `keys` cover the rows that have a key. Rows are
    dealt by rank into buckets of `bucket_rows`, then each bucket is sorted in
    memory and appended, so memory holds one bucket rather than the table."""
    bucket_rows = bucket_rows or SORT_BUCKET_ROWS
    source = pq.ParquetFile(unsorted_path)
    n_rows = source.metadata.num_rows
    rank = np.empty(n_rows, np.int64)
    order = np.lexsort((ids, keys))
    rank[ids[order] - 1] = np.arange(len(order))
    keyless = np.ones(n_rows, bool)
    keyless[ids - 1] = False
    keyless = np.flatnonzero(keyless)
    rank[keyless] = len(order) + np.arange(len(keyless))
    key_of = np.zeros(n_rows, np.int64)
    key_of[ids - 1] = keys
    has_key = np.zeros(n_rows, bool)
    has_key[ids - 1] = True
    del order, keyless

    drop = [c for c in ('__kx', '__ky') if c in source.schema_arrow.names]
    schema = source.schema_arrow
    for name in drop:
        schema = schema.remove(schema.get_field_index(name))
    # A bucket holds millions of rows, more WKB than Arrow's 32-bit offsets
    # address, so its strings and binaries use 64-bit offsets while sorting.
    for i, field in enumerate(schema):
        if pa.types.is_binary(field.type):
            schema = schema.set(i, field.with_type(pa.large_binary()))
        elif pa.types.is_string(field.type):
            schema = schema.set(i, field.with_type(pa.large_string()))
    bucket_schema = schema
    schema = schema.append(pa.field('__h', pa.int64()))
    bucket_count = max(1, -(-n_rows // bucket_rows))
    buckets = [sorted_path.with_suffix(f'.bucket{b:05d}.parquet') for b in range(bucket_count)]
    writers = [pq.ParquetWriter(p, bucket_schema, compression='zstd') for p in buckets]
    pending: list[list[pa.Table]] = [[] for _ in buckets]
    pending_rows = [0] * bucket_count

    def flush_bucket(b: int) -> None:
        if pending[b]:
            writers[b].write_table(pa.concat_tables(pending[b]))
            pending[b], pending_rows[b] = [], 0

    try:
        for batch in source.iter_batches(batch_size=262_144):
            table = pa.Table.from_batches([batch]).drop_columns(drop).cast(bucket_schema)
            bucket = rank[table.column('__id').to_numpy() - 1] // bucket_rows
            by_bucket = np.argsort(bucket, kind='stable')
            bucket = bucket[by_bucket]
            edges = np.flatnonzero(np.diff(bucket)) + 1
            for start, end in zip(np.r_[0, edges], np.r_[edges, len(bucket)], strict=True):
                b = int(bucket[start])
                # take copies the rows out, so a bucket's few rows do not keep
                # the whole batch alive the way a slice of it would
                pending[b].append(table.take(pa.array(by_bucket[start:end])))
                pending_rows[b] += end - start
                if pending_rows[b] >= SORT_FLUSH_ROWS:
                    flush_bucket(b)
        for b in range(bucket_count):
            flush_bucket(b)
    finally:
        for w in writers:
            w.close()
    unsorted_path.unlink()

    with pq.ParquetWriter(sorted_path, schema, compression='zstd') as out:
        for path in buckets:
            table = pq.read_table(path)
            ids_b = table.column('__id').to_numpy() - 1
            order = np.argsort(rank[ids_b])
            ids_b = ids_b[order]
            table = table.take(pa.array(order)).append_column(
                '__h', pa.array(key_of[ids_b], mask=~has_key[ids_b])
            )
            out.write_table(table, row_group_size=row_group_rows)
            del table, ids_b, order
            path.unlink()


def _rebatch(batches, rows: int):
    """Single-chunk tables of exactly `rows` rows from a stream of batches, the
    last one shorter. The writer cuts pages per chunk, so one chunk per row
    group keeps page boundaries independent of how the input was batched."""
    held: list[pa.RecordBatch] = []
    count = 0
    for batch in batches:
        held.append(batch)
        count += batch.num_rows
        while count >= rows:
            table = pa.Table.from_batches(held)
            yield table.slice(0, rows).combine_chunks()
            rest = table.slice(rows)
            held = rest.to_batches()
            count = rest.num_rows
    if count:
        yield pa.Table.from_batches(held).combine_chunks()


def _first_rows(path: Path, columns: list[str], valid: str, rows: int) -> pa.Table:
    """The first `rows` rows of `path` in file order whose `valid` column is not null."""
    parts, count = [], 0
    for batch in pq.ParquetFile(path).iter_batches(batch_size=rows, columns=[*columns, valid]):
        kept = batch.filter(pc.is_valid(batch.column(valid)))
        parts.append(kept.select(columns))
        count += kept.num_rows
        if count >= rows:
            break
    return pa.Table.from_batches(parts).slice(0, rows) if parts else pa.table({})


def _default_memory_limit() -> str | None:
    try:
        total = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (AttributeError, ValueError, OSError):
        return None
    return f'{max(1, int(total * DEFAULT_DUCKDB_MEMORY / 2**30))}GB'


def connect(memory_limit: str | None = None, temp_dir: str | None = None):
    con = duckdb.connect()
    con.execute('INSTALL spatial; LOAD spatial;')
    memory_limit = memory_limit or _default_memory_limit()
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


def guess_zooms(con, sorted_path, geometry, family, zooms, complete_from, extent, log):
    """Tippecanoe's `-zg` in spirit: the coarsest zoom is one coarser than where
    the whole extent spans about a pixel, every feature is present from the zoom
    where the median feature spans a pixel, and the finest zoom is where the
    median vertex spacing spans four pixels, so the finest grid does not
    visibly bend shapes when the map overzooms it, up to AUTO_FINEST_ZOOM:
    every complete LOD stores every feature again, and a map overzooms the
    finest LOD past it.
    Explicit values are kept; `'auto'` is replaced and logged."""
    import math

    import shapely

    def zoom_where(size: float) -> int:
        # first zoom whose pixel is no larger than `size`
        return max(0, min(22, math.ceil(math.log2(resolution(0) / max(size, 1e-12)))))

    auto_zooms = zooms == 'auto'
    auto_from = complete_from == 'auto'
    if auto_zooms or auto_from:
        sample = con.execute(
            f"SELECT __diag AS diag, {geometry} FROM '{sorted_path}' WHERE __cx IS NOT NULL USING SAMPLE 5000 ROWS"
        ).to_arrow_table()
        diag = float(np.median(sample.column('diag').to_numpy())) if sample.num_rows else 0.0
        geoms = shapely.from_wkb(sample.column(geometry).to_numpy(zero_copy_only=False))
        coords, index = shapely.get_coordinates(geoms, return_index=True)
        steps = np.hypot(*np.diff(np.stack(mercator(coords[:, 0], coords[:, 1]), 1), axis=0).T)
        same = index[1:] == index[:-1]
        spacing = float(np.median(steps[same])) if same.any() else 0.0
        extent_size = max(extent[2] - extent[0], extent[3] - extent[1])
        coarsest = max(0, zoom_where(extent_size) - 1)
        finest = min(
            AUTO_FINEST_ZOOM, max(coarsest + 1, zoom_where(spacing / 4) if spacing else 16)
        )
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


def _check_grid(extent, finest_zoom: int, dot_cells: float) -> None:
    """Every grid coordinate, placeholders included, must fit int32 at the finest grid."""
    span = max(extent[2] - extent[0], extent[3] - extent[1]) / grid(finest_zoom) + dot_cells + 2
    if span > INT32.max:
        raise ValueError(
            f'the extent spans {span:.3g} grid cells at z{finest_zoom}, more than int32 holds; '
            'lower the finest zoom or split the data'
        )


def _full_detail_bytes(con, sorted_path, geometry, family, finest_zoom, origin, n_rows) -> float:
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
    parts = render(wkb, zeros, zeros, grid(finest_zoom), origin)
    return 8.0 * len(parts[3]) / len(wkb) * n_rows


GLOB_CHARS = '*?['


def _read_schema(src: str) -> pa.Schema:
    """Arrow schema with geo metadata of a local path or a URI Arrow's
    filesystems open, such as s3://; a glob reads its first match."""
    import fnmatch

    from pyarrow import fs

    if '://' in src:
        # the glob is matched below, so only the directory before it is a URI
        at = min((src.find(ch) for ch in GLOB_CHARS if ch in src), default=len(src))
        cut = src.rfind('/', 0, at) if at < len(src) else len(src)
        filesystem, path = fs.FileSystem.from_uri(src[:cut])
        path += src[cut:]
    else:
        filesystem, path = fs.LocalFileSystem(), os.path.abspath(src)
    if any(ch in path for ch in GLOB_CHARS):
        parts = path.split('/')
        fixed = next(i for i, part in enumerate(parts) if any(ch in part for ch in GLOB_CHARS))
        base = '/'.join(parts[:fixed]) or '/'
        infos = filesystem.get_file_info(fs.FileSelector(base, recursive=True))
        matches = sorted(
            i.path for i in infos if i.type == fs.FileType.File and fnmatch.fnmatch(i.path, path)
        )
        if not matches:
            raise FileNotFoundError(src)
        path = matches[0]
    return pq.read_schema(path, filesystem=filesystem)


def _attach_source(con, src: str) -> None:
    """Let `con` read `src` when it is remote: httpfs, and for s3:// the
    credentials from the environment, config files or instance role."""
    if '://' not in src:
        return
    con.execute('INSTALL httpfs; LOAD httpfs;')
    if src.startswith('s3://'):
        con.execute('INSTALL aws; LOAD aws;')
        con.execute('CREATE OR REPLACE SECRET (TYPE S3, PROVIDER credential_chain)')


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
    # running ends, kept apart from the pieces since a batch with no shapes adds an empty one
    geom_end = poly_end = ring_end = 0
    for g, p, r, x, y in parts_list:
        geom.append(g[1:] + geom_end)
        poly.append(p[1:] + poly_end)
        ring.append(r[1:] + ring_end)
        geom_end += int(g[-1])
        poly_end += int(p[-1])
        ring_end += int(r[-1])
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
    single_lod_bytes: int = 16 * 1024 * 1024,
    row_group_rows: int = 1_048_576,
    page_bytes: int = 16384,
    page_rows: int = 2048,
    lod_page_bytes: int = 65536,
    compression_level: int = 9,
    batch_rows: int | None = None,
    placeholder_pixels: float | None = None,
    metadata: Mapping[str, str] | None = None,
    workers: int | None = None,
    memory_limit: str | None = None,
    temp_dir: str | None = None,
    log=print,
) -> Path:
    """Build a GeoParquet LOD file in one call.

    Rows use one global Hilbert order.
    ``thinning`` chooses displayed rows at each zoom. ``complete_from`` sets
    the zoom where every feature is shown; 'auto' derives it from the data.
    ``carry`` embeds selected attributes in the overviews for fast styling.
    ``compression_level`` applies to source and overview Zstd pages.
    ``placeholder_pixels`` is the width of the square a shape under a pixel
    draws as; when unset, the thinning's own ``placeholder_pixels`` decides.
    ``metadata`` is added to the file's key-value metadata.

    ``src`` is a GeoParquet path or glob, local or a URI such as s3:// that
    DuckDB and Arrow open; S3 credentials come from the usual AWS chain.

    All overview generation options are passed to the writer. ``page_bytes``
    controls analytical/staging pages; ``lod_page_bytes`` controls the final
    tile-aligned overview pages (64 KiB by default). The build preserves the
    existing output until both phases succeed and cleans up intermediate files.
    Temporary build files live beside ``out``; ``temp_dir`` controls DuckDB spill.
    """
    from ._layout import finalize_layout

    if page_bytes < 1 or lod_page_bytes < 1:
        raise ValueError('page_bytes and lod_page_bytes must be positive')
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.lod-build-', dir=out.parent) as tmp:
        packed = Path(tmp) / 'packed.parquet'
        _write_unindexed(
            src,
            packed,
            thinning=thinning,
            columns=columns,
            zooms=zooms,
            complete_from=complete_from,
            carry=carry,
            geometry=geometry,
            single_lod_bytes=single_lod_bytes,
            row_group_rows=row_group_rows,
            page_bytes=page_bytes,
            page_rows=page_rows,
            compression_level=compression_level,
            batch_rows=batch_rows,
            placeholder_pixels=placeholder_pixels,
            metadata=metadata,
            workers=workers,
            memory_limit=memory_limit,
            temp_dir=temp_dir,
            log=log,
        )
        log('Finalizing tile-aligned pages and spatial index')
        finalize_layout(
            packed, out, page_bytes=lod_page_bytes, compression_level=compression_level, log=log
        )
    return out


def _write_unindexed(
    src: str,
    out: str | Path,
    *,
    thinning: Thin | None = None,
    columns=(),
    zooms='auto',
    complete_from: int | None | str = 'auto',
    carry=(),
    geometry: str = 'geometry',
    single_lod_bytes: int = 16 * 1024 * 1024,
    row_group_rows: int = 1_048_576,
    page_bytes: int = 16384,
    page_rows: int = 2048,
    compression_level: int = 9,
    batch_rows: int | None = None,
    placeholder_pixels: float | None = None,
    metadata: Mapping[str, str] | None = None,
    workers: int | None = None,
    memory_limit: str | None = None,
    temp_dir: str | None = None,
    log=print,
) -> Path:
    """Write `src` (any GeoParquet with a WKB geometry column) to `out` in the
    GeoParquet LOD layout. `thinning` chooses the rows shown at every zoom;
    `complete_from` is the zoom from which every feature is shown whatever the
    thinning says, and the base zoom a rate-based thinning counts from; `'auto'`
    guesses the base and records which zooms the thinning leaves complete.
    `compression_level` is zstd's;
    decoding speed does not depend on it. Returns the output path."""
    src = str(src)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    workers = workers or min(os.cpu_count() or 1, DEFAULT_WORKERS)
    con = connect(memory_limit, temp_dir)
    _attach_source(con, src)
    source_schema = _read_schema(src)
    attributes = infer_attributes(source_schema, carry)
    geo = json.loads(dict(source_schema.metadata or {}).get(b'geo', b'{}') or b'{}')
    column = geo.get('columns', {}).get(geometry, {})
    if not is_crs84(column):
        raise NotImplementedError(
            'the primary geometry must be OGC:CRS84 or EPSG:4326 (crs omitted or declared); reproject first'
        )
    if column.get('edges', 'planar') != 'planar':
        raise NotImplementedError(f'the primary geometry has {column["edges"]} edges; only planar')
    types = [t.lower() for t in column.get('geometry_types', [])]
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
    (mxmin, mxmax), (mymin, mymax) = (
        tuple(map(float, v)) for v in mercator([xmin, xmax], [ymin, ymax])
    )
    if thinning is None:
        thinning = default_thinning(family)
        log(f'  thinning: {describe(thinning)["name"]}')
    # placeholder squares are given in pixels; the grid is finer than a pixel
    if placeholder_pixels is None:
        placeholder_pixels = getattr(thinning, 'placeholder_pixels', DOT_CELLS / SUBPIXELS)
    dot_cells = float(placeholder_pixels) * SUBPIXELS
    if zooms != 'auto':
        _check_grid((mxmin, mymin, mxmax, mymax), max(zooms), dot_cells)
    # the key cell holds the bbox centre; its Hilbert index is computed in numpy and the
    # rows sorted by it
    cell_x, cell_y = sql_key_cell(
        f'(ST_XMin({geometry}) + ST_XMax({geometry})) / 2',
        f'(ST_YMin({geometry}) + ST_YMax({geometry})) / 2',
    )
    # thinnings and grids work in Web Mercator metres; the covering stays in degrees
    centroid = f'ST_Centroid({geometry})'
    width = f'({sql_mercator_x(f"ST_XMax({geometry})")} - {sql_mercator_x(f"ST_XMin({geometry})")})'
    height = (
        f'({sql_mercator_y(f"ST_YMax({geometry})")} - {sql_mercator_y(f"ST_YMin({geometry})")})'
    )
    # the source is lon/lat; DuckDB 2 requires the axis order to be stated
    area = f"ST_Area(ST_Transform({geometry}, 'OGC:CRS84', 'EPSG:3857', true))"
    sorted_path = out.with_suffix('.sorted.parquet')
    unsorted_path = out.with_suffix('.unsorted.parquet')
    log(f'sorting {src}')
    passthrough = '* EXCLUDE (bbox)' if 'bbox' in source_schema.names else '*'
    con.execute(
        f"""
        COPY (
            SELECT row_number() OVER () AS __id,
                CASE WHEN {valid} THEN {sql_mercator_x(f'ST_X({centroid})')}::DOUBLE END AS __cx,
                CASE WHEN {valid} THEN {sql_mercator_y(f'ST_Y({centroid})')}::DOUBLE END AS __cy,
                CASE WHEN {valid} THEN sqrt({width} ^ 2 + {height} ^ 2) END AS __diag,
                CASE WHEN {valid} THEN {area} END AS __area,
                CASE WHEN {valid} THEN ST_NPoints({geometry}) END AS __nv,
                CASE WHEN {valid} THEN {{xmin: ST_XMin({geometry}), ymin: ST_YMin({geometry}), xmax: ST_XMax({geometry}), ymax: ST_YMax({geometry})}} END AS __bbox,
                CASE WHEN {valid} THEN {cell_x} END AS __kx,
                CASE WHEN {valid} THEN {cell_y} END AS __ky,
                {passthrough}
            FROM read_parquet('{src}')
        ) TO '{unsorted_path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {UNSORTED_ROW_GROUP_ROWS})
    """
    )
    cells_table = con.execute(
        f"SELECT __id, __kx, __ky FROM '{unsorted_path}' WHERE __kx IS NOT NULL"
    ).fetchnumpy()
    key_ids = cells_table['__id'].astype(np.int64)
    keys = hilbert_key(cells_table['__kx'], cells_table['__ky'], KEY_DEPTH)
    del cells_table
    _sort_by_key(unsorted_path, sorted_path, key_ids, keys, row_group_rows)
    del key_ids, keys
    # a fresh connection returns the memory DuckDB held for the copy
    con.close()
    con = connect(memory_limit, temp_dir)
    pa.default_memory_pool().release_unused()
    n_rows = con.execute(f"SELECT count(*) FROM '{sorted_path}'").fetchone()[0]
    log(f'  {n_rows:,} rows sorted, {time.time() - t0:.0f}s')
    forced_complete = complete_from if isinstance(complete_from, int) else None
    zooms, complete_from = guess_zooms(
        con,
        sorted_path,
        geometry,
        family,
        zooms,
        complete_from,
        (mxmin, mymin, mxmax, mymax),
        log,
    )
    _check_grid((mxmin, mymin, mxmax, mymax), zooms[-1], dot_cells)
    # 2. which rows appear at which zoom: run `thinning` at every zoom over every row
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
    # The sorted copy is already in (key, id) order; reading it in file order,
    # streamed, gives the thinnings Hilbert order without DuckDB sorting and
    # holding the table beside its Arrow copy.
    con.execute('SET preserve_insertion_order = true')
    rows = pa.Table.from_batches(
        con.execute(
            f'SELECT {slim}, (hash({geometry}) % 1000000007) / 1000000007.0 AS u '
            f"FROM '{sorted_path}' WHERE __cx IS NOT NULL"
        ).to_arrow_reader(1_000_000)
    ).combine_chunks()
    con.execute('SET preserve_insertion_order = false')
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
    ctx = Context(tuple(zooms), zooms[-1] if complete_from is None else complete_from, 512)
    # bit i of a row's levels is set when it is shown at zooms[i]; null or empty geometry is shown nowhere
    levels = np.zeros(n_rows, dtype=np.int64)
    if rows.num_rows < n_rows:
        log(
            f'  {n_rows - rows.num_rows:,} rows have null or empty geometry and are shown at no zoom'
        )
    overrides: dict[int, pa.Table] = {}
    counts = {}
    # position in `rows` by id, to compare a thinning's attribute values with the source's
    row_of = np.full(n_rows, -1, np.int64)
    row_of[rows.column('id').to_numpy() - 1] = np.arange(rows.num_rows)
    for i in range(len(zooms) - 1, -1, -1):
        # the finest zoom shows every row, whatever the thinning says, as does every zoom
        # from an explicit complete_from
        complete = i == len(zooms) - 1 or (
            forced_complete is not None and zooms[i] >= forced_complete
        )
        kept = rows if complete else thinning(rows, zooms[i], ctx)
        if not complete and kept.num_rows >= NEAR_COMPLETE * rows.num_rows:
            kept, complete = rows, True
        ids = kept.column('id').to_numpy()
        levels[ids - 1] |= 1 << i
        counts[i] = len(ids)
        if not complete:
            # keep only the attributes a thinning changed, such as a cluster's count
            source_rows = pa.array(row_of[ids - 1])
            changed = [
                a.name
                for a in attributes
                if a.name in kept.column_names
                and not kept.column(a.name).equals(rows.column(a.name).take(source_rows))
            ]
            if changed:
                overrides[i] = kept.select(['id', *changed])
            del source_rows
        # free this zoom's rows before the next zoom thins
        del kept, ids
    del row_of
    pa.default_memory_pool().release_unused()
    log(
        '  rows shown at each zoom: '
        + ', '.join(f'z{zooms[i]}={counts[i]:,}' for i in range(len(zooms)))
    )
    n_valid = rows.num_rows
    del rows
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
                con, sorted_path, geometry, family, zooms[-1], (mxmin, mymin), n_rows
            )
            <= single_lod_bytes
        )
        if single:
            log('  full-detail shapes are small; one shape LOD serves every zoom')
    lod_plan = lods(zooms, complete_from, family, single_shape_lod=single)

    # 3. schema and metadata, known before writing
    origin = (mxmin, mymin)
    grids = {b['name']: (grid(b['grid_zoom']), origin) for b in lod_plan}
    shape_lods = [b for b in lod_plan if b['geometry_type'] != 'Point']
    # 3b. straddlers: a feature reaching into a tile beyond its own gets a copy of its LOD
    # geometry on the first row of that tile's key interval, so a tile is one interval
    lod_bit = {b['name']: zooms.index(b['zooms'][0]) for b in lod_plan}
    extent = con.execute(
        f'SELECT __id, __bbox.xmin AS xmin, __bbox.ymin AS ymin, __bbox.xmax AS xmax, '
        f"__bbox.ymax AS ymax, __h FROM '{sorted_path}' WHERE __cx IS NOT NULL"
    ).fetchnumpy()
    valid_ids = extent.pop('__id').astype(np.int64) - 1
    # each column moves out of `extent` as it is placed, so the two never both hold it
    bounds = {}
    for k in ('xmin', 'ymin', 'xmax', 'ymax'):
        bounds[k] = np.full(n_rows, np.nan)
        bounds[k][valid_ids] = extent.pop(k)
    keys = np.full(n_rows, -1, np.int64)
    keys[valid_ids] = extent.pop('__h')
    del extent
    # file order: key, then id, whatever order the query returned rows in
    by_key = valid_ids[np.lexsort((valid_ids, keys[valid_ids]))]
    keys_by_file = keys[by_key]
    file_pos = np.full(n_rows, -1, np.int64)
    file_pos[by_key] = np.arange(len(valid_ids))
    # LOD name -> (host row, tile tag, copied row) per copy, sorted by host
    spill_entries: dict[str, tuple] = {}
    for b in lod_plan:
        shown = np.flatnonzero((levels >> lod_bit[b['name']]) & 1)
        size = EARTH_RADIUS * np.radians(bounds['xmax'][shown] - bounds['xmin'][shown])
        np.maximum(
            size,
            mercator_y(bounds['ymax'][shown]) - mercator_y(bounds['ymin'][shown]),
            out=size,
        )
        parts = []
        for z in b['zooms']:
            # a shape under a pixel draws as a placeholder square its own tile shows whole
            wide = size / resolution(z) >= 1
            large = shown[wide]
            copied, tiles = straddlers(
                bounds['xmin'][large],
                bounds['ymin'][large],
                bounds['xmax'][large],
                bounds['ymax'][large],
                keys[large],
                z,
            )
            copied = large[copied]
            host = hosts(tiles, z, keys_by_file, by_key)
            parts.append((host, (z << 48) | tiles, copied))
        if parts:
            host, tile_of, copied = (np.concatenate(c) for c in zip(*parts, strict=True))
            if len(host):
                order = np.argsort(host, kind='stable')
                spill_entries[b['name']] = (host[order], tile_of[order], copied[order])
    spill_total = sum(len(v[0]) for v in spill_entries.values())
    log(f'  {spill_total:,} straddler copies across {len(spill_entries)} LODs')
    del bounds, keys, by_key, keys_by_file
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
        {
            **(source_schema.metadata or {}),
            **{k.encode(): v.encode() for k, v in (metadata or {}).items()},
            b'geo': json.dumps(geo).encode(),
        }
    )

    # 4. page sizing: write batches small enough that dense coordinate pages land near page_bytes
    if batch_rows is None:
        # the first rows in file order, so the sample does not depend on scan threads
        sample = _first_rows(sorted_path, [geometry], '__cx', 2000)
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
    # Source scalars keep their statistics, so analytical filters such as a GEOID
    # prefix or a state skip row groups; in Hilbert order a row group is one region.
    source_stats = [
        f.name
        for f in rest
        if not pa.types.is_nested(f.type)
        and not pa.types.is_binary(f.type)
        and not pa.types.is_large_binary(f.type)
    ]
    options = dict(
        compression='zstd',
        compression_level=compression_level,
        use_dictionary=False,
        column_encoding={KEY_COLUMN: 'DELTA_BINARY_PACKED'},
        write_statistics=[
            'bbox.xmin',
            'bbox.ymin',
            'bbox.xmax',
            'bbox.ymax',
            KEY_COLUMN,
            *source_stats,
        ],
        write_page_index=True,
        write_batch_size=batch_rows,
        data_page_size=page_bytes,
        max_rows_per_page=page_rows,
        data_page_version='1.0',
    )
    log(f'  {len(lod_plan)} LODs, batch {batch_rows} rows')

    # 5. write every row in Hilbert order
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
        pa.default_memory_pool().release_unused()
        log(f'  straddler copies rendered, {time.time() - t0:.0f}s')
    row_groups = 0

    def flush(table: pa.Table, futs):
        nonlocal row_groups
        levels = table.column('__levels').to_numpy(zero_copy_only=False).astype(np.int64)
        cx = table.column('__cx').to_numpy(zero_copy_only=False)
        cy = table.column('__cy').to_numpy(zero_copy_only=False)
        # a row group's render jobs cover consecutive rows; their parts join per LOD
        results = [f.result() for f in futs]
        rendered = [_concat_parts([r[k] for r in results]) for k in range(len(shape_lods))]
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

    def write_rows():
        # the sorted copy is already in output order: Hilbert key, then source id, rows
        # without geometry last. Equal keys keep source-id order, matching file_pos, so
        # a boundary copy points at its own source row.
        batches = pq.ParquetFile(sorted_path).iter_batches(batch_size=row_group_rows)
        # enough render jobs in flight to keep the pool busy, and no more row groups held
        # than those jobs need
        max_jobs = 2 * workers
        pending = []
        for table in _rebatch(batches, row_group_rows):
            row_levels = levels[table.column('__id').to_numpy() - 1]
            table = table.append_column('__levels', pa.array(row_levels))
            futs = []
            if shape_lods:
                wkb = table.column(geometry).to_numpy(zero_copy_only=False)
                cx = table.column('__cx').to_numpy(zero_copy_only=False)
                cy = table.column('__cy').to_numpy(zero_copy_only=False)
                for start in range(0, table.num_rows, RENDER_ROWS):
                    part = slice(start, start + RENDER_ROWS)
                    part_levels = row_levels[part]
                    job = (
                        wkb[part],
                        cx[part],
                        cy[part],
                        [
                            (
                                *grids[b['name']],
                                np.flatnonzero((part_levels >> lod_bit[b['name']]) & 1),
                            )
                            for b in shape_lods
                        ],
                        family,
                        dot_cells,
                    )
                    futs.append(pool.submit(_render, job))
            pending.append((table, futs))
            # with no shapes to render (points) nothing is in flight, so write now
            while len(pending) > 1 and (
                not shape_lods or sum(len(f) for _, f in pending) > max_jobs
            ):
                flush(*pending.pop(0))
        for t, f in pending:
            flush(t, f)

    write_rows()
    writer.close()
    pool.shutdown()
    sorted_path.unlink()
    log(
        f'{n_rows:,} rows, {row_groups} row groups, {out.stat().st_size / 1e9:.2f} GB, {time.time() - t0:.0f}s -> {out}'
    )
    return out
