"""Straddlers: a feature whose geometry reaches into a tile other than the one
holding its bbox centre is needed by that tile too, but its row lives in its
own tile's key interval. The writer therefore stores a copy of its LOD
geometry, with its attributes and file row index, on a host row inside each
such tile's interval, so a tile is exactly its own interval and a reader
never searches for neighbours. The copies are the LOD's `spill` list.
"""

from __future__ import annotations

import numpy as np

from .key import KEY_DEPTH, hilbert_cell, hilbert_key, tile_x, tile_y

BUFFER = 64 / 4096  # the tile encoder's buffer as a share of the tile


def ranges(starts: np.ndarray, stops: np.ndarray) -> np.ndarray:
    """The integers of each range [start, stop), concatenated in order."""
    lengths = np.asarray(stops, dtype=np.int64) - starts
    ends = np.cumsum(lengths)
    return np.arange(ends[-1] if len(ends) else 0) + np.repeat(starts - (ends - lengths), lengths)


def straddlers(
    xmin: np.ndarray,
    ymin: np.ndarray,
    xmax: np.ndarray,
    ymax: np.ndarray,
    keys: np.ndarray,
    zoom: int,
) -> tuple[np.ndarray, np.ndarray]:
    """For features reaching into tiles beyond their own at `zoom`: the row
    index of each such feature and the tile (x * 2^zoom + y) it reaches into,
    one pair per foreign tile, taking the encoder's buffer into account. A
    feature's own tile is the one its Hilbert key (`keys`, at KEY_DEPTH) is
    in, so copies go to exactly the tiles whose intervals do not hold its row."""
    n = 2**zoom
    x0 = np.clip(np.floor(tile_x(xmin, zoom) - BUFFER), 0, n - 1).astype(np.int64)
    x1 = np.clip(np.floor(tile_x(xmax, zoom) + BUFFER), 0, n - 1).astype(np.int64)
    y0 = np.clip(np.floor(tile_y(ymax, zoom) - BUFFER), 0, n - 1).astype(np.int64)
    y1 = np.clip(np.floor(tile_y(ymin, zoom) + BUFFER), 0, n - 1).astype(np.int64)
    many = np.flatnonzero((x0 < x1) | (y0 < y1))
    x0, x1, y0, y1 = (v[many] for v in (x0, x1, y0, y1))
    own_x, own_y = hilbert_cell(np.asarray(keys, np.int64)[many] >> 2 * (KEY_DEPTH - zoom), zoom)
    w, h = x1 - x0 + 1, y1 - y0 + 1
    out_rows, out_tiles = [], []
    # two tiles: the other one; two by two: the three other corners; more: every other tile
    two = np.flatnonzero(w * h == 2)
    out_rows.append(two)
    out_tiles.append((x0[two] + x1[two] - own_x[two]) * n + (y0[two] + y1[two] - own_y[two]))
    square = np.flatnonzero((w == 2) & (h == 2))
    for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        x, y = x0[square] + dx, y0[square] + dy
        foreign = (x != own_x[square]) | (y != own_y[square])
        out_rows.append(square[foreign])
        out_tiles.append(x[foreign] * n + y[foreign])
    more = np.flatnonzero((w * h > 2) & ((w != 2) | (h != 2)))
    row = np.repeat(more, (w * h)[more])
    k = ranges(np.zeros(len(more), np.int64), (w * h)[more])
    x, y = x0[row] + k % w[row], y0[row] + k // w[row]
    foreign = (x != own_x[row]) | (y != own_y[row])
    out_rows.append(row[foreign])
    out_tiles.append(x[foreign] * n + y[foreign])
    return many[np.concatenate(out_rows)], np.concatenate(out_tiles)


def hosts(
    tiles: np.ndarray, zoom: int, keys_sorted: np.ndarray, key_index: np.ndarray
) -> np.ndarray:
    """Row index of each tile's host: the first row, in key order, whose key is
    at or past the start of the tile's interval, or the last row when none is,
    so an empty tile still has a host a reader finds right after its interval.
    `keys_sorted` are a section's keys in file order and `key_index` their rows."""
    n = 2**zoom
    starts = hilbert_key(tiles // n, tiles % n, zoom) * 4 ** (KEY_DEPTH - zoom)
    pos = np.minimum(np.searchsorted(keys_sorted, starts, side='left'), len(keys_sorted) - 1)
    return key_index[pos]


def take_ragged(parts, picks: np.ndarray):
    """The ragged polygon arrays of the rows `picks` from a rendered store, in order."""
    geom_offsets, poly_offsets, ring_offsets, x, y = parts
    g0, g1 = geom_offsets[picks], geom_offsets[picks + 1]
    p0, p1 = poly_offsets[g0], poly_offsets[g1]
    r0, r1 = ring_offsets[p0], ring_offsets[p1]
    polys_per = g1 - g0
    rings_per_poly = (
        np.concatenate([np.diff(poly_offsets[a : b + 1]) for a, b in zip(g0, g1, strict=True)])
        if len(picks)
        else np.zeros(0, np.int64)
    )
    coords_per_ring = (
        np.concatenate([np.diff(ring_offsets[a : b + 1]) for a, b in zip(p0, p1, strict=True)])
        if len(picks)
        else np.zeros(0, np.int64)
    )
    xs = (
        np.concatenate([x[a:b] for a, b in zip(r0, r1, strict=True)])
        if len(picks)
        else np.zeros(0, np.int32)
    )
    ys = (
        np.concatenate([y[a:b] for a, b in zip(r0, r1, strict=True)])
        if len(picks)
        else np.zeros(0, np.int32)
    )
    return (
        np.concatenate([[0], np.cumsum(polys_per)]),
        np.concatenate([[0], np.cumsum(rings_per_poly)]),
        np.concatenate([[0], np.cumsum(coords_per_ring)]),
        xs,
        ys,
    )
