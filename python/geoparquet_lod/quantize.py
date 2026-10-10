"""Geometry on an LOD's grid.

An LOD's grid is the Web Mercator pixel size at its finest zoom, expressed in
the file's CRS units, anchored at the dataset's minimum corner. Footprints are
simplified and snapped to that grid; anything that collapses becomes a small
square around its centroid so it still draws.
"""

from __future__ import annotations

import numpy as np
import shapely

WEBMERC_CIRCUMFERENCE = 40075016.68557849
METERS_PER_DEGREE = 111320
DOT_CELLS = 2


def resolution(zoom: int, tile_size: int = 512, degrees: bool = True) -> float:
    """Map units per pixel at `zoom`. Degrees for EPSG:4326, metres otherwise."""
    metres = WEBMERC_CIRCUMFERENCE / (tile_size * 2**zoom)
    return metres / METERS_PER_DEGREE if degrees else metres


def _dot_squares(qx, qy):
    h = DOT_CELLS / 2
    corners = [(-h, -h), (h, -h), (h, h), (-h, h), (-h, -h)]
    ring = np.stack([np.stack([qx + dx + 1, qy + dy + 1], axis=1) for dx, dy in corners], axis=1)
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


def render_polygons(wkb, cx, cy, scale: float, offset: tuple[float, float]):
    """Quantized MultiPolygon per row as ragged int32 arrays:
    (geometry offsets, polygon offsets, ring offsets, x, y)."""
    g = shapely.transform(shapely.from_wkb(wkb), lambda c: (c - offset) / scale)
    g = _quantize(g, scale)
    minx, miny, maxx, maxy = shapely.bounds(g).T
    with np.errstate(invalid='ignore'):
        small = shapely.is_missing(g) | shapely.is_empty(g) | ~shapely.is_valid(g)
        small |= (maxx - minx < DOT_CELLS) | (maxy - miny < DOT_CELLS)
    if small.any():
        g = g.copy()
        g[small] = _dot_squares(
            np.floor((cx[small] - offset[0]) / scale), np.floor((cy[small] - offset[1]) / scale)
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
        np.rint(coords[:, 0]).astype(np.int32),
        np.rint(coords[:, 1]).astype(np.int32),
    )


def render_lines(wkb, cx, cy, scale: float, offset: tuple[float, float]):
    """Quantized MultiLineString per row in the polygon tree shape: each part is
    one 'ring' that is not closed. A line that collapses on the grid becomes a
    two-cell segment at its centroid so it still draws."""
    g = shapely.transform(shapely.from_wkb(wkb), lambda c: (c - offset) / scale)
    g = _quantize(g, scale)
    minx, miny, maxx, maxy = shapely.bounds(g).T
    with np.errstate(invalid='ignore'):
        small = shapely.is_missing(g) | shapely.is_empty(g)
        small |= (maxx - minx < 1) & (maxy - miny < 1)
    if small.any():
        g = g.copy()
        qx = np.floor((cx[small] - offset[0]) / scale)
        qy = np.floor((cy[small] - offset[1]) / scale)
        g[small] = shapely.linestrings(
            np.stack([np.stack([qx, qy], 1), np.stack([qx + DOT_CELLS, qy], 1)], 1)
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
        np.rint(coords[:, 0]).astype(np.int32),
        np.rint(coords[:, 1]).astype(np.int32),
    )
