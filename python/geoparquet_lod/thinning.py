"""Which rows are shown at which zoom.

A thinning assigns every row the coarsest zoom at which it appears; it is then
shown at every finer zoom too. Rows assigned nothing are never drawn, which is
fine for analysis-only rows. The writer calls `thinning.assign(con, zooms)`
with a DuckDB connection holding a temp table `rows` and expects it to fill
`rows.level` with a zoom index (0 = coarsest) or NULL.

`rows` has: id, cx, cy (centroid), diag (bbox diagonal, CRS units), and every
column the thinning or the payload asked for. Built-ins cover the common cases;
anything else is a short SQL or Python function.
"""

from __future__ import annotations

from collections.abc import Callable

import duckdb
import numpy as np
import pyarrow as pa

from .quantize import resolution

TILE_SIZE = 512


def _webmerc_tile_cell(n: int) -> str:
    return (
        f'floor((cx + 180) / 360 * {n}), '
        f'floor((1 - ln(tan(radians(cy)) + 1 / cos(radians(cy))) / pi()) / 2 * {n})'
    )


class Thinning:
    """Base: subclasses implement `assign`; `columns` lists source columns they read."""

    columns: tuple[str, ...] = ()

    def assign(self, con: duckdb.DuckDBPyConnection, zooms: list[int]) -> None:
        raise NotImplementedError

    def describe(self) -> dict:
        return {'kind': type(self).__name__}


class density_budget(Thinning):
    """Tippecanoe-style sampling: at each zoom keep a uniform fraction of the
    eligible rows so the busiest tile holds at most `budget` of them. Rows kept
    at a coarse zoom stay kept. `where` limits eligibility, for example
    `'risk > 0'`; `budget_below` applies a smaller budget to zooms under 4,
    where tiles cover whole countries."""

    def __init__(
        self, budget: int = 60_000, where: str | None = None, budget_below: int | None = None
    ):
        self.budget = budget
        self.where = where
        self.budget_below = budget_below if budget_below is not None else budget // 3

    def assign(self, con, zooms):
        finest = len(zooms) - 1
        con.execute('ALTER TABLE rows ADD COLUMN IF NOT EXISTS level INTEGER')
        con.execute(f'UPDATE rows SET level = {finest}')
        con.execute('ALTER TABLE rows ADD COLUMN IF NOT EXISTS u DOUBLE')
        con.execute('UPDATE rows SET u = random()')
        eligible = f'({self.where})' if self.where else 'TRUE'
        survival = 1.0
        for i in range(finest - 1, -1, -1):
            n = 2 ** zooms[i]
            busiest = con.execute(f"""
                SELECT max(c) FROM (
                    SELECT count(*) AS c FROM rows
                    WHERE level <= {i} OR ({eligible} AND u < {survival})
                    GROUP BY {_webmerc_tile_cell(n)}
                )
            """).fetchone()[0]
            budget = self.budget if zooms[i] >= 4 else self.budget_below
            survival *= min(1.0, budget / max(busiest or 1, 1))
            con.execute(
                f'UPDATE rows SET level = {i} WHERE level > {i} AND {eligible} AND u < {survival}'
            )

    def describe(self):
        return {'kind': 'density_budget', 'budget': self.budget, 'where': self.where}


class size_threshold(Thinning):
    """Show a feature from the first zoom where its bbox diagonal spans at least
    `pixels` pixels. The rule lines, polygons and COGP use."""

    def __init__(self, pixels: float = 4.0, degrees: bool = True):
        self.pixels = pixels
        self.degrees = degrees

    def assign(self, con, zooms):
        finest = len(zooms) - 1
        con.execute('ALTER TABLE rows ADD COLUMN IF NOT EXISTS level INTEGER')
        con.execute(f'UPDATE rows SET level = {finest}')
        for i, zoom in reversed(list(enumerate(zooms))):
            threshold = self.pixels * resolution(zoom, TILE_SIZE, self.degrees)
            con.execute(f'UPDATE rows SET level = {i} WHERE diag >= {threshold}')

    def describe(self):
        return {'kind': 'size_threshold', 'pixels': self.pixels}


class importance(Thinning):
    """Rank rows by an expression (higher first) and keep the top `per_tile`
    per tile at each zoom. Good for named places, large facilities, anything
    with a natural priority. Combine with `density_budget` through `combine`."""

    columns: tuple[str, ...] = ()

    def __init__(self, order_by: str, per_tile: int = 1000, columns: tuple[str, ...] = ()):
        self.order_by = order_by
        self.per_tile = per_tile
        self.columns = columns

    def assign(self, con, zooms):
        finest = len(zooms) - 1
        con.execute('ALTER TABLE rows ADD COLUMN IF NOT EXISTS level INTEGER')
        con.execute(f'UPDATE rows SET level = {finest}')
        for i in range(finest - 1, -1, -1):
            n = 2 ** zooms[i]
            con.execute(f"""
                UPDATE rows SET level = {i} WHERE id IN (
                    SELECT id FROM (
                        SELECT id, row_number() OVER (
                            PARTITION BY {_webmerc_tile_cell(n)} ORDER BY ({self.order_by}) DESC
                        ) AS rk FROM rows
                    ) WHERE rk <= {self.per_tile}
                )
            """)

    def describe(self):
        return {'kind': 'importance', 'order_by': self.order_by, 'per_tile': self.per_tile}


class combine(Thinning):
    """Run several thinnings and keep each row's coarsest assignment."""

    def __init__(self, *parts: Thinning):
        self.parts = parts
        self.columns = tuple(c for p in parts for c in p.columns)

    def assign(self, con, zooms):
        levels = []
        for k, part in enumerate(self.parts):
            part.assign(con, zooms)
            con.execute(f'ALTER TABLE rows ADD COLUMN IF NOT EXISTS level_{k} INTEGER')
            con.execute(f'UPDATE rows SET level_{k} = level')
            levels.append(f'level_{k}')
        con.execute(f'UPDATE rows SET level = least({", ".join(levels)})')

    def describe(self):
        return {'kind': 'combine', 'parts': [p.describe() for p in self.parts]}


class custom(Thinning):
    """A Python function `fn(table: pyarrow.Table, zooms) -> np.ndarray` returning
    the zoom index per row (or -1 for never). `table` holds id, cx, cy, diag
    and `columns`. Simple to write; loads the slim table into memory."""

    def __init__(
        self, fn: Callable[[pa.Table, list[int]], np.ndarray], columns: tuple[str, ...] = ()
    ):
        self.fn = fn
        self.columns = columns

    def assign(self, con, zooms):
        cols = ', '.join(['id', 'cx', 'cy', 'diag', *self.columns])
        table = con.execute(f'SELECT {cols} FROM rows ORDER BY id').fetch_arrow_table()
        level = np.asarray(self.fn(table, zooms), dtype=np.int32)
        assigned = pa.table({'id': table.column('id'), 'level_new': pa.array(level)})
        con.register('assigned', assigned)
        con.execute('ALTER TABLE rows ADD COLUMN IF NOT EXISTS level INTEGER')
        con.execute(
            'UPDATE rows SET level = (SELECT CASE WHEN level_new < 0 THEN NULL ELSE level_new END '
            'FROM assigned WHERE assigned.id = rows.id)'
        )
        con.unregister('assigned')

    def describe(self):
        return {'kind': 'custom', 'fn': getattr(self.fn, '__name__', 'fn')}
