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
 * Key intervals of a tile and a one-cell ring around it at a finer depth, so
 * features centred just outside the tile that reach into its buffer are
 * read too. The ring is one cell wide at `min(depth, zoom + RING_ZOOMS)`,
 * a sixteenth of the tile, which covers any feature up to that size.
 * Beyond the key's depth the tile's cell at the depth stands for it.
 */
export const RING_ZOOMS = 4

export const tileIntervals = (depth: number, zoom: number, x: number, y: number): Interval[] => {
  const d = Math.min(depth, zoom + RING_ZOOMS)
  const intervals: Interval[] = []
  if (zoom >= depth) {
    const shift = 2 ** (zoom - depth)
    const cx = Math.floor(x / shift)
    const cy = Math.floor(y / shift)
    const n = 2 ** depth
    for (let j = cy - 1; j <= cy + 1; j++)
      for (let i = cx - 1; i <= cx + 1; i++)
        if (i >= 0 && j >= 0 && i < n && j < n) intervals.push(cellInterval(depth, depth, i, j))
    return merge(intervals)
  }
  intervals.push(cellInterval(depth, zoom, x, y))
  const k = 2 ** (d - zoom)
  const n = 2 ** d
  const x0 = x * k - 1
  const y0 = y * k - 1
  const x1 = x * k + k
  const y1 = y * k + k
  for (let j = y0; j <= y1; j++) {
    for (let i = x0; i <= x1; i++) {
      const ring = i === x0 || i === x1 || j === y0 || j === y1
      if (!ring || i < 0 || j < 0 || i >= n || j >= n) continue
      intervals.push(cellInterval(depth, d, i, j))
    }
  }
  return merge(intervals)
}

const merge = (intervals: Interval[]): Interval[] => {
  const sorted = [...intervals].sort((a, b) => a[0] - b[0])
  const out: Interval[] = []
  for (const r of sorted) {
    const last = out[out.length - 1]
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1])
    else out.push([r[0], r[1]])
  }
  return out
}
