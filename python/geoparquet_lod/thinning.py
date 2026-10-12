"""Which rows are shown at which zoom, as one function shape.

    def thin(rows: pa.Table, zoom: int, ctx: Context) -> pa.Table

The writer starts with every row at the complete zoom (every feature is
present from there up) and calls `thin` once per coarser zoom, passing the
rows kept at the zoom just finer. The
function returns the subset to keep at this zoom. Nesting (shown at z means
shown at every finer zoom) is therefore guaranteed by construction, and a
rule only ever answers "of these, which survive one step coarser?".

`rows` always has: `id`, `cx`, `cy` (centroid), `diag` (bbox diagonal in CRS
units), `area` (in CRS units squared), `u` (a uniform in [0, 1) hashed from the
geometry, so stable across rebuilds),
and every carried attribute and extra column the writer was told to load.
A returned table must keep `id`; any carried attribute column it includes
overrides that attribute in this zoom's LOD, which is how accumulated
values such as cluster counts reach the renderer.

The built-ins below are small enough to read and copy. Write your own when
they do not fit; the writer does not care how `thin` decides.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from .quantize import resolution

Thin = Callable[[pa.Table, int, 'Context'], pa.Table]
RESERVED = ('id', 'cx', 'cy', 'diag', 'u')
# `area` is also provided but a thinning may update it, as `tiny_polygons` does


@dataclass(frozen=True)
class Context:
    """What a thinning may need beyond the rows: the zoom list, the finest
    sampled zoom, the tile size, and whether CRS units are degrees."""

    zooms: tuple[int, ...]
    finest_zoom: int
    tile_size: int = 512
    degrees: bool = True

    def resolution(self, zoom: int) -> float:
        """CRS units per pixel at `zoom`."""
        return resolution(zoom, self.tile_size, self.degrees)

    def tile_of(self, rows: pa.Table, zoom: int) -> np.ndarray:
        """Web Mercator tile id (x * 2^zoom + y) of each row's centroid."""
        n = 2**zoom
        cx = rows.column('cx').to_numpy()
        cy = np.clip(rows.column('cy').to_numpy(), -85.05, 85.05)
        tx = np.floor((cx + 180) / 360 * n)
        lat = np.radians(cy)
        ty = np.floor((1 - np.log(np.tan(lat) + 1 / np.cos(lat)) / np.pi) / 2 * n)
        return (tx * n + ty).astype(np.int64)

    def cell_of(self, rows: pa.Table, zoom: int, pixels: float) -> np.ndarray:
        """Id of a `pixels`-wide grid cell at `zoom` containing each centroid."""
        size = pixels * self.resolution(zoom)
        cx = rows.column('cx').to_numpy()
        cy = rows.column('cy').to_numpy()
        ix = np.floor((cx + 180) / size).astype(np.int64)
        iy = np.floor((cy + 90) / size).astype(np.int64)
        return ix * 4_000_000_000 + iy


def _mask(rows: pa.Table, keep: np.ndarray) -> pa.Table:
    return rows.filter(pa.array(np.asarray(keep, dtype=bool)))


def drop_rate(rate: float = 2.5) -> Thin:
    """Tippecanoe's `-r`: each zoom coarser keeps 1/`rate` of the rows,
    uniformly, so relative density is preserved everywhere. The default."""

    def thin(rows, zoom, ctx):
        keep = rows.column('u').to_numpy() < rate ** (zoom - ctx.finest_zoom)
        return _mask(rows, keep)

    thin.__name__ = f'drop_rate({rate})'
    return thin


def size_threshold(pixels: float = 4.0) -> Thin:
    """Keep a feature while its bbox would still span `pixels` pixels at the
    zoom: large features survive zooming out before small ones do."""

    def thin(rows, zoom, ctx):
        keep = rows.column('diag').to_numpy() >= pixels * ctx.resolution(zoom)
        return _mask(rows, keep)

    thin.__name__ = f'size_threshold({pixels})'
    return thin


