/**
 * The Hilbert key a file stores per row: the index at `depth` of the Web
 * Mercator cell holding the feature's bbox centre, cells numbered as XYZ
 * tiles are. The curve's quadtree prefix property makes a tile at any zoom up
 * to the depth one contiguous key interval, `[h * 4^(depth - z), (h + 1) *
 * 4^(depth - z))` for the tile's own index `h` at zoom `z`.
 */

/** Hilbert index of cell (x, y) on a 2^order grid, the standard xy2d. */
export const hilbertIndex = (order: number, x: number, y: number): number => {
  const n = 2 ** order
  let d = 0
  for (let s = n / 2; s >= 1; s /= 2) {
    const rx = (x & s) > 0 ? 1 : 0
    const ry = (y & s) > 0 ? 1 : 0
    d += s * s * ((3 * rx) ^ ry)
    if (ry === 0) {
      if (rx === 1) {
        x = n - 1 - x
        y = n - 1 - y
      }
      const t = x
      x = y
      y = t
    }
  }
  return d
}

export type Interval = [number, number]

/** Key interval of one cell at `zoom`, as a half-open range at the key's depth. */
export const cellInterval = (depth: number, zoom: number, x: number, y: number): Interval => {
  const span = 4 ** (depth - zoom)
  const h = hilbertIndex(zoom, x, y)
  return [h * span, (h + 1) * span]
}

/**
 * Key interval of tile z/x/y: its own cell, or beyond the key's depth the
 * depth cell holding it. Features centred outside the tile that reach into
 * it are stored as copies on a row inside this interval (or on the first row
 * past it), so this interval is all a tile reads.
 */
export const tileInterval = (depth: number, zoom: number, x: number, y: number): Interval => {
  if (zoom <= depth) return cellInterval(depth, zoom, x, y)
  const shift = 2 ** (zoom - depth)
  return cellInterval(depth, depth, Math.floor(x / shift), Math.floor(y / shift))
}
