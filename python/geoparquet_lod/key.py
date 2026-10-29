"""Web Mercator tiles and the Hilbert key that orders rows.

Grids, sizes and thinnings work in EPSG:3857 metres. Tiles are numbered as
XYZ tiles are: x from the antimeridian eastward, y from the north edge
southward. A row's key is the Hilbert index of the cell at depth `KEY_DEPTH`
holding its bbox centre, so a tile at any zoom is one interval of keys, and a
key shifted down two bits per zoom is the Hilbert index of the row's tile at
that zoom. The `sql_` functions are DuckDB expressions with the same
arithmetic as their numpy namesakes.
"""

from __future__ import annotations

import numpy as np

EARTH_RADIUS = 6378137.0
HALF_WORLD = np.pi * EARTH_RADIUS
MAX_LATITUDE = 85.0511287798066
KEY_DEPTH = 24


def mercator_y(lat) -> np.ndarray:
    """EPSG:3857 northing in metres of latitude degrees, clamped to the Mercator limit."""
    lat = np.clip(np.asarray(lat, dtype=np.float64), -MAX_LATITUDE, MAX_LATITUDE)
    return EARTH_RADIUS * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def mercator(lon, lat) -> tuple[np.ndarray, np.ndarray]:
    """EPSG:3857 metres of longitude and latitude degrees."""
    return EARTH_RADIUS * np.radians(np.asarray(lon, dtype=np.float64)), mercator_y(lat)


def sql_mercator_x(lon: str) -> str:
    return f'({EARTH_RADIUS} * radians({lon}))'


def sql_mercator_y(lat: str) -> str:
    phi = f'radians(greatest(-{MAX_LATITUDE}, least({MAX_LATITUDE}, {lat})))'
    return f'({EARTH_RADIUS} * ln(tan(pi() / 4 + {phi} / 2)))'


def resolution(zoom: int, tile_size: int = 512) -> float:
    """Web Mercator metres per pixel at `zoom`."""
    return 2 * HALF_WORLD / (tile_size * 2**zoom)


def tile_x(lon, zoom: int) -> np.ndarray:
    """Tile column, with its fraction, of longitude degrees at `zoom`."""
    return (np.asarray(lon, dtype=np.float64) + 180) / 360 * 2**zoom


def tile_y(lat, zoom: int) -> np.ndarray:
    """Tile row, with its fraction, of latitude degrees at `zoom`, clamped to the Mercator limit."""
    lat = np.radians(np.clip(np.asarray(lat, dtype=np.float64), -MAX_LATITUDE, MAX_LATITUDE))
    return (1 - np.log(np.tan(lat) + 1 / np.cos(lat)) / np.pi) / 2 * 2**zoom


def sql_key_cell(lon: str, lat: str) -> tuple[str, str]:
    """The cell at `KEY_DEPTH` holding a point: `tile_x` and `tile_y` floored and
    kept on the grid."""
    cells = 1 << KEY_DEPTH
    lat = f'radians(greatest(-{MAX_LATITUDE}, least({MAX_LATITUDE}, {lat})))'
    x = f'floor(({lon} + 180) / 360 * {cells})'
    y = f'floor((1 - ln(tan({lat}) + 1 / cos({lat})) / pi()) / 2 * {cells})'
    return tuple(f'least({cells - 1}, greatest(0, {v}))::BIGINT' for v in (x, y))


def tile_id(x, y, zoom: int) -> np.ndarray:
    """Tile id (x * 2^zoom + y) at `zoom` of points in EPSG:3857 metres."""
    n = 2**zoom
    tx = np.clip(np.floor((x + HALF_WORLD) / (2 * HALF_WORLD) * n), 0, n - 1)
    ty = np.clip(np.floor((HALF_WORLD - y) / (2 * HALF_WORLD) * n), 0, n - 1)
    return (tx * n + ty).astype(np.int64)


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


def hilbert_cell(d: np.ndarray, depth: int) -> tuple[np.ndarray, np.ndarray]:
    """The cells (x, y) on a 2^depth grid of Hilbert indexes `d` (d2xy), the
    inverse of `hilbert_key`."""
    t = np.asarray(d, dtype=np.int64).copy()
    x = np.zeros(len(t), dtype=np.int64)
    y = np.zeros(len(t), dtype=np.int64)
    s = 1
    while s < 1 << depth:
        rx = 1 & (t >> 1)
        ry = 1 & (t ^ rx)
        flip = (ry == 0) & (rx == 1)
        x = np.where(flip, s - 1 - x, x)
        y = np.where(flip, s - 1 - y, y)
        x, y = np.where(ry == 0, y, x), np.where(ry == 0, x, y)
        x += s * rx
        y += s * ry
        t >>= 2
        s <<= 1
    return x, y