def tiny_polygons(pixels: float = 0.5) -> Thin:
    """Tippecanoe's tiny-polygon reduction: a polygon smaller than a `pixels`
    square is drawn only as a share of its neighbours' area. Within a tile, in
    row (Hilbert) order, small areas accumulate and one row is kept each time
    the sum crosses a square's worth; the kept row carries that sum as its
    `area` so the next zoom coarser sees it. Dense blocks become a few squares
    with the right total area and sparse ones vanish. Area-true, so a country
    of buildings is nearly empty zoomed out; `drop_rate` keeps the settlement
    pattern visible instead, which is why it stays the default."""

    def thin(rows, zoom, ctx):
        square = (pixels * ctx.resolution(zoom)) ** 2
        area = rows.column('area').to_numpy(zero_copy_only=False).astype(np.float64)
        area = np.where(np.isnan(area), 0.0, area)
        small = area < square
        keep = ~small
        carried = area.copy()
        idx = np.flatnonzero(small)
        if len(idx):
            tiles = ctx.tile_of(rows, zoom)[idx]
            order = np.argsort(tiles, kind='stable')
            idx, tiles = idx[order], tiles[order]
            a = area[idx]
            cum = np.cumsum(a)
            starts = np.r_[0, np.flatnonzero(np.diff(tiles)) + 1]
            sizes = np.diff(np.r_[starts, len(a)])
            local = cum - np.repeat(cum[starts] - a[starts], sizes)
            k = np.floor(local / square)
            before = np.floor((local - a) / square)
            crossed = k > before
            keep[idx[crossed]] = True
            carried[idx[crossed]] = (k - before)[crossed] * square
        kept = _mask(rows, keep)
        return kept.set_column(kept.schema.get_field_index('area'), 'area', pa.array(carried[keep]))

    thin.__name__ = f'tiny_polygons({pixels})'
    thin.placeholder_pixels = pixels  # collapsed shapes are drawn this many pixels wide
    return thin


def density_budget(budget: int = 60_000) -> Thin:
    """Keep a uniform fraction so the busiest tile at this zoom holds at most
    `budget` rows. Flattens the densest places; use when bytes per tile matter
    more than density character."""

    def thin(rows, zoom, ctx):
        tiles = ctx.tile_of(rows, zoom)
        _, counts = np.unique(tiles, return_counts=True)
        busiest = int(counts.max()) if len(counts) else 1
        fraction = min(1.0, budget / max(busiest, 1))
        # the rows handed in already passed finer zooms' cuts on u, so the
        # fraction is taken of their u distribution, not of [0, 1)
        u = rows.column('u').to_numpy()
        keep = u < np.quantile(u, fraction) if len(u) and fraction < 1.0 else np.ones(len(u), bool)
        return _mask(rows, keep)

    thin.__name__ = f'density_budget({budget})'
    return thin


def drop_smallest(budget: int = 40_000) -> Thin:
    """Tippecanoe's `--drop-smallest-as-needed`: a tile over `budget` rows
    keeps its `budget` largest by `area`. After `tiny_polygons` the area is
    the carried sum, so a square standing for a block outranks one house."""

    thin = importance('area', budget)
    thin.__name__ = f'drop_smallest({budget})'
    return thin


def importance(column: str, per_tile: int = 1000, descending: bool = True) -> Thin:
    """Keep the top `per_tile` rows of each tile by `column`."""

    def thin(rows, zoom, ctx):
        tiles = ctx.tile_of(rows, zoom)
        values = rows.column(column).to_numpy(zero_copy_only=False).astype(float)
        values = np.where(np.isnan(values), -np.inf, values)
        order = np.lexsort((-values if descending else values, tiles))
        sorted_tiles = tiles[order]
        starts = np.r_[0, np.flatnonzero(np.diff(sorted_tiles)) + 1]
        sizes = np.diff(np.r_[starts, len(order)])
        rank = np.arange(len(order)) - np.repeat(starts, sizes)
        keep = np.zeros(len(order), dtype=bool)
        keep[order] = rank < per_tile
        return _mask(rows, keep)

    thin.__name__ = f'importance({column}, {per_tile})'
    return thin


