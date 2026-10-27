"""Geometry on an LOD's grid.

An LOD's grid is a quarter of the Web Mercator pixel at its finest zoom, in
EPSG:3857 metres, anchored at the dataset's minimum corner, so a feature a
pixel or two across keeps its own shape rather than snapping to a square, as
tippecanoe's eighth-pixel tolerance does. Footprints are projected,
simplified and snapped to that grid; anything that collapses becomes a small
square around its centroid, half a pixel wide, so it still draws.
"""

from __future__ import annotations

import numpy as np
import shapely

from .encoding import round_away

EARTH_RADIUS = 6378137.0
HALF_WORLD = np.pi * EARTH_RADIUS
SUBPIXELS = 4
DOT_CELLS = 2
MAX_LATITUDE = 85.0511287798066


def mercator_y(lat) -> np.ndarray:
    """EPSG:3857 northing in metres of latitude degrees, clamped to the Mercator limit."""
    lat = np.clip(np.asarray(lat, dtype=np.float64), -MAX_LATITUDE, MAX_LATITUDE)
    return EARTH_RADIUS * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def mercator(lon, lat) -> tuple[np.ndarray, np.ndarray]:
    """EPSG:3857 metres of longitude and latitude degrees."""
    return EARTH_RADIUS * np.radians(np.asarray(lon, dtype=np.float64)), mercator_y(lat)


def resolution(zoom: int, tile_size: int = 512) -> float:
    """Web Mercator metres per pixel at `zoom`."""
    return 2 * HALF_WORLD / (tile_size * 2**zoom)


def grid(zoom: int, tile_size: int = 512) -> float:
    """Metres per grid cell of an LOD whose finest zoom is `zoom`."""
    return resolution(zoom, tile_size) / SUBPIXELS


def _to_grid(wkb, scale: float, offset: tuple[float, float]):
    def project(c):
        x, y = mercator(c[:, 0], c[:, 1])
        return np.stack([(x - offset[0]) / scale, (y - offset[1]) / scale], axis=1)

    return shapely.transform(shapely.from_wkb(wkb), project)


def _dot_squares(qx, qy, cells: float):
    """A `cells`-wide square on the centre of the grid cell holding each
    centroid. It reaches at most `1 + cells / 2` cells past the centroid,
    which the reader pads tile bounds by."""
    h = max(cells / 2, 0.5)
    corners = [(-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1)]
    ring = np.stack(
        [np.stack([qx + 0.5 + dx * h, qy + 0.5 + dy * h], axis=1) for dx, dy in corners],
        axis=1,
    )
    return shapely.polygons(ring)


def _quantize(g, scale: float):
    try:
        return shapely.set_precision(shapely.simplify(g, 0.7, preserve_topology=True), 1.0)
    except shapely.errors.GEOSException:
        out = np.empty(len(g), dtype=object)
        for i, geom in enumerate(g):
            try:
                out[i] = shapely.set_precision(
                    shapely.simplify(geom, 0.7, preserve_topology=True), 1.0
                )
            except shapely.errors.GEOSException:
                out[i] = None
        return out


def render_polygons(
    wkb, cx, cy, scale: float, offset: tuple[float, float], dot_cells: float = DOT_CELLS
):
    """Quantized MultiPolygon per row as ragged int32 arrays:
    (geometry offsets, polygon offsets, ring offsets, x, y). `wkb` is in
    longitude and latitude; `cx`, `cy` are centroids in metres."""
    g = _quantize(_to_grid(wkb, scale, offset), scale)
    minx, miny, maxx, maxy = shapely.bounds(g).T
    with np.errstate(invalid='ignore'):
        small = shapely.is_missing(g) | shapely.is_empty(g) | ~shapely.is_valid(g)
        small |= (maxx - minx < dot_cells) & (maxy - miny < dot_cells)
    if small.any():
        g = g.copy()
        g[small] = _dot_squares(
            np.floor((cx[small] - offset[0]) / scale),
            np.floor((cy[small] - offset[1]) / scale),
            dot_cells,
        )
    polys, poly_parent = shapely.get_parts(g, return_index=True)
    rings, ring_parent = shapely.get_rings(polys, return_index=True)
    coords = shapely.get_coordinates(rings)
    geom_offsets = np.zeros(len(g) + 1, dtype=np.int64)
    np.add.at(geom_offsets, poly_parent + 1, 1)
    poly_offsets = np.zeros(len(polys) + 1, dtype=np.int64)
    np.add.at(poly_offsets, ring_parent + 1, 1)
    ring_offsets = np.concatenate([[0], shapely.get_num_coordinates(rings)])
    return (
        np.cumsum(geom_offsets),
        np.cumsum(poly_offsets),
        np.cumsum(ring_offsets),
        round_away(coords[:, 0]),
        round_away(coords[:, 1]),
    )


def render_lines(
    wkb, cx, cy, scale: float, offset: tuple[float, float], dot_cells: float = DOT_CELLS
):
    """Quantized MultiLineString per row in the polygon tree shape: each part is
    one 'ring' that is not closed. A line that collapses on the grid becomes a
    two-cell segment at its centroid so it still draws."""
    g = _quantize(_to_grid(wkb, scale, offset), scale)
    minx, miny, maxx, maxy = shapely.bounds(g).T
    with np.errstate(invalid='ignore'):
        small = shapely.is_missing(g) | shapely.is_empty(g)
        small |= (maxx - minx < 1) & (maxy - miny < 1)
    if small.any():
        g = g.copy()
        qx = np.floor((cx[small] - offset[0]) / scale)
        qy = np.floor((cy[small] - offset[1]) / scale)
        g[small] = shapely.linestrings(
            np.stack([np.stack([qx, qy], 1), np.stack([qx + dot_cells, qy], 1)], 1)
        )
    parts, parent = shapely.get_parts(g, return_index=True)
    coords = shapely.get_coordinates(parts)
    geom_offsets = np.zeros(len(g) + 1, dtype=np.int64)
    np.add.at(geom_offsets, parent + 1, 1)
    part_offsets = np.arange(len(parts) + 1, dtype=np.int64)
    ring_offsets = np.concatenate([[0], shapely.get_num_coordinates(parts)])
    return (
        np.cumsum(geom_offsets),
        part_offsets,
        np.cumsum(ring_offsets),
        round_away(coords[:, 0]),
        round_away(coords[:, 1]),
    )