def cluster(pixels: float = 8.0, count: str = 'count') -> Thin:
    """Collapse rows within `pixels` of each other into one representative
    carrying their total in attribute `count`, which must be a carried
    attribute. Counts accumulate across zooms because each call sees the
    previous zoom's counts."""

    def thin(rows, zoom, ctx):
        cells = ctx.cell_of(rows, zoom, pixels)
        prior = (
            rows.column(count).to_numpy(zero_copy_only=False).astype(np.int64)
            if count in rows.column_names
            else np.ones(len(cells), dtype=np.int64)
        )
        prior = np.where(prior <= 0, 1, prior)
        order = np.argsort(cells, kind='stable')
        sorted_cells = cells[order]
        starts = (
            np.r_[0, np.flatnonzero(np.diff(sorted_cells)) + 1] if len(order) else np.zeros(0, int)
        )
        totals = np.add.reduceat(prior[order], starts) if len(order) else np.zeros(0, np.int64)
        kept = rows.take(pa.array(order[starts]))
        if count in kept.column_names:
            kept = kept.set_column(kept.schema.get_field_index(count), count, pa.array(totals))
        else:
            kept = kept.append_column(count, pa.array(totals))
        return kept

    thin.__name__ = f'cluster({pixels})'
    return thin


def only(predicate: Callable[[pa.Table], np.ndarray] | str, inner: Thin) -> Thin:
    """Apply `inner` to the rows passing `predicate` and drop the rest. The
    predicate is a function of the table returning a boolean array, or a
    DuckDB SQL expression over the row columns, e.g. `'risk > 0'`."""

    def thin(rows, zoom, ctx):
        if isinstance(predicate, str):
            import duckdb

            con = duckdb.connect()
            con.register('rows', rows)
            keep = next(iter(con.execute(f'SELECT ({predicate}) FROM rows').fetchnumpy().values()))
            keep = np.asarray(keep, dtype=bool)
        else:
            keep = np.asarray(predicate(rows), dtype=bool)
        return inner(_mask(rows, keep), zoom, ctx)

    thin.__name__ = f'only({predicate!r}, {getattr(inner, "__name__", "thin")})'
    if hasattr(inner, 'placeholder_pixels'):
        thin.placeholder_pixels = inner.placeholder_pixels
    return thin


def combine(*parts: Thin) -> Thin:
    """Keep a row if any part keeps it; attribute overrides from later parts win."""

    def thin(rows, zoom, ctx):
        kept = [p(rows, zoom, ctx) for p in parts]
        ids = pc.unique(pa.concat_arrays([k.column('id').combine_chunks() for k in kept]))
        base = rows.filter(pc.is_in(rows.column('id'), value_set=ids))
        for k in kept:
            extra = [c for c in k.column_names if c not in RESERVED]
            if not extra:
                continue
            idx = pc.index_in(base.column('id'), value_set=k.column('id').combine_chunks())
            matched = pc.is_valid(idx)
            for c in extra:
                values = pc.take(k.column(c).combine_chunks(), idx)
                if c in base.column_names:
                    merged = pc.if_else(matched, values, base.column(c))
                    base = base.set_column(base.schema.get_field_index(c), c, merged)
                else:
                    base = base.append_column(c, values)
        return base

    thin.__name__ = 'combine(' + ', '.join(getattr(p, '__name__', 'thin') for p in parts) + ')'
    sizes = [p.placeholder_pixels for p in parts if hasattr(p, 'placeholder_pixels')]
    if sizes:
        thin.placeholder_pixels = max(sizes)
    return thin


def chain(*parts: Thin) -> Thin:
    """Apply the parts in order, each to what the one before kept."""

    def thin(rows, zoom, ctx):
        for p in parts:
            rows = p(rows, zoom, ctx)
        return rows

    thin.__name__ = 'chain(' + ', '.join(getattr(p, '__name__', 'thin') for p in parts) + ')'
    sizes = [p.placeholder_pixels for p in parts if hasattr(p, 'placeholder_pixels')]
    if sizes:
        thin.placeholder_pixels = max(sizes)
    return thin


def default_thinning(family: str) -> Thin:
    """What the writer uses when given none: tippecanoe's `-r`, so relative
    density survives zooming out for every family."""
    return drop_rate()


def describe(thin: Thin) -> dict:
    return {'name': getattr(thin, '__name__', 'thin')}
